import os
import sys
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402
import datastore  # noqa: E402
import duckdb  # noqa: E402
import quality  # noqa: E402
import treasury as T  # noqa: E402


# ---------- bill recognition and maturity ----------

@pytest.mark.parametrize("ticker, expected", [
    ("اخزا405", True), (" اخزا202 ", True),
    ("اخزا4054", False), ("اخزا2022", False),   # block-trade boards
    ("اخزا", False), ("فملي", False), ("", False),
])
def test_is_bill(ticker, expected):
    assert T.is_bill(ticker) is expected


@pytest.mark.parametrize("name, expected", [
    ("اسناد خزانه-م5بودجه04-070420", date(2028, 7, 10)),     # 1407/04/20
    ("اسنادخزانه-م3بودجه02-050818", date(2026, 11, 9)),      # 1405/08/18
    ("اسناد خزانه-م1-س.قوا03-060615 ", date(2027, 9, 6)),    # 1406/06/15
])
def test_parse_maturity(name, expected):
    assert T.parse_maturity(name) == expected


@pytest.mark.parametrize("name", ["", None, "بدون تاریخ", "اسناد خزانه-071332", "اسناد خزانه-070731"])
def test_parse_maturity_rejects_invalid(name):
    assert T.parse_maturity(name) is None


# ---------- yield ----------

def test_ytm_one_year_is_simple_return():
    assert T.ytm(800_000, 365) == pytest.approx(0.25)


def test_ytm_compounds_over_two_years():
    assert T.ytm(640_000, 730) == pytest.approx(0.25)


def test_ytm_at_par_is_zero_and_after_maturity_is_none():
    assert T.ytm(1_000_000, 10) == pytest.approx(0.0)
    assert T.ytm(990_000, 0) is None
    assert T.ytm(990_000, -3) is None
    assert T.ytm(0, 100) is None


def raw(deven, close, trades=5, value=1e9):
    return {"insCode": "b1", "dEven": deven, "pClosing": close, "pDrCotVal": close, "priceFirst": close, "priceMax": close,
            "priceMin": close, "priceYesterday": close, "qTotTran5J": 10, "qTotCap": value, "zTotTran": trades}


def test_bill_rows_keep_traded_days_only():
    rows = T.bill_rows("b1", date(2027, 11, 4), [
        raw(20261104, 800_000),
        raw(20261103, 790_000, trades=0, value=0),   # TSETMC repeats the last price on an untraded day
        raw(20261102, 0),
    ])
    assert [r["d"] for r in rows] == [date(2026, 11, 4)]
    assert rows[0]["days_left"] == 365
    assert rows[0]["ytm"] == pytest.approx(0.25)


def test_bill_rows_drop_a_session_still_open():
    rows = T.bill_rows("b1", date(2027, 11, 4), [raw(20261104, 800_000), raw(20261103, 790_000)], before=date(2026, 11, 4))
    assert [r["d"] for r in rows] == [date(2026, 11, 3)]


# ---------- daily series ----------

D = date(2026, 11, 4)


def bill(code, days_left, y, value=1e9, d=D):
    return {"ins_code": code, "d": d, "maturity": d + timedelta(days=days_left), "days_left": days_left,
            "close": 1e6 / (1 + y) ** (days_left / 365), "value": value, "trades": 5, "ytm": y}


@pytest.fixture
def con():
    c = datastore.connect(":memory:")
    yield c
    c.close()


def test_daily_median_and_short_end(con):
    datastore.upsert(con, "bill_yields", [
        bill("a", 100, 0.30), bill("b", 300, 0.34), bill("c", 500, 0.38), bill("d", 700, 0.40), bill("e", 900, 0.42),
    ])
    assert T.build_daily(con) == 1
    med, short, n, n_short, days = con.execute(
        "SELECT ytm_median, ytm_short, n_bills, n_short, median_days FROM bill_yield_daily").fetchone()
    assert med == pytest.approx(0.38)
    assert short == pytest.approx(0.32)
    assert (n, n_short, days) == (5, 2, 500)


