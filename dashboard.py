"""
Iran Market AI — dashboard (Phase 4).

Shows only what passed the Phase 3 out-of-sample tests, plus context and data health:
- your portfolio split (stocks / gold / fixed income / cash) against the target you set
- gold funds side by side (NAV returns, premium vs each fund's own normal, liquidity, data warnings)
- free-market USD, 18k gold, its bubble vs intrinsic value, TEDPIX in dollars (context only)
- global gold: the ounce's drivers against their own history, what moves Iranian 18k gold, and
  which global timing signals were tested (none passed out of sample)
- treasury bill yield as the risk-free rate (context, not a signal)
- whether today's data is complete and final
- your time-weighted return against simple holdings (tracking.py)

Run:  .venv\\Scripts\\python.exe dashboard.py   ->  http://127.0.0.1:8085
"""

import json
import os
import socket
import sys
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

import duckdb

import datastore
import freeusd
import globalgold
import goldfunds
import sources
import treasury
import tracking
from net import FetchError, HttpClient

PORT = 8085
PORTFOLIO_PATH = os.path.join(datastore.BASE_DIR, "data", "portfolio.json")
TEDPIX = "32097828799138957"
# On 50/50 stocks and gold, a 20-point band beat calendar and narrower bands, and matched never
# trading on the test period (docs/external_review.md). The evidence is weak but points to trading rarely.
REBALANCE_BAND_POINTS = 20.0

CLASS_LABELS = {
    "stocks": "سهام و صندوق سهامی",
    "gold": "طلا",
    "fixed": "درآمد ثابت",
    "other": "سایر (نقره، دلار، ...)",
    "cash": "نقد",
}
DEFAULT_PORTFOLIO = {
    "holdings": [],
    "manual": {"gold18_grams": 0, "coin_emami": 0, "cash_toman": 0, "usd": 0},
    "target": {"stocks": 50, "gold": 50, "fixed": 0, "other": 0, "cash": 0},
}


def normalize_ticker(t: str) -> str:
    return (t or "").strip().replace("ی", "ي").replace("ک", "ك")


# ---------------------------------------------------------------- portfolio file

def load_portfolio(path: Optional[str] = None) -> Dict[str, Any]:
    try:
        with open(path or PORTFOLIO_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_PORTFOLIO))
    out = json.loads(json.dumps(DEFAULT_PORTFOLIO))
    out["holdings"] = data.get("holdings", [])
    out["manual"].update(data.get("manual", {}))
    out["target"].update(data.get("target", {}))
    return out


def validate_portfolio(con: duckdb.DuckDBPyConnection, data: Dict[str, Any]) -> Dict[str, Any]:
    """Returns a clean portfolio or raises ValueError with a Persian message."""
    holdings = []
    for h in data.get("holdings", []):
        ticker = normalize_ticker(str(h.get("ticker", "")))
        try:
            qty = float(h.get("qty", 0))
        except (TypeError, ValueError):
            raise ValueError(f"تعداد نماد «{ticker}» عدد نیست")
        if not ticker or qty <= 0:
            continue
        if not con.execute("SELECT 1 FROM instrument_class WHERE ticker = ?", [ticker]).fetchall():
            raise ValueError(f"نماد «{h.get('ticker')}» در بورس پیدا نشد")
        holdings.append({"ticker": ticker, "qty": qty})
    manual = {}
    for k in DEFAULT_PORTFOLIO["manual"]:
        try:
            manual[k] = max(0.0, float(data.get("manual", {}).get(k, 0) or 0))
        except (TypeError, ValueError):
            raise ValueError("مقدار طلا، سکه، نقد یا دلار باید عدد باشد")
    target = {}
    for k in DEFAULT_PORTFOLIO["target"]:
        try:
            target[k] = max(0.0, float(data.get("target", {}).get(k, 0) or 0))
        except (TypeError, ValueError):
            raise ValueError("درصدهای هدف باید عدد باشند")
    if abs(sum(target.values()) - 100) > 0.5:
        raise ValueError(f"جمع درصدهای هدف باید ۱۰۰ باشد (الان {sum(target.values()):g})")
    return {"holdings": holdings, "manual": manual, "target": target}


