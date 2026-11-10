"""
Backtest engine with Tehran Stock Exchange rules (Phase 2).

A strategy is a function that, given the data up to and including day t, returns target
portfolio weights. The engine then:

- executes on day t+1 (never on the day the signal was computed: no look-ahead),
- fills at that day's VWAP (traded value / volume) plus slippage, because a retail order
  cannot be assumed to get the close or the open,
- refuses buys on buy-queue days, sells on sell-queue days and any trade on a day without
  trades; the order waits and is retried on the following days,
- caps each fill at a share of the day's traded value (thin names cannot absorb large orders),
- charges per-asset-class buy/sell fees,
- holds positions in adjusted units, so cash dividends and capital increases are included,
- optionally accrues interest on idle cash (e.g. a fixed-income fund yield).

Results are reported in rial and in free-market dollars, next to buy-and-hold benchmarks.
"""

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional

import duckdb
import numpy as np
import polars as pl

import adjust
import datastore

TRADING_DAYS_PER_YEAR = 240
TEDPIX = "32097828799138957"


@dataclass(frozen=True)
class Fees:
    buy: float
    sell: float


# Defaults as fractions of trade value. Rates change by regulation and broker; check yours
# and override via BacktestConfig.fees.
DEFAULT_FEES: Dict[str, Fees] = {
    "stock": Fees(buy=0.003712, sell=0.008812),        # sell includes 0.5% transfer tax
    "etf_equity": Fees(buy=0.00116, sell=0.001875),
    "etf_commodity": Fees(buy=0.0012, sell=0.0012),     # gold / silver / saffron funds
    "etf_fixed_income": Fees(buy=0.000375, sell=0.000375),
}


@dataclass
class BacktestConfig:
    start: date
    end: date
    capital: float = 1_000_000_000.0          # rial (100M toman)
    fees: Dict[str, Fees] = field(default_factory=lambda: dict(DEFAULT_FEES))
    slippage: float = 0.001                    # added to the buy price / taken off the sell price
    max_participation: float = 0.10            # max share of a day's traded value per fill
    cash_rate: float = 0.0                     # annual yield on idle cash (0.25 = 25%)
    min_trade_frac: float = 0.005              # ignore rebalances smaller than this share of equity


@dataclass
class MarketData:
    """Wide matrices (days x instruments) of adjusted prices and tradability, plus calendars."""
    days: List[date]
    codes: List[str]
    close: np.ndarray       # adjusted close; NaN before listing
    vwap: np.ndarray        # adjusted VWAP; NaN on days without trades
    value: np.ndarray       # traded value (rial)
    can_buy: np.ndarray     # bool
    can_sell: np.ndarray    # bool
    asset_class: List[str]
    tickers: List[str]
    usd: np.ndarray         # free-market USD (rial) per trading day, forward-filled
    tedpix: np.ndarray
    gold18: np.ndarray

    def col(self, code: str) -> int:
        return self.codes.index(code)


def load_market(con: duckdb.DuckDBPyConnection, codes: List[str], start: date, end: date) -> MarketData:
    days = [r[0] for r in con.execute(
        "SELECT d FROM index_daily WHERE index_code = ? AND d BETWEEN ? AND ? ORDER BY d", [TEDPIX, start, end]
    ).fetchall()]
    if not days:
        raise ValueError(f"no trading days between {start} and {end}")
    df = pl.from_arrow(con.sql(
        f"""SELECT ins_code, d, adj_close, adj_vwap, value, trades, buy_locked, sell_locked
            FROM price_adj WHERE d BETWEEN ? AND ? AND ins_code IN ({', '.join('?' for _ in codes)})""",
        params=[start, end, *codes],
    ).to_arrow_table())
    day_idx = pl.DataFrame({"d": days, "i": range(len(days))})
    col_idx = pl.DataFrame({"ins_code": codes, "j": range(len(codes))})
    df = df.join(day_idx, on="d").join(col_idx, on="ins_code")
    ii, jj = df["i"].to_numpy(), df["j"].to_numpy()
    shape = (len(days), len(codes))

    def matrix(column: str, fill: float, dtype=float) -> np.ndarray:
        m = np.full(shape, fill, dtype=dtype)
        m[ii, jj] = df[column].fill_null(fill).to_numpy().astype(dtype)
        return m

    close = _ffill(matrix("adj_close", np.nan))  # a day without a row (halt) keeps the last valuation
    vwap = matrix("adj_vwap", np.nan)
    value = matrix("value", 0.0)
    trades = matrix("trades", 0.0)
    buy_locked = matrix("buy_locked", False, bool)
    sell_locked = matrix("sell_locked", False, bool)
    traded = (trades > 0) & ~np.isnan(vwap)
    meta = dict(con.execute(
        f"SELECT ins_code, asset_class FROM instrument_class WHERE ins_code IN ({', '.join('?' for _ in codes)})", codes
    ).fetchall())
    tick = dict(con.execute(
        f"SELECT ins_code, ticker FROM instruments WHERE ins_code IN ({', '.join('?' for _ in codes)})", codes
    ).fetchall())
    return MarketData(
        days=days, codes=codes, close=close, vwap=vwap, value=np.nan_to_num(value),
        can_buy=traded & ~buy_locked, can_sell=traded & ~sell_locked,
        asset_class=[meta.get(c, "stock") for c in codes], tickers=[tick.get(c, c) for c in codes],
        usd=_series(con, "SELECT d, close FROM macro_daily WHERE series = 'usd_irr'", days),
        tedpix=_series(con, f"SELECT d, close FROM index_daily WHERE index_code = '{TEDPIX}'", days),
        gold18=_series(con, "SELECT d, close FROM macro_daily WHERE series = 'gold_18k_irr'", days),
    )


