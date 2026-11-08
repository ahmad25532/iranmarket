import os
import sys
from datetime import date, timedelta

import polars as pl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datastore  # noqa: E402
import goldfunds  # noqa: E402

DAYS = [date(2025, 1, 1) + timedelta(days=i) for i in range(400)]


def make_db(funds):
    """funds: {code: {"nav": callable(i) -> fipiran NAV, "price": callable(i) -> price, "value": float,
                      "tsetmc_nav": optional NAV on the last day}}"""
    con = datastore.connect(":memory:")
    datastore.upsert(con, "instruments", [{"ins_code": c, "ticker": c} for c in funds])
    con.execute("CREATE TABLE instrument_class AS SELECT ins_code, ticker, ticker AS name, 'etf_commodity' AS asset_class FROM instruments")
    con.execute("CREATE TABLE gold_funds AS SELECT ins_code FROM instruments")
    navs, prices, adj = [], [], []
    for c, f in funds.items():
        for i, d in enumerate(DAYS):
            nav, px = f["nav"](i), f["price"](i)
            navs.append({"ins_code": c, "d": d, "nav_redeem": nav, "source": "fipiran"})
            prices.append({"ins_code": c, "d": d, "close": px, "volume": f["value"] / px, "value": f["value"], "trades": 500})
            adj.append({"ins_code": c, "d": d, "adj_close": px, "adj_vwap": px, "factor": 1.0, "trades": 500})
        if f.get("tsetmc_nav"):
            navs[-1] = {"ins_code": c, "d": DAYS[-1], "nav_redeem": f["tsetmc_nav"], "source": "tsetmc"}
    datastore.upsert(con, "etf_nav", navs)
    datastore.upsert(con, "daily_prices", prices)
    con.execute("CREATE TABLE price_adj AS SELECT * FROM (VALUES (NULL::VARCHAR, NULL::DATE, NULL::DOUBLE, NULL::DOUBLE, NULL::DOUBLE, NULL::INTEGER)) t(ins_code, d, adj_close, adj_vwap, factor, trades) WHERE false")
    con.register("_adj", pl.DataFrame(adj).to_arrow())
    con.execute("INSERT INTO price_adj SELECT ins_code, d, adj_close, adj_vwap, factor, trades FROM _adj")
    datastore.upsert(con, "macro_daily", [{"series": "gold_18k_irr", "d": d, "close": 100.0 * 1.001 ** i, "source": "history"}
                                          for i, d in enumerate(DAYS)])
    return con


def grow(rate, start=100.0):
    return lambda i: start * (1 + rate) ** i


def test_artifact_is_detected_against_peers_and_replaced():
    jump = lambda i: 100.0 * 1.001 ** i * (2.0 if i >= 300 else 1.0)    # unit change on day 300
    con = make_db({"a": {"nav": grow(0.001), "price": grow(0.001), "value": 2e11},
                   "b": {"nav": grow(0.001), "price": grow(0.001), "value": 2e11},
                   "c": {"nav": grow(0.001), "price": grow(0.001), "value": 2e11},
                   "x": {"nav": jump, "price": grow(0.001), "value": 2e11}})
    nav = goldfunds.load_nav(con)
    flagged = nav.filter(pl.col("artifact"))
    assert flagged["ins_code"].to_list() == ["x"] and flagged["d"][0] == DAYS[300]
    x = nav.filter(pl.col("ins_code") == "x")
    assert x["nav_index"][-1] == pytest.approx(1.001 ** 399, rel=1e-6)   # the jump is removed


def test_shared_market_move_is_not_an_artifact():
    big_day = lambda i: 100.0 * (1.10 if i >= 200 else 1.0)            # every fund +10% the same day
    con = make_db({c: {"nav": big_day, "price": big_day, "value": 2e11} for c in "abc"})
    assert goldfunds.load_nav(con)["artifact"].sum() == 0


def test_premium_relative_to_own_normal_cancels_constant_bias():
    # Fund "low" always trades 2% under its (biased) NAV; today it is at its normal level, so rel ~ 0.
    con = make_db({"low": {"nav": lambda i: 100.0, "price": lambda i: 98.0, "value": 2e11},
                   "spike": {"nav": lambda i: 100.0, "price": lambda i: 104.0 if i == 399 else 100.0, "value": 2e11}})
    rel = goldfunds.own_relative_premium(goldfunds.load_premiums(con))
    last = {r["ins_code"]: r for r in rel.filter(pl.col("d") == DAYS[-1]).iter_rows(named=True)}
    assert last["low"]["rel_premium"] == pytest.approx(0.0, abs=1e-9)
    assert last["spike"]["rel_premium"] == pytest.approx(0.04, abs=1e-9)


def test_fund_table_flags_and_order():
    con = make_db({
        "big": {"nav": grow(0.001), "price": grow(0.001), "value": 9e11},
        "small": {"nav": grow(0.001), "price": grow(0.001), "value": 1e9},
        # Fipiran NAV 15% too low: TSETMC's NAV on the last day disagrees
        "bias": {"nav": lambda i: 85.0 * 1.001 ** i, "price": grow(0.001), "value": 2e11, "tsetmc_nav": 100.0 * 1.001 ** 399},
    })
    t = goldfunds.fund_table(con)
    rows = {r["ticker"]: r for r in t["rows"]}
    assert [r["ticker"] for r in t["rows"]] == ["big", "bias", "small"]          # by liquidity, not a ranking
    assert rows["big"]["liquid"] and not rows["small"]["liquid"]
    assert rows["bias"]["nav_sources_disagree"] and not rows["bias"]["reliable"]
    assert rows["big"]["nav_12m"] == pytest.approx(1.001 ** 365 - 1, rel=1e-3)
    assert "top_third_12m" not in rows["big"]
