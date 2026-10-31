"""
Iran Market AI — data collector (Phase 1).

Usage:
    python collector.py daily              # snapshot + catch-up of missed days (safe to run any time, any number of times)
    python collector.py daily --allow-open # also snapshot during the session (marked as not final)
    python collector.py daily --force      # retake today's snapshot even if it already exists
    python collector.py backfill           # full history for every listed stock/ETF + NAV + indices + TGJU
    python collector.py backfill --limit 20
    python collector.py status             # what is stored so far and the last 10 trading days' coverage

`daily` is idempotent: when the computer was off for a day or a whole trip, the next run
detects the missing trading days and fills prices, flows, NAV and indices from the official
history endpoints (TSETMC + Fipiran), so nothing is lost.
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import duckdb

import adjust
import datastore
import quality
import sources
from net import FetchError, HttpClient

WORKERS = 6
BATCH = 40
TEDPIX = "32097828799138957"
CATCHUP_LOOKBACK_DAYS = 120
SETTLE_HOUR = 14  # local (Tehran) hour after which the day's snapshot is considered final


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


def _universe(con: duckdb.DuckDBPyConnection, paper_types: Tuple[int, ...] = (1, 2, 8)) -> List[str]:
    """Normal-board stocks and ETFs (block-trade boards and rights issues excluded)."""
    placeholders = ", ".join("?" for _ in paper_types)
    return [
        code for code, ticker in con.execute(
            f"SELECT ins_code, ticker FROM instruments WHERE paper_type IN ({placeholders}) ORDER BY ins_code",
            list(paper_types),
        ).fetchall() if sources.is_normal_board(ticker)
    ]


def _trading_days(con: duckdb.DuckDBPyConnection, since: date) -> List[date]:
    return [r[0] for r in con.execute(
        "SELECT d FROM index_daily WHERE index_code = ? AND d >= ? ORDER BY d", [TEDPIX, since]
    ).fetchall()]


# ---------------------------------------------------------------- building blocks

def take_snapshot(run: Run, d: date, final: bool) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """End-of-day market watch, client flows, ETF NAV and sector indices for market date d."""
    con, http = run.con, run.http
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

    # Individual/institutional flows (volumes and counts; values come from the history endpoint)
    ct = run.fetch("client_types", "all", lambda: sources.fetch_client_types_all(http))
    if ct:
        flows = [sources.parse_ct_snapshot(r, d) for r in ct if str(r.get("insCode")) in prices_today]
        n = datastore.upsert(
            con, "client_flows", flows,
            preserve_existing=["buy_i_value", "buy_n_value", "sell_i_value", "sell_n_value"],
        )
        print(f"  client flows: {n}")

    etf_codes = sorted(set(etf_codes))
    navs = run.fetch_many("etf_nav", [(c, lambda c=c: sources.fetch_etf_nav(http, c)) for c in etf_codes])
    nav_rows = [
        row for code, raw in navs.items()
        if (row := sources.parse_etf_nav(raw, prices_today.get(code, {}).get("close")))
    ]
    n = datastore.upsert(con, "etf_nav", nav_rows)
    print(f"  ETF NAV: {n}/{len(etf_codes)}")

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
        print(f"  sector indices: {datastore.upsert(con, 'index_daily', rows)}")

    datastore.upsert(con, "snapshots", [{"d": d, "taken_at": datetime.now(), "final": final}])
    return prices_today, len(etf_codes)


def fill_prices_and_flows(run: Run, codes: List[str], days: int, label: str) -> None:
    """Official daily price list (last `days` trading days, 0 = all) + full client-flow history."""
    con, http = run.con, run.http
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        prices = run.fetch_many("price_history", [(c, lambda c=c: sources.fetch_price_history(http, c, days)) for c in chunk])
        flows = run.fetch_many("flow_history", [(c, lambda c=c: sources.fetch_client_type_history(http, c)) for c in chunk])
        status = []
        for code, raw in prices.items():
            rows = [r for r in map(sources.parse_price_history, raw) if r]
            datastore.upsert(con, "daily_prices", rows)
            if days == 0:
                status.append({"ins_code": code, "kind": "prices", "rows": len(rows), "completed_at": datetime.now()})
        for code, raw in flows.items():
            rows = [r for r in map(sources.parse_ct_history, raw) if r]
            datastore.upsert(con, "client_flows", rows)
            if days == 0:
                status.append({"ins_code": code, "kind": "flows", "rows": len(rows), "completed_at": datetime.now()})
        datastore.upsert(con, "backfill_status", status)
        print(f"  {label}: {min(i + BATCH, len(codes))}/{len(codes)} instruments")


def refresh_fipiran_funds(run: Run) -> Dict[str, str]:
    """Returns {insCode: regNo} for exchange-traded funds and stores the mapping."""
    funds = run.fetch("fipiran", "fund_list", lambda: sources.fetch_fipiran_funds(run.http)) or []
    rows = [
        {"ins_code": str(f["insCode"]), "reg_no": str(f["regNo"]), "name": f.get("name"), "fund_type": f.get("fundType")}
        for f in funds if f.get("insCode") and f.get("regNo")
    ]
    datastore.upsert(run.con, "fipiran_funds", rows)
    return dict(run.con.execute("SELECT ins_code, reg_no FROM fipiran_funds").fetchall())


def fill_nav(run: Run, codes: List[str], show_all: bool) -> int:
    """Fills ETF NAV from Fipiran's end-of-day history without overwriting TSETMC snapshot rows."""
    mapping = refresh_fipiran_funds(run)
    targets = [(c, mapping[c]) for c in codes if c in mapping]
    added = 0
    for i in range(0, len(targets), BATCH):
        chunk = targets[i:i + BATCH]
        hist = run.fetch_many("fipiran_nav", [
            (code, lambda reg=reg: sources.fetch_fipiran_nav_history(run.http, reg, show_all)) for code, reg in chunk
        ])
        status = []
        for code, raw in hist.items():
            rows = [r for r in (sources.parse_fipiran_nav(x, code) for x in raw) if r]
            added += datastore.upsert(run.con, "etf_nav", rows, keep_existing=True)
            if show_all:
                status.append({"ins_code": code, "kind": "nav", "rows": len(rows), "completed_at": datetime.now()})
        datastore.upsert(run.con, "backfill_status", status)
    print(f"  NAV from Fipiran: {len(targets)}/{len(codes)} funds matched, {added} new rows")
    return added


