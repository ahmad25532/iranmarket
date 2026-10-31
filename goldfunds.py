"""
Gold fund metrics shared by the research report and the dashboard.

Two NAV sources exist and they do not always agree: TSETMC's live NAV (captured daily from
the snapshot) and Fipiran's end-of-day history. For some funds Fipiran's NAV sits a fixed ratio
away (wrong unit or a different NAV definition) and a few series contain jumps that are data
errors. So:

- NAV returns use the Fipiran series only (one source, no jump where the sources switch), and a
  daily NAV move more than ARTIFACT_GAP away from the other gold funds' median move that day is
  treated as a data error: that day's return is replaced by the median and the fund is flagged.
- The premium is judged against the fund's own history from the same source, which cancels any
  constant bias in that source's NAV: a fund that always trades 1% below NAV is not "cheap" today
  just because it is at -1%.
"""

from datetime import date
from typing import Any, Dict, List, Optional

import duckdb
import numpy as np
import polars as pl

ARTIFACT_GAP = 0.06          # daily NAV return minus gold's daily return
OWN_PREMIUM_WINDOW = 250     # trading observations used for a fund's normal premium
SOURCE_DISAGREE = 0.03       # TSETMC vs Fipiran premium gap that marks a fund's NAV as unreliable
RETURN_WINDOWS = {"1m": 30, "3m": 91, "6m": 182, "12m": 365}   # calendar days


