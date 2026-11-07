import os
import sys
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest  # noqa: E402
import stockpick as sp  # noqa: E402


def make_stocks(n_days=400, n_codes=80, seed=1):
    rng = np.random.default_rng(seed)
    close = 1000 * np.cumprod(1 + rng.normal(0, 0.02, (n_days, n_codes)), axis=0)
    value = np.full((n_days, n_codes), 1e12) * (1 + np.arange(n_codes))
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(n_days)]
    shape = close.shape
    market = backtest.MarketData(
        days=days, codes=[f"c{j}" for j in range(n_codes)], close=close, vwap=close.copy(), value=value,
        can_buy=np.ones(shape, bool), can_sell=np.ones(shape, bool), asset_class=["stock"] * n_codes,
        tickers=[f"t{j}" for j in range(n_codes)], usd=np.full(n_days, 1e6), tedpix=close.mean(axis=1),
        gold18=np.linspace(1e7, 2e7, n_days),
    )
    return sp.Stocks(
        market=market, bad=np.zeros(shape, bool), buy_locked=np.zeros(shape, bool), sell_locked=np.zeros(shape, bool),
        flow_net=rng.normal(0, 1e10, shape), buy_i_val=np.full(shape, 1e11), buy_i_cnt=rng.integers(10, 100, shape).astype(float),
        sell_i_val=np.full(shape, 1e11), sell_i_cnt=rng.integers(10, 100, shape).astype(float),
    )


def truncated(s, t):
    """The same data with everything after day t destroyed."""
    def cut(x):
        out = x.copy()
        out[t + 1:] = np.nan if x.dtype == float else False
        return out
    m = s.market
    arrays = {f: cut(getattr(m, f)) for f in ("close", "vwap", "value", "can_buy", "can_sell")}
    return sp.Stocks(replace(m, **arrays), *(cut(getattr(s, f)) for f in (
        "bad", "buy_locked", "sell_locked", "flow_net", "buy_i_val", "buy_i_cnt", "sell_i_val", "sell_i_cnt")))


@pytest.mark.parametrize("name", list(sp.FACTORS))
def test_factors_do_not_read_the_future(name):
    s = make_stocks()
    t = 300
    full = sp.FACTORS[name](s, t)
    cut = sp.FACTORS[name](truncated(s, t), t)
    np.testing.assert_array_equal(full, cut)


def test_universe_and_selection_do_not_read_the_future():
    s = make_stocks()
    t = 300
    cut = truncated(s, t)
    np.testing.assert_array_equal(sp.universe(s, t), sp.universe(cut, t))
    assert sp.select(s, t, sp.FACTORS["momentum 12-1"]) == sp.select(cut, t, sp.FACTORS["momentum 12-1"])


def test_universe_needs_history_activity_and_clean_factors():
    s = make_stocks()
    t = 300
    assert sp.universe(s, sp.MIN_HISTORY - 2).sum() == 0
    s.market.close[: t - sp.MIN_HISTORY + 2, 0] = np.nan   # listed too recently
    s.market.value[t - 59: t - 29, 1] = 0.0                # traded on only half of the last 60 days
    s.market.value[t - 4: t + 1, 2] = 0.0                  # halted in the last week
    s.bad[t - 100, 3] = True                               # corrupt factor within the last year
    mask = sp.universe(s, t)
    assert not mask[:4].any()
    assert mask[4:].all()


def test_universe_survives_a_market_wide_closure():
    s = make_stocks()
    t = 300
    s.market.value[t - 40: t + 1] = 0.0     # nothing traded anywhere for 40 days
    assert sp.universe(s, t).all()


def test_benchmark_keeps_earning_after_a_closure():
    s = make_stocks()
    m = s.market
    m.value[280:300] = 0.0
    m.close[300:] = m.close[299] * 2        # the market reopens at double
    assert sp.universe_curve(s, 260, 320, 20)[-1] > 1.5


def test_fee_drag_is_measured_on_average_equity():
    s = make_stocks()
    result = sp.run_rule(s, lambda st, t: np.arange(80, dtype=float), 260, 400, 20)
    years = (result.days[-1] - result.days[0]).days / 365.25
    assert sp.annual_fee_drag(result) == pytest.approx(result.fees_paid / result.equity.mean() / years)


def test_random_control_is_reproducible():
    s = make_stocks()
    s.market.days = [date(2020, 1, 1) + timedelta(days=i) for i in range(400)]
    period = (s.market.days[260], s.market.days[399])
    assert sp.random_control(s, period, 20, seeds=3) == sp.random_control(s, period, 20, seeds=3)


