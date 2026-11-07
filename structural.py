"""
Tests of two ideas that need no market forecast, with the same discipline as the other research:
parameters are chosen on the design period and judged unchanged on the later period.

1. Gold fund switching: hold one liquid gold fund, and move to another when the held fund's premium
   over its own normal exceeds the other fund's by a threshold. Exposure to gold stays 100%, so only
   the difference between funds matters. Benchmark: the funds usable on the first day, bought and
   held in equal weight at no cost. Liquid funds with a clean NAV history are recent, so the range
   with at least 3 of them is split in half into design and test.
   Passes if net excess is positive with t >= 2 on the design period, and again on the test period.

2. Rebalancing bands for a 50/50 stocks and gold portfolio: trade only when the weight has drifted
   by a band, against never trading and trading on a calendar. Uses the phase 3 periods and proxies.
"""

import sys
import warnings
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

import duckdb
import numpy as np
import polars as pl

import backtest
import datastore
import goldfunds
import research

THRESHOLDS = (0.005, 0.01, 0.02, 0.03)
MIN_VALUE = 1e11             # rial per day, the dashboard's "liquid"
MIN_TRADES = 100
SWITCH_FEE = 2 * 0.0012      # commodity fund, sell plus buy
SWITCH_SLIPPAGE = 2 * 0.001  # both legs, against the day's average price
MIN_T = 2.0
WINDOW = 20


@dataclass
class FundPanel:
    days: List[date]
    codes: List[str]
    price: np.ndarray        # adjusted VWAP, forward-filled for marking
    traded: np.ndarray       # the fund has a VWAP that day
    rel: np.ndarray          # premium minus the fund's own past median; NaN when unknown
    usable: np.ndarray       # liquid and without NAV errors in the last 250 observations


def load_funds(con: duckdb.DuckDBPyConnection) -> FundPanel:
    rel = goldfunds.own_relative_premium(goldfunds.load_premiums(con))
    errors = goldfunds.load_nav(con).sort("ins_code", "d").with_columns(
        pl.col("artifact").cast(pl.Int32).rolling_sum(250, min_samples=1).over("ins_code").alias("recent_errors")
    ).select("ins_code", "d", "recent_errors")
    # Liquidity over 20 observations: filtering on one day's value favours days of unusual demand, when
    # the premium is high, and made a benchmark built on it look about 10 points a year too weak.
    x = rel.join(errors, on=["ins_code", "d"], how="left").sort("ins_code", "d").with_columns(
        pl.col("value").rolling_mean(20, min_samples=10).over("ins_code").alias("avg_value"),
        pl.col("trades").rolling_mean(20, min_samples=10).over("ins_code").alias("avg_trades"),
    )
    px = pl.from_arrow(con.sql(
        "SELECT ins_code, d, adj_vwap FROM price_adj JOIN gold_funds USING (ins_code) WHERE trades > 0 AND adj_vwap IS NOT NULL"
    ).to_arrow_table())
    days = sorted(set(px["d"].to_list()))
    codes = sorted(set(px["ins_code"].to_list()))
    i_of = {d: i for i, d in enumerate(days)}
    j_of = {c: j for j, c in enumerate(codes)}
    shape = (len(days), len(codes))
    vwap, rel_m = np.full(shape, np.nan), np.full(shape, np.nan)
    usable = np.zeros(shape, bool)
    for r in px.iter_rows(named=True):
        vwap[i_of[r["d"]], j_of[r["ins_code"]]] = r["adj_vwap"]
    for r in x.iter_rows(named=True):
        i, j = i_of.get(r["d"]), j_of.get(r["ins_code"])
        if i is None or j is None or r["rel_premium"] is None:
            continue
        rel_m[i, j] = r["rel_premium"]
        usable[i, j] = ((r["avg_value"] or 0) >= MIN_VALUE and (r["avg_trades"] or 0) >= MIN_TRADES
                        and (r["recent_errors"] or 0) == 0)
    return FundPanel(days, codes, backtest._ffill(vwap), ~np.isnan(vwap), rel_m, usable)


def simulate_switching(p: FundPanel, a: int, b: int, threshold: float, lag: int = 1,
                       cost: float = SWITCH_FEE + SWITCH_SLIPPAGE) -> Tuple[np.ndarray, int]:
    """
    Value of a single-fund holding over rows a..b-1, and the number of switches. A signal from the
    close of day t fills at day t+lag's average price, when both funds traded that day.
    """
    curve = np.ones(b - a)
    start = np.flatnonzero(p.usable[a])
    if not len(start):
        raise ValueError("no usable fund on the first day")
    held = int(start[0])
    pending: Optional[Tuple[int, int]] = None      # (fill row, target column)
    switches = 0
    for t in range(a + 1, b):
        curve[t - a] = curve[t - a - 1] * p.price[t, held] / p.price[t - 1, held]
        if pending and t >= pending[0]:
            target = pending[1]
            if p.traded[t, held] and p.traded[t, target]:
                curve[t - a] *= 1 - cost
                held, pending = target, None
                switches += 1
        if pending is None and p.usable[t, held]:
            cands = np.flatnonzero(p.usable[t])
            cands = cands[cands != held]
            if len(cands):
                best = int(cands[np.argmin(p.rel[t, cands])])
                if p.rel[t, held] - p.rel[t, best] >= threshold:
                    pending = (t + lag, best)
    return curve, switches


def hold_funds(p: FundPanel, a: int, b: int) -> np.ndarray:
    """The funds usable on the first day, bought in equal weight and held at no cost."""
    members = p.usable[a]
    return (p.price[a:b, members] / p.price[a, members]).mean(axis=1)


