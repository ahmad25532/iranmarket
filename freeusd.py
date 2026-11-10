"""
Free-market USD (rial): the real exchange rate, the gap to the official rate, inflation and trend,
and which of them predicted the free rate out of sample.

Sources (free, no API key, reachable from Iran):
- TGJU: free-market USD (already stored), NIMA (since 2018-11) and Sana (2015-06 .. 2026-01) rates
- IMF consumer price index for Iran (Statistical Centre, monthly) via DBnomics; IMF stopped
  updating it after May 2025
- Central Bank of Iran: 12-month-average inflation by Jalali month, current to last month
- US Bureau of Labor Statistics: CPI-U, monthly

Iran's CPI after the IMF series ends is an estimate built from the CBI's inflation (see splice_cpi).
Every feature at day t uses only values published by t (released_d).
"""

import http.client as httplib
import re
import time
import urllib.parse
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import numpy as np
import polars as pl

import globalgold
import sources
from net import HEADERS, SSL_CTX, FetchError, HttpClient

OFFICIAL_SERIES = {"usd_nima_irr": "nima_sell_usd", "usd_sana_irr": "sana_sell_usd"}
IMF_CPI = "https://api.db.nomics.world/v22/series/IMF/CPI/M.IR.PCPI_IX?observations=1"
BLS_CPI = "https://download.bls.gov/pub/time.series/cu/cu.data.1.AllItems"
BLS_SERIES = "CUUR0000SA0"         # CPI-U, all items, not seasonally adjusted
CBI_HOST, CBI_PATH = "www.cbi.ir", "/Inflation/Inflation_FA.aspx"
CBI_YEAR_FIELD = "ctl00$ucBody$ucContent$ctl00$ddlYear"
CBI_FIRST_YEAR = 1383               # the IMF series starts in 1383; earlier CBI years are not needed

# Days from the end of the month until the figure is public (conservative)
RELEASE_LAG = {"cpi_ir_sci": 15, "infl12_ir_cbi": 35, "cpi_us": 20}
LAG_MONTHS = 5.5                    # a 12-month average price level trails the point level by 5.5 months

JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                 "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫", "01234567890123456789.")


# ---------------------------------------------------------------- Jalali calendar

def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    return date(gy, 1, 1) + timedelta(days=days)


def gregorian_to_jalali(d: date) -> Tuple[int, int, int]:
    jy = d.year - 621 if d >= jalali_to_gregorian(d.year - 621, 1, 1) else d.year - 622
    offset = (d - jalali_to_gregorian(jy, 1, 1)).days
    jm = offset // 31 + 1 if offset < 186 else (offset - 186) // 30 + 7
    jd = offset - (jm - 1) * 31 + 1 if jm <= 6 else offset - 186 - (jm - 7) * 30 + 1
    return jy, jm, jd