def _ffill(a: np.ndarray) -> np.ndarray:
    """Forward-fills NaNs down each column."""
    idx = np.where(~np.isnan(a), np.arange(a.shape[0])[:, None], 0)
    np.maximum.accumulate(idx, axis=0, out=idx)
    out = a[idx, np.arange(a.shape[1])]
    return out


def _series(con: duckdb.DuckDBPyConnection, sql: str, days: List[date]) -> np.ndarray:
    """Values on each trading day, carrying the last known value forward (TGJU also has non-trading days)."""
    rows = con.execute(f"SELECT * FROM ({sql}) WHERE d <= ? ORDER BY d", [days[-1]]).fetchall()
    out = np.full(len(days), np.nan)
    k, last = 0, np.nan
    for i, d in enumerate(days):
        while k < len(rows) and rows[k][0] <= d:
            last = rows[k][1]
            k += 1
        out[i] = last
    return out


# A strategy maps (day index, market data) -> target weights {ins_code: weight}; it may only
# read rows 0..t of the matrices. Returning None keeps the current targets.
Strategy = Callable[[int, MarketData], Optional[Dict[str, float]]]


@dataclass
class Trade:
    d: date
    ticker: str
    side: str
    value: float
    price: float
    fee: float


@dataclass
class Result:
    days: List[date]
    equity: np.ndarray
    cash: np.ndarray
    trades: List[Trade]
    blocked_days: int
    fees_paid: float
    market: MarketData
    config: BacktestConfig

    def metrics(self) -> Dict[str, float]:
        m = _curve_metrics(self.equity, self.days, self.config.cash_rate)
        usd_eq = self.equity / self.market.usd
        m_usd = _curve_metrics(usd_eq, self.days, 0.0)
        exposure = 1.0 - self.cash / self.equity
        return {
            **m,
            "cagr_usd": m_usd["cagr"],
            "total_return_usd": m_usd["total_return"],
            "max_drawdown_usd": m_usd["max_drawdown"],
            # With 30-50% inflation a rial Sharpe mostly measures inflation; the dollar one is meaningful.
            "sharpe_usd": m_usd["sharpe"],
            "avg_exposure": float(np.mean(exposure)),
            "trades": len(self.trades),
            "fees_paid_pct": self.fees_paid / self.config.capital,
            "blocked_order_days": self.blocked_days,
        }

    def benchmarks(self) -> Dict[str, Dict[str, float]]:
        out = {}
        for name, series in (("TEDPIX", self.market.tedpix), ("USD", self.market.usd), ("Gold 18k", self.market.gold18)):
            if np.isnan(series[0]):
                continue
            m = _curve_metrics(series, self.days, 0.0)
            m_usd = _curve_metrics(series / self.market.usd, self.days, 0.0)
            m["cagr_usd"], m["max_drawdown_usd"], m["sharpe_usd"] = m_usd["cagr"], m_usd["max_drawdown"], m_usd["sharpe"]
            out[name] = m
        return out


def _curve_metrics(curve: np.ndarray, days: List[date], rf_annual: float) -> Dict[str, float]:
    years = max((days[-1] - days[0]).days / 365.25, 1e-9)
    total = curve[-1] / curve[0] - 1
    rets = np.diff(curve) / curve[:-1]
    peak = np.maximum.accumulate(curve)
    rf_daily = (1 + rf_annual) ** (1 / TRADING_DAYS_PER_YEAR) - 1
    vol = float(np.std(rets) * math.sqrt(TRADING_DAYS_PER_YEAR)) if len(rets) > 1 else 0.0
    excess = float(np.mean(rets - rf_daily) * TRADING_DAYS_PER_YEAR) if len(rets) else 0.0
    return {
        "total_return": float(total),
        "cagr": float((1 + total) ** (1 / years) - 1) if total > -1 else -1.0,
        "max_drawdown": float(np.min(curve / peak - 1)),
        "volatility": vol,
        "sharpe": excess / vol if vol > 0 else 0.0,
    }