def test_daily_excludes_near_maturity_token_trades_and_bad_prints(con):
    datastore.upsert(con, "bill_yields", [
        bill("good", 300, 0.35),
        bill("near", 10, 0.90),                 # too close to maturity
        bill("token", 300, 0.90, value=1e6),    # token trade
        bill("wild", 300, 2.50),                # above the plausible range
        bill("above_par", 300, -0.01),          # traded above redemption value
    ])
    T.build_daily(con)
    med, n = con.execute("SELECT ytm_median, n_bills FROM bill_yield_daily").fetchone()
    assert med == pytest.approx(0.35)
    assert n == 1


def test_daily_median_resists_one_outlier(con):
    datastore.upsert(con, "bill_yields", [bill("a", 300, 0.35), bill("b", 400, 0.36), bill("c", 500, 1.80)])
    T.build_daily(con)
    assert con.execute("SELECT ytm_median FROM bill_yield_daily").fetchone()[0] == pytest.approx(0.36)


def test_build_daily_removes_days_no_longer_usable(con):
    datastore.upsert(con, "bill_yields", [bill("a", 300, 0.35)])
    T.build_daily(con)
    con.execute("DELETE FROM bill_yields")
    assert T.build_daily(con) == 0


# ---------- refresh ----------

class FakeRun:
    def __init__(self, c, history):
        self.con, self.http, self.history = c, None, history

    def fetch_many(self, source, items):
        return {code: self.history[code] for code, _ in items if code in self.history}


def add_instruments(c):
    datastore.upsert(c, "instruments", [
        {"ins_code": "live", "ticker": "اخزا501", "name": "اسنادخزانه-م1بودجه05-991230"},
        {"ins_code": "block", "ticker": "اخزا5012", "name": "اسنادخزانه-م1بودجه05-991230"},
        {"ins_code": "matured", "ticker": "اخزا101", "name": "اسناد خزانه-م1بودجه01-010101"},
        {"ins_code": "noname", "ticker": "اخزا999", "name": "اسناد خزانه"},
        {"ins_code": "stock", "ticker": "فملي", "name": "ملی صنایع مس"},
    ])


def test_listed_bills_skips_block_boards_and_matured(con):
    add_instruments(con)
    bills, unparsed = T.listed_bills(con, date(2026, 11, 7))
    assert [b[0] for b in bills] == ["live"]
    assert unparsed == ["اخزا999"]


def test_refresh_stores_yields_and_keeps_matured_rows(con):
    add_instruments(con)
    datastore.upsert(con, "bill_yields", [bill("matured", 40, 0.30, d=date(2022, 1, 1))])
    out = T.refresh(FakeRun(con, {"live": [raw(20261104, 500_000)]}))
    assert out == {"bills": 1, "fetched": 1, "rows": 1, "days": 2, "unparsed": 1}
    codes = {r[0] for r in con.execute("SELECT ins_code FROM bill_yields").fetchall()}
    assert codes == {"live", "matured"}
    assert con.execute("SELECT count(*) FROM daily_prices").fetchone()[0] == 0   # the stock universe is untouched


def test_refresh_is_idempotent(con):
    add_instruments(con)
    run = FakeRun(con, {"live": [raw(20261104, 500_000), raw(20261103, 495_000)]})
    T.refresh(run)
    T.refresh(run)
    assert con.execute("SELECT count(*) FROM bill_yields").fetchone()[0] == 2


def test_refresh_survives_a_failed_fetch(con):
    add_instruments(con)
    assert T.refresh(FakeRun(con, {}))["fetched"] == 0


# ---------- comovement ----------

def trading_days(n, start=date(2024, 1, 6)):
    return [start + timedelta(days=i) for i in range(n)]


def test_block_changes_follow_the_same_bills():
    days = trading_days(10)
    cal = pl.DataFrame({"d": days, "ted": [100.0] * 5 + [110.0] * 5})
    # 'a' rises 2 points; 'b' only appears in the second block, at a much higher yield.
    bills = pl.DataFrame({
        "ins_code": ["a"] * 10 + ["b"] * 5,
        "d": days + days[5:],
        "ytm": [0.30] * 5 + [0.32] * 5 + [0.90] * 5,
    })
    out = T.block_changes(cal, bills, 5)
    assert out.height == 1
    assert out["dy"][0] == pytest.approx(0.02)     # a difference of daily medians would give about 0.31
    assert out["n_bills"][0] == 1
    assert out["r"][0] == pytest.approx(np.log(1.1))


