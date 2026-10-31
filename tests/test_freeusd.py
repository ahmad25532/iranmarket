import os
import sys
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datastore  # noqa: E402
import freeusd as F  # noqa: E402


# ---------- Jalali calendar (pairs taken from TGJU rows) ----------

@pytest.mark.parametrize("j, g", [
    ((1405, 8, 9), date(2026, 10, 31)), ((1404, 10, 18), date(2026, 1, 8)),
    ((1397, 8, 24), date(2018, 11, 15)), ((1403, 12, 30), date(2025, 3, 20)), ((1404, 1, 1), date(2025, 3, 21)),
])
def test_jalali_round_trip(j, g):
    assert F.jalali_to_gregorian(*j) == g
    assert F.gregorian_to_jalali(g) == j


def test_jalali_month_end():
    assert F.jalali_month_end(1383, 1) == date(2004, 4, 19)
    assert F.jalali_month_end(1403, 12) == date(2025, 3, 20)          # leap Esfand has 30 days
    assert F.jalali_month_end(1405, 6) == date(2026, 9, 22)


# ---------- parsers (fixtures trimmed from real responses) ----------

CBI_PAGE = """<select name="ctl00$ucBody$ucContent$ctl00$ddlYear">
<option value="1404">۱۴۰۴</option><option selected="selected" value="1405">۱۴۰۵</option></select>
<h4>نرخ تورم منتهی به ماه</h4><table><tr><td>ماه</td><td>نرخ تورم</td></tr>
<tr><td>شهریور</td><td class="DirLtr">۶۸.۴</td></tr><tr><td>مرداد</td><td>۶۵.۱</td></tr>
<tr><td>فروردین</td><td>۵۰.۶</td></tr></table><h4>نرخ تورم سالانه</h4><td>۱۴۰۴</td><td>۴۸.۳</td>"""


def test_parse_cbi_inflation():
    rows = {r["period_end"]: r for r in F.parse_cbi_inflation(CBI_PAGE)}
    assert len(rows) == 3                                               # the annual table is ignored
    r = rows[date(2026, 9, 22)]                                         # Shahrivar 1405
    assert r["value"] == 68.4 and r["series"] == "infl12_ir_cbi"
    assert r["released_d"] == date(2026, 9, 22) + timedelta(days=35)
    assert rows[date(2026, 4, 20)]["value"] == 50.6                     # Farvardin 1405


def test_parse_cbi_inflation_rejects_a_changed_layout():
    with pytest.raises(ValueError):
        F.parse_cbi_inflation("<html>maintenance</html>")


def test_parse_imf_cpi_maps_to_jalali_months():
    rows = F.parse_imf_cpi({"period": ["2025-03", "2025-04", "2025-05"], "value": [315.7, "NA", 336.9]})
    assert [r["period_end"] for r in rows] == [date(2025, 3, 20), date(2025, 5, 21)]   # Esfand 1403, Ordibehesht 1404
    assert rows[0]["released_d"] == date(2025, 4, 4)


def test_parse_bls_cpi_skips_missing_and_annual():
    text = ("series_id        \tyear\tperiod\t       value\tfootnote_codes\n"
            "CUUR0000SA0      \t2025\tM09\t     324.80\t\n"
            "CUUR0000SA0      \t2025\tM10\t          -\t\n"
            "CUUR0000SA0      \t2025\tM12\t     326.03\t\n"
            "CUUR0000SA0      \t2025\tM13\t     322.10\t\n"
            "CUSR0000SA0      \t2025\tM09\t     324.00\t\n")
    rows = F.parse_bls_cpi(text)
    assert [(r["period_end"], r["value"]) for r in rows] == [(date(2025, 9, 30), 324.80), (date(2025, 12, 31), 326.03)]


# ---------- CPI splice ----------

def _steady(n_months: int, monthly: float, start: int = F.month_index(1390, 1)) -> dict:
    return {start + i: 100 * (1 + monthly) ** i for i in range(n_months)}


def test_splice_is_exact_under_steady_inflation():
    m = 0.03
    full = _steady(60, m)
    sci = {n: v for n, v in full.items() if n < min(full) + 40}
    rate = (1 + m) ** 12 - 1                                            # 12-month-average rate = point rate here
    cbi = {n: rate * 100 for n in full}
    out = F.splice_cpi(sci, cbi)
    for n in range(min(full) + 40, min(full) + 60):
        v, est = out[n]
        assert est and v == pytest.approx(full[n], rel=1e-9)


def test_splice_rescales_the_other_agencys_rate():
    full = _steady(60, 0.03)
    sci = {n: v for n, v in full.items() if n < min(full) + 40}
    cbi = {n: ((1.03 ** 12) * 1.04 - 1) * 100 for n in full}            # CBI reads 4% higher throughout
    v, _ = F.splice_cpi(sci, cbi)[min(full) + 50]
    assert v == pytest.approx(full[min(full) + 50], rel=1e-9)


def test_splice_needs_overlap():
    sci = _steady(30, 0.02)
    assert all(not est for _, est in F.splice_cpi(sci, {}).values())


# ---------- panel: nothing is used before it is published ----------

@pytest.fixture
def con():
    c = datastore.connect(":memory:")
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(500)]
    datastore.upsert(c, "macro_daily", [{"series": "usd_irr", "d": d, "close": 1000.0 + i, "source": "history"}
                                        for i, d in enumerate(days)])
    datastore.upsert(c, "macro_daily", [{"series": "usd_nima_irr", "d": d, "close": 800.0, "source": "history"}
                                        for d in days[:300]])
    sci, rows = _steady(40, 0.02, F.month_index(1401, 1)), []
    for n, v in sci.items():
        end = F.jalali_month_end(*F.index_month(n))
        rows.append({"series": "cpi_ir_sci", "period_end": end, "value": v, "released_d": end + timedelta(days=15)})
    for y in range(2022, 2027):
        for m in range(1, 13):
            end = date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)
            rows.append({"series": "cpi_us", "period_end": end, "value": 300.0, "released_d": end + timedelta(days=20)})
    datastore.upsert(c, "monthly_macro", rows)
    yield c
    c.close()


def test_cpi_used_only_after_release(con):
    p = F.load_panel(con)
    cpi = F.iran_cpi(con)
    for r in p.filter(pl.col("cpi_ir").is_not_null()).iter_rows(named=True):
        published = cpi.filter(pl.col("released_d") <= r["d"])
        assert r["cpi_ir"] == published["cpi_ir"][-1]


def test_official_rate_goes_stale_after_ten_days(con):
    p = F.load_panel(con)
    by_day = dict(zip(p["d"].to_list(), p["official"].to_list()))
    last_nima = date(2025, 1, 1) + timedelta(days=299)
    assert by_day[last_nima + timedelta(days=10)] == 800.0
    assert by_day[last_nima + timedelta(days=11)] is None


def test_real_trend_recovers_slope():
    days = [date(2012, 1, 1) + timedelta(days=i) for i in range(0, 3 * 365, 1)]
    t = np.array([(d - days[0]).days / 365.25 for d in days])
    p = pl.DataFrame({"d": days, "rer": 12 + np.log(1.05) * t})
    assert F.real_trend(p, days[0], days[-1])["per_year"] == pytest.approx(0.05, abs=1e-6)
