"""
Global gold: the ounce's drivers, where each one stands today against its own history, and which
of them held up out of sample.

Sources (free, no API key, reachable from Iran):
- Yahoo Finance chart API: COMEX gold and silver futures, dollar index (DXY), gold volatility (GVZ),
  10-year nominal and 3-month US Treasury yields
- US Treasury: 10-year real yield (TIPS), daily since 2003
- CFTC disaggregated Commitments of Traders: COMEX gold positions by trader type, weekly since 2006
- the spot ounce already stored from TGJU (macro_daily, since 1979)

Every feature at day t uses only data published by t. COT positions are as of Tuesday but released
on Friday evening (US time), so a report is used from the following Monday.
"""

import csv
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import duckdb
import numpy as np
import polars as pl

from net import HttpClient

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={p1}&period2={p2}&interval=1d"
YAHOO_SERIES = {
    "gold_fut": "GC=F",       # COMEX front-month; cross-check for the TGJU spot ounce
    "silver_fut": "SI=F",
    "dxy": "DX-Y.NYB",
    "gvz": "^GVZ",            # CBOE gold volatility index (implied, 30-day)
    "us10y": "^TNX",          # 10y nominal yield, %
    "us3m": "^IRX",           # 13-week bill yield, %
}
TREASURY_REAL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                 "daily-treasury-rates.csv/{year}/all?type=daily_treasury_real_yield_curve"
                 "&field_tdr_date_value={year}&page&_format=csv")
REAL_YIELD_FIRST_YEAR = 2003
CFTC_HIST = "https://www.cftc.gov/files/dea/history/fut_disagg_txt_hist_2006_2016.zip"
CFTC_YEAR = "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
CFTC_GOLD = "088691"          # GOLD - COMMODITY EXCHANGE INC.
COT_RELEASE_LAG = 6           # Tuesday positions -> usable Monday


# ---------------------------------------------------------------- fetch + parse