def test_block_changes_ignore_a_partial_last_block():
    days = trading_days(12)
    cal = pl.DataFrame({"d": days, "ted": [100.0 + i for i in range(12)]})
    bills = pl.DataFrame({"ins_code": ["a"] * 12, "d": days, "ytm": [0.3 + i / 100 for i in range(12)]})
    assert T.block_changes(cal, bills, 5)["end"].to_list() == [days[9]]


def test_bootstrap_interval_contains_a_strong_correlation():
    rng = np.random.default_rng(1)
    x = rng.normal(size=200)
    y = -x + rng.normal(scale=0.3, size=200)
    lo, hi = T.block_bootstrap_ci(x, y)
    assert lo < np.corrcoef(x, y)[0, 1] < hi
    assert hi < -0.8


def test_comovement_recovers_a_built_in_negative_link(con):
    rng = np.random.default_rng(3)
    days = trading_days(400)
    dy = rng.normal(scale=0.002, size=400)
    ret = -3 * dy + rng.normal(scale=0.001, size=400)
    y = 0.35 + np.cumsum(dy)
    ted = 1000 * np.exp(np.cumsum(ret))
    datastore.upsert(con, "index_daily", [{"index_code": T.TEDPIX, "d": d, "close": float(v)} for d, v in zip(days, ted)])
    rows = []
    for code, offset in (("a", 0.0), ("b", 0.01), ("c", 0.02)):
        rows += [bill(code, 900, float(v) + offset, d=d) for d, v in zip(days, y)]
    datastore.upsert(con, "bill_yields", rows)
    T.build_daily(con)
    out = T.comovement(con)
    assert out["weekly"]["r"] < -0.8
    assert out["monthly"]["r"] < -0.8
    assert abs(next(s for s in out["lags"] if s["k"] == 2)["r"]) < 0.4
    assert len(out["lags"]) == 9
    assert sum(t["n"] for t in out["terciles"]) == out["monthly"]["n"] - 1


# ---------- dashboard and quality ----------

def test_dashboard_needs_at_least_three_bills(con):
    assert T.dashboard(con) == {"available": False}
    datastore.upsert(con, "bill_yields", [bill("a", 300, 0.30), bill("b", 400, 0.35)])
    T.build_daily(con)
    assert T.dashboard(con) == {"available": False}


def test_dashboard_values(con):
    datastore.upsert(con, "bill_yields", [
        bill("a", 100, 0.30), bill("b", 200, 0.32), bill("c", 300, 0.34), bill("d", 700, 0.40),
        bill("a", 101, 0.20, d=D - timedelta(days=1)), bill("b", 201, 0.20, d=D - timedelta(days=1)), bill("c", 301, 0.20, d=D - timedelta(days=1)),
    ])
    T.build_daily(con)
    out = T.dashboard(con)
    assert out["available"] and out["as_of"] == D and out["n_bills"] == 4
    assert out["ytm_median"] == pytest.approx(0.33)
    assert out["ytm_short"] == pytest.approx(0.32)
    assert out["monthly"] == [{"m": "2026-11", "median": pytest.approx(33.0), "short": pytest.approx(32.0)}]


def test_dashboard_hides_a_thin_short_end(con):
    datastore.upsert(con, "bill_yields", [bill("a", 100, 0.30), bill("b", 500, 0.32), bill("c", 600, 0.34)])
    T.build_daily(con)
    out = T.dashboard(con)
    assert out["ytm_short"] is None and out["monthly"][0]["short"] is None


def test_dashboard_works_on_a_database_from_before_the_table(tmp_path):
    # The dashboard opens read-only, so the schema is not applied until the collector has run once.
    path = str(tmp_path / "old.duckdb")
    c = duckdb.connect(path)
    c.execute("CREATE TABLE instruments (ins_code VARCHAR)")
    c.close()
    ro = dashboard._connect(path)
    try:
        assert T.dashboard(ro) == {"available": False}
    finally:
        ro.close()


def test_quality_check_warns_when_empty_stale_or_unparsed(con):
    today = date(2026, 11, 7)
    assert quality.check_bill_yields(con, today, 0)[1] == "warn"
    datastore.upsert(con, "bill_yields", [bill("a", 300, 0.35)])
    T.build_daily(con)
    assert quality.check_bill_yields(con, today, 0)[1] == "ok"
    assert quality.check_bill_yields(con, today, 2)[1] == "warn"
    assert quality.check_bill_yields(con, date(2026, 11, 30), 0)[1] == "warn"
