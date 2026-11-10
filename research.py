"""
Signal research (Phase 3).

Every signal here is tested the same way:

1. Rules and their few parameters are chosen on the design period only (IN_SAMPLE).
2. The chosen rule is then run, unchanged, on the later period it never saw (OUT_OF_SAMPLE).
3. Results are net of fees and slippage, executed the day after the signal, and an exit
   cannot be filled on days when most of the market is locked in sell queues.

Asset proxies (investable today through funds):
- STOCKS: TEDPIX (total return: it grows 9-14 points a year faster than the price index in most
  years, so dividends are included; it is not itself buyable, and pays no fund costs here)
- GOLD:   18k gold, TGJU free-market price
- FIXED:  composite NAV of fixed-income ETFs (median daily return across funds)
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

import duckdb
import numpy as np
import polars as pl

import backtest
import datastore
import goldfunds

IN_SAMPLE = (date(2014, 4, 1), date(2020, 12, 31))
OUT_OF_SAMPLE = (date(2021, 1, 1), date(2026, 12, 31))
TEDPIX = backtest.TEDPIX
EQUAL_WEIGHT = "67130298613737946"
# A day when at least this share of traded stocks closed locked in a sell queue cannot be exited.
MARKET_LOCK_SHARE = 0.40


# ---------------------------------------------------------------- panel

@dataclass
class Panel:
    days: List[date]
    tedpix: np.ndarray
    eqw: np.ndarray
    usd: np.ndarray
    gold18: np.ndarray
    ounce: np.ndarray
    fixed: np.ndarray        # fixed-income composite NAV index (1.0 at the first day)
    sellq_share: np.ndarray  # share of traded stocks closing in a sell queue
    buyq_share: np.ndarray
    flow_ratio: np.ndarray   # net individual money flow / traded value, all stocks, that day

    def slice(self, start: date, end: date) -> "Panel":
        idx = [i for i, d in enumerate(self.days) if start <= d <= end]
        a, b = idx[0], idx[-1] + 1
        return Panel(self.days[a:b], *(getattr(self, f)[a:b] for f in (
            "tedpix", "eqw", "usd", "gold18", "ounce", "fixed", "sellq_share", "buyq_share", "flow_ratio")))


def _on_days(con: duckdb.DuckDBPyConnection, sql: str, days: List[date]) -> np.ndarray:
    """Value per trading day, carrying the last known value forward."""
    return backtest._series(con, sql, days)


def fixed_income_index(con: duckdb.DuckDBPyConnection, days: List[date]) -> np.ndarray:
    """
    Composite of fixed-income fund NAVs. Many of these funds pay monthly distributions, which
    show up as a NAV drop on the payout day, so daily returns outside +-1% are discarded and the
    median of the remaining funds' returns is chained.
    """
    rows = con.execute(
        """WITH n AS (
               SELECT n.ins_code, n.d, n.nav_redeem / lag(n.nav_redeem) OVER (PARTITION BY n.ins_code ORDER BY n.d) - 1 AS r,
                      n.d - lag(n.d) OVER (PARTITION BY n.ins_code ORDER BY n.d) AS gap
               FROM etf_nav n JOIN instrument_class c USING (ins_code) WHERE c.asset_class = 'etf_fixed_income')
           SELECT d, median(r) FROM n WHERE r BETWEEN -0.01 AND 0.01 AND gap <= 7 GROUP BY d ORDER BY d"""
    ).fetchall()
    by_day = dict(rows)
    level, out, k = 1.0, np.zeros(len(days)), 0
    nav_days = sorted(by_day)
    for i, d in enumerate(days):
        while k < len(nav_days) and nav_days[k] <= d:
            if i > 0 or nav_days[k] == d:
                level *= 1 + by_day[nav_days[k]]
            k += 1
        out[i] = level
    return out


def load_panel(con: duckdb.DuckDBPyConnection, start: date = date(2013, 7, 22), end: date = date(2100, 1, 1),
               closing_queues: bool = False) -> Panel:
    """
    closing_queues also counts a stock that traded away from its limit but closed on the day's extreme
    at least 1.5% from the reference price. A whole-day lock misses a queue that formed a moment after
    the open and this counts some days that were not queues, so the two bracket the real figure.
    """
    days = [r[0] for r in con.execute(
        "SELECT d FROM index_daily WHERE index_code = ? AND d BETWEEN ? AND ? ORDER BY d", [TEDPIX, start, end]
    ).fetchall()]
    sell_extra = "OR (r.last = r.low AND r.last < r.yesterday * 0.985)" if closing_queues else ""
    buy_extra = "OR (r.last = r.high AND r.last > r.yesterday * 1.015)" if closing_queues else ""
    stress = dict(
        (r[0], r[1:]) for r in con.execute(
            f"""SELECT p.d,
                      avg(CASE WHEN p.sell_locked {sell_extra} THEN 1.0 ELSE 0.0 END),
                      avg(CASE WHEN p.buy_locked {buy_extra} THEN 1.0 ELSE 0.0 END)
               FROM price_adj p JOIN daily_prices r USING (ins_code, d) JOIN instrument_class c USING (ins_code)
               WHERE c.asset_class = 'stock' AND p.trades > 0 GROUP BY p.d"""
        ).fetchall()
    )
    flows = dict(con.execute(
        """SELECT f.d,
                  sum(coalesce(f.buy_i_value - f.sell_i_value,
                               (f.buy_i_volume - f.sell_i_volume) * p.value / nullif(p.volume, 0)))
                  / nullif(sum(p.value), 0)
           FROM client_flows f JOIN daily_prices p USING (ins_code, d) JOIN instrument_class c USING (ins_code)
           WHERE c.asset_class = 'stock' AND p.volume > 0 GROUP BY f.d"""
    ).fetchall())
    return Panel(
        days=days,
        tedpix=_on_days(con, f"SELECT d, close FROM index_daily WHERE index_code = '{TEDPIX}'", days),
        eqw=_on_days(con, f"SELECT d, close FROM index_daily WHERE index_code = '{EQUAL_WEIGHT}'", days),
        usd=_on_days(con, "SELECT d, close FROM macro_daily WHERE series = 'usd_irr'", days),
        gold18=_on_days(con, "SELECT d, close FROM macro_daily WHERE series = 'gold_18k_irr'", days),
        ounce=_on_days(con, "SELECT d, close FROM macro_daily WHERE series = 'gold_ounce_usd'", days),
        fixed=fixed_income_index(con, days),
        sellq_share=np.array([stress.get(d, (0.0, 0.0))[0] for d in days]),
        buyq_share=np.array([stress.get(d, (0.0, 0.0))[1] for d in days]),
        flow_ratio=np.array([flows.get(d) or 0.0 for d in days], dtype=float),
    )


# ---------------------------------------------------------------- rolling helpers (all use data up to t only)

def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def rolling_sum(x: np.ndarray, n: int) -> np.ndarray:
    return sma(x, n) * n


# ---------------------------------------------------------------- allocation backtests

ASSETS = ("STOCKS", "GOLD", "FIXED")
ASSET_CLASS = {"STOCKS": "etf_equity", "GOLD": "etf_commodity", "FIXED": "etf_fixed_income"}


def panel_market(p: Panel) -> backtest.MarketData:
    """Wraps the proxies as three always-liquid instruments; STOCKS cannot be traded on market-wide lock days."""
    close = np.column_stack([p.tedpix, p.gold18, p.fixed])
    n = len(p.days)
    sell_ok = np.ones((n, 3), dtype=bool)
    buy_ok = np.ones((n, 3), dtype=bool)
    sell_ok[:, 0] = p.sellq_share < MARKET_LOCK_SHARE
    buy_ok[:, 0] = p.buyq_share < MARKET_LOCK_SHARE
    return backtest.MarketData(
        days=p.days, codes=list(ASSETS), close=close, vwap=close.copy(), value=np.full((n, 3), 1e30),
        can_buy=buy_ok, can_sell=sell_ok, asset_class=[ASSET_CLASS[a] for a in ASSETS], tickers=list(ASSETS),
        usd=p.usd, tedpix=p.tedpix, gold18=p.gold18,
    )


def weights_strategy(weights: np.ndarray, rebalance_every: int = 0) -> backtest.Strategy:
    """
    weights: (days x 3) target weights decided with data up to each day. Trades when they change,
    and also every `rebalance_every` trading days (0 = never) to pull drifted weights back.
    """
    def strategy(t: int, m: backtest.MarketData) -> Optional[Dict[str, float]]:
        changed = t == 0 or not np.allclose(weights[t], weights[t - 1])
        if changed or (rebalance_every and t % rebalance_every == 0):
            return {a: float(w) for a, w in zip(ASSETS, weights[t]) if w > 0}
        return None
    return strategy


def run_weights(p: Panel, weights: np.ndarray, slippage: float = 0.003, rebalance_every: int = 0) -> backtest.Result:
    # min_trade_frac skips tiny rebalances so periodic rebalancing does not pay fees on noise
    cfg = backtest.BacktestConfig(start=p.days[0], end=p.days[-1], slippage=slippage,
                                  min_trade_frac=0.02 if rebalance_every else 0.0)
    return backtest.run(weights_strategy(weights, rebalance_every), panel_market(p), cfg)


def static(p: Panel, stocks: float, gold: float, fixed: float) -> np.ndarray:
    return np.tile([stocks, gold, fixed], (len(p.days), 1))


def switch(risk_on: np.ndarray, on: Tuple[float, float, float], off: Tuple[float, float, float]) -> np.ndarray:
    return np.where(risk_on[:, None], np.array(on), np.array(off))


# ---- stock exit signals: True = stay in stocks. NaN warm-up counts as "in" (no signal yet).

def sig_trend(p: Panel, n: int) -> np.ndarray:
    m = sma(p.tedpix, n)
    return ~(p.tedpix < m)


def sig_trend_usd(p: Panel, n: int) -> np.ndarray:
    x = p.tedpix / p.usd
    return ~(x < sma(x, n))


def sig_flow(p: Panel, n: int) -> np.ndarray:
    return ~(rolling_sum(p.flow_ratio, n) < 0)


def sig_with_hysteresis(raw_in: np.ndarray, confirm: int) -> np.ndarray:
    """Only switches state after `confirm` consecutive days of the opposite reading (fewer whipsaws)."""
    out = np.zeros_like(raw_in, dtype=bool)
    state, streak = True, 0
    for t, v in enumerate(raw_in):
        if v != state:
            streak += 1
            if streak >= confirm:
                state, streak = v, 0
        else:
            streak = 0
        out[t] = state
    return out


EXIT_SIGNALS: Dict[str, Callable[[Panel], np.ndarray]] = {
    "trend 100d": lambda p: sig_trend(p, 100),
    "trend 200d": lambda p: sig_trend(p, 200),
    "trend 200d, 5d confirm": lambda p: sig_with_hysteresis(sig_trend(p, 200), 5),
    "trend in USD 100d": lambda p: sig_trend_usd(p, 100),
    "trend in USD 200d": lambda p: sig_trend_usd(p, 200),
    "money flow 20d": lambda p: sig_flow(p, 20),
    "money flow 60d": lambda p: sig_flow(p, 60),
    "trend 200d AND flow 60d": lambda p: sig_trend(p, 200) | sig_flow(p, 60),
}
# Note on the last one: risk-off only when BOTH say out (in = trend in OR flow in).

SAFE_MIXES = {
    "gold": (0.0, 1.0, 0.0),
    "fixed income": (0.0, 0.0, 1.0),
    "half gold, half fixed": (0.0, 0.5, 0.5),
}


def sig_relative_momentum(p: Panel, n: int) -> np.ndarray:
    """True when stocks beat gold over the last n trading days (hold the stronger of the two)."""
    rs = np.full(len(p.days), np.nan)
    rs[n:] = (p.tedpix[n:] / p.tedpix[:-n]) / (p.gold18[n:] / p.gold18[:-n])
    return ~(rs < 1.0)


ROTATIONS: Dict[str, Callable[[Panel], np.ndarray]] = {
    "stocks vs gold momentum 60d": lambda p: sig_relative_momentum(p, 60),
    "stocks vs gold momentum 120d": lambda p: sig_relative_momentum(p, 120),
    "stocks vs gold momentum 250d": lambda p: sig_relative_momentum(p, 250),
}


def summarize(res: backtest.Result, weights: np.ndarray) -> Dict[str, float]:
    m = res.metrics()
    switches = sum(1 for t in res.trades if t.side == "sell")
    return {"cagr": m["cagr"], "cagr_usd": m["cagr_usd"], "maxdd_usd": m["max_drawdown_usd"],
            "sharpe_usd": m["sharpe_usd"], "switches": switches, "fees": m["fees_paid_pct"],
            "in_stocks": float(np.mean(weights[:, 0]))}


def _row(p: Panel, label: str, safe: str, w: np.ndarray, rebalance_every: int = 0) -> Tuple[str, str, Dict[str, float]]:
    return label, safe, summarize(run_weights(p, w, rebalance_every=rebalance_every), w)


def aligned(signal: Callable[[Panel], np.ndarray], p: Panel, full: Panel) -> np.ndarray:
    """A signal on p's days, computed on the longer history so its averages are warm on p's first day."""
    a = full.days.index(p.days[0])
    return signal(full)[a:a + len(p.days)]


def exit_signal_table(p: Panel, full: Panel) -> List[Tuple[str, str, Dict[str, float]]]:
    rows = [_row(p, "buy and hold", "100% stocks", static(p, 1, 0, 0)),
            _row(p, "buy and hold", "100% gold", static(p, 0, 1, 0)),
            _row(p, "buy and hold", "100% fixed income", static(p, 0, 0, 1)),
            _row(p, "static mix, no rebalance", "1/3 each", static(p, 1 / 3, 1 / 3, 1 / 3)),
            _row(p, "static mix, no rebalance", "half stocks half gold", static(p, 0.5, 0.5, 0)),
            _row(p, "mix, rebalance 60d", "1/3 each", static(p, 1 / 3, 1 / 3, 1 / 3), 60),
            _row(p, "mix, rebalance 60d", "half stocks half gold", static(p, 0.5, 0.5, 0), 60)]
    for name, fn in EXIT_SIGNALS.items():
        on = aligned(fn, p, full)
        for safe_name, off in SAFE_MIXES.items():
            rows.append(_row(p, name, safe_name, switch(on, (1.0, 0.0, 0.0), off)))
    for name, fn in ROTATIONS.items():
        rows.append(_row(p, name, "gold (rotation)", switch(aligned(fn, p, full), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))))
    return rows


# ---------------------------------------------------------------- gold signals (event studies)

def forward_return(x: np.ndarray, h: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    out[:-h] = x[h:] / x[:-h] - 1
    return out


def bucket_study(signal: np.ndarray, fwd: np.ndarray, edges: List[float]) -> List[Tuple[str, int, float, float]]:
    """Forward-return stats per signal bucket: (label, n, mean, share positive)."""
    out = []
    bounds = [-np.inf] + edges + [np.inf]
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        mask = (signal >= lo) & (signal < hi) & ~np.isnan(fwd) & ~np.isnan(signal)
        n = int(mask.sum())
        label = f"{'' if lo == -np.inf else f'{lo:+.0%}'} .. {'' if hi == np.inf else f'{hi:+.0%}'}".strip()
        out.append((label, n, float(np.mean(fwd[mask])) if n else float("nan"),
                    float(np.mean(fwd[mask] > 0)) if n else float("nan")))
    return out


def gold18_bubble(p: Panel) -> np.ndarray:
    intrinsic = p.ounce * p.usd / 31.1034768 * 0.75
    return p.gold18 / intrinsic - 1


def gold_etf_premium(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """
    Daily premium of each gold fund's traded price over its redemption NAV, with the fund's
    forward returns. Gold funds come from adjust.gold_funds (identified by tracking 18k gold);
    |premium| > 30% is treated as a NAV/price unit mismatch rather than a real premium.
    """
    # Premium uses the day's VWAP (not the closing price, which goes stale on thin days), and
    # forward returns run from the next day's VWAP, i.e. a price someone could actually get.
    return pl.from_arrow(con.sql(
        """WITH x AS (
               SELECT p.ins_code, p.d, p.adj_vwap, d.value, d.trades,
                      (d.value / d.volume) / n.nav_redeem - 1 AS premium
               FROM price_adj p JOIN daily_prices d USING (ins_code, d) JOIN etf_nav n USING (ins_code, d)
               JOIN gold_funds USING (ins_code)
               WHERE d.trades > 0 AND d.volume > 0 AND n.nav_redeem > 0
                 AND abs(d.close / n.nav_redeem - 1) <= 0.30)
           SELECT *, lead(adj_vwap, 21) OVER w / lead(adj_vwap, 1) OVER w - 1 AS fwd20,
                     lead(adj_vwap, 61) OVER w / lead(adj_vwap, 1) OVER w - 1 AS fwd60,
                     lead(premium, 20) OVER w AS premium_20d_later
           FROM x WINDOW w AS (PARTITION BY ins_code ORDER BY d)"""
    ).to_arrow_table())


# Liquidity tiers for the cheapest-fund comparison (traded value in rial, trades per day).
LIQUIDITY_TIERS = {
    "all gold funds": (0.0, 0),
    "liquid (>=10B toman/day, >=100 trades)": (1e11, 100),
    "very large (>=100B toman/day)": (1e12, 100),
}


def cheapest_fund_spread(g: pl.DataFrame, horizon: str = "fwd60") -> pl.DataFrame:
    """
    Same-day comparison across gold funds: forward return of the fund with the lowest premium
    minus the average forward return of the other funds. Gold price moves cancel out, so this
    isolates whether a lower premium pays off. Days with fewer than 3 funds are skipped.
    """
    g = g.filter(pl.col(horizon).is_not_null())
    per_day = g.group_by("d").agg(
        pl.len().alias("n"),
        pl.col(horizon).sort_by("premium").first().alias("cheapest"),
        pl.col(horizon).mean().alias("all_mean"),
        pl.col("premium").min().alias("min_premium"),
        pl.col("premium").max().alias("max_premium"),
    ).filter(pl.col("n") >= 3)
    others_mean = (pl.col("all_mean") * pl.col("n") - pl.col("cheapest")) / (pl.col("n") - 1)
    return per_day.with_columns((pl.col("cheapest") - others_mean).alias("spread")).sort("d")


def cost_decomposition(p: Panel, signal: Callable[[Panel], np.ndarray], full: Panel) -> List[Tuple[str, float]]:
    """Runs one stocks/gold switching signal with frictions added one at a time."""
    w = switch(aligned(signal, p, full), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    out = []
    for label, slip, fees, locks in (("no costs, no queue locks", 0.0, False, False),
                                      ("+ fees", 0.0, True, False),
                                      ("+ slippage 0.3%", 0.003, True, False),
                                      ("+ market-wide queue locks (realistic)", 0.003, True, True)):
        m = panel_market(p)
        if not locks:
            m.can_buy[:] = True
            m.can_sell[:] = True
        cfg = backtest.BacktestConfig(start=p.days[0], end=p.days[-1], slippage=slip, min_trade_frac=0.0)
        if not fees:
            cfg.fees = {k: backtest.Fees(0.0, 0.0) for k in cfg.fees}
        out.append((label, backtest.run(weights_strategy(w), m, cfg).metrics()["cagr_usd"]))
    return out


# ---------------------------------------------------------------- crisis check

CRISES = [
    ("2018 sanctions / currency shock", date(2018, 4, 1), date(2018, 12, 31)),
    ("2020 bubble burst", date(2020, 8, 1), date(2021, 6, 30)),
    ("2022 unrest", date(2022, 9, 1), date(2023, 1, 31)),
    ("2024 Israel strikes", date(2024, 4, 1), date(2024, 11, 30)),
    ("2025 war", date(2025, 6, 1), date(2025, 8, 31)),
]


def crisis_table(p: Panel, risk_on: np.ndarray) -> List[Tuple[str, float, float, Optional[date]]]:
    """For each crisis window: TEDPIX change in USD, share of the window the signal was out of stocks, first exit date."""
    out = []
    for name, a, b in CRISES:
        idx = [i for i, d in enumerate(p.days) if a <= d <= b]
        if not idx:
            continue
        i0, i1 = idx[0], idx[-1]
        chg = (p.tedpix[i1] / p.usd[i1]) / (p.tedpix[i0] / p.usd[i0]) - 1
        out_share = float(np.mean(~risk_on[i0:i1 + 1]))
        first_exit = next((p.days[i] for i in range(max(i0 - 60, 1), i1 + 1) if not risk_on[i] and risk_on[i - 1]), None)
        out.append((name, chg, out_share, first_exit))
    return out


# ---------------------------------------------------------------- report

def _fmt_row(label: str, safe: str, r: Dict[str, float]) -> str:
    return (f"  {label:<26}{safe:<22}{r['cagr']:>8.1%}{r['cagr_usd']:>9.1%}{r['maxdd_usd']:>9.1%}"
            f"{r['sharpe_usd']:>8.2f}{r['switches']:>6}{r['in_stocks']:>8.0%}")


def report(con: duckdb.DuckDBPyConnection) -> None:
    full = load_panel(con)
    header = f"  {'signal':<26}{'off-stocks asset':<22}{'CAGR':>8}{'CAGR$':>9}{'MaxDD$':>9}{'Shrp$':>8}{'sells':>6}{'stocks':>8}"

    for title, (a, b) in (("DESIGN PERIOD", IN_SAMPLE), ("TEST PERIOD (unseen)", OUT_OF_SAMPLE)):
        p = full.slice(a, b)
        print(f"\n=== Stock exit signals, {title}: {p.days[0]} .. {p.days[-1]} ===")
        print(header)
        for label, safe, r in exit_signal_table(p, full):
            print(_fmt_row(label, safe, r))

    print("\n=== 18k gold bubble vs intrinsic (ounce x free USD) -> next 60 trading days of 18k gold ===")
    bub = gold18_bubble(full)
    fwd = forward_return(full.gold18 / full.usd, 60)  # in USD terms, so the dollar trend does not dominate
    for title, (a, b) in (("design", IN_SAMPLE), ("test", OUT_OF_SAMPLE)):
        mask = np.array([a <= d <= b for d in full.days])
        print(f"  {title}: bucket, days, mean fwd 60d return in USD, share positive")
        for label, n, mean, pos in bucket_study(np.where(mask, bub, np.nan), fwd, [-0.02, 0.02, 0.05]):
            print(f"    {label:<14}{n:>6}{mean:>9.1%}{pos:>8.0%}")

    print("\n=== Gold ETF premium over NAV -> next 20 / 60 trading days of the fund (gold funds, VWAP) ===")
    g = gold_etf_premium(con)
    for title, (a, b) in (("design", IN_SAMPLE), ("test", OUT_OF_SAMPLE)):
        s = g.filter((pl.col("d") >= a) & (pl.col("d") <= b))
        print(f"  {title}: premium bucket, fund-days, mean fwd20, mean fwd60, premium 20d later")
        for lo, hi in ((-1, 0.0), (0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 10)):
            b_ = s.filter((pl.col("premium") >= lo) & (pl.col("premium") < hi))
            if b_.height:
                print(f"    {lo:+.0%}..{hi:+.0%}".ljust(18) + f"{b_.height:>7}{b_['fwd20'].mean():>9.1%}"
                      f"{b_['fwd60'].mean():>9.1%}{b_['premium_20d_later'].mean():>9.1%}")

    print("\n=== Same day, lowest-premium gold fund minus the other gold funds (gold price moves cancel out) ===")
    print("  entry at next day's VWAP; only days with >= 3 funds in the tier (design period has too few funds)")
    # Funds whose Fipiran NAV had a data error in the previous 250 observations are a separate tier:
    # a stale or wrong NAV makes the premium look low, which is where the raw "edge" came from.
    recent_errors = goldfunds.load_nav(con).sort("ins_code", "d").with_columns(
        pl.col("artifact").cast(pl.Int32).rolling_sum(250, min_samples=1).over("ins_code").alias("recent_errors")
    ).select("ins_code", "d", "recent_errors")
    g_clean = g.join(recent_errors, on=["ins_code", "d"], how="left").filter(pl.col("recent_errors").fill_null(1) == 0)
    tiers = [(tier, g, v, n) for tier, (v, n) in LIQUIDITY_TIERS.items()]
    tiers.append(("liquid, clean NAV data only", g_clean, 1e11, 100))
    for tier, source, min_value, min_trades in tiers:
        gt = source.filter((pl.col("value") >= min_value) & (pl.col("trades") >= min_trades))
        for horizon in ("fwd20", "fwd60"):
            s = cheapest_fund_spread(gt, horizon).filter(pl.col("d") >= OUT_OF_SAMPLE[0])
            if s.height:
                print(f"  {tier:<42}{horizon}: days {s.height:>4}, mean spread {s['spread'].mean():+.2%}, "
                      f"cheapest wins {float((s['spread'] > 0).mean()):.0%}")

    print("\n=== Gold fund selection with cleaned NAV (liquid funds, test period, next 60 trading days, price) ===")
    r = goldfunds.rel_premium_test(con, OUT_OF_SAMPLE[0])
    if r:
        print(f"  furthest below its OWN normal premium vs others: days {r['days']}, price {r['price_spread']:+.2%} "
              f"(NAV part {r['nav_spread']:+.2%}, premium part {r['premium_part']:+.2%}), wins {r['wins']:.0%}")
    for label, days in goldfunds.RETURN_WINDOWS.items():
        m = goldfunds.nav_momentum_test(con, OUT_OF_SAMPLE[0], days)
        if m:
            print(f"  top third by past {label} NAV return vs rest: rebalances {m['rebalances']}, "
                  f"price {m['price_spread']:+.2%}, NAV {m['nav_spread']:+.2%}, wins {m['wins']:.0%}")

    print("\n=== Where the exit signal's edge goes (money flow 60d -> gold, test period, CAGR in USD) ===")
    for label, cagr in cost_decomposition(full.slice(*OUT_OF_SAMPLE), EXIT_SIGNALS["money flow 60d"], full):
        print(f"  {label:<40}{cagr:>8.1%}")
    strict = load_panel(con, closing_queues=True)
    print(f"  {'same, counting closing queues as locks':<40}{cost_decomposition(strict.slice(*OUT_OF_SAMPLE), EXIT_SIGNALS['money flow 60d'], strict)[-1][1]:>8.1%}")

    print("\n=== Past crises: TEDPIX change in USD, share of the window out of stocks, first exit ===")
    for name in ("trend 200d", "trend in USD 200d", "money flow 60d"):
        on = EXIT_SIGNALS[name](full)
        print(f"  {name}:")
        for crisis, chg, out_share, first_exit in crisis_table(full, on):
            print(f"    {crisis:<34}{chg:>8.1%}{out_share:>8.0%}   first exit {first_exit or '-'}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Phase 3 signal research report")
    ap.add_argument("--db", default=datastore.DEFAULT_DB_PATH)
    args = ap.parse_args()
    con = datastore.connect(args.db)
    try:
        report(con)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
