import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adjust  # noqa: E402
import advisor  # noqa: E402
import dashboard  # noqa: E402
import datastore  # noqa: E402
import stockpick  # noqa: E402

BAND = 20.0
M = 10_000_000.0   # one million toman, in rial
TODAY = date(2026, 11, 10)


def valuation(target=None, items=None, **values):
    """A dashboard valuation with the given class values (rial) and target percentages."""
    target = target or {"stocks": 50, "gold": 50}
    return {
        "items": items or [],
        "allocation": [{"key": k, "label": k, "value": float(values.get(k, 0.0)), "target_pct": float(target.get(k, 0))}
                       for k in dashboard.CLASS_LABELS],
    }


def steps(p):
    return {(s["verb"], s["key"]): s["toman"] for s in p["steps"]}


# ---------- the verdict ----------

def test_nothing_entered_asks_for_the_assets_first():
    p = advisor.plan(valuation(), BAND)
    assert p["status"] == "start" and not p["steps"]


def test_idle_cash_is_split_by_the_target_and_nothing_is_sold():
    p = advisor.plan(valuation(cash=200 * M), BAND)
    assert p["status"] == "invest"
    assert steps(p) == {("buy", "stocks"): 100_000_000, ("buy", "gold"): 100_000_000}


def test_cash_goes_only_to_the_classes_that_are_behind():
    # 100 gold and 100 cash: the whole plan is 200, so stocks need 100 and gold needs nothing.
    p = advisor.plan(valuation(gold=100 * M, cash=100 * M), BAND)
    assert p["status"] == "invest"
    assert steps(p) == {("buy", "stocks"): 100_000_000}


def test_a_drift_inside_the_band_means_do_nothing():
    p = advisor.plan(valuation(stocks=65 * M, gold=35 * M), BAND)
    assert p["status"] == "hold" and not p["steps"]
    assert p["max_gap"] == pytest.approx(15.0)


def test_a_drift_of_the_whole_band_means_rebalance_back_to_the_target():
    p = advisor.plan(valuation(stocks=30 * M, gold=70 * M), BAND)
    assert p["status"] == "rebalance"
    assert steps(p) == {("sell", "gold"): 20_000_000, ("buy", "stocks"): 20_000_000}
    assert [s["verb"] for s in p["steps"]] == ["sell", "buy"]     # the sale pays for the purchase


def test_cash_is_spent_before_the_band_is_judged():
    # 20 stocks, 80 gold alone would be 30 points off; 60 of new cash brings stocks to 80 of 160.
    p = advisor.plan(valuation(stocks=20 * M, gold=80 * M, cash=60 * M), BAND)
    assert p["status"] == "invest"
    assert steps(p) == {("buy", "stocks"): 60_000_000}


def test_small_change_is_not_worth_a_trade():
    p = advisor.plan(valuation(stocks=500 * M, gold=500 * M, cash=2 * M), BAND)
    assert p["status"] == "hold"


def test_money_needed_soon_stays_in_fixed_income():
    p = advisor.plan(valuation(target={"stocks": 37.5, "gold": 37.5, "fixed": 25}, cash=400 * M), BAND)
    assert steps(p) == {("buy", "stocks"): 150_000_000, ("buy", "gold"): 150_000_000, ("buy", "fixed"): 100_000_000}


def test_dollars_are_left_out_of_the_plan_instead_of_being_called_a_mistake():
    p = advisor.plan(valuation(stocks=50 * M, gold=50 * M, other=300 * M), BAND)
    assert p["status"] == "hold"
    assert p["base"] == 100 * M and p["outside"] == 300 * M
    assert any("دارایی دیگر" in w for w in p["warnings"])


def test_a_target_for_other_assets_brings_them_into_the_plan():
    p = advisor.plan(valuation(target={"stocks": 25, "gold": 25, "other": 50}, stocks=50 * M, gold=50 * M, other=100 * M), BAND)
    assert p["status"] == "hold" and p["outside"] == 0 and p["base"] == 200 * M


