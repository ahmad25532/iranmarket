import io
import os
import sys
import zipfile
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datastore  # noqa: E402
import globalgold as G  # noqa: E402


# ---------- parsers (fixtures copied from real responses) ----------

def test_parse_yahoo_uses_exchange_date_and_skips_gaps():
    # GC=F: gmtoffset -14400 (New York, summer); a bar at 04:00 UTC is still the same NY day.
    res = {"meta": {"gmtoffset": -14400}, "timestamp": [1761796800, 1761883200, 1761969600],
           "indicators": {"quote": [{"close": [4012.5, None, 3999.1]}]}}
    rows = G.parse_yahoo(res, "gold_fut")
    assert [r["d"] for r in rows] == [date(2025, 10, 30), date(2025, 11, 1)]
    assert rows[0] == {"series": "gold_fut", "d": date(2025, 10, 30), "close": 4012.5}


def test_parse_real_yield_csv_keeps_10y_only():
    text = 'Date,"5 YR","7 YR","10 YR","20 YR","30 YR"\n10/30/2026,2.25,2.42,2.60,2.92,3.08\n10/29/2026,2.31,2.46,,2.92,3.07\n'
    assert G.parse_real_yield_csv(text) == [{"series": "real10", "d": date(2026, 10, 30), "close": 2.60}]


COT_ROW = {
    "Market_and_Exchange_Names": "GOLD - COMMODITY EXCHANGE INC.", "Report_Date_as_YYYY-MM-DD": "2026-10-27",
    "CFTC_Contract_Market_Code": "088691", "Open_Interest_All": "  396810",
    "M_Money_Positions_Long_All": "  122508", "M_Money_Positions_Short_All": "   16946",
    "Prod_Merc_Positions_Long_All": "   20487", "Prod_Merc_Positions_Short_All": "   30061",
    "Swap_Positions_Long_All": "   20230", "Swap__Positions_Short_All": "  226280",
}


def test_parse_cot_row_gold_only_and_release_lag():
    r = G.parse_cot_row(COT_ROW)
    assert r["report_d"] == date(2026, 10, 27)                 # Tuesday positions
    assert r["released_d"] == date(2026, 11, 2)                # usable from the next Monday
    assert r["mm_long"] == 122508 and r["sw_short"] == 226280
    assert G.parse_cot_row({**COT_ROW, "CFTC_Contract_Market_Code": "084691"}) is None   # silver


def test_parse_cot_zip():
    buf = io.BytesIO()
    header = list(COT_ROW)
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("f_year.txt", ",".join(header) + "\n" + ",".join(f'"{COT_ROW[k]}"' for k in header) + "\n")
    assert len(G.parse_cot_zip(buf.getvalue())) == 1


# ---------- panel: no look-ahead ----------

@pytest.fixture
def con():
    c = datastore.connect(":memory:")
    days = [date(2026, 10, 19) + timedelta(days=i) for i in range(16) if (date(2026, 10, 19) + timedelta(days=i)).weekday() < 5]
    datastore.upsert(c, "global_daily", [{"series": s, "d": d, "close": v}
                                         for d in days for s, v in (("dxy", 100.0), ("real10", 2.0), ("gold_fut", 4000.0))])
    datastore.upsert(c, "macro_daily", [{"series": "gold_ounce_usd", "d": d, "close": 3990.0, "source": "history"} for d in days])
    datastore.upsert(c, "cot_gold", [G.parse_cot_row(COT_ROW)])
    yield c
    c.close()


def test_cot_used_only_after_release(con):
    p = G.load_panel(con)
    by_day = dict(zip(p["d"].to_list(), p["mm_net"].to_list()))
    assert by_day[date(2026, 10, 30)] is None                  # Friday after the Tuesday report: not yet
    assert by_day[date(2026, 11, 2)] == pytest.approx((122508 - 16946) / 396810)


def test_spot_ounce_preferred_over_future(con):
    assert G.load_panel(con)["gold"][0] == 3990.0


def test_fair_value_resid_ignores_the_future():
    rng = np.random.default_rng(0)
    n = 900
    real, dxy = rng.normal(0, 1, n).cumsum() * 0.05, 100 + rng.normal(0, 1, n).cumsum() * 0.3
    gold = 7.5 - 0.1 * real + rng.normal(0, 0.02, n)
    a = G.fair_value_resid(gold, real, np.log(dxy), window=300)
    gold2 = gold.copy()
    gold2[800:] += 1.0                                          # change only the future
    b = G.fair_value_resid(gold2, real, np.log(dxy), window=300)
    np.testing.assert_allclose(a[:800], b[:800], equal_nan=True)


def test_tercile_cut_points_come_from_design_period_only():
    days = pl.date_range(date(2006, 7, 3), date(2024, 12, 31), "1d", eager=True)
    n = len(days)
    x = np.sin(np.arange(n) / 50.0)
    p = pl.DataFrame({"d": days, "f": x, "fwd60": x * 0.01})
    t1 = G.tercile_test(p, "f", 60)
    p2 = p.with_columns(pl.when(pl.col("d") >= date(2017, 1, 1)).then(pl.col("f") + 5).otherwise(pl.col("f")).alias("f"))
    t2 = G.tercile_test(p2, "f", 60)
    assert t1["cuts"] == t2["cuts"]
    assert t1["design"]["spread"] > 0 and G.verdict(t1) == "validated"


def test_verdict_rules():
    mk = lambda s, t: {"spread": s, "t": t}
    assert G.verdict({"design": mk(0.02, 1.5), "test": mk(0.03, 2.5)}) == "validated"
    assert G.verdict({"design": mk(0.02, 1.5), "test": mk(0.03, 1.0)}) == "weak"
    assert G.verdict({"design": mk(0.02, 1.5), "test": mk(-0.03, -2.5)}) == "failed"
    assert G.verdict({"design": mk(0.02, 1.5), "test": mk(0.003, 3.0)}) == "failed"
    assert G.verdict({"design": None, "test": None}) == "untested"


# ---------- Iranian gold decomposition ----------

def _iran_panel(n_months=24):
    d0 = date(2024, 1, 15)
    rng = np.random.default_rng(1)
    rows, o, u, b = [], 2000.0, 500_000.0, 1.02
    for i in range(n_months * 30):
        o *= 1 + rng.normal(0, 0.008)
        u *= 1 + rng.normal(0.001, 0.012)
        b = 1 + 0.5 * (b - 1) + rng.normal(0, 0.005)
        rows.append({"d": d0 + timedelta(days=i), "ounce": o, "usd": u, "bubble_factor": b,
                     "g18": o * u / G.TROY_OUNCE_G * 0.75 * b})
    return pl.DataFrame(rows)


def test_decompose_is_exact():
    d = G.decompose(_iran_panel(), 365)
    assert (1 + d["total"]) == pytest.approx((1 + d["ounce"]) * (1 + d["usd"]) * (1 + d["bubble"]))
    assert sum(d["share"].values()) == pytest.approx(1.0)


def test_variance_attribution_sums_to_one():
    v = G.variance_attribution(_iran_panel(), date(2024, 1, 1))
    assert v["ounce"] + v["usd"] + v["bubble"] == pytest.approx(1.0)
    assert v["usd"] > v["ounce"]                                # USD was simulated with the larger moves