def excess_stats(curve: np.ndarray, bench: np.ndarray, days: List[date]) -> Dict[str, float]:
    marks = np.arange(0, len(curve), WINDOW)
    diff = curve[marks[1:]] / curve[marks[:-1]] - bench[marks[1:]] / bench[marks[:-1]]
    years = max((days[-1] - days[0]).days / 365.25, 1e-9)
    excess = (curve[-1] / curve[0]) ** (1 / years) - (bench[-1] / bench[0]) ** (1 / years)
    t = float(diff.mean() / diff.std(ddof=1) * np.sqrt(len(diff))) if len(diff) > 1 and diff.std(ddof=1) > 0 else 0.0
    return {"excess": float(excess), "t": t, "win": float((diff > 0).mean())}


def split_rows(p: FundPanel) -> Dict[str, Tuple[int, int]]:
    """Design and test rows: the range from the first to the last day with 3 usable funds, cut in half."""
    enough = np.flatnonzero(p.usable.sum(axis=1) >= 3)
    if not len(enough):
        raise ValueError("fewer than 3 usable funds on every day")
    first, end = int(enough[0]), int(enough[-1]) + 1
    middle = (first + end) // 2
    return {"design": (first, middle), "test": (middle, end)}


def switching_report(con: duckdb.DuckDBPyConnection) -> None:
    p = load_funds(con)
    print("=== Gold fund switching, net of 0.44% per switch, against the starting funds held at no cost ===")
    chosen: Optional[float] = None
    best_t = -np.inf
    for label, (a, b) in split_rows(p).items():
        days = p.days[a:b]
        bench = hold_funds(p, a, b)
        print(f"{label} {days[0]} .. {days[-1]}, usable funds {int(p.usable[a].sum())} to {int(p.usable[b - 1].sum())}")
        print(f"  {'threshold':>9}{'lag':>5}{'switches':>10}{'excess/y':>10}{'t':>7}{'win':>7}{'before costs':>14}")
        for thr in THRESHOLDS:
            for lag in (1, 2):
                curve, n = simulate_switching(p, a, b, thr, lag)
                s = excess_stats(curve, bench, days)
                gross = excess_stats(simulate_switching(p, a, b, thr, lag, cost=0.0)[0], bench, days)
                print(f"  {thr:>9.1%}{lag:>5}{n:>10}{s['excess']:>10.1%}{s['t']:>7.1f}{s['win']:>7.0%}{gross['excess']:>14.1%}")
                if label == "design" and lag == 1 and s["excess"] > 0 and s["t"] >= MIN_T and s["t"] > best_t:
                    chosen, best_t = thr, s["t"]
        if label == "design":
            print(f"  passed design at lag 1: {'threshold ' + format(chosen, '.1%') if chosen else 'none'}")
        elif chosen:
            s = excess_stats(simulate_switching(p, a, b, chosen)[0], bench, days)
            ok = s["excess"] > 0 and s["t"] >= MIN_T
            print(f"  threshold {chosen:.1%} on the test period: {'confirmed' if ok else 'not confirmed'}")
        else:
            print("  shown for context only: nothing passed the design period")


# ---------------------------------------------------------------- rebalancing bands

def band_strategy(band: float, weight: float = 0.5) -> backtest.Strategy:
    """Goes to the target on day 0, then again whenever the stock weight has drifted by `band`."""
    base: Dict[str, Optional[np.ndarray]] = {"prices": None}

    def strategy(t: int, m: backtest.MarketData) -> Optional[Dict[str, float]]:
        target = {"STOCKS": weight, "GOLD": 1 - weight}
        if base["prices"] is None:
            base["prices"] = m.close[t, :2].copy()
            return target
        grown = m.close[t, :2] / base["prices"]
        now = weight * grown[0] / (weight * grown[0] + (1 - weight) * grown[1])
        if abs(now - weight) >= band:
            base["prices"] = m.close[t, :2].copy()
            return target
        return None
    return strategy


def run_band(p: research.Panel, band: float) -> backtest.Result:
    cfg = backtest.BacktestConfig(start=p.days[0], end=p.days[-1], slippage=0.003, min_trade_frac=0.0)
    return backtest.run(band_strategy(band), research.panel_market(p), cfg)


def band_report(con: duckdb.DuckDBPyConnection) -> None:
    print("\n=== 50/50 stocks and gold: when to rebalance ===")
    full = research.load_panel(con)
    for label, period in (("design", research.IN_SAMPLE), ("test", research.OUT_OF_SAMPLE)):
        p = full.slice(*period)
        print(f"{label} {p.days[0]} .. {p.days[-1]}")
        print(f"  {'rule':<22}{'CAGR':>8}{'USD':>8}{'DD USD':>9}{'Sharpe':>8}{'trades':>8}{'fees':>8}")
        runs = [("never", research.run_weights(p, research.static(p, .5, .5, 0)))]
        runs += [(f"every {n} days", research.run_weights(p, research.static(p, .5, .5, 0), rebalance_every=n)) for n in (60, 240)]
        runs += [(f"band {b * 100:.0f} points", run_band(p, b)) for b in (0.05, 0.10, 0.15, 0.20)]
        for name, r in runs:
            m = r.metrics()
            print(f"  {name:<22}{m['cagr']:>8.1%}{m['cagr_usd']:>8.1%}{m['max_drawdown_usd']:>9.1%}{m['sharpe_usd']:>8.2f}{m['trades']:>8}{m['fees_paid_pct']:>8.2%}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    con = duckdb.connect(datastore.DEFAULT_DB_PATH, read_only=True)
    try:
        switching_report(con)
        band_report(con)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
