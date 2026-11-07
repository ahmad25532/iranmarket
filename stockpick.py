"""
Stock selection research: can any rule pick liquid Tehran stocks that beat simply owning them all?

The rules below were fixed before the test period was looked at, and selection is automatic:

1. Universe, re-decided at every rebalance from past data only: ordinary shares of the main
   boards (ISIN IRO1/IRO3, series 0001), at least 250 trading days of history, traded on at
   least 80% of the last 60 open-market days and in the last 5, no corrupt adjustment factor in
   the last 250 days, and among the UNIVERSE_SIZE highest by median traded value over those days.
2. Each factor buys the TOP_N best-ranked stocks, equal weight, and rebalances every 20 or 60
   trading days through backtest.run: next-day VWAP, stock fees, slippage, queues, volume cap.
3. Hurdle: net-of-cost return against the equal-weight universe held at no cost, measured over
   non-overlapping holding windows.
4. A factor passes the design period if its net excess is positive with t >= MIN_T and its gross
   top-minus-bottom quintile spread is positive; the better of its two horizons is the one kept.
5. The design passers, and one composite of them (mean rank), run unchanged on the test period.
   Only a rule with positive net excess and t >= MIN_T there counts as confirmed.

`today` prints a current list only for confirmed rules.

There is no fundamental data in the database (earnings, book value, shares outstanding), so value
and size factors are not tested. Only currently listed names exist, so results carry survivorship
bias, which flatters every rule and the universe alike.
"""

import argparse
import sys
import warnings
from dataclasses import dataclass, replace
from datetime import date
from itertools import groupby
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import duckdb
import numpy as np

import backtest
import datastore
import research

DESIGN = research.IN_SAMPLE
TEST = research.OUT_OF_SAMPLE
MIN_HISTORY = 250
UNIVERSE_SIZE = 150
MIN_UNIVERSE = 50
TOP_N = 20
HORIZONS = (20, 60)
MIN_T = 2.0
SLIPPAGE = 0.003          # same as the phase 3 tests
CAPITAL_USD = 50_000.0    # starting capital per test, so the volume cap bites equally in each period
RANDOM_SEEDS = 30

UNIVERSE_SQL = """
    SELECT ins_code FROM instruments
    WHERE (isin LIKE 'IRO1%0001' OR isin LIKE 'IRO3%0001') ORDER BY ins_code
"""


@dataclass
class Stocks:
    """Backtest market plus the matrices only the factors need. All are days x instruments."""
    market: backtest.MarketData
    bad: np.ndarray            # corrupt adjustment factor that day
    buy_locked: np.ndarray
    sell_locked: np.ndarray
    flow_net: np.ndarray       # individuals' net buying, rial
    buy_i_val: np.ndarray
    buy_i_cnt: np.ndarray
    sell_i_val: np.ndarray
    sell_i_cnt: np.ndarray


def chain_levels(rows: Iterable[Tuple[date, float, float, float, float, int]]) -> List[Tuple[date, float, Optional[float]]]:
    """
    Total-return level of one stock from its raw rows (d, close, yesterday, value, volume, trades),
    oldest first. Returns (d, level, level at the day's VWAP or None).

    price_adj cannot be used for shares. Before the first trade the board shows a 1000-rial
    placeholder, so the first trade looks like a jump of up to 18000%. A price reset during a halt
    can appear in the close of a day without trades, and a large reset is dropped as a bad factor;
    either way it reads as a fall of up to 99% that nobody could have traded. So only a day with
    trades changes the level, by close / that day's reference price, and the series starts at the
    first trade.
    """
    out: List[Tuple[date, float, Optional[float]]] = []
    level, prev_close = None, None
    for d, close, yesterday, value, volume, trades in rows:
        traded = bool(trades) and bool(value) and bool(volume) and bool(close) and close > 0
        if traded:
            if level is None:
                level = float(close)
            else:
                ref = yesterday if yesterday and yesterday > 0 else prev_close
                level *= close / ref
            prev_close = close
        if level is not None:
            out.append((d, level, level * (value / volume) / close if traded else None))
    return out