def fetch_yahoo(http: HttpClient, symbol: str, since: date) -> Dict[str, Any]:
    p1 = int(datetime(since.year, since.month, since.day, tzinfo=timezone.utc).timestamp())
    url = YAHOO.format(symbol=symbol.replace("=", "%3D").replace("^", "%5E"), p1=p1, p2=int(datetime.now().timestamp()) + 86400)
    data = http.get_json_url(url)
    result = ((data.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise ValueError(f"no chart result for {symbol}: {(data.get('chart') or {}).get('error')}")
    return result


def parse_yahoo(result: Dict[str, Any], series: str) -> List[Dict[str, Any]]:
    """
    Daily closes keyed by the exchange's local date (timestamps are the session start in UTC, which
    for US markets is still the same calendar day). The last bar of a session still trading is kept;
    it is replaced by the settled close on the next run.
    """
    offset = int((result.get("meta") or {}).get("gmtoffset") or 0)
    ts = result.get("timestamp") or []
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = []
    for t, c in zip(ts, closes):
        if c is None or c <= 0:
            continue
        d = datetime.fromtimestamp(t + offset, tz=timezone.utc).date()
        out.append({"series": series, "d": d, "close": float(c)})
    return out


def fetch_real_yield_csv(http: HttpClient, year: int) -> str:
    return http.get_bytes(TREASURY_REAL.format(year=year), timeout=30).decode("utf-8", errors="replace")


def parse_real_yield_csv(text: str) -> List[Dict[str, Any]]:
    """Treasury CSV: Date (MM/DD/YYYY), then one column per maturity; only the 10-year is kept."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        raw = (row.get("10 YR") or "").strip()
        try:
            d = datetime.strptime(row.get("Date", "").strip(), "%m/%d/%Y").date()
            v = float(raw)
        except ValueError:
            continue
        out.append({"series": "real10", "d": d, "close": v})
    return out


def fetch_cot_zip(http: HttpClient, url: str) -> bytes:
    return http.get_bytes(url, timeout=120)


def parse_cot_zip(blob: bytes) -> List[Dict[str, Any]]:
    out = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            with z.open(name) as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="latin-1")):
                    r = parse_cot_row(row)
                    if r:
                        out.append(r)
    return out


def parse_cot_row(row: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if (row.get("CFTC_Contract_Market_Code") or "").strip() != CFTC_GOLD:
        return None

    def n(key: str) -> Optional[float]:
        try:
            return float((row.get(key) or "").strip())
        except ValueError:
            return None
    try:
        d = datetime.strptime((row.get("Report_Date_as_YYYY-MM-DD") or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    oi = n("Open_Interest_All")
    if not oi:
        return None
    return {
        "report_d": d,
        "released_d": d + timedelta(days=COT_RELEASE_LAG),
        "open_interest": oi,
        "mm_long": n("M_Money_Positions_Long_All"),
        "mm_short": n("M_Money_Positions_Short_All"),
        "pm_long": n("Prod_Merc_Positions_Long_All"),
        "pm_short": n("Prod_Merc_Positions_Short_All"),
        "sw_long": n("Swap_Positions_Long_All"),
        "sw_short": n("Swap__Positions_Short_All"),
    }


def refresh(run: Any, full: bool) -> Dict[str, int]:
    """
    Pulls every global series. `run` is collector.Run (logs each fetch). full=True re-downloads all
    history; otherwise only the last few weeks of Yahoo data, this year's real yields and this
    year's COT file are fetched (plus anything missing, e.g. on the first run).
    """
    import datastore
    con, http = run.con, run.http
    counts: Dict[str, int] = {}
    today = date.today()
    have = dict(con.execute("SELECT series, max(d) FROM global_daily GROUP BY series").fetchall())

    for series, symbol in YAHOO_SERIES.items():
        since = date(1990, 1, 1) if full or series not in have else have[series] - timedelta(days=21)
        res = run.fetch("yahoo", symbol, lambda symbol=symbol, since=since: fetch_yahoo(http, symbol, since))
        if res:
            counts[series] = datastore.upsert(con, "global_daily", parse_yahoo(res, series))

    first = REAL_YIELD_FIRST_YEAR if full or "real10" not in have else have["real10"].year
    n = 0
    for year in range(first, today.year + 1):
        text = run.fetch("treasury", f"real_yield_{year}", lambda year=year: fetch_real_yield_csv(http, year))
        if text:
            n += datastore.upsert(con, "global_daily", parse_real_yield_csv(text))
    counts["real10"] = n

    last_cot = con.execute("SELECT max(report_d) FROM cot_gold").fetchall()[0][0]
    urls = []
    if full or last_cot is None:
        urls.append(("hist_2006_2016", CFTC_HIST))
        urls += [(str(y), CFTC_YEAR.format(year=y)) for y in range(2017, today.year + 1)]
    else:
        urls += [(str(y), CFTC_YEAR.format(year=y)) for y in range(last_cot.year, today.year + 1)]
    n = 0
    for label, url in urls:
        blob = run.fetch("cftc", label, lambda url=url: fetch_cot_zip(http, url))
        if blob:
            n += datastore.upsert(con, "cot_gold", parse_cot_zip(blob))
    counts["cot"] = n
    return counts


# ---------------------------------------------------------------- panel and features

IN_SAMPLE = (date(2006, 7, 1), date(2016, 12, 31))
TROY_OUNCE_G = 31.1034768
OUT_OF_SAMPLE = (date(2017, 1, 1), date(2100, 1, 1))


def load_panel(con: duckdb.DuckDBPyConnection, start: date = date(2003, 1, 1)) -> pl.DataFrame:
    """
    One row per US trading day (the DXY calendar). Each series is its last value on or before that
    day; the gold price is the TGJU spot ounce (same-day aligned with COMEX: corr 0.90 on daily
    moves, 0.17-0.27 one day off), falling back to the front-month future where TGJU has no row.
    """
    def frame(sql: str, params: Optional[list] = None) -> pl.DataFrame:
        return pl.from_arrow(con.sql(sql, params=params or []).to_arrow_table()).sort("d")

    out = frame("SELECT d FROM global_daily WHERE series = 'dxy' AND d >= ?", [start])
    out = out.join(frame("""SELECT d, close AS spot FROM macro_daily
                            WHERE series = 'gold_ounce_usd' AND source IS DISTINCT FROM 'live'"""), on="d", how="left")
    for name in ("gold_fut", "silver_fut", "dxy", "gvz", "us10y", "us3m", "real10"):
        s = frame("SELECT d, close AS v FROM global_daily WHERE series = ?", [name]).rename({"v": name})
        out = out.join_asof(s, on="d", strategy="backward")
    cot = frame("""SELECT released_d AS d, (mm_long - mm_short) / open_interest AS mm_net,
                          (sw_long - sw_short) / open_interest AS sw_net FROM cot_gold""")
    out = out.join_asof(cot, on="d", strategy="backward")
    return out.with_columns(pl.coalesce("spot", "gold_fut").forward_fill().alias("gold")).drop("spot")


def _rolling_pct_rank(x: np.ndarray, window: int, min_obs: int) -> np.ndarray:
    """Share of the trailing `window` values (including today) at or below today's value."""
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        if np.isnan(x[i]):
            continue
        w = x[max(0, i - window + 1): i + 1]
        w = w[~np.isnan(w)]
        if len(w) >= min_obs:
            out[i] = float(np.mean(w <= x[i]))
    return out


def fair_value_resid(log_gold: np.ndarray, real10: np.ndarray, log_dxy: np.ndarray, window: int = 756) -> np.ndarray:
    """
    Gold against its 'macro fair value': log gold regressed on the 10y real yield and log DXY over
    the trailing `window` days; today's residual in units of the window's residual std. Positive =
    gold is above what real yields and the dollar explain.
    """
    out = np.full(len(log_gold), np.nan)
    for i in range(window - 1, len(log_gold)):
        sl = slice(i - window + 1, i + 1)
        y, a, b = log_gold[sl], real10[sl], log_dxy[sl]
        ok = ~(np.isnan(y) | np.isnan(a) | np.isnan(b))
        if not ok[-1] or ok.sum() < window * 0.8:
            continue
        X = np.column_stack([np.ones(ok.sum()), a[ok], b[ok]])
        coef, *_ = np.linalg.lstsq(X, y[ok], rcond=None)
        resid = y[ok] - X @ coef
        sd = resid.std()
        if sd > 0:
            out[i] = resid[-1] / sd
    return out


def add_features(p: pl.DataFrame) -> pl.DataFrame:
    p = p.with_columns(
        (pl.col("gold") / pl.col("gold").rolling_mean(200) - 1).alias("trend200"),
        (pl.col("gold") / pl.col("gold").shift(252) - 1).alias("mom12m"),
        (pl.col("real10") - pl.col("real10").shift(63)).alias("real10_chg3m"),
        (pl.col("dxy") / pl.col("dxy").shift(63) - 1).alias("dxy_chg3m"),
        (pl.col("gold") / pl.col("silver_fut")).alias("gold_silver"),
        (pl.col("gold") / pl.col("gold").cum_max() - 1).alias("from_high"),
    )
    arr = lambda c: p[c].cast(pl.Float64).fill_null(np.nan).to_numpy()
    p = p.with_columns(
        pl.Series("cot_pct3y", _rolling_pct_rank(arr("mm_net"), 756, 250)),
        pl.Series("gvz_pct3y", _rolling_pct_rank(arr("gvz"), 756, 250)),
        pl.Series("gsr_pct5y", _rolling_pct_rank(arr("gold_silver"), 1260, 500)),
        pl.Series("fair_value_z", fair_value_resid(np.log(arr("gold")), arr("real10"), np.log(arr("dxy")))),
    )
    return p.with_columns(*(
        (pl.col("gold").shift(-h) / pl.col("gold") - 1).alias(f"fwd{h}") for h in (20, 60, 120)))


# ---------------------------------------------------------------- out-of-sample tests

# feature -> (Persian label, sign the textbook story predicts for forward gold returns)
SIGNALS = {
    "trend200": ("روند: فاصله انس از میانگین ۲۰۰ روزه", +1),
    "mom12m": ("مومنتوم: بازده ۱۲ ماه گذشته انس", +1),
    "real10_chg3m": ("تغییر ۳ ماهه بازده واقعی ۱۰ ساله آمریکا", -1),
    "dxy_chg3m": ("تغییر ۳ ماهه شاخص دلار (DXY)", -1),
    "cot_pct3y": ("خالص خرید صندوق‌ها در COT (صدک ۳ ساله)", -1),
    "gvz_pct3y": ("نوسان ضمنی طلا GVZ (صدک ۳ ساله)", +1),
    "gsr_pct5y": ("نسبت طلا به نقره (صدک ۵ ساله)", -1),
    "fair_value_z": ("فاصله از ارزش منصفانه (بازده واقعی + دلار)", -1),
}


def tercile_test(p: pl.DataFrame, feature: str, horizon: int = 60) -> Dict[str, Any]:
    """
    Tercile breakpoints come from the design period only and are applied unchanged to the test
    period. Spread = mean forward return of the top tercile minus the bottom one. The t-stat counts
    one independent observation per `horizon` days, since overlapping windows are not independent.
    """
    fwd = f"fwd{horizon}"
    d = p.select("d", feature, fwd).drop_nulls().filter(pl.col(feature).is_not_nan())
    design = d.filter((pl.col("d") >= IN_SAMPLE[0]) & (pl.col("d") <= IN_SAMPLE[1]))
    if design.height < 500:
        return {"feature": feature}
    lo, hi = np.quantile(design[feature].to_numpy(), [1 / 3, 2 / 3])
    out: Dict[str, Any] = {"feature": feature, "cuts": (float(lo), float(hi)), "horizon": horizon}
    for name, (a, b) in (("design", IN_SAMPLE), ("test", OUT_OF_SAMPLE)):
        s = d.filter((pl.col("d") >= a) & (pl.col("d") <= b))
        x, y = s[feature].to_numpy(), s[fwd].to_numpy()
        top, mid, bot = y[x > hi], y[(x >= lo) & (x <= hi)], y[x < lo]
        if min(len(top), len(bot)) < horizon:
            out[name] = None
            continue
        spread = top.mean() - bot.mean()
        se = np.sqrt(top.var() / max(2.0, len(top) / horizon) + bot.var() / max(2.0, len(bot) / horizon))
        out[name] = {"days": int(len(y)), "start": s["d"][0], "end": s["d"][-1],
                     "low": float(bot.mean()), "mid": float(mid.mean()) if len(mid) else None,
                     "high": float(top.mean()), "spread": float(spread), "t": float(spread / se) if se else 0.0,
                     "n_low": int(len(bot)), "n_high": int(len(top))}
    return out


def verdict(t: Dict[str, Any]) -> str:
    """
    'validated': same direction in both periods, at least 1 standard error in design and 2 in the
    unseen test period; 'weak': same direction but not significant; 'failed': direction flipped or
    the test-period spread is under half a percent.
    """
    a, b = t.get("design"), t.get("test")
    if not a or not b:
        return "untested"
    if np.sign(a["spread"]) != np.sign(b["spread"]) or abs(b["spread"]) < 0.005:
        return "failed"
    return "validated" if abs(b["t"]) >= 2 and abs(a["t"]) >= 1 else "weak"


def trend_timing(p: pl.DataFrame, start: date, end: date, ma: int = 200, cost: float = 0.001) -> Dict[str, Dict[str, float]]:
    """
    Hold gold while it closed above its `ma`-day average the day before, otherwise T-bills (3m
    yield); `cost` is charged on each switch. Compared with simply holding gold, both in USD.
    """
    g = p["gold"].to_numpy().astype(float)
    sma = p["gold"].rolling_mean(ma).to_numpy()
    bill = np.nan_to_num(p["us3m"].cast(pl.Float64).to_numpy()) / 100 / 252
    days = p["d"].to_list()
    idx = np.array([i for i, d in enumerate(days) if start <= d <= end and i > ma])
    r_gold = g[idx] / g[idx - 1] - 1
    on = g[idx - 1] > sma[idx - 1]
    switches = np.abs(np.diff(on.astype(int), prepend=int(on[0]))).astype(float)
    r_trend = np.where(on, r_gold, bill[idx]) - switches * cost
    years = (days[idx[-1]] - days[idx[0]]).days / 365.25
    out = {"start": days[idx[0]], "end": days[idx[-1]]}
    for name, r in (("hold", r_gold), ("trend", r_trend)):
        curve = np.cumprod(1 + r)
        out[name] = {"cagr": float(curve[-1] ** (1 / years) - 1),
                     "maxdd": float(np.min(curve / np.maximum.accumulate(curve) - 1)),
                     "vol": float(r.std() * np.sqrt(252)),
                     "in_gold": float(on.mean()) if name == "trend" else 1.0,
                     "switches": int(switches.sum()) if name == "trend" else 0}
    return out


# ---------------------------------------------------------------- context for the dashboard

def rolling_corr_weekly(p: pl.DataFrame, weeks: int = 52) -> pl.DataFrame:
    """
    Correlation of weekly gold returns with weekly changes in the 10y real yield and in DXY over the
    trailing `weeks`. Up to about 2021 gold moved against real yields almost mechanically; this
    shows whether that still holds.
    """
    w = (p.sort("d").group_by_dynamic("d", every="1w", label="right").agg(
        pl.col("gold").last(), pl.col("real10").last(), pl.col("dxy").last())
        .with_columns((pl.col("gold") / pl.col("gold").shift(1) - 1).alias("rg"),
                      (pl.col("real10") - pl.col("real10").shift(1)).alias("dr"),
                      (pl.col("dxy") / pl.col("dxy").shift(1) - 1).alias("rd")))
    return w.with_columns(
        pl.rolling_corr("rg", "dr", window_size=weeks).alias("corr_real10"),
        pl.rolling_corr("rg", "rd", window_size=weeks).alias("corr_dxy"),
    ).select("d", "corr_real10", "corr_dxy")


def iran_gold_panel(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Days with 18k gold, the ounce and free-market USD from TGJU on the same day (closed days only)."""
    return pl.from_arrow(con.sql("""
        SELECT g.d, g.close AS g18, o.close AS ounce, u.close AS usd
        FROM macro_daily g JOIN macro_daily o ON o.d = g.d AND o.series = 'gold_ounce_usd'
        JOIN macro_daily u ON u.d = g.d AND u.series = 'usd_irr'
        WHERE g.series = 'gold_18k_irr' AND g.source IS DISTINCT FROM 'live'
          AND o.source IS DISTINCT FROM 'live' AND u.source IS DISTINCT FROM 'live'
        ORDER BY g.d""").to_arrow_table()).with_columns(
        (pl.col("g18") / (pl.col("ounce") * pl.col("usd") / TROY_OUNCE_G * 0.75)).alias("bubble_factor"))


def decompose(ip: pl.DataFrame, days: int) -> Optional[Dict[str, Any]]:
    """
    18k gold (rial) = ounce x USD x bubble factor x constant, so its log return over a window is
    exactly the sum of the three parts' log returns. Each part is returned as a plain percentage,
    plus its share of the log move.
    """
    if ip.height < 2:
        return None
    end = ip.row(-1, named=True)
    base = ip.filter(pl.col("d") <= end["d"] - timedelta(days=days))
    if not base.height:
        return None
    b = base.row(-1, named=True)
    parts = {k: float(np.log(end[c] / b[c])) for k, c in (("ounce", "ounce"), ("usd", "usd"), ("bubble", "bubble_factor"))}
    total = float(np.log(end["g18"] / b["g18"]))
    return {"from": b["d"], "to": end["d"], "total": float(np.expm1(total)),
            **{k: float(np.expm1(v)) for k, v in parts.items()},
            "share": {k: v / total for k, v in parts.items()} if abs(total) > 1e-9 else None}


def variance_attribution(ip: pl.DataFrame, since: date) -> Optional[Dict[str, Any]]:
    """
    Share of the variance of monthly 18k gold returns that comes from each part
    (cov(part, total) / var(total); the three shares sum to 1).
    """
    m = (ip.filter(pl.col("d") >= since).group_by_dynamic("d", every="1mo").agg(
        pl.col("g18").last(), pl.col("ounce").last(), pl.col("usd").last(), pl.col("bubble_factor").last()))
    if m.height < 13:
        return None
    lr = {c: np.diff(np.log(m[c].to_numpy())) for c in ("g18", "ounce", "usd", "bubble_factor")}
    var = lr["g18"].var()
    return {"months": int(len(lr["g18"])),
            **{k: float(np.cov(lr[c], lr["g18"], bias=True)[0, 1] / var)
               for k, c in (("ounce", "ounce"), ("usd", "usd"), ("bubble", "bubble_factor"))}}


DRIVERS = [
    # key, Persian label, unit, column, how the textbook links it to gold
    ("gold", "انس طلا", "$", "gold", ""),
    ("real10", "بازده واقعی ۱۰ ساله آمریکا (TIPS)", "%", "real10", "هزینه فرصت نگه‌داشتن طلا؛ در کتاب‌ها رابطه معکوس"),
    ("us10y", "بازده اسمی ۱۰ ساله آمریکا", "%", "us10y", ""),
    ("dxy", "شاخص دلار (DXY)", "", "dxy", "طلا به دلار قیمت می‌خورد؛ در کتاب‌ها رابطه معکوس"),
    ("gvz", "نوسان ضمنی طلا (GVZ)", "", "gvz", "انتظار بازار از نوسان ۳۰ روز آینده"),
    ("gold_silver", "نسبت طلا به نقره", "", "gold_silver", ""),
    ("mm_net", "خالص خرید صندوق‌ها در کامکس (COT)", "%oi", "mm_net", "درصد از قراردادهای باز"),
    ("sw_net", "خالص موقعیت بانک‌های بولیون (COT)", "%oi", "sw_net", "معمولاً طرف مقابل صندوق‌ها"),
]


def driver_rows(p: pl.DataFrame) -> List[Dict[str, Any]]:
    """Each driver today, its 3-month change and where today sits within its own last 5 years."""
    rows = []
    five_years_ago = p["d"][-1] - timedelta(days=5 * 365)
    for key, label, unit, col, note in DRIVERS:
        s = p.select("d", col).drop_nulls()
        if not s.height:
            continue
        cur = s.row(-1, named=True)
        prev = s.filter(pl.col("d") <= cur["d"] - timedelta(days=91))
        hist = s.filter(pl.col("d") >= five_years_ago)[col].to_numpy()
        v0 = prev[col][-1] if prev.height else None
        is_level = unit in ("%", "%oi")
        rows.append({
            "key": key, "label": label, "unit": unit, "note": note, "d": cur["d"], "value": float(cur[col]),
            "chg3m": None if v0 is None else (float(cur[col] - v0) if is_level else float(cur[col] / v0 - 1)),
            "chg_is_points": is_level,
            "pct5y": float(np.mean(hist <= cur[col])),
            "min5y": float(hist.min()), "max5y": float(hist.max()),
        })
    return rows


def signal_table(p: pl.DataFrame, horizon: int = 60) -> List[Dict[str, Any]]:
    out = []
    for f, (label, textbook) in SIGNALS.items():
        t = tercile_test(p, f, horizon)
        a, b = t.get("design"), t.get("test")
        out.append({
            "feature": f, "label": label, "verdict": verdict(t), "textbook_sign": textbook,
            "design_spread": a["spread"] if a else None, "design_t": a["t"] if a else None,
            "test_spread": b["spread"] if b else None, "test_t": b["t"] if b else None,
            "matches_textbook": bool(a and b and np.sign(a["spread"]) == textbook == np.sign(b["spread"])),
        })
    return out


_cache: Dict[str, Any] = {"key": None, "value": None}


def dashboard(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    """Everything the dashboard's global gold section needs; recomputed only when new data arrives."""
    key = con.execute("""SELECT (SELECT max(d) FROM global_daily), (SELECT max(report_d) FROM cot_gold),
                                (SELECT max(d) FROM macro_daily WHERE source IS DISTINCT FROM 'live')""").fetchall()[0]
    if key[0] is None:
        return {"available": False}
    if _cache["key"] == key:
        return _cache["value"]
    p = add_features(load_panel(con))
    last = p.row(-1, named=True)
    corr = rolling_corr_weekly(p)
    ip = iran_gold_panel(con)
    monthly = (p.group_by_dynamic("d", every="1mo").agg(pl.col("gold").last(), pl.col("real10").last())
               .join(corr.group_by_dynamic("d", every="1mo").agg(pl.col("corr_real10").last(), pl.col("corr_dxy").last()),
                     on="d", how="left").filter(pl.col("d") >= date(2006, 1, 1)))
    corr_now = corr.drop_nulls().row(-1, named=True) if corr.drop_nulls().height else {}
    value = {
        "available": True,
        "as_of": last["d"],
        "cot_report": con.execute("SELECT max(report_d) FROM cot_gold").fetchall()[0][0],
        "gold": {"price": last["gold"], "trend200": last["trend200"], "from_high": last["from_high"],
                 "high": float(p["gold"].max()), "mom12m": last["mom12m"], "fair_value_z": last["fair_value_z"]},
        "drivers": driver_rows(p),
        "signals": signal_table(p),
        "trend_timing": {"design": trend_timing(p, *IN_SAMPLE), "test": trend_timing(p, *OUT_OF_SAMPLE)},
        "corr_now": {"real10": corr_now.get("corr_real10"), "dxy": corr_now.get("corr_dxy")},
        "decomposition": {k: decompose(ip, n) for k, n in (("1m", 30), ("3m", 91), ("12m", 365))},
        "variance": {"3y": variance_attribution(ip, last["d"] - timedelta(days=3 * 365)),
                     "all": variance_attribution(ip, date(2013, 7, 1))},
        "monthly": [{"m": r["d"].strftime("%Y-%m"), "gold": r["gold"], "real10": r["real10"],
                     "corr_real10": r["corr_real10"], "corr_dxy": r["corr_dxy"]} for r in monthly.iter_rows(named=True)],
        "periods": {"design": IN_SAMPLE, "test": (OUT_OF_SAMPLE[0], last["d"])},
    }
    _cache.update(key=key, value=value)
    return value


# ---------------------------------------------------------------- report

def report(con: duckdb.DuckDBPyConnection) -> None:
    p = add_features(load_panel(con))
    print(f"Panel: {p['d'][0]} .. {p['d'][-1]}, {p.height} US trading days")
    print(f"Design period {IN_SAMPLE[0]} .. {IN_SAMPLE[1]}; test period {OUT_OF_SAMPLE[0]} .. {p['d'][-1]}")
    for h in (20, 60, 120):
        print(f"\n=== Terciles (design-period cut points) -> next {h} US trading days of the ounce ===")
        print(f"  {'signal':<14}{'textbook':>9} | {'design low':>10}{'high':>8}{'spread':>8}{'t':>6} | "
              f"{'test low':>9}{'high':>8}{'spread':>8}{'t':>6}  verdict")
        for f, (_, sign) in SIGNALS.items():
            t = tercile_test(p, f, h)
            a, b = t.get("design"), t.get("test")
            if not a or not b:
                print(f"  {f:<14} not enough data")
                continue
            print(f"  {f:<14}{'+' if sign > 0 else '-':>9} | {a['low']:>+10.1%}{a['high']:>+8.1%}{a['spread']:>+8.1%}{a['t']:>+6.1f} | "
                  f"{b['low']:>+9.1%}{b['high']:>+8.1%}{b['spread']:>+8.1%}{b['t']:>+6.1f}  {verdict(t)}")

    print("\n=== Trend timing: ounce above its 200-day average, else T-bills (0.1% per switch), USD ===")
    for name, (a, b) in (("design", IN_SAMPLE), ("test", OUT_OF_SAMPLE)):
        r = trend_timing(p, a, b)
        for k in ("hold", "trend"):
            m = r[k]
            print(f"  {name:<7}{k:<6} CAGR {m['cagr']:+.1%}  MaxDD {m['maxdd']:+.1%}  vol {m['vol']:.1%}  "
                  f"in gold {m['in_gold']:.0%}  switches {m['switches']}")

    corr = rolling_corr_weekly(p)
    print("\n=== 52-week correlation of weekly gold returns with real-yield changes / DXY returns (year end) ===")
    ye = corr.group_by_dynamic("d", every="1y").agg(pl.col("corr_real10").last(), pl.col("corr_dxy").last()).drop_nulls()
    for r in ye.iter_rows(named=True):
        print(f"  {r['d'].year}  real10 {r['corr_real10']:+.2f}   DXY {r['corr_dxy']:+.2f}")

    ip = iran_gold_panel(con)
    print("\n=== What moves Iranian 18k gold: share of monthly variance (cov with total / var total) ===")
    for label, since in (("since 2013", date(2013, 7, 1)), ("last 3 years", p["d"][-1] - timedelta(days=3 * 365))):
        v = variance_attribution(ip, since)
        if v:
            print(f"  {label:<14} months {v['months']:>4}  ounce {v['ounce']:.0%}  USD {v['usd']:.0%}  bubble {v['bubble']:.0%}")
    print("\n=== 18k gold return split into ounce x USD x bubble ===")
    for k, n in (("1m", 30), ("3m", 91), ("12m", 365)):
        d = decompose(ip, n)
        if d:
            print(f"  {k:<4}{d['from']} .. {d['to']}  18k {d['total']:+.1%} = ounce {d['ounce']:+.1%} "
                  f"x USD {d['usd']:+.1%} x bubble {d['bubble']:+.1%}")


def main() -> int:
    import argparse
    import sys
    import datastore
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Global gold drivers: out-of-sample report")
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
