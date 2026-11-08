"""
Risk-free yield from Iranian treasury bills (اخزا), stored daily as context. It is NOT a signal.

Bills pay no coupon and redeem at FACE_VALUE on the Jalali date at the end of their name, so a day's
weighted closing price gives a yield to maturity directly. Only days with real trades count: TSETMC
keeps repeating the last price on days a bill did not trade.

Limits of the data:
- TSETMC lists no matured bills, so the history holds only bills still alive today. It reaches back
  at most about 700 trading days and the early part rests on a few long-dated bills.
- From now on every collector run keeps the rows, so bills that mature stay in the table. Only that
  will make a proper design/test split possible; the comovement report here is description only.
"""

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import numpy as np
import polars as pl

import datastore
import sources
from freeusd import jalali_to_gregorian

FACE_VALUE = 1_000_000.0
TEDPIX = "32097828799138957"

# Normal board only. Block-trade boards add a digit to the ticker (اخزا4022) and their prices are negotiated.
BILL_TICKER = re.compile(r"^اخزا\d{3}$")

# Chosen before looking at the yields:
MIN_DAYS = 30                 # closer to maturity, a small price move is a huge annualised yield
SHORT_MAX_DAYS = 365          # the short end of the curve
MIN_VALUE = 100_000_000.0     # rial; about the 5th percentile of a bill's daily value, removes token trades
MAX_YTM = 2.0                 # above 200% a year is a bad print, not a yield
MIN_BILLS = 3                 # fewer bills than this is too thin to call a market yield


# ---------------------------------------------------------------- pure calculations

def is_bill(ticker: str) -> bool:
    return bool(BILL_TICKER.match((ticker or "").strip()))


def parse_maturity(name: str) -> Optional[date]:
    """'اسناد خزانه-م5بودجه04-070420' -> 1407/04/20 in Gregorian. None if the name has no valid date."""
    m = re.search(r"(\d{2})(\d{2})(\d{2})\s*$", name or "")
    if not m:
        return None
    yy, mm, dd = (int(x) for x in m.groups())
    if not (1 <= mm <= 12 and 1 <= dd <= (31 if mm <= 6 else 30)):
        return None
    return jalali_to_gregorian(1400 + yy, mm, dd)


def ytm(price: float, days_left: int, face: float = FACE_VALUE) -> Optional[float]:
    """Annual compounded yield, actual/365. None when the bill has matured or the price is unusable."""
    if days_left <= 0 or price <= 0:
        return None
    return (face / price) ** (365.0 / days_left) - 1.0


def bill_rows(ins_code: str, maturity: date, raw: List[Dict[str, Any]], before: Optional[date] = None) -> List[Dict[str, Any]]:
    """Traded days of one bill from the official daily list. `before` drops a session still open."""
    out = []
    for row in raw:
        p = sources.parse_price_history(row)
        if not p or not p["trades"] or not p["value"] or (before and p["d"] >= before):
            continue
        days_left = (maturity - p["d"]).days
        out.append({
            "ins_code": ins_code, "d": p["d"], "maturity": maturity, "days_left": days_left,
            "close": p["close"], "value": p["value"], "trades": p["trades"], "ytm": ytm(p["close"], days_left),
        })
    return out


# ---------------------------------------------------------------- storage

BUILD_DAILY_SQL = f"""
DELETE FROM bill_yield_daily;
INSERT INTO bill_yield_daily
SELECT d,
       median(ytm) AS ytm_median,
       median(ytm) FILTER (WHERE days_left <= {SHORT_MAX_DAYS}) AS ytm_short,
       count(*) AS n_bills,
       count(*) FILTER (WHERE days_left <= {SHORT_MAX_DAYS}) AS n_short,
       median(days_left) AS median_days,
       sum(value) AS value
FROM bill_yields
WHERE days_left >= {MIN_DAYS} AND value >= {MIN_VALUE} AND ytm > 0 AND ytm <= {MAX_YTM}
GROUP BY d;
"""


def build_daily(con: duckdb.DuckDBPyConnection) -> int:
    """Rebuilds the daily series from the per-bill table."""
    con.execute(BUILD_DAILY_SQL)
    return con.execute("SELECT count(*) FROM bill_yield_daily").fetchone()[0]