def _repaired_prices(con: duckdb.DuckDBPyConnection, m: backtest.MarketData) -> Tuple[np.ndarray, np.ndarray]:
    i_of = {d: i for i, d in enumerate(m.days)}
    close = np.full(m.close.shape, np.nan)
    vwap = np.full(m.close.shape, np.nan)
    wanted = set(m.codes)
    rows = con.execute(
        "SELECT ins_code, d, close, yesterday, value, volume, trades FROM daily_prices WHERE d <= ? ORDER BY ins_code, d", [m.days[-1]]
    ).fetchall()
    for j_code, group in groupby(rows, key=lambda r: r[0]):
        if j_code not in wanted:
            continue
        j = m.col(j_code)
        for d, level, at_vwap in chain_levels(r[1:] for r in group):
            i = i_of.get(d)
            if i is not None:
                close[i, j] = level
                if at_vwap is not None:
                    vwap[i, j] = at_vwap
    return backtest._ffill(close), vwap


def load(con: duckdb.DuckDBPyConnection, start: date = date(2013, 1, 1), end: date = date(2100, 1, 1)) -> Stocks:
    codes = [r[0] for r in con.execute(UNIVERSE_SQL).fetchall()]
    m = backtest.load_market(con, codes, start, end)
    m.close, m.vwap = _repaired_prices(con, m)
    i_of = {d: i for i, d in enumerate(m.days)}
    j_of = {c: j for j, c in enumerate(codes)}
    shape = m.close.shape
    bad = np.zeros(shape, bool)
    buy_locked = np.zeros(shape, bool)
    sell_locked = np.zeros(shape, bool)
    for code, d, b, bl, sl in con.execute(
        "SELECT ins_code, d, bad_factor, buy_locked, sell_locked FROM price_adj WHERE d BETWEEN ? AND ?", [m.days[0], m.days[-1]]
    ).fetchall():
        i, j = i_of.get(d), j_of.get(code)
        if i is not None and j is not None:
            bad[i, j], buy_locked[i, j], sell_locked[i, j] = bool(b), bool(bl), bool(sl)
    flow_net, buy_v, buy_c, sell_v, sell_c = (np.full(shape, np.nan) for _ in range(5))
    # Snapshot days have volumes and counts but no values; volume at the day's average price stands in.
    for code, d, fn, bv, bc, sv, sc in con.execute(
        """SELECT f.ins_code, f.d,
                  coalesce(f.buy_i_value - f.sell_i_value, (f.buy_i_volume - f.sell_i_volume) * p.value / nullif(p.volume, 0)),
                  coalesce(f.buy_i_value, f.buy_i_volume * p.value / nullif(p.volume, 0)), f.buy_i_count,
                  coalesce(f.sell_i_value, f.sell_i_volume * p.value / nullif(p.volume, 0)), f.sell_i_count
           FROM client_flows f JOIN daily_prices p USING (ins_code, d) WHERE f.d BETWEEN ? AND ?""", [m.days[0], m.days[-1]]
    ).fetchall():
        i, j = i_of.get(d), j_of.get(code)
        if i is not None and j is not None:
            flow_net[i, j], buy_v[i, j], buy_c[i, j], sell_v[i, j], sell_c[i, j] = fn, bv, bc, sv, sc
    return Stocks(m, bad, buy_locked, sell_locked, flow_net, buy_v, buy_c, sell_v, sell_c)


# ---------------------------------------------------------------- universe and factors (rows <= t only)

def _open_day_values(value: np.ndarray, t: int, n: int = 60) -> np.ndarray:
    """Traded value on the last n days up to t when at least a fifth of the busiest day's names traded."""
    recent = value[max(0, t - 3 * n):t + 1]
    counts = (recent > 0).sum(axis=1)
    open_days = counts >= max(1, 0.2 * counts.max())
    return recent[open_days][-n:]


