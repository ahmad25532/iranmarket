import os
import sys
from datetime import date, timedelta

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adjust  # noqa: E402
import datastore  # noqa: E402
import research  # noqa: E402
import structural as st  # noqa: E402


def panel(prices, rel=None, usable=None):
    price = np.array(prices, float)
    n, m = price.shape
    return st.FundPanel(
        days=[date(2024, 1, 1) + timedelta(days=i) for i in range(n)], codes=[f"f{j}" for j in range(m)], price=price,
        traded=np.ones((n, m), bool), rel=np.array(rel, float) if rel is not None else np.zeros((n, m)),
        usable=np.array(usable, bool) if usable is not None else np.ones((n, m), bool),
    )


def test_no_gap_means_no_switch_and_no_cost():
    p = panel([[100, 100], [110, 90], [121, 81]])
    curve, n = st.simulate_switching(p, 0, 3, 0.01)
    assert n == 0
    assert curve[-1] == pytest.approx(1.21)


def test_switch_fills_the_day_after_the_signal_and_pays_cost():
    # Day 1 shows f0 2 points rich against f1. The fill is on day 2, so day 2's move in f0 still counts.
    p = panel([[100, 100], [100, 100], [110, 100], [110, 120]],
              rel=[[0, 0], [0.02, 0], [0, 0], [0, 0]])
    curve, n = st.simulate_switching(p, 0, 4, 0.01, lag=1, cost=0.01)
    assert n == 1
    assert curve[2] == pytest.approx(1.10 * 0.99)
    assert curve[3] == pytest.approx(1.10 * 0.99 * 1.2)


def test_longer_lag_delays_the_fill():
    p = panel([[100, 100], [100, 100], [110, 100], [110, 120]],
              rel=[[0, 0], [0.02, 0], [0, 0], [0, 0]])
    curve, n = st.simulate_switching(p, 0, 4, 0.01, lag=2, cost=0.0)
    assert n == 1
    assert curve[3] == pytest.approx(1.10)    # filled at day 3's close, so f1's jump is missed


def test_no_fill_while_a_fund_does_not_trade():
    p = panel([[100, 100], [100, 100], [100, 100], [100, 100]], rel=[[0, 0], [0.02, 0], [0, 0], [0, 0]])
    p.traded[2, 1] = False
    curve, n = st.simulate_switching(p, 0, 4, 0.01, cost=0.01)
    assert n == 1
    assert curve[2] == pytest.approx(1.0)
    assert curve[3] == pytest.approx(0.99)


def test_unusable_funds_are_never_targets():
    p = panel([[100, 100], [100, 100], [100, 100]], rel=[[0, 0], [0.05, 0], [0, 0]],
              usable=[[1, 1], [1, 0], [1, 0]])
    assert st.simulate_switching(p, 0, 3, 0.01)[1] == 0


def test_hold_funds_keeps_its_first_day_members():
    p = panel([[100, 100, 100], [110, 90, 500]], usable=[[1, 1, 0], [1, 1, 1]])
    assert st.hold_funds(p, 0, 2)[-1] == pytest.approx(1.0)


def test_split_covers_only_days_with_three_funds():
    usable = np.zeros((10, 3), bool)
    usable[2:8] = True
    rows = st.split_rows(panel(np.full((10, 3), 100.0), usable=usable))
    assert rows == {"design": (2, 5), "test": (5, 8)}


def test_band_trades_only_after_enough_drift():
    n = 5
    close = np.array([[100, 100, 1], [110, 100, 1], [120, 100, 1], [200, 100, 1], [200, 100, 1]], float)
    calls = []
    strategy = st.band_strategy(0.10)
    market = type("M", (), {"close": close})()
    for t in range(n):
        calls.append(strategy(t, market) is not None)
    # Stock weight is 52.4% and 54.5% on days 1-2, 66.7% on day 3, then the base resets.
    assert calls == [True, False, False, True, False]


def test_closing_queues_count_a_close_on_the_low_as_a_sell_lock():
    d = date(2024, 1, 1)
    con = datastore.connect(":memory:")
    datastore.upsert(con, "instruments", [{"ins_code": "s", "ticker": "s", "isin": "IRO1TEST0001", "paper_type": 8}])
    datastore.upsert(con, "daily_prices", [{
        "ins_code": "s", "d": d, "open": 100.0, "high": 100.0, "low": 95.0, "close": 96.0, "last": 95.0,
        "yesterday": 100.0, "volume": 1000.0, "value": 96000.0, "trades": 10, "source": "history"}])
    datastore.upsert(con, "index_daily", [{"index_code": research.TEDPIX, "d": d, "close": 1000.0}])
    adjust.build(con)
    assert research.load_panel(con, start=d).sellq_share[0] == 0.0
    assert research.load_panel(con, start=d, closing_queues=True).sellq_share[0] == 1.0