def listed_bills(con: duckdb.DuckDBPyConnection, today: date) -> Tuple[List[Tuple[str, date]], List[str]]:
    """(ins_code, maturity) of normal-board bills not yet matured, and tickers whose name has no date."""
    bills, unparsed = [], []
    for code, ticker, name in con.execute(
        "SELECT ins_code, ticker, name FROM instruments WHERE ticker LIKE 'اخزا%' ORDER BY ticker"
    ).fetchall():
        if not is_bill(ticker):
            continue
        maturity = parse_maturity(name)
        if maturity is None:
            unparsed.append(ticker)
        elif maturity >= today:
            bills.append((code, maturity))
    return bills, unparsed


def refresh(run: Any, before: Optional[date] = None) -> Dict[str, int]:
    """
    Fetches each live bill's whole list (one short request per bill, so a missed week needs no catch-up
    logic) and rebuilds the daily series. Rows of bills that have matured are kept.
    """
    bills, unparsed = listed_bills(run.con, date.today())
    fetched = run.fetch_many("bill_history", [
        (code, lambda code=code: sources.fetch_price_history(run.http, code, 0)) for code, _ in bills
    ])
    rows = 0
    for code, maturity in bills:
        if code in fetched:
            rows += datastore.upsert(run.con, "bill_yields", bill_rows(code, maturity, fetched[code], before))
    return {"bills": len(bills), "fetched": len(fetched), "rows": rows,
            "days": build_daily(run.con), "unparsed": len(unparsed)}


# ---------------------------------------------------------------- dashboard

def _has_table(con: duckdb.DuckDBPyConnection) -> bool:
    # The dashboard opens the database read-only, so the table may not exist before the first collector run.
    return bool(con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'bill_yield_daily'"
    ).fetchall())