def refresh_indices(run: Run, codes: List[str]) -> int:
    hist = run.fetch_many("index_history", [(c, lambda c=c: sources.fetch_index_history(run.http, c)) for c in codes])
    return sum(
        datastore.upsert(run.con, "index_daily", [r for r in map(sources.parse_index_history, raw) if r])
        for raw in hist.values()
    )


def refresh_tgju(run: Run, length: int) -> None:
    for series, slug in sources.TGJU_SERIES.items():
        rows = run.fetch("tgju", slug, lambda slug=slug: sources.fetch_tgju(run.http, slug, length))
        if rows:
            n = datastore.upsert(run.con, "macro_daily", [r for r in (sources.parse_tgju_row(x, series) for x in rows) if r])
            if length > 30:
                print(f"  TGJU {series}: {n} days")
    current = run.fetch("tgju", "live", lambda: sources.fetch_tgju_live(run.http))
    if current:
        store_live_macro(run.con, sources.parse_tgju_live(current))


def store_live_macro(con: duckdb.DuckDBPyConnection, rows: List[Dict[str, Any]]) -> int:
    """Stores live prices for days TGJU has not closed yet; never overwrites a closed-day row."""
    closed = {(s, d) for s, d in con.execute(
        "SELECT series, d FROM macro_daily WHERE source IS DISTINCT FROM 'live'").fetchall()}
    return datastore.upsert(con, "macro_daily", [r for r in rows if (r["series"], r["d"]) not in closed])


def _new_instruments(con: duckdb.DuckDBPyConnection) -> List[str]:
    done = {r[0] for r in con.execute(
        "SELECT ins_code FROM backfill_status WHERE kind IN ('prices', 'flows') GROUP BY ins_code HAVING count(*) = 2"
    ).fetchall()}
    return [c for c in _universe(con) if c not in done]


# ---------------------------------------------------------------- daily (snapshot + catch-up)