def test_a_holding_without_a_price_blocks_any_advice():
    p = advisor.plan(valuation(cash=100 * M, items=[{"ticker": "فلان", "qty": 1, "error": "قیمت پیدا نشد"}]), BAND)
    assert p["status"] == "blocked" and not p["steps"] and "فلان" in p["why"]


def test_old_data_blocks_any_advice():
    data = advisor.data_status({"last_market_date": TODAY - timedelta(days=12), "worst": "ok"}, TODAY)
    assert data["level"] == "stop" and not data["usable"]
    p = advisor.plan(valuation(cash=100 * M), BAND, data)
    assert p["status"] == "blocked" and not p["steps"]


def test_a_weekend_or_short_holiday_is_not_old_data():
    data = advisor.data_status({"last_market_date": TODAY - timedelta(days=5), "worst": "ok"}, TODAY)
    assert data["level"] == "ok" and data["usable"]
    assert advisor.data_status({"last_market_date": None}, TODAY)["level"] == "stop"
    assert advisor.data_status({"last_market_date": TODAY, "worst": "fail"}, TODAY)["level"] == "warn"


def test_stale_prices_are_named():
    p = advisor.plan(valuation(stocks=50 * M, gold=50 * M, items=[{"ticker": "متوقف", "value": 1.0, "stale": True}]), BAND)
    assert any("متوقف" in w for w in p["warnings"])


# ---------- new money ----------

def test_new_money_into_an_empty_plan_follows_the_target():
    got = {s["key"]: s["toman"] for s in advisor.split_new_money(valuation(), 10 * M)}
    assert got == {"stocks": 5_000_000, "gold": 5_000_000}


def test_new_money_goes_to_the_class_that_is_behind():
    got = {s["key"]: s["toman"] for s in advisor.split_new_money(valuation(stocks=30 * M, gold=70 * M), 10 * M)}
    assert got == {"stocks": 10_000_000}
    assert advisor.split_new_money(valuation(stocks=30 * M, gold=70 * M), 0) == []


# ---------- wording ----------

@pytest.mark.parametrize("rial,text", [
    (164.3 * M, "۱۶۴ میلیون تومان"),
    (36.44 * M, "۳۶٫۴ میلیون تومان"),
    (3_680 * M, "۳٫۶۸ میلیارد تومان"),
    (0.45 * M, "۴۵۰٬۰۰۰ تومان"),
])
def test_amounts_read_like_a_person_would_say_them(rial, text):
    assert advisor.toman_text(rial) == text


# ---------- database-backed parts ----------

def _bars(con, code, rows, isin, name="x"):
    datastore.upsert(con, "instruments", [{"ins_code": code, "ticker": code, "name": name, "isin": isin}])
    datastore.upsert(con, "daily_prices", [
        {"ins_code": code, "d": d, "close": c, "last": c, "open": c, "high": c * 1.01, "low": c * 0.99, "yesterday": y,
         "volume": 1000.0 if n else 0.0, "value": 1000.0 * c if n else 0.0, "trades": n, "source": "history"}
        for d, c, y, n in rows])


def _adj(con, code):
    return con.execute("SELECT adj_close, bad_factor FROM price_adj WHERE ins_code = ? ORDER BY d", [code]).fetchall()


DAYS = [date(2026, 1, 3) + timedelta(days=i) for i in range(6)]


def test_placeholder_before_the_first_trade_is_not_a_price():
    con = datastore.connect(":memory:")
    # Two days at the board's 1000-rial placeholder, then the first real trade at 18000.
    _bars(con, "a", [(DAYS[0], 1000, 1000, 0), (DAYS[1], 1000, 1000, 0), (DAYS[2], 18000, 1000, 50), (DAYS[3], 18900, 18000, 50)], "IRO1AAAA0001")
    adjust.build(con)
    adj = [r[0] for r in _adj(con, "a")]
    assert adj[:2] == [None, None]
    assert adj[3] / adj[2] == pytest.approx(1.05)