def jalali_month_end(jy: int, jm: int) -> date:
    return jalali_to_gregorian(jy + jm // 12, jm % 12 + 1, 1) - timedelta(days=1)


def month_index(jy: int, jm: int) -> int:
    return jy * 12 + jm - 1


def index_month(n: int) -> Tuple[int, int]:
    return n // 12, n % 12 + 1


# ---------------------------------------------------------------- fetch and parse

def parse_imf_cpi(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    IMF labels each Iranian month with the Gregorian month it ends in (2025-03 = Esfand 1403:
    its 12-month average rate is the 32.5% the Statistical Centre published for 1403).
    """
    out = []
    for period, value in zip(doc["period"], doc["value"]):
        if value in ("NA", None):
            continue
        y, m = (int(x) for x in period.split("-"))
        jy, jm = (y - 621, m - 3) if m >= 4 else (y - 622, m + 9)
        end = jalali_month_end(jy, jm)
        out.append({"series": "cpi_ir_sci", "period_end": end, "value": float(value),
                    "released_d": end + timedelta(days=RELEASE_LAG["cpi_ir_sci"])})
    return out


def fetch_imf_cpi(http: HttpClient) -> Dict[str, Any]:
    docs = http.get_json_url(IMF_CPI, timeout=60)["series"]["docs"]
    if not docs:
        raise ValueError("DBnomics returned no IMF CPI series for Iran")
    return docs[0]


def parse_bls_cpi(text: str) -> List[Dict[str, Any]]:
    out = []
    for line in text.splitlines()[1:]:
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) < 4 or cols[0] != BLS_SERIES or not re.fullmatch(r"M(0[1-9]|1[0-2])", cols[2]):
            continue
        if not re.fullmatch(r"\d+(\.\d+)?", cols[3]):
            continue                                    # '-': not collected (Oct 2025 shutdown)
        y, m = int(cols[1]), int(cols[2][1:])
        end = date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)
        out.append({"series": "cpi_us", "period_end": end, "value": float(cols[3]),
                    "released_d": end + timedelta(days=RELEASE_LAG["cpi_us"])})
    return out


def fetch_bls_cpi(http: HttpClient) -> str:
    return http.get_bytes(BLS_CPI, timeout=120).decode("utf-8", errors="replace")


def parse_cbi_inflation(html: str) -> List[Dict[str, Any]]:
    """The CBI page for one Jalali year: the year is the selected option, then month -> rate rows."""
    m = re.search(r'<option selected="selected" value="(\d{4})"', html)
    i, j = html.find("منتهی به ماه"), html.find("نرخ تورم سالانه")
    if not m or i < 0 or j < i:
        raise ValueError("CBI inflation page layout changed")
    jy = int(m.group(1))
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[i:j]))
    out = []
    for name, raw in re.findall("(" + "|".join(JALALI_MONTHS) + r")\s+(-?[0-9۰-۹٠-٩.٫]+)", text):
        jm = JALALI_MONTHS.index(name) + 1
        end = jalali_month_end(jy, jm)
        out.append({"series": "infl12_ir_cbi", "period_end": end, "value": float(raw.translate(_DIGITS)),
                    "released_d": end + timedelta(days=RELEASE_LAG["infl12_ir_cbi"])})
    return out


def fetch_cbi_inflation(http: HttpClient, years: List[int], timeout: float = 60) -> List[str]:
    """
    The page shows one year at a time behind an ASP.NET postback, so: GET once for the form state
    and session cookie, then POST the year selector for each year. Domestic host: LAN route.
    """
    def call(body: Optional[str], cookie: str) -> Tuple[str, str]:
        kwargs = {"source_address": (http.source_ip, 0)} if http.source_ip else {}
        conn = httplib.HTTPSConnection(CBI_HOST, timeout=timeout, context=SSL_CTX, **kwargs)
        headers = {**HEADERS, **({"Cookie": cookie} if cookie else {}),
                   **({"Content-Type": "application/x-www-form-urlencoded"} if body else {})}
        try:
            conn.request("POST" if body else "GET", CBI_PATH, body=body, headers=headers)
            res = conn.getresponse()
            text = res.read().decode("utf-8", errors="replace")
            if res.status != 200:
                raise FetchError(f"HTTP {res.status} for https://{CBI_HOST}{CBI_PATH}")
            set_cookie = "; ".join(v.split(";")[0] for k, v in res.getheaders() if k.lower() == "set-cookie")
            return text, set_cookie or cookie
        finally:
            conn.close()

    def with_retries(body: Optional[str], cookie: str) -> Tuple[str, str]:
        last: Optional[Exception] = None
        for attempt in range(http.retries):
            try:
                return call(body, cookie)
            except (OSError, FetchError, httplib.HTTPException) as e:
                last = e
                time.sleep(http.backoff_s * (attempt + 1))
        raise FetchError(f"{CBI_HOST}{CBI_PATH}: {last}")

    page, cookie = with_retries(None, "")
    form = dict(re.findall(r'<input type="hidden" name="([^"]+)" id="[^"]+" value="([^"]*)"', page))
    if "__VIEWSTATE" not in form:
        raise ValueError("CBI inflation page has no form state")
    pages = []
    for y in years:
        body = urllib.parse.urlencode({**form, "__EVENTTARGET": CBI_YEAR_FIELD, CBI_YEAR_FIELD: str(y)})
        pages.append(with_retries(body, cookie)[0])
    return pages


def refresh(run: Any, full: bool) -> Dict[str, int]:
    """
    Official rates from TGJU, CBI inflation for this Jalali year (all years from 1383 on a full
    run), and the IMF and BLS CPI files when the stored series is missing or a month behind.
    """
    import datastore
    con, http = run.con, run.http
    counts: Dict[str, int] = {}
    today = sources.tehran_today()
    have_daily = dict(con.execute("""SELECT series, max(d) FROM macro_daily
                                     WHERE series IN ('usd_nima_irr', 'usd_sana_irr') GROUP BY series""").fetchall())
    have_monthly = dict(con.execute("SELECT series, max(period_end) FROM monthly_macro GROUP BY series").fetchall())

    for series, slug in OFFICIAL_SERIES.items():
        last = have_daily.get(series)
        if not full and last is not None and (today - last).days > 60:
            continue                                    # a rate TGJU stopped publishing (Sana)
        length = 10000 if full or last is None else 30
        rows = run.fetch("tgju", slug, lambda slug=slug, length=length: sources.fetch_tgju(http, slug, length))
        if rows:
            parsed = [r for r in (sources.parse_tgju_row(x, series) for x in rows) if r]
            counts[series] = datastore.upsert(con, "macro_daily", parsed)

    jy = gregorian_to_jalali(today)[0]
    last_cbi = have_monthly.get("infl12_ir_cbi")
    years = list(range(CBI_FIRST_YEAR, jy + 1)) if full or last_cbi is None else \
        sorted({gregorian_to_jalali(last_cbi)[0], jy})
    pages = run.fetch("cbi", f"inflation_{years[0]}_{years[-1]}", lambda: fetch_cbi_inflation(http, years))
    if pages:
        counts["infl12_ir_cbi"] = datastore.upsert(con, "monthly_macro", [r for p in pages for r in parse_cbi_inflation(p)])

    def behind(series: str) -> bool:
        last = have_monthly.get(series)
        return full or last is None or (today - last).days > 45

    if behind("cpi_ir_sci"):
        doc = run.fetch("dbnomics", "imf_cpi_iran", lambda: fetch_imf_cpi(http))
        if doc:
            counts["cpi_ir_sci"] = datastore.upsert(con, "monthly_macro", parse_imf_cpi(doc))
    if behind("cpi_us"):
        text = run.fetch("bls", BLS_SERIES, lambda: fetch_bls_cpi(http))
        if text:
            counts["cpi_us"] = datastore.upsert(con, "monthly_macro", parse_bls_cpi(text))
    return counts


# ---------------------------------------------------------------- Iran's price level

def splice_cpi(sci: Dict[int, float], cbi12: Dict[int, float]) -> Dict[int, Tuple[float, bool]]:
    """
    Point CPI by Jalali month index -> (level, estimated?). The Statistical Centre's index where the
    IMF has it; after its last month (t0) it is extended with the CBI's 12-month-average inflation a:
      - the 12-month average level P follows P[t] = P[t-12] * (1 + a[t]), seeded from SCI's own months
      - CBI rates are rescaled so the CBI rate at t0 equals SCI's own 12-month-average rate at t0
        (the two agencies' rates differ by a few points)
      - the point level is P moved forward 5.5 months at P's own latest monthly growth (exact under
        steady inflation; P's monthly growth already carries most of the last year's acceleration)
    Over 35 historical cut points this was unbiased, with an RMS error of about 5% six months after
    the cut and 7% after sixteen (splice_accuracy).
    """
    out = {n: (v, False) for n, v in sci.items()}
    if not sci:
        return out
    pbar = {n: float(np.mean([sci[k] for k in range(n - 11, n + 1)]))
            for n in sci if all(k in sci for k in range(n - 11, n + 1))}
    t0 = max(sci)
    if t0 - 1 not in pbar or t0 - 12 not in pbar or t0 not in cbi12:
        return out
    scale = pbar[t0] / pbar[t0 - 12] / (1 + cbi12[t0] / 100)
    point = lambda n: pbar[n] * (pbar[n] / pbar[n - 1]) ** LAG_MONTHS
    base = point(t0)
    n = t0 + 1
    while n in cbi12 and n - 12 in pbar:
        pbar[n] = pbar[n - 12] * (1 + cbi12[n] / 100) * scale
        out[n] = (sci[t0] * point(n) / base, True)
        n += 1
    return out


def splice_accuracy(sci: Dict[int, float], cbi12: Dict[int, float], horizons: Tuple[int, ...] = (6, 12, 16),
                    cuts: int = 35) -> Dict[int, Dict[str, float]]:
    """
    Pretends the SCI series ended at each of `cuts` earlier months (every third month, starting 16
    months back) and compares the estimate `h` months after the cut with what SCI published.
    """
    errs: Dict[int, List[float]] = {h: [] for h in horizons}
    for back in range(max(horizons), max(horizons) + 3 * cuts, 3):
        t0 = max(sci) - back
        est = splice_cpi({n: v for n, v in sci.items() if n <= t0}, cbi12)
        for h in horizons:
            if t0 + h in est and t0 + h in sci:
                errs[h].append(est[t0 + h][0] / sci[t0 + h] - 1)
    return {h: {"cuts": len(e), "bias": float(np.mean(e)), "rmse": float(np.sqrt(np.mean(np.square(e))))}
            for h, e in errs.items() if e}


def _monthly(con: duckdb.DuckDBPyConnection, series: str) -> List[Tuple[date, float]]:
    return con.execute("SELECT period_end, value FROM monthly_macro WHERE series = ? ORDER BY period_end",
                       [series]).fetchall()


def _by_jalali_month(rows: List[Tuple[date, float]]) -> Dict[int, float]:
    return {month_index(*gregorian_to_jalali(d)[:2]): v for d, v in rows}


def iran_cpi(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """period_end, released_d, cpi_ir, estimated: the spliced monthly CPI with its publication date."""
    spliced = splice_cpi(_by_jalali_month(_monthly(con, "cpi_ir_sci")), _by_jalali_month(_monthly(con, "infl12_ir_cbi")))
    rows = []
    for n in sorted(spliced):
        v, est = spliced[n]
        end = jalali_month_end(*index_month(n))
        lag = RELEASE_LAG["infl12_ir_cbi" if est else "cpi_ir_sci"]
        rows.append({"period_end": end, "released_d": end + timedelta(days=lag), "cpi_ir": v, "estimated": est})
    return pl.DataFrame(rows, schema={"period_end": pl.Date, "released_d": pl.Date, "cpi_ir": pl.Float64,
                                      "estimated": pl.Boolean})


# ---------------------------------------------------------------- panel and features

DESIGN = (date(2012, 1, 1), date(2018, 12, 31))
TEST = (date(2019, 1, 1), date(2100, 1, 1))
ROWS_PER_YEAR = 280                 # TGJU publishes the free rate about 280 days a year
DEPOSIT_RATE = 0.20                 # one-year bank deposit, roughly flat 2012-2026


def load_panel(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """One row per day TGJU has a closed free-market USD price; everything else as of that day."""
    def frame(sql: str, params: Optional[list] = None) -> pl.DataFrame:
        return pl.from_arrow(con.sql(sql, params=params or []).to_arrow_table()).sort("d")

    out = frame("""SELECT d, close AS usd FROM macro_daily
                   WHERE series = 'usd_irr' AND source IS DISTINCT FROM 'live'""")
    for name in OFFICIAL_SERIES:
        s = frame("SELECT d, close AS v FROM macro_daily WHERE series = ? AND source IS DISTINCT FROM 'live'",
                  [name]).rename({"v": name})
        out = out.join_asof(s, on="d", strategy="backward", tolerance=timedelta(days=10))
    cpi = iran_cpi(con).select(pl.col("released_d").alias("d"), "cpi_ir", pl.col("estimated").alias("cpi_estimated"))
    out = out.join_asof(cpi.sort("d"), on="d", strategy="backward")
    us = frame("SELECT released_d AS d, value AS cpi_us FROM monthly_macro WHERE series = 'cpi_us'")
    out = out.join_asof(us, on="d", strategy="backward")
    infl = frame("SELECT released_d AS d, value AS infl12 FROM monthly_macro WHERE series = 'infl12_ir_cbi'")
    infl = infl.with_columns((pl.col("infl12") - pl.col("infl12").shift(3)).alias("infl_accel"))
    out = out.join_asof(infl, on="d", strategy="backward")
    return out.with_columns(pl.coalesce("usd_nima_irr", "usd_sana_irr").alias("official"))


def add_features(p: pl.DataFrame) -> pl.DataFrame:
    p = p.with_columns(
        (pl.col("usd") * pl.col("cpi_us") / pl.col("cpi_ir")).log().alias("rer"),
        (pl.col("usd") / pl.col("usd").rolling_mean(200) - 1).alias("trend200"),
        (pl.col("usd") / pl.col("usd").shift(ROWS_PER_YEAR) - 1).alias("mom12m"),
        (pl.col("usd") / pl.col("official")).log().alias("gap"),
        (pl.col("usd") / pl.col("usd").cum_max() - 1).alias("from_high"),
    )
    p = p.with_columns((pl.col("rer") - pl.col("rer").rolling_mean(3 * ROWS_PER_YEAR, min_samples=2 * ROWS_PER_YEAR))
                       .alias("rer_dev3y"))
    return p.with_columns(*(
        (pl.col("usd").shift(-h) / pl.col("usd") - 1).alias(f"fwd{h}") for h in (20, 60, 120)))


# feature -> (Persian label, sign the common story predicts for the free rate's forward return).
# The real rate's level is not a signal here: it rose ~3x after 2018 and every test-period day lies
# above the design period's top tercile, so its level cannot be tested (see real_trend).
SIGNALS = {
    "rer_dev3y": ("نرخ ارز واقعی نسبت به میانگین ۳ ساله‌اش", -1),
    "gap": ("فاصله دلار آزاد از نرخ رسمی (نیما/سنا)", -1),
    "infl_accel": ("شتاب تورم: تغییر ۳ ماهه تورم ۱۲ ماهه بانک مرکزی", +1),
    "trend200": ("روند: فاصله دلار از میانگین ۲۰۰ روزه", +1),
    "mom12m": ("مومنتوم: بازده ۱۲ ماه گذشته دلار", +1),
}


def tercile_test(p: pl.DataFrame, feature: str, horizon: int = 60) -> Dict[str, Any]:
    return globalgold.tercile_test(p, feature, horizon, periods=(DESIGN, TEST))


def signal_table(p: pl.DataFrame, horizons: Tuple[int, ...] = (20, 60)) -> List[Dict[str, Any]]:
    out = []
    for f, (label, story) in SIGNALS.items():
        row: Dict[str, Any] = {"feature": f, "label": label, "textbook_sign": story}
        for h in horizons:
            t = tercile_test(p, f, h)
            a, b = t.get("design"), t.get("test")
            row[f"h{h}"] = {
                "verdict": globalgold.verdict(t),
                "design_spread": a["spread"] if a else None, "design_t": a["t"] if a else None,
                "test_spread": b["spread"] if b else None, "test_t": b["t"] if b else None,
            }
        out.append(row)
    return out


def real_trend(p: pl.DataFrame, start: date, end: date) -> Optional[Dict[str, Any]]:
    """Average yearly change of the real rate (log-linear fit) and its change over the period."""
    s = p.filter((pl.col("d") >= start) & (pl.col("d") <= end)).select("d", "rer").drop_nulls()
    if s.height < ROWS_PER_YEAR:
        return None
    t = np.array([(d - s["d"][0]).days / 365.25 for d in s["d"]])
    slope = np.polyfit(t, s["rer"].to_numpy(), 1)[0]
    return {"start": s["d"][0], "end": s["d"][-1], "per_year": float(np.exp(slope) - 1),
            "total": float(np.exp(s["rer"][-1] - s["rer"][0]) - 1)}


def trend_timing(p: pl.DataFrame, start: date, end: date, ma: int = 200, cost: float = 0.005) -> Dict[str, Any]:
    """
    Hold dollars while the free rate closed above its `ma`-day average the day before, otherwise a
    rial deposit at DEPOSIT_RATE; `cost` (half the free market's buy/sell spread) on each switch.
    """
    u = p["usd"].to_numpy().astype(float)
    sma = p["usd"].rolling_mean(ma).to_numpy()
    days = p["d"].to_list()
    idx = np.array([i for i, d in enumerate(days) if start <= d <= end and i > ma])
    gap_days = np.array([(days[i] - days[i - 1]).days for i in idx], dtype=float)
    r_usd = u[idx] / u[idx - 1] - 1
    r_dep = (1 + DEPOSIT_RATE) ** (gap_days / 365) - 1
    on = u[idx - 1] > sma[idx - 1]
    switches = np.abs(np.diff(on.astype(int), prepend=int(on[0]))).astype(float)
    r_trend = np.where(on, r_usd, r_dep) - switches * cost
    years = (days[idx[-1]] - days[idx[0]]).days / 365.25
    out: Dict[str, Any] = {"start": days[idx[0]], "end": days[idx[-1]]}
    for name, r in (("hold", r_usd), ("trend", r_trend), ("deposit", r_dep)):
        curve = np.cumprod(1 + r)
        out[name] = {"cagr": float(curve[-1] ** (1 / years) - 1),
                     "maxdd": float(np.min(curve / np.maximum.accumulate(curve) - 1)),
                     "in_usd": float(on.mean()) if name == "trend" else (1.0 if name == "hold" else 0.0),
                     "switches": int(switches.sum()) if name == "trend" else 0}
    return out


# ---------------------------------------------------------------- dashboard and report

_cache: Dict[str, Any] = {"key": None, "value": None}


def dashboard(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    """Everything the dashboard's free-USD section needs; recomputed only when new data arrives."""
    key = con.execute("""SELECT (SELECT max(d) FROM macro_daily WHERE series = 'usd_irr' AND source IS DISTINCT FROM 'live'),
                                (SELECT max(d) FROM macro_daily WHERE series = 'usd_nima_irr'),
                                (SELECT max(period_end) FROM monthly_macro)""").fetchall()[0]
    if key[2] is None:
        return {"available": False}
    if _cache["key"] == key:
        return _cache["value"]
    p = add_features(load_panel(con)).filter(pl.col("d") >= DESIGN[0])
    last = p.row(-1, named=True)
    base = float(p.filter(pl.col("d") <= DESIGN[1])["rer"].drop_nulls().mean())   # 2012-2018 average = 100
    cpi = iran_cpi(con)
    est = cpi.filter(pl.col("estimated"))
    yoy = cpi.with_columns((pl.col("cpi_ir") / pl.col("cpi_ir").shift(12) - 1).alias("yoy")).row(-1, named=True)
    infl = _monthly(con, "infl12_ir_cbi")
    monthly = (p.group_by_dynamic("d", every="1mo")
               .agg(pl.col("usd").last(), pl.col("official").drop_nulls().last(), pl.col("rer").drop_nulls().last()))
    value = {
        "available": True,
        "as_of": last["d"],
        "usd": last["usd"], "official": last["official"],
        "gap": float(np.exp(last["gap"]) - 1) if last["gap"] is not None else None,
        "trend200": last["trend200"], "mom12m": last["mom12m"], "from_high": last["from_high"],
        "rer_index": float(100 * np.exp(last["rer"] - base)) if last["rer"] is not None else None,
        "rer_dev3y": float(np.exp(last["rer_dev3y"]) - 1) if last["rer_dev3y"] is not None else None,
        "real_trend": {"design": real_trend(p, *DESIGN), "test": real_trend(p, *TEST)},
        "infl12": {"value": infl[-1][1], "month": gregorian_to_jalali(infl[-1][0])[:2]} if infl else None,
        "cpi_yoy": {"value": yoy["yoy"], "month": gregorian_to_jalali(yoy["period_end"])[:2],
                    "estimated": yoy["estimated"]},
        "cpi_published_to": gregorian_to_jalali(cpi.filter(~pl.col("estimated"))["period_end"][-1])[:2] if est.height else None,
        "signals": signal_table(p),
        "trend_timing": {"design": trend_timing(p, *DESIGN), "test": trend_timing(p, *TEST)},
        "monthly": [{"m": r["d"].strftime("%Y-%m"), "usd": r["usd"], "official": r["official"],
                     "rer_index": float(100 * np.exp(r["rer"] - base)) if r["rer"] is not None else None}
                    for r in monthly.iter_rows(named=True)],
        "periods": {"design": DESIGN, "test": (TEST[0], last["d"])},
    }
    _cache.update(key=key, value=value)
    return value


def report(con: duckdb.DuckDBPyConnection) -> None:
    sci = _by_jalali_month(_monthly(con, "cpi_ir_sci"))
    cbi = _by_jalali_month(_monthly(con, "infl12_ir_cbi"))
    print(f"IMF/SCI CPI: {len(sci)} months, last {index_month(max(sci))}; CBI inflation: {len(cbi)} months, "
          f"last {index_month(max(cbi))}")
    print("\n=== Splice check: SCI cut at earlier months, extended with CBI inflation, vs what SCI published ===")
    for h, a in splice_accuracy(sci, cbi).items():
        print(f"  {h:>2} months after the cut: {a['cuts']} cut points, bias {a['bias']:+.1%}, RMS error {a['rmse']:.1%}")
    cpi = iran_cpi(con).with_columns((pl.col("cpi_ir") / pl.col("cpi_ir").shift(12) - 1).alias("yoy"))
    print("\n=== Iran CPI, last 8 months (est = extended from CBI inflation) ===")
    for r in cpi.tail(8).iter_rows(named=True):
        jy, jm, _ = gregorian_to_jalali(r["period_end"])
        print(f"  {jy}/{jm:02d}  CPI {r['cpi_ir']:8.1f}  y/y {r['yoy']:+.1%}  {'est' if r['estimated'] else ''}")

    p = add_features(load_panel(con)).filter(pl.col("d") >= DESIGN[0])
    print(f"\nPanel: {p['d'][0]} .. {p['d'][-1]}, {p.height} days with a free-market price")
    print(f"Design period {DESIGN[0]} .. {DESIGN[1]}; test period {TEST[0]} .. {p['d'][-1]}")
    for h in (20, 60, 120):
        print(f"\n=== Terciles (design-period cut points) -> free USD over the next {h} TGJU days ===")
        print(f"  {'signal':<12}{'story':>6} | {'design low':>10}{'high':>8}{'spread':>8}{'t':>6} | "
              f"{'test low':>9}{'high':>8}{'spread':>8}{'t':>6}  verdict")
        for f, (_, sign) in SIGNALS.items():
            t = tercile_test(p, f, h)
            a, b = t.get("design"), t.get("test")
            if not a or not b:
                print(f"  {f:<12} not enough data")
                continue
            print(f"  {f:<12}{'+' if sign > 0 else '-':>6} | {a['low']:>+10.1%}{a['high']:>+8.1%}{a['spread']:>+8.1%}{a['t']:>+6.1f} | "
                  f"{b['low']:>+9.1%}{b['high']:>+8.1%}{b['spread']:>+8.1%}{b['t']:>+6.1f}  {globalgold.verdict(t)}")

    print(f"\n=== Trend timing: dollars above the 200-day average, else a {DEPOSIT_RATE:.0%} rial deposit "
          "(0.5% per switch), in rial ===")
    for name, (a, b) in (("design", DESIGN), ("test", TEST)):
        r = trend_timing(p, a, b)
        for k in ("hold", "trend", "deposit"):
            m = r[k]
            print(f"  {name:<7}{k:<8} CAGR {m['cagr']:+.1%}  MaxDD {m['maxdd']:+.1%}  in USD {m['in_usd']:.0%}  "
                  f"switches {m['switches']}")

    base = p.filter(pl.col("d") <= DESIGN[1])["rer"].drop_nulls().mean()
    print("\n=== Real rate (free USD x US CPI / Iran CPI), yearly average, 2012-2018 average = 100 ===")
    yearly = p.group_by_dynamic("d", every="1y").agg(pl.col("rer").mean(), pl.col("usd").mean())
    for r in yearly.iter_rows(named=True):
        print(f"  {r['d'].year}  index {100 * np.exp(r['rer'] - base):5.0f}   free USD {r['usd']:>12,.0f}")
    for name, (a, b) in (("design", DESIGN), ("test", TEST)):
        t = real_trend(p, a, b)
        print(f"  {name:<7} real rate {t['per_year']:+.1%} a year (log-linear fit), {t['total']:+.0%} first to last day")

    last = p.row(-1, named=True)
    print(f"\n=== Today ({last['d']}) ===")
    print(f"  free {last['usd']:,.0f}  official {last['official']:,.0f}  gap {np.exp(last['gap']) - 1:+.0%}")
    print(f"  real index {100 * np.exp(last['rer'] - base):.0f}; vs its 3-year average {np.exp(last['rer_dev3y']) - 1:+.0%}")


def main() -> int:
    import argparse
    import sys
    import datastore
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Free-market USD: out-of-sample report")
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
