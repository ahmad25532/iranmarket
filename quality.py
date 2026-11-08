"""
Data-quality checks run after every collection. Each check writes one row to
quality_checks with severity ok / warn / fail, so bad or stale data is visible
instead of silently flowing into signals.
"""

from datetime import date
from typing import List, Optional, Tuple

import duckdb

from datastore import record_check

Check = Tuple[str, str, str]  # (name, severity, detail)


def _scalar(con: duckdb.DuckDBPyConnection, sql: str, params: Optional[list] = None):
    row = con.execute(sql, params or []).fetchone()
    return row[0] if row else None


def check_snapshot_date(d: date, today: date) -> Check:
    # Iranian weekend is Thursday/Friday plus holidays; more than 5 calendar days is suspicious.
    lag = (today - d).days
    if lag < 0:
        return ("snapshot_date", "fail", f"market date {d} is in the future")
    if lag > 5:
        return ("snapshot_date", "warn", f"last market activity {d} is {lag} days old (holiday or stale feed)")
    return ("snapshot_date", "ok", f"market date {d}")


def check_price_rows(con: duckdb.DuckDBPyConnection, d: date) -> Check:
    n = _scalar(con, "SELECT count(*) FROM daily_prices WHERE d = ?", [d]) or 0
    prev = con.execute(
        """SELECT count(*) AS n FROM daily_prices WHERE d < ? AND d >= ? - INTERVAL 30 DAY
           GROUP BY d ORDER BY d DESC LIMIT 5""",
        [d, d],
    ).fetchall()
    if n == 0:
        return ("price_rows", "fail", f"no price rows for {d}")
    if prev:
        med = sorted(r[0] for r in prev)[len(prev) // 2]
        if n < 0.7 * med:
            return ("price_rows", "warn", f"{n} rows vs recent median {med}")
        return ("price_rows", "ok", f"{n} rows (recent median {med})")
    return ("price_rows", "ok", f"{n} rows (no earlier days to compare)")


def check_price_sanity(con: duckdb.DuckDBPyConnection, d: date) -> Check:
    # The closing price (pClosing) only moves once enough volume trades, so on thin days it can
    # legitimately sit outside the day's range; the last trade price must always be inside it.
    bad = con.execute(
        """SELECT i.ticker FROM daily_prices p LEFT JOIN instruments i USING (ins_code)
           WHERE p.d = ? AND (p.close <= 0 OR p.last <= 0
                 OR (p.low > 0 AND p.high > 0 AND (p.last < p.low * 0.999 OR p.last > p.high * 1.001))
                 OR (p.yesterday > 0 AND abs(p.last / p.yesterday - 1) > 0.35))""",
        [d],
    ).fetchall()
    if bad:
        sample = ", ".join(str(r[0]) for r in bad[:8])
        return ("price_sanity", "warn", f"{len(bad)} rows with last price outside range or >35% move: {sample}")
    return ("price_sanity", "ok", "all last prices inside daily range")


def check_flow_coverage(con: duckdb.DuckDBPyConnection, d: date) -> Check:
    n_prices, n_flows = con.execute(
        """SELECT count(*), count(f.ins_code) FROM daily_prices p
           LEFT JOIN client_flows f ON f.ins_code = p.ins_code AND f.d = p.d WHERE p.d = ?""",
        [d],
    ).fetchone()
    if not n_prices:
        return ("flow_coverage", "fail", "no price rows")
    pct = 100.0 * n_flows / n_prices
    sev = "ok" if pct >= 90 else "warn"
    return ("flow_coverage", sev, f"{n_flows}/{n_prices} traded instruments have flow data ({pct:.1f}%)")


def check_flow_volume_consistency(con: duckdb.DuckDBPyConnection, d: date) -> Check:
    """Individual + institutional buy volume must equal the day's traded volume."""
    total, bad = con.execute(
        """SELECT count(*),
                  count(*) FILTER (WHERE abs((f.buy_i_volume + f.buy_n_volume) / p.volume - 1) > 0.01)
           FROM daily_prices p JOIN client_flows f ON f.ins_code = p.ins_code AND f.d = p.d
           WHERE p.d = ? AND p.volume > 0""",
        [d],
    ).fetchone()
    if not total:
        return ("flow_volume_consistency", "warn", "no rows to compare")
    pct = 100.0 * bad / total
    sev = "ok" if pct <= 5 else "warn"
    return ("flow_volume_consistency", sev, f"{bad}/{total} rows where buy volume != traded volume (>1%)")


def check_nav(con: duckdb.DuckDBPyConnection, d: date, etf_count: int) -> Check:
    n, outliers = con.execute(
        """SELECT count(*), count(*) FILTER (WHERE close > 0 AND abs(close / nav_redeem - 1) > 0.30)
           FROM etf_nav WHERE d = ?""",
        [d],
    ).fetchone()
    if etf_count and n < 0.8 * etf_count:
        return ("etf_nav", "warn", f"NAV captured for {n}/{etf_count} ETFs on {d}")
    if outliers:
        return ("etf_nav", "warn", f"{outliers} ETFs with price/NAV gap >30% (likely data error)")
    return ("etf_nav", "ok", f"NAV captured for {n}/{etf_count} ETFs")


def check_fundamentals(con: duckdb.DuckDBPyConnection, seen_on: date) -> Check:
    # A day missed here cannot be fetched later, and an API change would otherwise lose data silently.
    n = _scalar(con, "SELECT count(*) FROM fundamentals_seen WHERE seen_on = ?", [seen_on]) or 0
    prev = con.execute(
        "SELECT count(*) FROM fundamentals_seen WHERE seen_on < ? GROUP BY seen_on ORDER BY seen_on DESC LIMIT 5",
        [seen_on],
    ).fetchall()
    if n == 0:
        return ("fundamentals", "fail", f"no EPS or P/E stored for {seen_on}; this day cannot be recovered later")
    if prev:
        med = sorted(r[0] for r in prev)[len(prev) // 2]
        if n < 0.7 * med:
            return ("fundamentals", "warn", f"{n} instruments with EPS or P/E vs recent median {med}")
    return ("fundamentals", "ok", f"{n} instruments with EPS or P/E")


def check_macro_freshness(con: duckdb.DuckDBPyConnection, today: date, series: List[str]) -> Check:
    stale = []
    for s in series:
        last = _scalar(con, "SELECT max(d) FROM macro_daily WHERE series = ?", [s])
        if last is None or (today - last).days > 4:
            stale.append(f"{s}:{last}")
    if stale:
        return ("macro_freshness", "warn", "stale TGJU series: " + ", ".join(stale))
    return ("macro_freshness", "ok", f"{len(series)} TGJU series up to date")


def check_global_freshness(con: duckdb.DuckDBPyConnection, today: date) -> Check:
    """US markets close for weekends and holidays; more than 5 days without data means a feed broke."""
    rows = con.execute("SELECT series, max(d) FROM global_daily GROUP BY series").fetchall()
    cot = _scalar(con, "SELECT max(report_d) FROM cot_gold")
    stale = [f"{s}:{d}" for s, d in rows if (today - d).days > 5]
    if cot is None or (today - cot).days > 14:
        stale.append(f"cot:{cot}")
    if not rows:
        return ("global_freshness", "warn", "no global gold data yet (run: collector.py global)")
    if stale:
        return ("global_freshness", "warn", "stale global series: " + ", ".join(stale))
    return ("global_freshness", "ok", f"{len(rows)} global series + COT up to date")


def check_usd_inputs_freshness(con: duckdb.DuckDBPyConnection, today: date) -> Check:
    """
    NIMA trades on Iranian working days; CBI inflation and US CPI come monthly, about a month after
    the month ends. The IMF's Iran CPI stopped in 2025 and is extended from CBI data, so it is not checked.
    """
    nima = _scalar(con, "SELECT max(d) FROM macro_daily WHERE series = 'usd_nima_irr'")
    monthly = dict(con.execute("SELECT series, max(period_end) FROM monthly_macro GROUP BY series").fetchall())
    if nima is None or not monthly:
        return ("usd_inputs_freshness", "warn", "no free-USD inputs yet (run: collector.py global)")
    stale = [f"usd_nima_irr:{nima}"] if (today - nima).days > 7 else []
    stale += [f"{s}:{monthly.get(s)}" for s in ("infl12_ir_cbi", "cpi_us")
              if monthly.get(s) is None or (today - monthly[s]).days > 75]
    if stale:
        return ("usd_inputs_freshness", "warn", "stale free-USD inputs: " + ", ".join(stale))
    return ("usd_inputs_freshness", "ok", "NIMA, CBI inflation and US CPI up to date")


def check_bill_yields(con: duckdb.DuckDBPyConnection, today: date, unparsed: int) -> Check:
    """Treasury bills trade on every session, so a week without a usable yield means something broke."""
    last = _scalar(con, "SELECT max(d) FROM bill_yield_daily")
    if last is None:
        return ("bill_yields", "warn", "no treasury bill yields yet (run: collector.py bills)")
    if (today - last).days > 7:
        return ("bill_yields", "warn", f"last treasury bill yield is from {last}")
    if unparsed:
        return ("bill_yields", "warn", f"{unparsed} bills have no maturity date in their name and were skipped")
    return ("bill_yields", "ok", f"treasury bill yields up to {last}")


def check_fetch_errors(con: duckdb.DuckDBPyConnection, run_id: str) -> Check:
    total, errors = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE status = 'error') FROM fetch_log WHERE run_id = ?", [run_id]
    ).fetchone()
    if not total:
        return ("fetch_errors", "fail", "nothing was fetched")
    if errors == 0:
        return ("fetch_errors", "ok", f"{total} requests, no errors")
    sev = "fail" if errors / total > 0.10 else "warn"
    samples = con.execute(
        "SELECT target, error FROM fetch_log WHERE run_id = ? AND status = 'error' LIMIT 3", [run_id]
    ).fetchall()
    detail = "; ".join(f"{t}: {e[:80]}" for t, e in samples)
    return ("fetch_errors", sev, f"{errors}/{total} requests failed. {detail}")