def test_universe_keeps_the_most_traded_names():
    s = make_stocks(n_codes=sp.UNIVERSE_SIZE + 10)
    mask = sp.universe(s, 300)
    assert mask.sum() == sp.UNIVERSE_SIZE
    assert not mask[:10].any()   # value rises with the column index in make_stocks


def test_select_returns_the_highest_scores():
    s = make_stocks()
    score = lambda st, t: np.arange(st.market.close.shape[1], dtype=float)
    assert sp.select(s, 300, score, n=3) == [79, 78, 77]


def test_select_is_empty_when_the_universe_is_too_small():
    s = make_stocks(n_codes=sp.MIN_UNIVERSE - 1)
    assert sp.select(s, 300, sp.FACTORS["low volatility"]) == []


def test_composite_ranks_ignore_scale_and_drop_incomplete_stocks():
    s = make_stocks()
    t = 300
    original = dict(sp.FACTORS)
    try:
        sp.FACTORS["a"] = lambda st, _: np.arange(80, dtype=float) * 1e9
        b = np.arange(80, dtype=float)
        b[5] = np.nan
        sp.FACTORS["b"] = lambda st, _: b
        score = sp.composite(["a", "b"])(s, t)
    finally:
        sp.FACTORS.clear()
        sp.FACTORS.update(original)
    assert np.isnan(score[5])
    assert np.nanargmax(score) == 79


def test_rule_pays_stock_fees_and_benchmark_is_free():
    s = make_stocks()
    # A flat market: any cost shows up directly as a loss against the universe.
    s.market.close[:] = 1000.0
    s.market.vwap[:] = 1000.0
    a, b = 260, 400
    result = sp.run_rule(s, lambda st, t: np.arange(80, dtype=float), a, b, 20)
    assert result.equity[-1] < result.equity[0]
    assert result.fees_paid > 0
    assert sp.universe_curve(s, a, b, 20)[-1] == pytest.approx(1.0)


def test_rule_with_real_edge_beats_universe_and_noise_does_not():
    s = make_stocks(n_days=900)
    m = s.market
    # The last 20 columns earn more every day, and the "edge" score knows which they are.
    r = np.random.default_rng(2).normal(0, 0.01, m.close.shape)
    r[:, 60:] += 0.003
    m.close[:] = 1000 * np.cumprod(1 + r, axis=0)
    m.vwap[:] = m.close
    a, b = 260, 900
    edge = np.arange(80, dtype=float)
    days = (m.days[a], m.days[b - 1])
    good = sp.evaluate(s, "edge", lambda st, t: edge, days, 20)
    assert sp.passes_design(good) and sp.confirmed(good)
    noise = sp.evaluate(s, "noise", lambda st, t: np.random.default_rng(t).random(80), days, 20)
    assert noise.excess < good.excess
    assert not sp.confirmed(noise)


D = [date(2021, 1, 1) + timedelta(days=i) for i in range(6)]


def test_chain_starts_at_first_trade_not_the_placeholder():
    rows = [(D[0], 1000, 1000, 0, 0, 0), (D[1], 65119, 1000, 1e12, 1e7, 100), (D[2], 65254, 65119, 1e9, 1e4, 10)]
    out = sp.chain_levels(rows)
    assert [r[0] for r in out] == [D[1], D[2]]
    assert out[1][1] / out[0][1] == pytest.approx(65254 / 65119)


def test_chain_ignores_a_price_reset_on_a_day_without_trades():
    rows = [(D[0], 100, 100, 1e6, 1e4, 5), (D[1], 3, 3, 0, 0, 0), (D[2], 3, 3, 0, 0, 0), (D[3], 3.3, 3, 1e6, 3e5, 5)]
    levels = [r[1] for r in sp.chain_levels(rows)]
    assert levels[1] == levels[2] == levels[0]
    assert levels[3] / levels[0] == pytest.approx(1.1)


def test_chain_adjusts_for_a_lowered_reference_price():
    rows = [(D[0], 100, 100, 1e6, 1e4, 5), (D[1], 82, 80, 1e6, 1e4, 5)]
    levels = [r[1] for r in sp.chain_levels(rows)]
    assert levels[1] / levels[0] == pytest.approx(82 / 80)


def test_chain_vwap_uses_the_same_scale_and_is_missing_without_trades():
    rows = [(D[0], 100, 100, 1e6, 1e4, 5), (D[1], 110, 100, 1.05e6, 1e4, 5), (D[2], 110, 110, 0, 0, 0)]
    out = sp.chain_levels(rows)
    assert out[1][2] == pytest.approx(out[0][1] * 1.05)
    assert out[2][2] is None


def test_fund_shares_are_outside_the_universe_query():
    sql = sp.UNIVERSE_SQL
    assert "IRO1%0001" in sql and "IRO3%0001" in sql
    assert "IRT" not in sql