def load_nav(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Fipiran NAV per gold fund and day with the cleaned daily return and an artifact flag."""
    nav = pl.from_arrow(con.sql("""
        WITH n AS (
            SELECT n.ins_code, n.d, n.nav_redeem AS nav,
                   lag(n.nav_redeem) OVER (PARTITION BY n.ins_code ORDER BY n.d) AS prev_nav,
                   lag(n.d) OVER (PARTITION BY n.ins_code ORDER BY n.d) AS prev_d
            FROM etf_nav n JOIN gold_funds USING (ins_code)
            WHERE n.source = 'fipiran' AND n.nav_redeem > 0),
        g AS (SELECT d, close FROM macro_daily WHERE series = 'gold_18k_irr' AND source IS DISTINCT FROM 'live')
        SELECT n.ins_code, n.d, n.nav, n.nav / n.prev_nav - 1 AS r,
               (SELECT close FROM g WHERE g.d <= n.d ORDER BY g.d DESC LIMIT 1)
             / (SELECT close FROM g WHERE g.d <= n.prev_d ORDER BY g.d DESC LIMIT 1) - 1 AS gold_r
        FROM n ORDER BY n.ins_code, n.d""").to_arrow_table())
    # Reference move for each day: the median NAV move of all gold funds that day (shared timing quirks,
    # like a day TGJU skipped, cancel out); 18k gold only when fewer than 3 funds reported.
    nav = nav.with_columns(
        pl.col("r").median().over("d").alias("peer_r"),
        pl.col("r").count().over("d").alias("peers"),
    ).with_columns(
        pl.when(pl.col("peers") >= 3).then(pl.col("peer_r")).otherwise(pl.col("gold_r")).alias("ref_r"),
    )
    return nav.with_columns(
        ((pl.col("r") - pl.col("ref_r")).abs() > ARTIFACT_GAP).fill_null(False).alias("artifact"),
    ).with_columns(
        pl.when(pl.col("artifact")).then(pl.col("ref_r")).otherwise(pl.col("r")).fill_null(0.0).alias("clean_r"),
    ).with_columns(
        (pl.col("clean_r") + 1).cum_prod().over("ins_code").alias("nav_index"),
    )


def load_premiums(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Daily VWAP premium over the same day's NAV, per source, on days the fund traded."""
    return pl.from_arrow(con.sql("""
        SELECT n.ins_code, n.d, n.source, (p.value / p.volume) / n.nav_redeem - 1 AS premium, p.value, p.trades
        FROM etf_nav n JOIN gold_funds USING (ins_code) JOIN daily_prices p USING (ins_code, d)
        WHERE p.trades > 0 AND p.volume > 0 AND n.nav_redeem > 0""").to_arrow_table())


def own_relative_premium(prem: pl.DataFrame) -> pl.DataFrame:
    """
    Fipiran-based premium minus the fund's own median premium over the previous
    OWN_PREMIUM_WINDOW observations (strictly past data, so it is usable as a signal).
    """
    f = prem.filter(pl.col("source") == "fipiran").sort("ins_code", "d")
    return f.with_columns(
        pl.col("premium").shift(1).rolling_median(OWN_PREMIUM_WINDOW, min_samples=60).over("ins_code").alias("normal_premium"),
    ).with_columns((pl.col("premium") - pl.col("normal_premium")).alias("rel_premium"))


def _return_at(nav: pl.DataFrame, as_of: date, days: int) -> Dict[str, Dict[str, Any]]:
    """Cleaned NAV return per fund from the last NAV on/before (as_of - days) to the last on/before as_of."""
    out: Dict[str, Dict[str, Any]] = {}
    for code, g in nav.group_by("ins_code"):
        code = code[0]
        end = g.filter(pl.col("d") <= as_of).tail(1)
        start = g.filter(pl.col("d") <= date.fromordinal(as_of.toordinal() - days)).tail(1)
        if end.height and start.height and (end["d"][0] - start["d"][0]).days >= days * 0.8:
            window = g.filter((pl.col("d") > start["d"][0]) & (pl.col("d") <= end["d"][0]))
            out[code] = {"ret": end["nav_index"][0] / start["nav_index"][0] - 1,
                         "artifacts": int(window["artifact"].sum())}
    return out


def fund_table(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    """Everything the dashboard's gold table shows, one row per gold fund that traded recently."""
    nav = load_nav(con)
    prem = load_premiums(con)
    rel = own_relative_premium(prem)
    last_market = con.execute("SELECT max(d) FROM daily_prices").fetchall()[0][0]
    nav_as_of = nav["d"].max() if nav.height else None

    meta = pl.from_arrow(con.sql("""
        WITH recent AS (
            SELECT p.ins_code, avg(p.value) AS avg_value, max(p.d) AS last_trade
            FROM daily_prices p JOIN gold_funds USING (ins_code)
            WHERE p.d >= (SELECT max(d) FROM daily_prices) - INTERVAL 30 DAY AND p.trades > 0
            GROUP BY 1)
        SELECT r.ins_code, c.ticker, c.name, r.avg_value, r.last_trade
        FROM recent r JOIN instrument_class c USING (ins_code)
        WHERE r.last_trade >= (SELECT max(d) FROM daily_prices) - INTERVAL 10 DAY""").to_arrow_table())

    returns = {k: _return_at(nav, nav_as_of, days) for k, days in RETURN_WINDOWS.items()} if nav_as_of else {}
    price_12m = dict(con.execute("""
        WITH p AS (SELECT ins_code, d, adj_close FROM price_adj JOIN gold_funds USING (ins_code) WHERE trades > 0)
        SELECT ins_code,
               arg_max(adj_close, d) / (SELECT arg_max(q.adj_close, q.d) FROM p q
                                        WHERE q.ins_code = p.ins_code AND q.d <= max(p.d) - INTERVAL 365 DAY) - 1
        FROM p GROUP BY ins_code""").fetchall())
    gold_12m = con.execute("""
        SELECT arg_max(close, d) / (SELECT arg_max(close, d) FROM macro_daily
                                    WHERE series = 'gold_18k_irr' AND source IS DISTINCT FROM 'live' AND d <= ? - INTERVAL 365 DAY) - 1
        FROM macro_daily WHERE series = 'gold_18k_irr' AND source IS DISTINCT FROM 'live' AND d <= ?""",
        [nav_as_of, nav_as_of]).fetchall()[0][0] if nav_as_of else None

    rows: List[Dict[str, Any]] = []
    for m in meta.iter_rows(named=True):
        code = m["ins_code"]
        today = prem.filter((pl.col("ins_code") == code) & (pl.col("source") == "tsetmc")).sort("d").tail(1)
        fip = rel.filter(pl.col("ins_code") == code).tail(1)
        row: Dict[str, Any] = {
            "ticker": m["ticker"], "name": m["name"], "avg_value": m["avg_value"], "last_trade": m["last_trade"],
            "liquid": (m["avg_value"] or 0) >= 1e11,
            "premium_today": today["premium"][0] if today.height else None,
            "premium_today_date": today["d"][0] if today.height else None,
            "premium_fipiran": fip["premium"][0] if fip.height else None,
            "normal_premium": fip["normal_premium"][0] if fip.height else None,
            "rel_premium": fip["rel_premium"][0] if fip.height else None,
            "price_12m": price_12m.get(code),
        }
        artifacts = 0
        for k in RETURN_WINDOWS:
            r = returns.get(k, {}).get(code)
            row[f"nav_{k}"] = r["ret"] if r else None
            artifacts = max(artifacts, r["artifacts"] if r else 0)
        row["nav_artifacts_12m"] = artifacts
        disagree = (row["premium_today"] is not None and row["premium_fipiran"] is not None
                    and abs(row["premium_today"] - row["premium_fipiran"]) > SOURCE_DISAGREE)
        row["nav_sources_disagree"] = bool(disagree)
        row["has_nav_history"] = row["nav_1m"] is not None
        row["reliable"] = artifacts == 0 and not disagree
        rows.append(row)

    # No ranking: none of premium, premium vs own normal, or past NAV return picked better funds out of
    # sample (docs/phase3_findings.md). Most liquid first, since liquidity is what actually differs.
    rows.sort(key=lambda r: -(r["avg_value"] or 0))
    return {"rows": rows, "nav_as_of": nav_as_of, "market_date": last_market, "gold18_12m": gold_12m}


# ---------------------------------------------------------------- research helpers

def wide(df: pl.DataFrame, value: str) -> pl.DataFrame:
    return df.pivot(on="ins_code", index="d", values=value).sort("d")


def rel_premium_test(con: duckdb.DuckDBPyConnection, start: date, horizon: int = 60,
                     min_value: float = 1e11) -> Optional[Dict[str, float]]:
    """
    Same-day comparison across liquid gold funds: the fund furthest below its own normal premium
    vs the others, forward price return from the next day's VWAP, split into the NAV part and the
    premium part.
    """
    rel = own_relative_premium(load_premiums(con)).filter(pl.col("rel_premium").is_not_null())
    px = pl.from_arrow(con.sql("""SELECT ins_code, d, adj_vwap, factor FROM price_adj JOIN gold_funds USING (ins_code)
                                  WHERE trades > 0 AND adj_vwap IS NOT NULL""").to_arrow_table())
    nav = load_nav(con).select("ins_code", "d", "nav_index")
    x = rel.join(px, on=["ins_code", "d"]).join(nav, on=["ins_code", "d"]).sort("ins_code", "d")
    x = x.with_columns(
        (pl.col("adj_vwap").shift(-horizon - 1).over("ins_code") / pl.col("adj_vwap").shift(-1).over("ins_code") - 1).alias("fwd_px"),
        (pl.col("nav_index").shift(-horizon - 1).over("ins_code") / pl.col("nav_index").shift(-1).over("ins_code") - 1).alias("fwd_nav"),
    ).filter((pl.col("d") >= start) & (pl.col("value") >= min_value) & pl.col("fwd_px").is_not_null() & pl.col("fwd_nav").is_not_null())
    per = x.group_by("d").agg(
        pl.len().alias("n"),
        pl.col("fwd_px").sort_by("rel_premium").first().alias("c_px"), pl.col("fwd_px").mean().alias("m_px"),
        pl.col("fwd_nav").sort_by("rel_premium").first().alias("c_nav"), pl.col("fwd_nav").mean().alias("m_nav"),
    ).filter(pl.col("n") >= 3)
    if not per.height:
        return None

    def spread(c: str, m: str) -> pl.Expr:
        return pl.col(c) - (pl.col(m) * pl.col("n") - pl.col(c)) / (pl.col("n") - 1)

    per = per.with_columns(spread("c_px", "m_px").alias("s_px"), spread("c_nav", "m_nav").alias("s_nav"))
    return {"days": per.height, "price_spread": per["s_px"].mean(), "nav_spread": per["s_nav"].mean(),
            "premium_part": per["s_px"].mean() - per["s_nav"].mean(), "wins": float((per["s_px"] > 0).mean())}


def nav_momentum_test(con: duckdb.DuckDBPyConnection, start: date, lookback_days: int, horizon: int = 60,
                      step: int = 20, min_value: float = 1e11) -> Optional[Dict[str, float]]:
    """Every `step` trading days: top third of liquid gold funds by past cleaned NAV return vs the rest, next `horizon` days."""
    nav = load_nav(con)
    px = pl.from_arrow(con.sql("""SELECT ins_code, d, adj_vwap, value FROM price_adj JOIN gold_funds USING (ins_code)
                                  WHERE trades > 0 AND adj_vwap IS NOT NULL""").to_arrow_table())
    N = wide(nav, "nav_index")
    P = wide(px, "adj_vwap").join(N.select("d"), on="d", how="right").sort("d")
    V = wide(px, "value").join(N.select("d"), on="d", how="right").sort("d")
    codes = [c for c in N.columns if c != "d"]
    days = N["d"].to_list()
    n_arr = N.select(codes).fill_null(strategy="forward").to_numpy()
    p_arr = P.select([c for c in codes if c in P.columns]).fill_null(strategy="forward").to_numpy() if all(c in P.columns for c in codes) else None
    if p_arr is None:
        P = P.with_columns([pl.lit(None, dtype=pl.Float64).alias(c) for c in codes if c not in P.columns])
        V = V.with_columns([pl.lit(None, dtype=pl.Float64).alias(c) for c in codes if c not in V.columns])
        p_arr = P.select(codes).fill_null(strategy="forward").to_numpy()
    v_arr = V.select(codes).to_numpy().astype(float)
    sp_nav, sp_px, wins = [], [], 0
    idx = [i for i, d in enumerate(days) if d >= start][::step]
    for t in idx:
        back = next((i for i in range(t, -1, -1) if (days[t] - days[i]).days >= lookback_days), None)
        if back is None or t + 1 + horizon >= len(days):
            continue
        liq = np.nanmean(v_arr[max(0, t - 20):t + 1], axis=0) >= min_value
        past = n_arr[t] / n_arr[back] - 1
        fn = n_arr[t + 1 + horizon] / n_arr[t + 1] - 1
        fp = p_arr[t + 1 + horizon] / p_arr[t + 1] - 1
        ok = liq & ~np.isnan(past) & ~np.isnan(fn) & ~np.isnan(fp)
        if ok.sum() < 4:
            continue
        k = max(1, ok.sum() // 3)
        order = np.argsort(-past[ok])
        a, b = fn[ok], fp[ok]
        sp_nav.append(a[order[:k]].mean() - a[order[k:]].mean())
        sp_px.append(b[order[:k]].mean() - b[order[k:]].mean())
        wins += sp_px[-1] > 0
    if not sp_px:
        return None
    return {"rebalances": len(sp_px), "nav_spread": float(np.mean(sp_nav)), "price_spread": float(np.mean(sp_px)),
            "wins": wins / len(sp_px)}
