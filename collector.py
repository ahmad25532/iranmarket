"""
Iran Market AI — data collector (Phase 1).

Usage:
    python collector.py daily              # end-of-day snapshot (run after the market closes)
    python collector.py daily --allow-open # snapshot during the session (marked as not final)
    python collector.py backfill           # full history for every listed stock/ETF + indices + TGJU
    python collector.py backfill --limit 20
    python collector.py status             # what is stored so far
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import duckdb

import datastore
import quality
import sources
from net import FetchError, HttpClient

WORKERS = 6


class Run:
    """Wraps fetches so every request is timed and logged to fetch_log, success or failure."""

    def __init__(self, con: duckdb.DuckDBPyConnection, http: HttpClient):
        self.con = con
        self.http = http
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S")

    def fetch(self, source: str, target: str, fn: Callable[[], Any]) -> Optional[Any]:
        t0 = time.monotonic()
        try:
            result = fn()
        except (FetchError, ValueError, KeyError, TypeError) as e:
            datastore.log_fetch(self.con, self.id, source, target, "error", 0, str(e), _ms(t0))
            return None
        rows = len(result) if isinstance(result, (list, dict)) else 1
        datastore.log_fetch(self.con, self.id, source, target, "ok", rows, None, _ms(t0))
        return result

    def fetch_many(
        self, source: str, items: List[Tuple[str, Callable[[], Any]]]
    ) -> Dict[str, Any]:
        """Runs fetches in a thread pool; logging and DB writes stay on the main thread."""
        out: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(_timed, fn): target for target, fn in items}
            for fut in as_completed(futures):
                target = futures[fut]
                ok, result, err, ms = fut.result()
                if ok:
                    rows = len(result) if isinstance(result, (list, dict)) else 1
                    datastore.log_fetch(self.con, self.id, source, target, "ok", rows, None, ms)
                    out[target] = result
                else:
                    datastore.log_fetch(self.con, self.id, source, target, "error", 0, err, ms)
        return out


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _timed(fn: Callable[[], Any]) -> Tuple[bool, Any, Optional[str], int]:
    t0 = time.monotonic()
    try:
        return True, fn(), None, _ms(t0)
    except (FetchError, ValueError, KeyError, TypeError) as e:
        return False, None, str(e), _ms(t0)


def _upsert_instruments(con: duckdb.DuckDBPyConnection, rows: List[Dict[str, Any]]) -> None:
    for r in rows:
        r["first_seen"] = None  # keep the stored first_seen; filled below for new instruments
    datastore.upsert(con, "instruments", rows, preserve_existing=["first_seen", "isin", "sector_code"])
    con.execute("UPDATE instruments SET first_seen = last_seen WHERE first_seen IS NULL")


# ---------------------------------------------------------------- daily

def collect_daily(con: duckdb.DuckDBPyConnection, http: HttpClient, allow_open: bool) -> int:
    run = Run(con, http)
    print(f"Daily snapshot run {run.id}")

    ov1 = run.fetch("overview", "bourse", lambda: sources.fetch_market_overview(http, 1))
    ov2 = run.fetch("overview", "farabourse", lambda: sources.fetch_market_overview(http, 2))
    if not ov1:
        print("  [FAIL] could not read market overview; aborting.")
        return 1
    if not sources.is_market_closed(ov1) and not allow_open:
        print("  Market session is still open. Run after the close, or pass --allow-open.")
        return 2

    d = sources.parse_deven(ov1.get("marketActivityDEven"))
    if d is None:
        print("  [FAIL] overview has no market date; aborting.")
        return 1
    print(f"  market date {d} (state: {ov1.get('marketStateTitle')})")
    datastore.upsert(
        con, "market_overview", [sources.parse_overview(ov, m) for m, ov in ((1, ov1), (2, ov2)) if ov]
    )

    # Market watch -> instruments + today's prices
    prices_today: Dict[str, Dict[str, Any]] = {}
    etf_codes: List[str] = []
    for pt in (sources.PAPER_STOCKS, sources.PAPER_BASE_MARKET, sources.PAPER_ETF):
        rows = run.fetch("market_watch", f"paperType={pt}", lambda pt=pt: sources.fetch_market_watch(http, pt))
        if not rows:
            continue
        _upsert_instruments(con, [sources.parse_watch_instrument(r, pt, d) for r in rows])
        for r in rows:
            # Block-trade boards and rights issues are kept as instruments but carry no usable prices.
            if not sources.is_normal_board(str(r.get("lva") or "")):
                continue
            p = sources.parse_watch_price(r, d)
            if p:
                prices_today[p["ins_code"]] = p
            if pt == sources.PAPER_ETF:
                etf_codes.append(str(r.get("insCode")))
    n = datastore.upsert(con, "daily_prices", prices_today.values())
    print(f"  prices: {n} traded instruments")

    # Individual/institutional flows (volumes and counts; values come from history backfill)
    ct = run.fetch("client_types", "all", lambda: sources.fetch_client_types_all(http))
    if ct:
        flows = [sources.parse_ct_snapshot(r, d) for r in ct if str(r.get("insCode")) in prices_today]
        n = datastore.upsert(
            con, "client_flows", flows,
            preserve_existing=["buy_i_value", "buy_n_value", "sell_i_value", "sell_n_value"],
        )
        print(f"  client flows: {n}")

    # ETF NAV (no history available anywhere, so this daily capture is the only source)
    etf_codes = sorted(set(etf_codes))
    navs = run.fetch_many("etf_nav", [(c, lambda c=c: sources.fetch_etf_nav(http, c)) for c in etf_codes])
    nav_rows = []
    for code, raw in navs.items():
        close = prices_today.get(code, {}).get("close")
        row = sources.parse_etf_nav(raw, close)
        if row:
            nav_rows.append(row)
    n = datastore.upsert(con, "etf_nav", nav_rows)
    print(f"  ETF NAV: {n}/{len(etf_codes)}")

    # Sector + main indices for today
    idx = run.fetch("index_list", "bourse", lambda: sources.fetch_index_list(http))
    if idx:
        datastore.upsert(con, "indices", [
            {"index_code": str(r.get("insCode")), "name": str(r.get("lVal30") or "").strip()} for r in idx
        ] + [{"index_code": k, "name": v} for k, v in sources.MAIN_INDICES.items()])
        rows = [
            {"index_code": str(r.get("insCode")), "d": d, "close": r.get("xDrNivJIdx004"),
             "low": r.get("xPbNivJIdx004"), "high": r.get("xPhNivJIdx004")}
            for r in idx if r.get("xDrNivJIdx004")
        ]
        print(f"  indices: {datastore.upsert(con, 'index_daily', rows)}")
    for code in sources.MAIN_INDICES:
        hist = run.fetch("index_history", code, lambda code=code: sources.fetch_index_history(http, code))
        if hist:
            datastore.upsert(con, "index_daily", [r for r in map(sources.parse_index_history, hist[-10:]) if r])

    # TGJU free-market USD / gold / coin / ounce
    for series, slug in sources.TGJU_SERIES.items():
        rows = run.fetch("tgju", slug, lambda slug=slug: sources.fetch_tgju(http, slug, 30))
        if rows:
            datastore.upsert(con, "macro_daily", [r for r in (sources.parse_tgju_row(x, series) for x in rows) if r])

    # Spot-check snapshot closes against the official daily list for a few liquid names
    sample = sorted(prices_today.values(), key=lambda p: -(p["value"] or 0))[:5]
    mismatches = []
    for p in sample:
        hist = run.fetch("spot_check", p["ins_code"], lambda c=p["ins_code"]: sources.fetch_price_history(http, c, 1))
        official = sources.parse_price_history(hist[0]) if hist else None
        if official and official["d"] == d and not quality.compare_closes(p["close"], official["close"]):
            mismatches.append(f"{p['ins_code']}: {p['close']} vs {official['close']}")

    print("Quality checks:")
    checks = [
        quality.check_snapshot_date(d, date.today()),
        quality.check_price_rows(con, d),
        quality.check_price_sanity(con, d),
        quality.check_flow_coverage(con, d),
        quality.check_flow_volume_consistency(con, d),
        quality.check_nav(con, d, len(etf_codes)),
        quality.check_macro_freshness(con, date.today(), list(sources.TGJU_SERIES)),
        ("spot_check", "fail" if mismatches else "ok",
         "; ".join(mismatches) if mismatches else f"{len(sample)} closes match the official daily list"),
    ]
    if not sources.is_market_closed(ov1):
        checks.append(("session_open", "warn", "snapshot taken during the session; values are not final"))
    checks.append(quality.check_fetch_errors(con, run.id))
    return 1 if quality.save_and_summarize(con, run.id, d, checks) else 0


# ---------------------------------------------------------------- backfill

def backfill(con: duckdb.DuckDBPyConnection, http: HttpClient, limit: Optional[int], refresh: bool) -> int:
    run = Run(con, http)
    print(f"Backfill run {run.id}")

    if not con.execute("SELECT count(*) FROM instruments").fetchone()[0]:
        today = date.today()
        for pt in (sources.PAPER_STOCKS, sources.PAPER_BASE_MARKET, sources.PAPER_ETF):
            rows = run.fetch("market_watch", f"paperType={pt}", lambda pt=pt: sources.fetch_market_watch(http, pt))
            if rows:
                _upsert_instruments(con, [sources.parse_watch_instrument(r, pt, today) for r in rows])

    universe = [
        code for code, ticker in con.execute(
            "SELECT ins_code, ticker FROM instruments WHERE paper_type IN (1, 2, 8) ORDER BY ins_code"
        ).fetchall() if sources.is_normal_board(ticker)
    ]
    if not refresh:
        done = {r[0] for r in con.execute(
            "SELECT ins_code FROM backfill_status GROUP BY ins_code HAVING count(*) = 2"
        ).fetchall()}
        universe = [c for c in universe if c not in done]
    if limit:
        universe = universe[:limit]
    print(f"  instruments to backfill: {len(universe)}")

    batch = 40
    for i in range(0, len(universe), batch):
        chunk = universe[i:i + batch]
        prices = run.fetch_many("price_history", [(c, lambda c=c: sources.fetch_price_history(http, c, 0)) for c in chunk])
        flows = run.fetch_many("flow_history", [(c, lambda c=c: sources.fetch_client_type_history(http, c)) for c in chunk])
        status = []
        for code, raw in prices.items():
            rows = [r for r in map(sources.parse_price_history, raw) if r]
            datastore.upsert(con, "daily_prices", rows)
            status.append({"ins_code": code, "kind": "prices", "rows": len(rows), "completed_at": datetime.now()})
        for code, raw in flows.items():
            rows = [r for r in map(sources.parse_ct_history, raw) if r]
            datastore.upsert(con, "client_flows", rows)
            status.append({"ins_code": code, "kind": "flows", "rows": len(rows), "completed_at": datetime.now()})
        datastore.upsert(con, "backfill_status", status)
        print(f"  {min(i + batch, len(universe))}/{len(universe)} instruments")

    idx = run.fetch("index_list", "bourse", lambda: sources.fetch_index_list(http)) or []
    names = {str(r.get("insCode")): str(r.get("lVal30") or "").strip() for r in idx}
    names.update(sources.MAIN_INDICES)
    datastore.upsert(con, "indices", [{"index_code": k, "name": v} for k, v in names.items()])
    hist = run.fetch_many("index_history", [(c, lambda c=c: sources.fetch_index_history(http, c)) for c in names])
    n = sum(datastore.upsert(con, "index_daily", [r for r in map(sources.parse_index_history, raw) if r])
            for raw in hist.values())
    print(f"  index history: {len(hist)} indices, {n} rows")

    for series, slug in sources.TGJU_SERIES.items():
        rows = run.fetch("tgju", slug, lambda slug=slug: sources.fetch_tgju(http, slug, 20000))
        if rows:
            n = datastore.upsert(con, "macro_daily", [r for r in (sources.parse_tgju_row(x, series) for x in rows) if r])
            print(f"  TGJU {series}: {n} days")

    print("Quality checks:")
    checks = [
        quality.check_macro_freshness(con, date.today(), list(sources.TGJU_SERIES)),
        quality.check_fetch_errors(con, run.id),
    ]
    return 1 if quality.save_and_summarize(con, run.id, None, checks) else 0


# ---------------------------------------------------------------- status

def status(con: duckdb.DuckDBPyConnection) -> int:
    for table in ("instruments", "daily_prices", "client_flows", "etf_nav", "index_daily", "macro_daily"):
        n, lo, hi = con.execute(
            f"SELECT count(*), {'NULL, NULL' if table == 'instruments' else 'min(d), max(d)'} FROM {table}"
        ).fetchone()
        span = f"  {lo} .. {hi}" if lo else ""
        print(f"  {table:<15} {n:>10,} rows{span}")
    last = con.execute(
        "SELECT run_id, check_name, severity, detail FROM quality_checks "
        "WHERE run_id = (SELECT max(run_id) FROM quality_checks) ORDER BY logged_at"
    ).fetchall()
    if last:
        print(f"Last quality run {last[0][0]}:")
        for _, name, sev, detail in last:
            print(f"  [{sev.upper():<4}] {name}: {detail}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Iran Market AI data collector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_daily = sub.add_parser("daily")
    p_daily.add_argument("--allow-open", action="store_true")
    p_back = sub.add_parser("backfill")
    p_back.add_argument("--limit", type=int)
    p_back.add_argument("--refresh", action="store_true", help="re-download instruments already backfilled")
    sub.add_parser("status")
    ap.add_argument("--db", default=datastore.DEFAULT_DB_PATH)
    args = ap.parse_args()

    con = datastore.connect(args.db)
    try:
        if args.cmd == "status":
            return status(con)
        http = HttpClient()
        if not http.source_ip:
            print("  note: no LAN interface reached TSETMC; using the default route")
        if args.cmd == "daily":
            return collect_daily(con, http, args.allow_open)
        return backfill(con, http, args.limit, args.refresh)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