def universe(s: Stocks, t: int) -> np.ndarray:
    """Boolean mask of instruments eligible at the close of day t."""
    n = s.market.close.shape[1]
    if t + 1 < MIN_HISTORY:
        return np.zeros(n, bool)
    close = s.market.close
    # Activity is measured on days the market was open. The whole market was shut for about two
    # months in 2026, and counting those days would leave no eligible stock for months afterwards.
    value = _open_day_values(s.market.value, t)
    listed = ~np.isnan(close[t - MIN_HISTORY + 1])
    traded = value > 0
    active = traded.mean(axis=0) >= 0.8
    recent = traded[-5:].any(axis=0)
    clean = ~s.bad[t - MIN_HISTORY + 1:t + 1].any(axis=0)
    ok = listed & active & recent & clean
    if ok.sum() <= UNIVERSE_SIZE:
        return ok
    median_value = np.median(value, axis=0)
    out = np.zeros(n, bool)
    idx = np.flatnonzero(ok)
    out[idx[np.argsort(-median_value[idx], kind="stable")[:UNIVERSE_SIZE]]] = True
    return out


def _momentum(s: Stocks, t: int, long: int, skip: int) -> np.ndarray:
    c = s.market.close
    return c[t - skip] / c[t - long] - 1


def _low_vol(s: Stocks, t: int) -> np.ndarray:
    c = s.market.close[t - 120:t + 1]
    return -np.nanstd(c[1:] / c[:-1] - 1, axis=0)


def _near_high(s: Stocks, t: int) -> np.ndarray:
    return s.market.close[t] / np.nanmax(s.market.close[t - 240:t + 1], axis=0)


def _flow(s: Stocks, t: int, n: int) -> np.ndarray:
    flow = np.nansum(s.flow_net[t - n + 1:t + 1], axis=0)
    return flow / np.where(s.market.value[t - n + 1:t + 1].sum(axis=0) > 0, s.market.value[t - n + 1:t + 1].sum(axis=0), np.nan)


