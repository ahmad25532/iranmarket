import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adjust  # noqa: E402
import backtest  # noqa: E402
import datastore  # noqa: E402

DAYS = [date(2026, 1, 3) + timedelta(days=i) for i in range(6)]
CODE = "1"


def make_db(bars, ins_code=CODE, isin="IRO1TEST0001"):
    """bars: list of dicts per day with close / yesterday / high / low / last / value / volume / trades."""
    con = datastore.connect(":memory:")
    datastore.upsert(con, "instruments", [{"ins_code": ins_code, "ticker": "تست", "isin": isin}])
    rows = []
    for d, b in zip(DAYS, bars):
        close = b["close"]
        rows.append({
            "ins_code": ins_code, "d": d, "close": close, "last": b.get("last", close),
            "yesterday": b.get("yesterday"), "high": b.get("high", close * 1.01), "low": b.get("low", close * 0.99),
            "open": close, "volume": b.get("volume", 1_000_000.0),
            "value": b.get("value", b.get("vwap", close) * b.get("volume", 1_000_000.0)),
            "trades": b.get("trades", 100), "source": "history",
        })
    datastore.upsert(con, "daily_prices", rows)
    datastore.upsert(con, "index_daily", [{"index_code": backtest.TEDPIX, "d": d, "close": 1000.0 + i} for i, d in enumerate(DAYS)])
    datastore.upsert(con, "macro_daily", [{"series": "usd_irr", "d": d, "close": 1_000_000.0} for d in DAYS])
    adjust.build(con)
    return con


def chain(closes, **extra):
    """Bars where yesterday = previous close (no corporate actions)."""
    out, prev = [], None
    for c in closes:
        out.append({"close": c, "yesterday": prev if prev else c, **extra})
        prev = c
    return out


def cfg(**kw):
    base = dict(start=DAYS[0], end=DAYS[-1], capital=1_000_000.0, slippage=0.0, max_participation=1.0)
    base.update(kw)
    return backtest.BacktestConfig(**base)


# ---------- adjustment ----------

def test_dividend_is_adjusted_out_of_history():
    # Day 3 goes ex-dividend: reference price drops from 100 to 90, stock closes at 90.
    bars = chain([100, 100]) + [{"close": 90, "yesterday": 90}] + chain([90, 90, 90])[1:]
    con = make_db(bars)
    adj = [r[0] for r in con.execute("SELECT adj_close FROM price_adj ORDER BY d").fetchall()]
    assert adj[:3] == pytest.approx([90, 90, 90])
    f = con.execute("SELECT event_factor FROM price_adj WHERE d = ?", [DAYS[2]]).fetchall()[0][0]
    assert f == pytest.approx(0.9)


def test_capital_increase_keeps_total_return():
    # 1:1 bonus issue: price halves on the event day, holder's wealth is unchanged.
    bars = chain([200, 210]) + [{"close": 110, "yesterday": 105}] + chain([110, 121])[1:]
    con = make_db(bars[:4])
    adj = [r[0] for r in con.execute("SELECT adj_close FROM price_adj ORDER BY d").fetchall()]
    assert adj[1] == pytest.approx(105)          # 210 * 0.5
    assert adj[2] / adj[1] - 1 == pytest.approx(110 / 105 - 1)


def test_bad_factor_is_ignored_and_flagged():
    bars = chain([100, 100]) + [{"close": 1, "yesterday": 1}]   # 1/100: unit change or error, not a dividend
    con = make_db(bars)
    bad, f = con.execute("SELECT bad_factor, event_factor FROM price_adj WHERE d = ?", [DAYS[2]]).fetchall()[0]
    assert bad and f == 1.0


def test_queue_detection():
    bars = chain([100]) + [
        {"close": 105, "yesterday": 100, "high": 105, "low": 105, "last": 105},   # buy queue
        {"close": 100, "yesterday": 105, "high": 100, "low": 100, "last": 100},   # sell queue
        {"close": 101, "yesterday": 100, "high": 101, "low": 101, "last": 101},   # single price, +1%: not a queue
    ]
    con = make_db(bars)
    flags = con.execute("SELECT buy_locked, sell_locked FROM price_adj ORDER BY d").fetchall()
    assert flags == [(False, False), (True, False), (False, True), (False, False)]


# ---------- engine ----------

def test_signal_executes_next_day_at_vwap():
    bars = chain([100, 110, 121, 121, 121, 121])
    for b, v in zip(bars, [100, 108, 120, 121, 121, 121]):
        b["vwap"] = v
    con = make_db(bars)
    m = backtest.load_market(con, [CODE], DAYS[0], DAYS[-1])
    res = backtest.run(backtest.buy_and_hold(CODE), m, cfg())
    assert res.equity[0] == pytest.approx(1_000_000.0)          # nothing bought on the signal day
    t = res.trades[0]
    assert t.d == DAYS[1] and t.price == pytest.approx(108)     # filled the next day at its VWAP
    fee = backtest.DEFAULT_FEES["stock"].buy
    units = (1_000_000.0 / (1 + fee)) / 108
    assert res.equity[2] == pytest.approx(units * 121, rel=1e-9)


def test_buy_queue_delays_the_fill():
    bars = chain([100]) + [{"close": 105, "yesterday": 100, "high": 105, "low": 105, "last": 105}] + chain([105, 106, 107, 108])[1:]
    con = make_db(bars)
    m = backtest.load_market(con, [CODE], DAYS[0], DAYS[-1])
    res = backtest.run(backtest.buy_and_hold(CODE), m, cfg())
    assert res.trades[0].d == DAYS[2]
    assert res.blocked_days == 1


def test_no_trades_day_cannot_be_traded():
    bars = chain([100, 100, 101, 102, 103, 104])
    bars[1]["trades"] = 0
    con = make_db(bars)
    m = backtest.load_market(con, [CODE], DAYS[0], DAYS[-1])
    res = backtest.run(backtest.buy_and_hold(CODE), m, cfg())
    assert res.trades[0].d == DAYS[2]


def test_participation_cap_splits_large_orders():
    bars = chain([100] * 6, volume=1000.0)          # 100k rial traded per day
    con = make_db(bars)
    m = backtest.load_market(con, [CODE], DAYS[0], DAYS[-1])
    res = backtest.run(backtest.buy_and_hold(CODE), m, cfg(capital=300_000.0, max_participation=0.5))
    assert [t.value for t in res.trades[:3]] == pytest.approx([50_000.0] * 3)
    assert len(res.trades) == 5                       # still filling on the last day


def test_fees_by_asset_class():
    bars = chain([100] * 6)
    con = make_db(bars)
    m = backtest.load_market(con, [CODE], DAYS[0], DAYS[-1])
    res = backtest.run(backtest.buy_and_hold(CODE), m, cfg())
    fee = backtest.DEFAULT_FEES["stock"].buy
    assert res.fees_paid == pytest.approx(1_000_000.0 / (1 + fee) * fee)
    assert res.equity[-1] == pytest.approx(1_000_000.0 / (1 + fee))


def test_cash_rate_accrues_on_idle_cash():
    bars = chain([100] * 6)
    con = make_db(bars)
    m = backtest.load_market(con, [CODE], DAYS[0], DAYS[-1])
    res = backtest.run(lambda t, mk: None, m, cfg(cash_rate=0.30))
    days = (DAYS[-1] - DAYS[0]).days
    assert res.equity[-1] == pytest.approx(1_000_000.0 * 1.30 ** (days / 365.25))
