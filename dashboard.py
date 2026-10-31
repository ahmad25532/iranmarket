"""
Iran Market AI — dashboard (Phase 4).

Shows only what passed the Phase 3 out-of-sample tests, plus context and data health:
- your portfolio split (stocks / gold / fixed income / cash) against the target you set
- today's gold funds ranked by premium over NAV, with the liquidity tiers the test used
- free-market USD, 18k gold, its bubble vs intrinsic value, TEDPIX in dollars (context only)
- whether today's data is complete and final

Run:  .venv\\Scripts\\python.exe dashboard.py   ->  http://127.0.0.1:8085
"""

import json
import os
import sys
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import duckdb

import datastore

PORT = 8085
PORTFOLIO_PATH = os.path.join(datastore.BASE_DIR, "data", "portfolio.json")
TEDPIX = "32097828799138957"
LIQUID_VALUE = 1e11       # rial/day (10B toman), as in the Phase 3 test
VERY_LARGE_VALUE = 1e12   # rial/day (100B toman)
LIQUID_TRADES = 100

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

def load_portfolio(path: str = PORTFOLIO_PATH) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
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


def save_portfolio(data: Dict[str, Any], path: str = PORTFOLIO_PATH) -> None:
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


def market_snapshot(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    usd, g18, ons = (_latest_macro(con, s) for s in ("usd_irr", "gold_18k_irr", "gold_ounce_usd"))
    out: Dict[str, Any] = {"usd": usd, "gold18": g18, "ounce": ons}
    # The bubble must compare prices of the same day: the ounce also trades on Iranian weekends.
    same_day = _rows(con, """
        SELECT g.d, g.close AS g, o.close AS o, u.close AS u
        FROM macro_daily g JOIN macro_daily o ON o.d = g.d AND o.series = 'gold_ounce_usd'
        JOIN macro_daily u ON u.d = g.d AND u.series = 'usd_irr'
        WHERE g.series = 'gold_18k_irr' ORDER BY g.d DESC LIMIT 1""")
    if same_day:
        r = same_day[0]
        intrinsic = r["o"] * r["u"] / 31.1034768 * 0.75
        out["gold18_intrinsic"] = intrinsic
        out["gold18_bubble"] = r["g"] / intrinsic - 1
        out["bubble_date"] = r["d"]
    ted = _rows(con, "SELECT d, close FROM index_daily WHERE index_code = ? ORDER BY d DESC LIMIT 1", [TEDPIX])
    out["tedpix"] = ted[0] if ted else None
    return out


def gold_funds_today(con: duckdb.DuckDBPyConnection) -> List[Dict[str, Any]]:
    """Each gold fund on its latest trading day in the last 10 days: VWAP premium over same-day NAV and liquidity."""
    rows = _rows(con, """
        WITH last AS (
            SELECT p.ins_code, max(p.d) AS d FROM daily_prices p JOIN gold_funds USING (ins_code)
            WHERE p.trades > 0 AND p.volume > 0 AND p.d >= (SELECT max(d) FROM daily_prices) - INTERVAL 10 DAY
            GROUP BY 1),
        liq AS (
            SELECT p.ins_code, avg(p.value) AS avg_value FROM daily_prices p JOIN gold_funds USING (ins_code)
            WHERE p.d >= (SELECT max(d) FROM daily_prices) - INTERVAL 30 DAY GROUP BY 1)
        SELECT c.ticker, c.name, l.d, p.close, p.value / p.volume AS vwap, p.value, p.trades, n.nav_redeem,
               (p.value / p.volume) / n.nav_redeem - 1 AS premium, liq.avg_value
        FROM last l JOIN daily_prices p USING (ins_code, d) JOIN instrument_class c USING (ins_code)
        JOIN etf_nav n ON n.ins_code = l.ins_code AND n.d = l.d
        LEFT JOIN liq ON liq.ins_code = l.ins_code
        WHERE n.nav_redeem > 0 AND abs(p.close / n.nav_redeem - 1) <= 0.30
        ORDER BY premium""")
    for r in rows:
        r["liquid"] = (r["avg_value"] or 0) >= LIQUID_VALUE and r["trades"] >= LIQUID_TRADES
        r["very_large"] = (r["avg_value"] or 0) >= VERY_LARGE_VALUE
    # The Phase 3 edge was measured within the >= 10B toman/day tier (which includes the largest funds).
    cheapest = next((r for r in rows if r["liquid"]), None)
    for r in rows:
        r["cheapest_liquid"] = r is cheapest
    return rows


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
    coin = _latest_macro(con, "coin_emami_irr")
    manual_items = [
        ("طلای ۱۸ عیار (گرم)", m["gold18_grams"], (market.get("gold18") or {}).get("close"), "gold"),
        ("سکه امامی (عدد)", m["coin_emami"], (coin or {}).get("close"), "gold"),
        ("نقد (تومان)", m["cash_toman"], 10.0, "cash"),
        ("دلار (اسکناس)", m["usd"], (market.get("usd") or {}).get("close"), "other"),
    ]
    for label, qty, price, cls in manual_items:
        if qty and price:
            items.append({"ticker": label, "qty": qty, "price": price, "value": qty * price, "cls": cls, "manual": True})
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
            "total_usd": total / market["usd"]["close"] if total and market.get("usd") else None}


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


def state(con: duckdb.DuckDBPyConnection, pf: Dict[str, Any]) -> Dict[str, Any]:
    market = market_snapshot(con)
    return {
        "generated_at": datetime.now(),
        "health": health(con),
        "market": market,
        "gold_funds": gold_funds_today(con),
        "portfolio": pf,
        "valuation": value_portfolio(con, pf, market),
        "context": context_series(con),
    }


# ---------------------------------------------------------------- HTTP

def _json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def _connect(db_path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path, read_only=True)


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

    def _with_db(self, fn):
        try:
            con = _connect(self.db_path)
        except duckdb.Error:
            # The collector holds a write lock for the few minutes it runs.
            return self._json(503, {"error": "داده‌ها در حال به‌روزرسانی هستند؛ چند دقیقه دیگر صفحه را تازه کنید."})
        try:
            return fn(con)
        finally:
            con.close()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(datastore.BASE_DIR, "dashboard.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._with_db(lambda con: self._json(200, state(con, load_portfolio())))
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/portfolio":
            return self._send(404, b"not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "درخواست نامعتبر"})

        def save(con):
            try:
                clean = validate_portfolio(con, payload)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            save_portfolio(clean)
            return self._json(200, state(con, clean))
        return self._with_db(save)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Iran Market AI dashboard: http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