def _per_capita(s: Stocks, t: int, n: int) -> np.ndarray:
    """Average individual buyer's ticket over the average individual seller's ("buyer power")."""
    a = t - n + 1
    sum_ = lambda x: np.nansum(x[a:t + 1], axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (sum_(s.buy_i_val) / sum_(s.buy_i_cnt)) / (sum_(s.sell_i_val) / sum_(s.sell_i_cnt))


def _illiquidity(s: Stocks, t: int) -> np.ndarray:
    c = s.market.close[t - 60:t + 1]
    value = s.market.value[t - 59:t + 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.abs(c[1:] / c[:-1] - 1) / np.where(value > 0, value, np.nan)
    return np.nanmean(ratio, axis=0)


def _queues(s: Stocks, t: int) -> np.ndarray:
    return (s.buy_locked[t - 19:t + 1].sum(axis=0) - s.sell_locked[t - 19:t + 1].sum(axis=0)).astype(float)


# Each returns a score per instrument at day t; higher means "buy".
FACTORS: Dict[str, Callable[[Stocks, int], np.ndarray]] = {
    "momentum 12-1": lambda s, t: _momentum(s, t, 240, 20),
    "momentum 6-1": lambda s, t: _momentum(s, t, 120, 20),
    "reversal 20d": lambda s, t: -_momentum(s, t, 20, 0),
    "low volatility": _low_vol,
    "near 1y high": _near_high,
    "individual flow 20d": lambda s, t: _flow(s, t, 20),
    "individual flow 60d": lambda s, t: _flow(s, t, 60),
    "buyer power 20d": lambda s, t: _per_capita(s, t, 20),
    "illiquidity": _illiquidity,
    "net queue days 20d": _queues,
}


def composite(names: List[str]) -> Callable[[Stocks, int], np.ndarray]:
    """Mean of the factors' ranks inside the universe; a stock missing any factor gets no score."""
    def score(s: Stocks, t: int) -> np.ndarray:
        mask = universe(s, t)
        total = np.zeros(mask.size)
        for name in names:
            x = np.where(mask, FACTORS[name](s, t), np.nan)
            ok = ~np.isnan(x)
            ranks = np.full(mask.size, np.nan)
            ranks[ok] = np.argsort(np.argsort(x[ok], kind="stable"), kind="stable")
            total += ranks
        return total
    return score


def select(s: Stocks, t: int, score: Callable[[Stocks, int], np.ndarray], n: int = TOP_N) -> List[int]:
    """Column indices of the n best-scored eligible stocks at the close of day t."""
    mask = universe(s, t)
    if mask.sum() < MIN_UNIVERSE:
        return []
    with np.errstate(divide="ignore", invalid="ignore"):
        x = np.where(mask, score(s, t), np.nan)
    idx = np.flatnonzero(np.isfinite(x))
    return idx[np.argsort(-x[idx], kind="stable")[:n]].tolist()


# ---------------------------------------------------------------- evaluation

@dataclass
class Evaluation:
    name: str
    horizon: int
    cagr: float
    cagr_usd: float
    max_dd_usd: float
    universe_cagr: float
    excess: float            # strategy minus universe, annualised rial CAGR
    t: float                 # on non-overlapping window excess returns
    win: float               # share of windows ahead of the universe
    fees_per_year: float     # share of capital
    blocked: int
    spread: float            # gross top-minus-bottom quintile return per window
    spread_t: float
    ic: float                # mean rank correlation of score with the next window's return


def window(s: Stocks, period: Tuple[date, date]) -> Tuple[int, int]:
    """Row range of a period, starting no earlier than the first day that has a full universe."""
    days = s.market.days
    idx = [i for i, d in enumerate(days) if period[0] <= d <= period[1]]
    a = idx[0]
    while a <= idx[-1] and universe(s, a).sum() < MIN_UNIVERSE:
        a += 1
    return a, idx[-1] + 1


def slice_market(m: backtest.MarketData, a: int, b: int) -> backtest.MarketData:
    arrays = {f: getattr(m, f)[a:b] for f in ("close", "vwap", "value", "can_buy", "can_sell", "usd", "tedpix", "gold18")}
    return replace(m, days=m.days[a:b], **arrays)


def run_rule(s: Stocks, score, a: int, b: int, horizon: int) -> backtest.Result:
    rows = list(range(a, b, horizon))
    targets = {}
    for t in rows:
        chosen = select(s, t, score)
        if chosen:
            targets[t - a] = {s.market.codes[j]: 1.0 / len(chosen) for j in chosen}
    m = slice_market(s.market, a, b)
    cfg = backtest.BacktestConfig(start=m.days[0], end=m.days[-1], slippage=SLIPPAGE, capital=CAPITAL_USD * m.usd[0])
    return backtest.run(lambda t, _: targets.get(t), m, cfg)


def universe_curve(s: Stocks, a: int, b: int, horizon: int) -> np.ndarray:
    """Equal-weight universe, re-formed every window with no cost: the hurdle every rule has to clear."""
    close = s.market.close
    curve = np.ones(b - a)
    members = universe(s, a)
    for t in range(a + 1, b):
        with np.errstate(divide="ignore", invalid="ignore"):
            r = close[t, members] / close[t - 1, members] - 1
        curve[t - a] = curve[t - a - 1] * (1 + (float(np.nanmean(r)) if members.any() else 0.0))
        if (t - a) % horizon == 0:
            members = universe(s, t)
    return curve


def _cagr(curve: np.ndarray, days: List[date]) -> float:
    return float((curve[-1] / curve[0]) ** (365.25 / max((days[-1] - days[0]).days, 1)) - 1)


def quintile_spread(s: Stocks, score, a: int, b: int, horizon: int) -> Tuple[float, float, float]:
    """
    Gross top-minus-bottom quintile return from t+1 to t+1+horizon, non-overlapping. Names that
    could not be bought on t+1 are left out of the buying side, as an order would not have filled.
    """
    price = backtest._ffill(np.where(np.isnan(s.market.vwap), s.market.close, s.market.vwap))
    spreads, ics = [], []
    for t in range(a, b - horizon - 1, horizon):
        mask = universe(s, t)
        if mask.sum() < MIN_UNIVERSE:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            x = np.where(mask, score(s, t), np.nan)
            fwd = price[t + 1 + horizon] / price[t + 1] - 1
        idx = np.flatnonzero(np.isfinite(x) & np.isfinite(fwd))
        if len(idx) < MIN_UNIVERSE:
            continue
        order = idx[np.argsort(x[idx], kind="stable")]
        q = len(order) // 5
        top = order[-q:]
        top = top[s.market.can_buy[t + 1, top]]
        if len(top) == 0:
            continue
        spreads.append(fwd[top].mean() - fwd[order[:q]].mean())
        rx = np.argsort(np.argsort(x[idx]))
        ry = np.argsort(np.argsort(fwd[idx]))
        ics.append(float(np.corrcoef(rx, ry)[0, 1]))
    if len(spreads) < 2:
        return 0.0, 0.0, 0.0
    arr = np.array(spreads)
    return float(arr.mean()), float(arr.mean() / arr.std(ddof=1) * np.sqrt(len(arr))), float(np.mean(ics))


def evaluate(s: Stocks, name: str, score, period: Tuple[date, date], horizon: int) -> Evaluation:
    a, b = window(s, period)
    result = run_rule(s, score, a, b, horizon)
    bench = universe_curve(s, a, b, horizon)
    days = result.days
    marks = np.arange(0, len(days), horizon)
    strat_r = result.equity[marks[1:]] / result.equity[marks[:-1]] - 1
    bench_r = bench[marks[1:]] / bench[marks[:-1]] - 1
    diff = strat_r - bench_r
    t_stat = float(diff.mean() / diff.std(ddof=1) * np.sqrt(len(diff))) if len(diff) > 1 and diff.std(ddof=1) > 0 else 0.0
    m = result.metrics()
    spread, spread_t, ic = quintile_spread(s, score, a, b, horizon)
    bench_cagr = _cagr(bench, days)
    return Evaluation(name, horizon, m["cagr"], m["cagr_usd"], m["max_drawdown_usd"], bench_cagr, m["cagr"] - bench_cagr,
                      t_stat, float((diff > 0).mean()), annual_fee_drag(result), m["blocked_order_days"], spread, spread_t, ic)


def annual_fee_drag(result: backtest.Result) -> float:
    """Fees per year as a share of average equity (a share of starting capital misleads when equity grows 20-fold)."""
    years = (result.days[-1] - result.days[0]).days / 365.25
    return result.fees_paid / float(np.mean(result.equity)) / years


def random_control(s: Stocks, period: Tuple[date, date], horizon: int, seeds: int = RANDOM_SEEDS) -> Tuple[float, float, float]:
    """
    Rial CAGR (5th, 50th, 95th percentile) of TOP_N stocks picked at random through the same engine.
    It pays the same frictions as a rule, so it separates "no skill" from "costs and queues".
    It is shown for context and decides nothing.
    """
    a, b = window(s, period)
    n = s.market.close.shape[1]
    out = []
    for seed in range(seeds):
        def score(_: Stocks, t: int, seed: int = seed) -> np.ndarray:
            return np.random.default_rng([seed, t]).random(n)
        result = run_rule(s, score, a, b, horizon)
        out.append(result.metrics()["cagr"])
    low, mid, high = np.percentile(out, [5, 50, 95])
    return float(low), float(mid), float(high)


def passes_design(e: Evaluation) -> bool:
    return e.excess > 0 and e.t >= MIN_T and e.spread > 0


def confirmed(e: Evaluation) -> bool:
    return e.excess > 0 and e.t >= MIN_T


def benchmark_row(s: Stocks, period: Tuple[date, date]) -> Dict[str, Tuple[float, float, float]]:
    """Name -> (rial CAGR, USD CAGR, USD max drawdown) for what the money could have done instead."""
    a, b = window(s, period)
    m = s.market
    days, usd = m.days[a:b], m.usd[a:b]
    curves = {
        "equal-weight universe (no cost)": universe_curve(s, a, b, HORIZONS[0]),
        "TEDPIX (price index)": m.tedpix[a:b],
        "18k gold": m.gold18[a:b],
    }
    curves["50% universe / 50% gold, static"] = 0.5 * curves["equal-weight universe (no cost)"] + 0.5 * curves["18k gold"] / curves["18k gold"][0]
    out = {}
    for name, curve in curves.items():
        in_usd = curve / usd
        out[name] = (_cagr(curve, days), _cagr(in_usd, days), float(np.min(in_usd / np.maximum.accumulate(in_usd) - 1)))
    return out


# ---------------------------------------------------------------- report

def _print_table(rows: List[Evaluation]) -> None:
    print(f"  {'rule':<34}{'hold':>5}{'CAGR':>8}{'USD':>8}{'DD USD':>8}{'excess':>8}{'t':>6}{'win':>6}{'fee/y':>7}{'block':>7}{'Q5-Q1':>8}{'t':>6}{'IC':>7}")
    for e in rows:
        print(f"  {e.name:<34}{e.horizon:>5}{e.cagr:>8.1%}{e.cagr_usd:>8.1%}{e.max_dd_usd:>8.0%}{e.excess:>8.1%}{e.t:>6.1f}"
              f"{e.win:>6.0%}{e.fees_per_year:>7.1%}{e.blocked:>7}{e.spread:>8.1%}{e.spread_t:>6.1f}{e.ic:>7.3f}")


def _print_benchmarks(s: Stocks, period: Tuple[date, date]) -> None:
    print(f"  {'benchmark':<34}{'CAGR':>8}{'USD':>8}{'DD USD':>8}")
    for name, (cagr, usd, dd) in benchmark_row(s, period).items():
        print(f"  {name:<34}{cagr:>8.1%}{usd:>8.1%}{dd:>8.0%}")
    for h in HORIZONS:
        low, mid, high = random_control(s, period, h)
        print(f"  random {TOP_N} stocks, hold {h}: CAGR median {mid:.1%}, 5-95% range {low:.1%} to {high:.1%} ({RANDOM_SEEDS} runs)")


def study(s: Stocks) -> Dict[str, object]:
    """Runs the whole protocol. The test period only ever sees what the design period selected."""
    a, b = window(s, DESIGN)
    print(f"DESIGN {s.market.days[a]} .. {s.market.days[b - 1]}, top {TOP_N} of up to {UNIVERSE_SIZE}, net of costs vs the equal-weight universe")
    _print_benchmarks(s, DESIGN)
    design = [evaluate(s, name, score, DESIGN, h) for name, score in FACTORS.items() for h in HORIZONS]
    _print_table(design)

    chosen: Dict[str, Evaluation] = {}
    for e in design:
        if passes_design(e) and (e.name not in chosen or e.t > chosen[e.name].t):
            chosen[e.name] = e
    print(f"\nPassed design (excess > 0, t >= {MIN_T}, gross spread > 0): {', '.join(chosen) or 'none'}")
    print(f"Trials: {len(design)}; a Bonferroni bar at 5% would be about t >= 3.0.")

    rules: Dict[str, Tuple[Callable, int]] = {n: (FACTORS[n], e.horizon) for n, e in chosen.items()}
    if len(chosen) > 1:
        horizon = max(set(e.horizon for e in chosen.values()), key=lambda h: sum(e.horizon == h for e in chosen.values()))
        rules["composite of passers"] = (composite(list(chosen)), horizon)

    a, b = window(s, TEST)
    print(f"\nTEST {s.market.days[a]} .. {s.market.days[b - 1]}")
    _print_benchmarks(s, TEST)
    test = [evaluate(s, n, score, TEST, h) for n, (score, h) in rules.items()]
    if test:
        _print_table(test)
    good = [e for e in test if confirmed(e)]
    print(f"\nConfirmed on the test period (excess > 0, t >= {MIN_T}): {', '.join(e.name for e in good) or 'none'}")

    # Shown for context only. These never decide anything: reading them to pick a rule would turn
    # the test period into a second design period.
    print("\nAll factors on the test period (context, not used for selection)")
    _print_table([evaluate(s, name, score, TEST, h) for name, score in FACTORS.items() for h in HORIZONS])
    return {"design": design, "test": test, "confirmed": good, "rules": rules}


def today(s: Stocks) -> int:
    out = study(s)
    good: List[Evaluation] = out["confirmed"]
    print("\nCurrent list")
    if not good:
        print("  No rule was confirmed, so no list is produced.")
        return 0
    t = len(s.market.days) - 1
    for e in good:
        score, _ = out["rules"][e.name]
        print(f"  {e.name} on {s.market.days[t]}: " + ", ".join(s.market.tickers[j] for j in select(s, t, score)))
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore", category=RuntimeWarning)   # columns of not-yet-listed stocks are all NaN
    ap = argparse.ArgumentParser(description="Out-of-sample test of stock selection rules")
    ap.add_argument("command", nargs="?", choices=("study", "today"), default="study")
    ap.add_argument("--db", default=datastore.DEFAULT_DB_PATH)
    args = ap.parse_args()
    con = duckdb.connect(args.db, read_only=True)
    try:
        s = load(con)
    finally:
        con.close()
    if args.command == "today":
        return today(s)
    study(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