def test_a_reset_during_a_halt_is_folded_into_the_reopening_day():
    con = datastore.connect(":memory:")
    # 1:1 bonus issue while halted: the board resets 2000 to 1000 on a day nobody trades,
    # then the share reopens at 1100. The holder made 10%, not a 45% loss.
    _bars(con, "a", [(DAYS[0], 2000, 2000, 50), (DAYS[1], 1000, 2000, 0), (DAYS[2], 1000, 1000, 0), (DAYS[3], 1100, 1000, 50)], "IRO1AAAA0001")
    adjust.build(con)
    adj = [r[0] for r in _adj(con, "a")]
    assert adj[1] == adj[0] and adj[2] == adj[0]          # no trades: the last traded value is carried
    assert adj[3] / adj[0] == pytest.approx(1.10)


def test_an_eighty_fold_fund_unit_split_is_not_a_crash():
    con = datastore.connect(":memory:")
    _bars(con, "f", [(DAYS[0], 1_140_000, 1_140_000, 50), (DAYS[1], 14_400, 14_250, 50)], "IRTKKIAN0001")
    adjust.build(con)
    (a0, bad0), (a1, bad1) = _adj(con, "f")
    assert not bad1 and a1 / a0 == pytest.approx(14_400 / 14_250)


def test_a_one_rial_reference_price_is_still_treated_as_an_error():
    con = datastore.connect(":memory:")
    _bars(con, "a", [(DAYS[0], 11_490, 11_490, 50), (DAYS[1], 206_879, 1, 50)], "IRO7AAAP0001")
    adjust.build(con)
    assert _adj(con, "a")[1][1] is True


def test_adjusted_prices_agree_with_the_stock_selection_chain():
    con = datastore.connect(":memory:")
    rows = [(DAYS[0], 1000, 1000, 0), (DAYS[1], 5000, 1000, 10), (DAYS[2], 5200, 5000, 10),
            (DAYS[3], 2600, 5200, 0), (DAYS[4], 2700, 2600, 10), (DAYS[5], 2500, 2450, 10)]
    _bars(con, "a", rows, "IRO1AAAA0001")
    adjust.build(con)
    raw = con.execute("SELECT d, close, yesterday, value, volume, trades FROM daily_prices ORDER BY d").fetchall()
    levels = {d: level for d, level, _ in stockpick.chain_levels(raw)}
    adj = dict(con.execute("SELECT d, adj_close FROM price_adj WHERE adj_close IS NOT NULL").fetchall())
    assert set(adj) == set(levels)
    for d in levels:
        assert adj[d] / adj[DAYS[5]] == pytest.approx(levels[d] / levels[DAYS[5]])


@pytest.mark.parametrize("isin,name,expected", [
    ("IRT3ABIF0001", "ص.س.درآمد ثابت آبي هامون-د", "etf_fixed_income"),
    ("IRT1YAGH0001", "صندوق س ياقوت آگاه-ثابت", "etf_fixed_income"),
    ("IRT3PYDF0001", "ص.س.درآمد ثابت رايا پايدار-د", "etf_fixed_income"),
    ("IRTKNAHL0001", "صندوق س.كالاي نوويرا", "etf_commodity"),
    ("IRT3ARTF0001", "صندوق س. آرتان-س", "etf_equity"),
    ("IRT1DARN0001", "صندوق س صنايع مفيد5-بخشي", "etf_equity"),
])
def test_funds_missing_from_fipiran_are_classified_by_name_and_isin(isin, name, expected):
    con = datastore.connect(":memory:")
    datastore.upsert(con, "instruments", [{"ins_code": "x", "ticker": "x", "name": name, "isin": isin}])
    adjust.build(con)
    assert con.execute("SELECT asset_class FROM instrument_class").fetchone()[0] == expected


def test_fipiran_type_wins_over_the_name():
    con = datastore.connect(":memory:")
    datastore.upsert(con, "instruments", [{"ins_code": "x", "ticker": "x", "name": "صندوق عجيب-د", "isin": "IRT1XXXX0001"}])
    datastore.upsert(con, "fipiran_funds", [{"ins_code": "x", "reg_no": "1", "name": "x", "fund_type": 6}])
    adjust.build(con)
    assert con.execute("SELECT asset_class FROM instrument_class").fetchone()[0] == "etf_equity"