def collect_daily(con: duckdb.DuckDBPyConnection, http: HttpClient, allow_open: bool, force: bool) -> int:
    run = Run(con, http)
    print(f"Daily run {run.id}")
    today = date.today()

    ov1 = run.fetch("overview", "bourse", lambda: sources.fetch_market_overview(http, 1))
    ov2 = run.fetch("overview", "farabourse", lambda: sources.fetch_market_overview(http, 2))
    if not ov1:
        print("  [FAIL] could not read market overview (network down?); nothing collected.")
        return 1
    d = sources.parse_deven(ov1.get("marketActivityDEven"))
    if d is None:
        print("  [FAIL] overview has no market date; nothing collected.")
        return 1
    closed = sources.is_market_closed(ov1)
    print(f"  market date {d} (state: {ov1.get('marketStateTitle')})")
    datastore.upsert(con, "market_overview", [sources.parse_overview(ov, m) for m, ov in ((1, ov1), (2, ov2)) if ov])

    # Trading calendar: the main indices' official history defines which days the market was open.
    refresh_indices(run, list(sources.MAIN_INDICES))

    # 1) Today's snapshot, once per market date
    checks: List[quality.Check] = []
    snap = con.execute("SELECT final FROM snapshots WHERE d = ?", [d]).fetchone()
    if not closed and not allow_open:
        print("  market session is open: snapshot deferred to the next run after the close")
    elif snap and snap[0] and not force:
        print(f"  snapshot for {d} already taken")
    else:
        # TSETMC keeps updating flows and closing prices for a while after the bell, so a snapshot
        # only counts as final once the data has settled; an earlier one is retaken next run.
        final = closed and (d < today or datetime.now().hour >= SETTLE_HOUR)
        prices_today, etf_count = take_snapshot(run, d, final=final)
        checks += snapshot_checks(run, d, prices_today, etf_count, final)

    # 2) Catch-up: fill any trading day in the lookback window that is missing or incomplete
    days = _trading_days(con, today - timedelta(days=CATCHUP_LOOKBACK_DAYS))
    if not closed:
        days = [x for x in days if x < d]  # today's history is not published until the close
    filled = {(r[0], r[1]) for r in con.execute("SELECT d, kind FROM filled_days").fetchall()}
    pf_gaps = sorted({
        x for x in quality.find_gaps(con, "daily_prices", days) + quality.find_gaps(con, "client_flows", days)
        if (x, "prices_flows") not in filled
    })
    nav_gaps = [x for x in quality.find_gaps(con, "etf_nav", days) if (x, "nav") not in filled]

    if pf_gaps:
        n_days = sum(1 for x in days if x >= pf_gaps[0]) + 3
        print(f"  catch-up: {len(pf_gaps)} missed trading days since {pf_gaps[0]}")
        fill_prices_and_flows(run, _universe(con), n_days, "catch-up")
        refresh_indices(run, [r[0] for r in con.execute("SELECT index_code FROM indices").fetchall()])
        datastore.upsert(con, "filled_days", [{"d": x, "kind": "prices_flows", "filled_at": datetime.now()} for x in pf_gaps])
    if nav_gaps:
        print(f"  catch-up: NAV missing on {len(nav_gaps)} trading days since {nav_gaps[0]}")
        fill_nav(run, _universe(con, (sources.PAPER_ETF,)), show_all=False)
        # Fipiran publishes a day or two late: only days it has had time to publish are marked as done.
        settled = days[:-3]
        datastore.upsert(con, "filled_days", [
            {"d": x, "kind": "nav", "filled_at": datetime.now()} for x in nav_gaps if x in settled
        ])

    # 3) Instruments listed since the last run get their full history
    new = _new_instruments(con)
    if new:
        print(f"  new instruments: {len(new)}")
        fill_prices_and_flows(run, new, 0, "new instruments")
        etfs = set(_universe(con, (sources.PAPER_ETF,)))
        fill_nav(run, [c for c in new if c in etfs], show_all=True)

    # 4) Free-market USD / gold / coin / ounce; a longer window after a long gap
    last_macro = con.execute("SELECT min(m) FROM (SELECT max(d) AS m FROM macro_daily GROUP BY series)").fetchone()[0]
    gap_days = (today - last_macro).days if last_macro else 20000
    refresh_tgju(run, max(30, gap_days + 10))

    # 5) Rebuild dividend/capital-increase adjusted prices and queue flags from the raw tables
    adj = adjust.build(con)
    print(f"  adjusted prices rebuilt: {adj['rows']:,} rows, {adj['events']:,} corporate actions")

    # Remaining gaps, ignoring days already re-filled from official history (thin by nature).
    filled = {(r[0], r[1]) for r in con.execute("SELECT d, kind FROM filled_days").fetchall()}

    def remaining(table: str, kind: str, window: List[date]) -> List[date]:
        return [x for x in quality.find_gaps(con, table, window) if (x, kind) not in filled]

    print("Quality checks:")
    checks += [
        quality.check_gaps("gaps_prices", remaining("daily_prices", "prices_flows", days)),
        quality.check_gaps("gaps_flows", remaining("client_flows", "prices_flows", days)),
        # Fipiran publishes NAV a day or two late, so the most recent days are not counted yet.
        quality.check_gaps("gaps_nav", remaining("etf_nav", "nav", days[:-3])),
        quality.check_macro_freshness(con, today, list(sources.TGJU_SERIES)),
        quality.check_fetch_errors(con, run.id),
    ]
    return 1 if quality.save_and_summarize(con, run.id, d, checks) else 0