def find_gaps(
    con: duckdb.DuckDBPyConnection, table: str, trading_days: List[date], min_ratio: float = 0.7
) -> List[date]:
    """
    Trading days (from the TEDPIX calendar) on which `table` has far fewer rows than usual.
    Days before the table's first stored date are ignored, so a table that only starts
    later (e.g. NAV) is not reported as missing its whole past.
    """
    if not trading_days:
        return []
    counts = dict(con.execute(
        f"SELECT d, count(*) FROM {table} WHERE d >= ? AND d <= ? GROUP BY d",
        [trading_days[0], trading_days[-1]],
    ).fetchall())
    first = _scalar(con, f"SELECT min(d) FROM {table}")
    present = sorted(n for n in counts.values() if n > 0)
    if first is None or not present:
        return list(trading_days)
    median = present[len(present) // 2]
    return [d for d in trading_days if d >= first and counts.get(d, 0) < min_ratio * median]


def check_gaps(name: str, gaps: List[date]) -> Check:
    if not gaps:
        return (name, "ok", "no missing trading days in the lookback window")
    shown = ", ".join(str(d) for d in gaps[:6]) + (" ..." if len(gaps) > 6 else "")
    return (name, "warn", f"{len(gaps)} trading days still incomplete: {shown}")


def compare_closes(snapshot_close: float, official_close: float, tol: float = 0.005) -> bool:
    return official_close > 0 and abs(snapshot_close / official_close - 1) <= tol


def save_and_summarize(con: duckdb.DuckDBPyConnection, run_id: str, d: Optional[date], checks: List[Check]) -> int:
    """Stores checks and prints a summary. Returns number of failed checks."""
    icons = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    fails = 0
    for name, sev, detail in checks:
        record_check(con, run_id, d, name, sev, detail)
        print(f"  [{icons[sev]}] {name}: {detail}")
        fails += sev == "fail"
    return fails