def save_portfolio(data: Dict[str, Any], path: Optional[str] = None) -> None:
    path = path or PORTFOLIO_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- queries

def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: Optional[list] = None) -> List[Dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _latest_macro(con: duckdb.DuckDBPyConnection, series: str) -> Optional[Dict[str, Any]]:
    r = _rows(con, "SELECT d, close FROM macro_daily WHERE series = ? ORDER BY d DESC LIMIT 1", [series])
    return r[0] if r else None


LIVE_TTL_SECONDS = 300
_live_cache: Dict[str, Any] = {"at": 0.0, "rows": None}
_live_lock = threading.Lock()
_http: Optional[HttpClient] = None


def get_live_prices() -> Optional[List[Dict[str, Any]]]:
    """TGJU live prices, cached for a few minutes; None when the feed cannot be reached."""
    global _http
    with _live_lock:
        if time.monotonic() - _live_cache["at"] < LIVE_TTL_SECONDS:
            return _live_cache["rows"]
        try:
            if _http is None:
                _http = HttpClient(retries=1)
            rows = sources.parse_tgju_live(sources.fetch_tgju_live(_http))
        except (FetchError, ValueError, OSError):
            rows = None
        _live_cache.update(at=time.monotonic(), rows=rows)
        return rows


def market_snapshot(con: duckdb.DuckDBPyConnection, live: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Latest USD / 18k gold / ounce / coin. Live TGJU prices win when they are newer than the stored
    closed-day row; every value carries its date (and time when live) so staleness is visible.
    """
    names = {"usd_irr": "usd", "gold_18k_irr": "gold18", "gold_ounce_usd": "ounce", "coin_emami_irr": "coin"}
    out: Dict[str, Any] = {k: _latest_macro(con, s) for s, k in names.items()}
    for r in live or []:
        key = names.get(r["series"])
        stored = out.get(key)
        if key and (stored is None or r["d"] >= stored["d"]):
            out[key] = {"d": r["d"], "close": r["close"], "as_of": r["as_of"], "live": True}
    usd, g18, ons = out["usd"], out["gold18"], out["ounce"]
    if usd and g18 and ons and usd.get("live") and g18.get("live") and ons.get("live"):
        # All three are current quotes, so they can be compared directly.
        bubble_inputs = {"d": g18["d"], "g": g18["close"], "o": ons["close"], "u": usd["close"]}
        out["bubble_live"] = True
    else:
        # Stored rows: compare prices of the same day (the ounce also trades on Iranian weekends).
        same_day = _rows(con, """
            SELECT g.d, g.close AS g, o.close AS o, u.close AS u
            FROM macro_daily g JOIN macro_daily o ON o.d = g.d AND o.series = 'gold_ounce_usd'
            JOIN macro_daily u ON u.d = g.d AND u.series = 'usd_irr'
            WHERE g.series = 'gold_18k_irr' ORDER BY g.d DESC LIMIT 1""")
        bubble_inputs = same_day[0] if same_day else None
    if bubble_inputs:
        intrinsic = bubble_inputs["o"] * bubble_inputs["u"] / 31.1034768 * 0.75
        out["gold18_intrinsic"] = intrinsic
        out["gold18_bubble"] = bubble_inputs["g"] / intrinsic - 1
        out["bubble_date"] = bubble_inputs["d"]
    ted = _rows(con, "SELECT d, close FROM index_daily WHERE index_code = ? ORDER BY d DESC LIMIT 1", [TEDPIX])
    out["tedpix"] = ted[0] if ted else None
    return out


def classify(asset_class: str, is_gold: bool) -> str:
    if is_gold:
        return "gold"
    return {"stock": "stocks", "etf_equity": "stocks", "etf_fixed_income": "fixed"}.get(asset_class, "other")


def value_portfolio(con: duckdb.DuckDBPyConnection, pf: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    items = []
    for h in pf["holdings"]:
        r = _rows(con, """
            SELECT c.ins_code, c.ticker, c.name, c.asset_class, g.ins_code IS NOT NULL AS is_gold, p.close, p.d
            FROM instrument_class c LEFT JOIN gold_funds g USING (ins_code)
            LEFT JOIN (SELECT ins_code, close, d FROM daily_prices WHERE ins_code IN
                       (SELECT ins_code FROM instrument_class WHERE ticker = ?) ORDER BY d DESC LIMIT 1) p USING (ins_code)
            WHERE c.ticker = ?""", [h["ticker"], h["ticker"]])
        if not r or r[0]["close"] is None:
            items.append({"ticker": h["ticker"], "qty": h["qty"], "error": "قیمت پیدا نشد"})
            continue
        r = r[0]
        items.append({"ticker": r["ticker"], "name": r["name"], "qty": h["qty"], "price": r["close"], "d": r["d"],
                      "value": h["qty"] * r["close"], "cls": classify(r["asset_class"], r["is_gold"])})
    m = pf["manual"]
    coin = market.get("coin")
    manual_items = [
        ("طلای ۱۸ عیار (گرم)", m["gold18_grams"], (market.get("gold18") or {}).get("close"), "gold"),
        ("سکه امامی (عدد)", m["coin_emami"], (coin or {}).get("close"), "gold"),
        ("نقد (تومان)", m["cash_toman"], 10.0, "cash"),
        ("دلار (اسکناس)", m["usd"], (market.get("usd") or {}).get("close"), "other"),
    ]
    for label, qty, price, cls in manual_items:
        if qty and price:
            items.append({"ticker": label, "qty": qty, "price": price, "value": qty * price, "cls": cls, "manual": True})
        elif qty:
            items.append({"ticker": label, "qty": qty, "error": "قیمت پیدا نشد"})
    total = sum(i.get("value", 0) for i in items)
    by_class = {k: 0.0 for k in CLASS_LABELS}
    for i in items:
        if "value" in i:
            by_class[i["cls"]] += i["value"]
    alloc = []
    for k, label in CLASS_LABELS.items():
        cur = by_class[k] / total * 100 if total else 0.0
        tgt = float(pf["target"].get(k, 0))
        alloc.append({"key": k, "label": label, "value": by_class[k], "current_pct": cur, "target_pct": tgt,
                      "gap_pct": cur - tgt, "gap_value": by_class[k] - tgt / 100 * total})
    return {"items": items, "total": total, "allocation": alloc,
            "max_gap_pct": max((abs(a["gap_pct"]) for a in alloc), default=0.0), "band_pct": REBALANCE_BAND_POINTS,
            "total_usd": total / market["usd"]["close"] if total and market.get("usd") else None}


def route_cash(allocation: List[Dict[str, Any]], total: float, amount: float) -> List[Dict[str, Any]]:
    """
    Splits new money (amount > 0) or a withdrawal (amount < 0) across classes so the portfolio moves
    towards its targets with no selling beyond the withdrawal: only classes on the wanted side of
    their target take part, in proportion to how far they are from it. Values are in rial.
    """
    if not amount or total + amount < 0:
        return []
    needs = {a["key"]: a["target_pct"] / 100 * (total + amount) - a["value"] for a in allocation}
    side = {k: v for k, v in needs.items() if v * amount > 0}
    size = sum(abs(v) for v in side.values())
    if not size:
        return []
    labels = {a["key"]: a["label"] for a in allocation}
    # All needs sum to the amount, so the wanted side alone is at least as large; scaling it fits the amount.
    return [{"key": k, "label": labels[k], "amount": amount * abs(v) / size} for k, v in side.items()]


def health(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    last_d = con.execute("SELECT max(d) FROM daily_prices").fetchall()[0][0]
    snap = _rows(con, "SELECT d, final, taken_at FROM snapshots ORDER BY d DESC LIMIT 1")
    run = con.execute("SELECT max(run_id) FROM quality_checks").fetchall()[0][0]
    checks = _rows(con, "SELECT check_name, severity, detail FROM quality_checks WHERE run_id = ? ORDER BY logged_at", [run]) if run else []
    last_fetch = con.execute("SELECT max(logged_at) FROM fetch_log").fetchall()[0][0]
    worst = "fail" if any(c["severity"] == "fail" for c in checks) else ("warn" if any(c["severity"] == "warn" for c in checks) else "ok")
    return {"last_market_date": last_d, "snapshot": snap[0] if snap else None, "last_run": run,
            "last_fetch": last_fetch, "checks": checks, "worst": worst}


def context_series(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    """Monthly series for the context charts (last value of each month)."""
    ted_usd = _rows(con, """
        WITH t AS (SELECT d, close FROM index_daily WHERE index_code = ?),
             u AS (SELECT d, close FROM macro_daily WHERE series = 'usd_irr')
        SELECT strftime(date_trunc('month', t.d), '%Y-%m') AS m, arg_max(t.close / u.close * 10, t.d) AS v
        FROM t JOIN u USING (d) GROUP BY 1 ORDER BY 1""", [TEDPIX])
    bubble = _rows(con, """
        WITH g AS (SELECT d, close FROM macro_daily WHERE series = 'gold_18k_irr'),
             o AS (SELECT d, close FROM macro_daily WHERE series = 'gold_ounce_usd'),
             u AS (SELECT d, close FROM macro_daily WHERE series = 'usd_irr')
        SELECT strftime(date_trunc('month', g.d), '%Y-%m') AS m,
               avg((g.close / (o.close * u.close / 31.1034768 * 0.75) - 1) * 100) AS v
        FROM g JOIN o USING (d) JOIN u USING (d) GROUP BY 1 ORDER BY 1""")
    return {"tedpix_usd": ted_usd, "gold18_bubble": bubble}


def record_today(con: duckdb.DuckDBPyConnection, source: str, overwrite: bool = True,
                 today: Optional[date] = None) -> str:
    """
    Records today's value from stored closing prices (never live quotes, so a day means the same
    whoever records it). Returns recorded | exists | no_portfolio | empty | incomplete | failed.

    A wrong record would read as a real gain or loss, so a portfolio file that cannot be read or a
    holding without a price means no record at all; the next record simply covers the gap.
    """
    today = today or date.today()
    try:
        with open(PORTFOLIO_PATH, encoding="utf-8") as f:
            json.load(f)  # load_portfolio treats a damaged file as an empty portfolio
    except (OSError, ValueError):
        return "no_portfolio"
    try:
        if not overwrite and tracking.has_day(today):
            return "exists"
        market = market_snapshot(con)
        valuation = value_portfolio(con, load_portfolio(), market)
        if any("error" in i for i in valuation["items"]):
            return "incomplete"
        if not valuation["total"] and not tracking.load_history():
            return "empty"
        tracking.upsert_day(tracking.build_day(today, valuation, market, source))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, duckdb.Error):
        return "failed"
    return "recorded"


def portfolio_record_check(con: duckdb.DuckDBPyConnection) -> Tuple[str, str, str]:
    """The collector's daily record, as a quality check; a problem here never stops the collection."""
    status = record_today(con, "collector")
    detail = {
        "recorded": "today's portfolio value recorded",
        "no_portfolio": "no readable data/portfolio.json, nothing recorded",
        "empty": "portfolio is empty, nothing recorded yet",
        "incomplete": "a holding has no price; today is not recorded",
        "failed": "could not record today's value; check data/portfolio_history.json",
    }[status]
    return ("portfolio_record", "warn" if status in ("incomplete", "failed") else "ok", detail)


def state(con: duckdb.DuckDBPyConnection, pf: Dict[str, Any],
          live: Optional[List[Dict[str, Any]]] = None, record_status: Optional[str] = None) -> Dict[str, Any]:
    market = market_snapshot(con, live)
    return {
        "generated_at": datetime.now(),
        "health": health(con),
        "market": market,
        "gold_funds": goldfunds.fund_table(con),
        "portfolio": pf,
        "valuation": value_portfolio(con, pf, market),
        "context": context_series(con),
        "global_gold": globalgold.dashboard(con),
        "free_usd": freeusd.dashboard(con),
        "bills": treasury.dashboard(con),
        "tracking": dict(tracking.report(), record_status=record_status),
    }


# ---------------------------------------------------------------- HTTP

def _json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def _connect(db_path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path, read_only=True)


_last_state: Optional[Dict[str, Any]] = None   # served, marked stale, while the collector locks the database


class Handler(BaseHTTPRequestHandler):
    db_path = datastore.DEFAULT_DB_PATH

    def log_message(self, format, *args):
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=_json_default).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _with_db(self, fn, on_locked=None):
        try:
            con = _connect(self.db_path)
        except duckdb.Error:
            # The collector holds a write lock for the few minutes it runs.
            if on_locked:
                return on_locked()
            return self._json(503, {"error": "داده‌ها در حال به‌روزرسانی هستند؛ چند دقیقه دیگر صفحه را تازه کنید."})
        try:
            return fn(con)
        finally:
            con.close()

    def _fresh_state(self, con):
        global _last_state
        # Covers days the collector did not run; the collector's later run replaces this row.
        _last_state = state(con, load_portfolio(), get_live_prices(), record_today(con, "dashboard", overwrite=False))
        return self._json(200, _last_state)

    def _stale_state(self):
        if _last_state is None:
            return self._json(503, {"error": "داده‌ها در حال به‌روزرسانی هستند؛ چند دقیقه دیگر صفحه را تازه کنید."})
        return self._json(200, {**_last_state, "stale": True})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(datastore.BASE_DIR, "dashboard.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._with_db(self._fresh_state, on_locked=self._stale_state)
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path not in ("/api/portfolio", "/api/route", "/api/flows", "/api/flows/delete"):
            return self._send(404, b"not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "درخواست نامعتبر"})

        if self.path == "/api/route":
            try:
                rial = float(payload.get("toman", 0)) * 10
            except (TypeError, ValueError):
                return self._json(400, {"error": "مبلغ باید عدد باشد"})

            def route(con):
                v = value_portfolio(con, load_portfolio(), market_snapshot(con, get_live_prices()))
                return self._json(200, {"parts": route_cash(v["allocation"], v["total"], rial)})
            return self._with_db(route)

        def save(con):
            try:
                if self.path == "/api/flows":
                    tracking.add_flow(payload)
                elif self.path == "/api/flows/delete":
                    tracking.delete_flow(str(payload.get("id")))
                else:
                    save_portfolio(validate_portfolio(con, payload))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            # Saving holdings replaces today's record, so changes made today count as today's.
            status = record_today(con, "dashboard") if self.path == "/api/portfolio" else None
            return self._json(200, state(con, load_portfolio(), get_live_prices(), status))
        return self._with_db(save)


class ExclusiveServer(ThreadingHTTPServer):
    # http.server enables SO_REUSEADDR, which on Windows lets a second copy bind the same port
    # silently; requests then land on either copy at random. Bind exclusively instead.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    port = int(os.environ.get("PORT", PORT))
    try:
        server = ExclusiveServer(("127.0.0.1", port), Handler)
    except OSError:
        print(f"The dashboard is already running at http://127.0.0.1:{port} - use that window.")
        print(f"(If you just updated the code, close the other dashboard window first and start again.)")
        return 0
    print(f"Iran Market AI dashboard: http://127.0.0.1:{port}  (keep this window open)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