def run(strategy: Strategy, market: MarketData, config: BacktestConfig) -> Result:
    n_days, n_codes = market.close.shape
    units = np.zeros(n_codes)              # adjusted units held
    cash = config.capital
    targets: Optional[Dict[int, float]] = None
    equity_curve = np.zeros(n_days)
    cash_curve = np.zeros(n_days)
    trades: List[Trade] = []
    blocked = 0
    fees_paid = 0.0

    for t in range(n_days):
        price = market.close[t]
        if t > 0 and config.cash_rate:
            gap = (market.days[t] - market.days[t - 1]).days
            cash *= (1 + config.cash_rate) ** (gap / 365.25)

        # 1) Execute yesterday's targets at today's VWAP
        if targets is not None:
            equity = cash + np.nansum(units * price)
            pending = False
            involved = set(targets) | set(np.flatnonzero(units).tolist())
            # Sells first so their proceeds fund the buys
            order = sorted(involved, key=lambda j: targets.get(j, 0.0) * equity - units[j] * np.nan_to_num(price[j]))
            for j in order:
                cur_val = units[j] * price[j] if units[j] and not np.isnan(price[j]) else 0.0
                delta = targets.get(j, 0.0) * equity - cur_val
                if abs(delta) < config.min_trade_frac * equity:
                    continue
                side = "buy" if delta > 0 else "sell"
                allowed = market.can_buy[t, j] if side == "buy" else market.can_sell[t, j]
                if not allowed:
                    if not np.isnan(price[j]):   # not yet listed is waiting, not a queue/halt block
                        blocked += 1
                    pending = True
                    continue
                cap = config.max_participation * market.value[t, j]
                amount = min(abs(delta), cap)
                if amount < abs(delta):
                    pending = True
                fee_rate = getattr(config.fees[market.asset_class[j]], side)
                fill = market.vwap[t, j] * (1 + config.slippage if side == "buy" else 1 - config.slippage)
                if side == "buy":
                    amount = min(amount, cash / (1 + fee_rate))
                    if amount <= 0:
                        continue
                    fee = amount * fee_rate
                    units[j] += amount / fill
                    cash -= amount + fee
                else:
                    amount = min(amount, units[j] * fill)
                    fee = amount * fee_rate
                    units[j] -= amount / fill
                    if units[j] * fill < 1:      # rounding dust
                        units[j] = 0.0
                    cash += amount - fee
                fees_paid += fee
                trades.append(Trade(market.days[t], market.tickers[j], side, amount, fill, fee))
            if not pending:
                targets = None

        # 2) Mark to market at today's adjusted close
        equity_curve[t] = cash + np.nansum(units * price)
        cash_curve[t] = cash

        # 3) Ask the strategy for new targets using data up to today; they execute tomorrow
        new = strategy(t, market)
        if new is not None:
            targets = {market.col(c): w for c, w in new.items() if c in market.codes}
            total = sum(targets.values())
            if total > 1.0 + 1e-9:
                targets = {j: w / total for j, w in targets.items()}

    return Result(market.days, equity_curve, cash_curve, trades, blocked, fees_paid, market, config)


# ---------------------------------------------------------------- example strategies

def buy_and_hold(code: str) -> Strategy:
    """Invests everything in one instrument on the first day and holds it."""
    def strategy(t: int, market: MarketData) -> Optional[Dict[str, float]]:
        return {code: 1.0} if t == 0 else None
    return strategy


def print_report(result: Result, title: str) -> None:
    m = result.metrics()
    print(f"\n{title}  ({result.days[0]} .. {result.days[-1]}, {len(result.days)} trading days)")
    print(f"  {'':<14}{'CAGR rial':>11}{'CAGR USD':>10}{'MaxDD rial':>12}{'MaxDD USD':>11}{'Sharpe USD':>12}")
    rows = [("strategy", m)] + list(result.benchmarks().items())
    for name, r in rows:
        print(f"  {name:<14}{r['cagr']:>10.1%}{r['cagr_usd']:>10.1%}{r['max_drawdown']:>12.1%}"
              f"{r['max_drawdown_usd']:>11.1%}{r['sharpe_usd']:>12.2f}")
    print(f"  trades {m['trades']}, fees {m['fees_paid_pct']:.2%} of capital, "
          f"avg exposure {m['avg_exposure']:.0%}, order-days blocked by queues/no trades {m['blocked_order_days']}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Buy-and-hold backtest of one symbol (engine sanity check)")
    ap.add_argument("ticker", help="symbol as shown on TSETMC, e.g. عیار or فملی")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--db", default=datastore.DEFAULT_DB_PATH)
    args = ap.parse_args()

    con = datastore.connect(args.db)
    try:
        if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'price_adj'").fetchall()[0][0]:
            adjust.build(con)
        q = args.ticker.replace("ی", "ي").replace("ک", "ك")
        # An old code can share its ticker with the current one: take the one that traded last.
        row = con.execute(
            """SELECT c.ins_code FROM instrument_class c
               LEFT JOIN (SELECT ins_code, max(d) AS last_d FROM price_adj GROUP BY 1) p USING (ins_code)
               WHERE c.ticker = ? ORDER BY p.last_d DESC NULLS LAST""", [q]
        ).fetchall()
        if not row:
            print(f"symbol {args.ticker} not found")
            return 1
        code = row[0][0]
        cfg = BacktestConfig(start=date.fromisoformat(args.start), end=date.fromisoformat(args.end))
        market = load_market(con, [code], cfg.start, cfg.end)
        print_report(run(buy_and_hold(code), market, cfg), f"Buy and hold {args.ticker}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