@pytest.fixture
def market():
    """Stocks double and gold triples over 300 trading days while the dollar doubles."""
    con = datastore.connect(":memory:")
    days = [date(2021, 1, 2) + timedelta(days=i) for i in range(300)]
    grow = lambda k, i: 100.0 * k ** (i / 299)
    datastore.upsert(con, "index_daily", [{"index_code": advisor.TEDPIX, "d": d, "close": grow(2, i)} for i, d in enumerate(days)])
    datastore.upsert(con, "macro_daily", [{"series": "gold_18k_irr", "d": d, "close": grow(3, i)} for i, d in enumerate(days)]
                     + [{"series": "usd_irr", "d": d, "close": grow(2, i)} for i, d in enumerate(days)])
    adjust.build(con)
    advisor._evidence_cache.clear()
    yield con
    advisor._evidence_cache.clear()
    con.close()


def test_evidence_reports_money_and_purchasing_power(market):
    periods = {e["key"]: e for e in advisor.evidence(market)}
    assert set(periods) == {"second", "whole"}            # no data before 2021 in this fixture
    rows = {r["key"]: r for r in periods["whole"]["rows"]}
    assert rows["stocks"]["rial_multiple"] == pytest.approx(2.0)
    assert rows["stocks"]["real_multiple"] == pytest.approx(1.0)     # kept up with the dollar, no more
    assert rows["gold"]["real_multiple"] == pytest.approx(1.5)
    assert rows["mix"]["rial_multiple"] == pytest.approx(2.5)        # half of each, bought once
    assert rows["usd"]["real_multiple"] == pytest.approx(1.0) and rows["usd"]["worst_fall"] == pytest.approx(0.0)
    assert rows["fixed"]["real_multiple"] == pytest.approx(0.5)      # no NAV data: flat in rial, halved in dollars


def test_instruments_lists_liquid_plain_funds_only(market):
    con = market
    d = date(2021, 10, 28)
    funds = [("idx", 23, 5e10), ("eq", 6, 8e10), ("fix", 4, 9e11), ("lev", 22, 9e12), ("thin", 23, 1e8)]
    datastore.upsert(con, "instruments", [{"ins_code": c, "ticker": c, "name": c, "isin": "IRT1XXXX0001"} for c, _, _ in funds])
    datastore.upsert(con, "fipiran_funds", [{"ins_code": c, "reg_no": c, "name": c, "fund_type": t} for c, t, _ in funds])
    datastore.upsert(con, "daily_prices", [{"ins_code": c, "d": d, "close": 100.0, "last": 100.0, "volume": v / 100, "value": v, "trades": 10}
                                           for c, _, v in funds])
    adjust.build(con)
    got = advisor.instruments(con)
    assert [r["ticker"] for r in got["stocks_index"]] == ["idx"]     # the thin index fund and the leveraged fund are left out
    assert [r["ticker"] for r in got["stocks_equity"]] == ["eq"]
    assert [r["ticker"] for r in got["fixed"]] == ["fix"]
    assert got["gold"] == []


def test_simple_state_end_to_end(market, monkeypatch):
    monkeypatch.setattr(dashboard.sources, "tehran_today", lambda: date(2021, 10, 29))
    datastore.upsert(market, "instruments", [{"ins_code": "s", "ticker": "s", "name": "s", "isin": "IRO1SSSS0001"}])
    datastore.upsert(market, "daily_prices", [{"ins_code": "s", "d": date(2021, 10, 28), "close": 1.0, "last": 1.0, "volume": 1.0, "value": 1.0, "trades": 1}])
    pf = {"holdings": [], "manual": {"gold18_grams": 0, "coin_emami": 0, "cash_toman": 50_000_000, "usd": 0},
          "target": {"stocks": 50, "gold": 50, "fixed": 0, "other": 0, "cash": 0}}
    s = dashboard.simple_state(market, pf)
    assert s["data"]["level"] == "ok"
    assert s["plan"]["status"] == "invest"
    assert {x["key"]: x["toman"] for x in s["plan"]["steps"]} == {"stocks": 25_000_000, "gold": 25_000_000}
    assert "پول نقدتان" in advisor.as_text(s)