def snapshot_checks(
    run: Run, d: date, prices_today: Dict[str, Dict[str, Any]], etf_count: int, final: bool
) -> List[quality.Check]:
    con, http = run.con, run.http
    # Spot-check snapshot closes against the official daily list for a few liquid names
    sample = sorted(prices_today.values(), key=lambda p: -(p["value"] or 0))[:5]
    mismatches = []
    for p in sample:
        hist = run.fetch("spot_check", p["ins_code"], lambda c=p["ins_code"]: sources.fetch_price_history(http, c, 1))
        official = sources.parse_price_history(hist[0]) if hist else None
        if official and official["d"] == d and not quality.compare_closes(p["close"], official["close"]):
            mismatches.append(f"{p['ins_code']}: {p['close']} vs {official['close']}")
    checks = [
        quality.check_snapshot_date(d, date.today()),
        quality.check_price_rows(con, d),
        quality.check_price_sanity(con, d),
        quality.check_flow_coverage(con, d),
        quality.check_flow_volume_consistency(con, d),
        quality.check_nav(con, d, etf_count),
        ("spot_check", "fail" if mismatches else "ok",
         "; ".join(mismatches) if mismatches else f"{len(sample)} closes match the official daily list"),
    ]
    if not final:
        checks.append(("not_final", "warn", "snapshot taken before the data settled; it will be retaken next run"))
    return checks


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

    universe = _universe(con) if refresh else _new_instruments(con)
    if limit:
        universe = universe[:limit]
    print(f"  instruments to backfill: {len(universe)}")
    fill_prices_and_flows(run, universe, 0, "backfill")

    etfs = _universe(con, (sources.PAPER_ETF,))
    if not refresh:
        nav_done = {r[0] for r in con.execute("SELECT ins_code FROM backfill_status WHERE kind = 'nav'").fetchall()}
        etfs = [c for c in etfs if c not in nav_done]
    if limit:
        etfs = etfs[:limit]
    fill_nav(run, etfs, show_all=True)

    idx = run.fetch("index_list", "bourse", lambda: sources.fetch_index_list(http)) or []
    names = {str(r.get("insCode")): str(r.get("lVal30") or "").strip() for r in idx}
    names.update(sources.MAIN_INDICES)
    datastore.upsert(con, "indices", [{"index_code": k, "name": v} for k, v in names.items()])
    print(f"  index history: {len(names)} indices, {refresh_indices(run, list(names))} rows")

    refresh_tgju(run, 20000)
    adj = adjust.build(con)
    print(f"  adjusted prices rebuilt: {adj['rows']:,} rows, {adj['events']:,} corporate actions")

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

    days = _trading_days(con, date.today() - timedelta(days=30))[-10:]
    if days:
        print("Last 10 trading days (prices / flows / NAV / snapshot):")
        for d in days:
            p, f, n = (con.execute(f"SELECT count(*) FROM {t} WHERE d = ?", [d]).fetchone()[0]
                       for t in ("daily_prices", "client_flows", "etf_nav"))
            snap = con.execute("SELECT final FROM snapshots WHERE d = ?", [d]).fetchone()
            snap_txt = "final" if snap and snap[0] else ("not final" if snap else "-")
            print(f"  {d}  {p:>5} / {f:>5} / {n:>4} / {snap_txt}")

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
    p_daily.add_argument("--force", action="store_true", help="retake today's snapshot even if it exists")
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
            return collect_daily(con, http, args.allow_open, args.force)
        return backfill(con, http, args.limit, args.refresh)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