def dashboard(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    if not _has_table(con):
        return {"available": False}
    rows = con.execute(f"""
        SELECT d, ytm_median, ytm_short, n_bills, n_short, median_days FROM bill_yield_daily
        WHERE n_bills >= {MIN_BILLS} ORDER BY d""").fetchall()
    if not rows:
        return {"available": False}
    d, med, short, n, n_short, days = rows[-1]
    monthly: Dict[str, Dict[str, Any]] = {}
    for r in rows:  # last value of each month
        monthly[r[0].strftime("%Y-%m")] = {"m": r[0].strftime("%Y-%m"), "median": r[1] * 100,
                                           "short": r[2] * 100 if r[2] is not None and r[4] >= MIN_BILLS else None}
    return {
        "available": True, "as_of": d, "ytm_median": med, "n_bills": n, "median_days": days,
        "ytm_short": short if n_short >= MIN_BILLS else None, "n_short": n_short,
        "since": rows[0][0], "monthly": list(monthly.values()),
    }


# ---------------------------------------------------------------- comovement with TEDPIX (description only)

def _calendar(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """TEDPIX trading days from the first day the series has MIN_BILLS bills."""
    start = con.execute(f"SELECT min(d) FROM bill_yield_daily WHERE n_bills >= {MIN_BILLS}").fetchone()[0]
    rows = con.execute(
        "SELECT d, close FROM index_daily WHERE index_code = ? AND d >= ? ORDER BY d", [TEDPIX, start]
    ).fetchall()
    return pl.DataFrame(rows, schema=["d", "ted"], orient="row")


def block_changes(cal: pl.DataFrame, bills: pl.DataFrame, size: int) -> pl.DataFrame:
    """
    Non-overlapping blocks of `size` trading days. For each block: the TEDPIX log return and the median
    across bills of each bill's own yield change. Following the same bills keeps the shift in the
    maturity mix out of the change, which a difference of the daily median would not.
    """
    blocks = cal.with_row_index("i").with_columns((pl.col("i") // size).cast(pl.Int64).alias("b"))
    ends = blocks.group_by("b").agg(pl.col("d").max().alias("end"), pl.col("ted").last(), pl.len().alias("n")).sort("b")
    ends = ends.filter(pl.col("n") == size)
    ted = ends.with_columns(pl.col("ted").log().diff().alias("r"))
    last = (bills.join(blocks.select("d", "b"), on="d").sort("d").group_by("ins_code", "b").agg(pl.col("ytm").last())
            .sort("ins_code", "b"))
    change = (last.with_columns(pl.col("ytm").diff().over("ins_code").alias("dy"), pl.col("b").diff().over("ins_code").alias("gap"))
              .filter(pl.col("gap") == 1)
              .group_by("b").agg(pl.col("dy").median(), pl.len().alias("n_bills")))
    return ted.join(change, on="b").drop_nulls(["r", "dy"]).sort("b").select("b", "end", "r", "dy", "n_bills")


def block_bootstrap_ci(x: np.ndarray, y: np.ndarray, block: int = 8, reps: int = 2000, seed: int = 7) -> Tuple[float, float]:
    """95% interval for a correlation, resampling runs of neighbouring periods to respect autocorrelation."""
    n = len(x)
    if n < block * 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(reps):
        starts = rng.integers(0, n - block + 1, size=n // block + 1)
        idx = np.concatenate([np.arange(s, s + block) for s in starts])[:n]
        if x[idx].std() > 0 and y[idx].std() > 0:
            out.append(np.corrcoef(x[idx], y[idx])[0, 1])
    return (float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5)))


def _corr(x: np.ndarray, y: np.ndarray, block: int) -> Dict[str, Any]:
    lo, hi = block_bootstrap_ci(x, y, block)
    rank = lambda a: pl.Series(a).rank().to_numpy()
    return {"n": len(x), "r": float(np.corrcoef(x, y)[0, 1]), "spearman": float(np.corrcoef(rank(x), rank(y))[0, 1]),
            "lo": lo, "hi": hi}


def comovement(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    """How the yield moved with TEDPIX in the available window. Describes the past; tests nothing."""
    cal = _calendar(con)
    bills = pl.DataFrame(con.execute(f"""
        SELECT ins_code, d, ytm FROM bill_yields
        WHERE days_left >= {MIN_DAYS} AND value >= {MIN_VALUE} AND ytm > 0 AND ytm <= {MAX_YTM}""").fetchall(),
        schema=["ins_code", "d", "ytm"], orient="row")
    out: Dict[str, Any] = {"start": cal["d"][0], "end": cal["d"][-1], "trading_days": cal.height}

    weekly = block_changes(cal, bills, 5)
    monthly = block_changes(cal, bills, 20)
    w_r, w_y = weekly["r"].to_numpy(), weekly["dy"].to_numpy()
    m_r, m_y = monthly["r"].to_numpy(), monthly["dy"].to_numpy()
    out["weekly"] = _corr(w_y, w_r, 8)
    out["monthly"] = _corr(m_y, m_r, 4)

    # Same-period link in each half of the window. Not a design/test split: nothing is chosen on the first half.
    mid = len(w_r) // 2
    out["weekly_first_half"] = _corr(w_y[:mid], w_r[:mid], 8)
    out["weekly_second_half"] = _corr(w_y[mid:], w_r[mid:], 8)

    # Without the largest stock moves, to see whether a few weeks carry the number.
    keep = np.abs(w_r) <= np.percentile(np.abs(w_r), 95)
    out["weekly_no_extremes"] = _corr(w_y[keep], w_r[keep], 8)

    # Yield change k weeks before the TEDPIX return (k > 0: yield first).
    lags = []
    for k in range(-4, 5):
        a, b = (w_y[:-k], w_r[k:]) if k > 0 else ((w_y[-k:], w_r[:k]) if k < 0 else (w_y, w_r))
        lags.append({"k": k, **_corr(a, b, 8)})
    out["lags"] = lags

    # TEDPIX in the month after a month in which the yield rose most / least.
    if len(m_y) >= 12:
        y, nxt = m_y[:-1], m_r[1:]
        lo_cut, hi_cut = np.percentile(y, [100 / 3, 200 / 3])
        terciles = []
        for label, mask in (("fell most", y <= lo_cut), ("middle", (y > lo_cut) & (y < hi_cut)), ("rose most", y >= hi_cut)):
            v = np.exp(nxt[mask]) - 1
            terciles.append({"label": label, "n": int(mask.sum()), "dy_mean": float(y[mask].mean()), "next_mean": float(v.mean()),
                             "next_median": float(np.median(v)), "up_share": float((v > 0).mean())})
        out["terciles"] = terciles

    # Levels share a trend in an inflationary market, so this number says little; reported to say exactly that.
    daily = pl.DataFrame(con.execute(f"""
        SELECT y.d, y.ytm_median, i.close FROM bill_yield_daily y
        JOIN index_daily i ON i.d = y.d AND i.index_code = '{TEDPIX}'
        WHERE y.n_bills >= {MIN_BILLS} ORDER BY y.d""").fetchall(), schema=["d", "y", "ted"], orient="row")
    out["level_r"] = float(np.corrcoef(daily["y"].to_numpy(), np.log(daily["ted"].to_numpy()))[0, 1])
    return out


def report(con: duckdb.DuckDBPyConnection) -> None:
    print("Treasury bill yield and TEDPIX - DESCRIPTION ONLY. Nothing here is tested or confirmed.")
    print("About 3 years, and only bills still alive today: too short for a design/test split.\n")

    print("Face value check: the bill closest to maturity, last traded days")
    for ticker, d, days, close, y in con.execute("""
        SELECT i.ticker, b.d, b.days_left, b.close, b.ytm FROM bill_yields b JOIN instruments i USING (ins_code)
        WHERE b.ins_code = (SELECT ins_code FROM bill_yields WHERE d = (SELECT max(d) FROM bill_yields) ORDER BY days_left LIMIT 1)
        ORDER BY b.d DESC LIMIT 5""").fetchall():
        print(f"  {ticker}  {d}  {days:>3} days left  close {close:>10,.0f}  ytm {y * 100 if y is not None else float('nan'):6.1f}%")

    print("\nCoverage by half year (days with at least one usable bill)")
    print("  period     days  bills(median)  maturity days(median)  median ytm  short ytm  days with short")
    for r in con.execute(f"""
        SELECT year(d) || (CASE WHEN month(d) <= 6 THEN 'H1' ELSE 'H2' END) AS p, count(*), median(n_bills), median(median_days),
               median(ytm_median), median(ytm_short) FILTER (WHERE n_short >= {MIN_BILLS}), count(*) FILTER (WHERE n_short >= {MIN_BILLS})
        FROM bill_yield_daily GROUP BY 1 ORDER BY 1""").fetchall():
        short = f"{r[5] * 100:8.1f}%" if r[5] is not None else "       - "
        print(f"  {r[0]}  {r[1]:>5}  {r[2]:>13.0f}  {r[3]:>21.0f}  {r[4] * 100:9.1f}%  {short}  {r[6]:>15}")

    c = comovement(con)
    print(f"\nWindow with at least {MIN_BILLS} bills: {c['start']} .. {c['end']} ({c['trading_days']} trading days)")

    def line(label: str, s: Dict[str, Any]) -> None:
        print(f"  {label:<34} n={s['n']:>3}  r={s['r']:+.2f}  spearman={s['spearman']:+.2f}  95% interval {s['lo']:+.2f} .. {s['hi']:+.2f}")

    print("\nSame-period correlation: same-bill yield change vs TEDPIX log return")
    line("weekly (5 trading days)", c["weekly"])
    line("monthly (20 trading days)", c["monthly"])
    line("weekly, first half", c["weekly_first_half"])
    line("weekly, second half", c["weekly_second_half"])
    line("weekly, without top 5% stock moves", c["weekly_no_extremes"])

    print("\nLead and lag, weekly (k > 0: yield change k weeks BEFORE the TEDPIX return). Nine looks, so one may stand out by chance.")
    for s in c["lags"]:
        line(f"k = {s['k']:+d}", s)

    if "terciles" in c:
        print("\nTEDPIX in the next 20 trading days, by yield change in the previous 20")
        print("  yield change   months  mean yield change  next mean  next median  share up")
        for t in c["terciles"]:
            print(f"  {t['label']:<12} {t['n']:>7}  {t['dy_mean'] * 100:>+16.1f}  {t['next_mean'] * 100:>+8.1f}%  {t['next_median'] * 100:>+10.1f}%  {t['up_share'] * 100:>7.0f}%")

    print(f"\nLevels, daily: yield vs log TEDPIX r={c['level_r']:+.2f}. Both trend, so this does not show a link.")


def main() -> int:
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Treasury bill yield: comovement report (description only)")
    ap.add_argument("--db", default=datastore.DEFAULT_DB_PATH)
    args = ap.parse_args()
    con = duckdb.connect(args.db, read_only=True)
    try:
        report(con)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
