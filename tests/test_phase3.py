import os
import sys
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datastore  # noqa: E402
import research  # noqa: E402


def make_panel(tedpix, gold, sellq=None, flow=None):
    n = len(tedpix)
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    return research.Panel(
        days=days, tedpix=np.array(tedpix, float), eqw=np.array(tedpix, float), usd=np.full(n, 1e6),
        gold18=np.array(gold, float), ounce=np.full(n, 2000.0), fixed=np.linspace(1, 1.01, n),
        sellq_share=np.array(sellq if sellq is not None else [0.0] * n, float), buyq_share=np.zeros(n),
        flow_ratio=np.array(flow if flow is not None else [0.0] * n, float),
    )


def test_sma_and_rolling_sum_use_only_past_data():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    assert np.isnan(research.sma(x, 3)[:2]).all()
    assert research.sma(x, 3)[2:] == pytest.approx([2.0, 3.0])
    assert research.rolling_sum(x, 2)[1:] == pytest.approx([3.0, 5.0, 7.0])


def test_trend_signal_warmup_counts_as_in():
    p = make_panel([100, 90, 80, 70, 60], [1] * 5)
    on = research.sig_trend(p, 3)
    assert list(on) == [True, True, False, False, False]


def test_hysteresis_needs_consecutive_days():
    raw = np.array([True, False, True, False, False, False, True])
    assert list(research.sig_with_hysteresis(raw, 2)) == [True, True, True, True, False, False, False]


def test_relative_momentum_picks_the_stronger_asset():
    p = make_panel([100, 100, 100, 120, 130], [100, 110, 120, 120, 120])
    on = research.sig_relative_momentum(p, 2)
    assert list(on) == [True, True, False, True, True]


def test_bucket_study_ignores_nan():
    sig = np.array([-0.05, 0.0, 0.03, 0.10, np.nan])
    fwd = np.array([0.1, 0.2, 0.3, np.nan, 0.5])
    rows = research.bucket_study(sig, fwd, [0.02, 0.05])
    assert [r[1] for r in rows] == [2, 1, 0]
    assert rows[0][2] == pytest.approx(0.15)


def test_cheapest_fund_spread():
    d = date(2024, 1, 1)
    g = pl.DataFrame({
        "d": [d] * 3, "ins_code": ["a", "b", "c"],
        "premium": [0.00, 0.03, 0.05], "fwd60": [0.10, 0.07, 0.04],
    })
    s = research.cheapest_fund_spread(g, "fwd60")
    assert s["spread"][0] == pytest.approx(0.10 - 0.055)
    assert research.cheapest_fund_spread(g.head(2), "fwd60").height == 0   # needs >= 3 funds


def test_exit_cannot_be_filled_on_market_lock_days():
    # Signal says exit on day 1, but the market is locked in sell queues on days 2-3.
    p = make_panel([100, 100, 90, 80, 80, 80], [100] * 6, sellq=[0, 0, 0.6, 0.6, 0, 0])
    on = np.array([True, False, False, False, False, False])
    res = research.run_weights(p, research.switch(on, (1, 0, 0), (0, 1, 0)), slippage=0.0)
    sells = [t for t in res.trades if t.side == "sell"]
    assert sells[0].d == p.days[4]          # first day the market is not locked
    assert res.blocked_days == 2


def test_fixed_income_index_skips_distribution_drops():
    con = datastore.connect(":memory:")
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(4)]
    datastore.upsert(con, "instruments", [{"ins_code": "f", "ticker": "ث", "paper_type": 8}])
    datastore.upsert(con, "fipiran_funds", [{"ins_code": "f", "reg_no": "1", "fund_type": 4}])
    con.execute("CREATE TABLE instrument_class AS SELECT 'f' AS ins_code, 'ث' AS ticker, '' AS name, 'etf_fixed_income' AS asset_class")
    # +0.1% a day, with a 2% payout drop on day 3
    navs = [10000.0, 10010.0, 9810.0, 9819.81]
    datastore.upsert(con, "etf_nav", [{"ins_code": "f", "d": d, "nav_redeem": v} for d, v in zip(days, navs)])
    idx = research.fixed_income_index(con, days)
    assert idx[1] / idx[0] - 1 == pytest.approx(0.001)
    assert idx[2] == pytest.approx(idx[1])                  # payout day ignored, not a loss
    assert idx[3] / idx[2] - 1 == pytest.approx(0.001, rel=1e-3)
