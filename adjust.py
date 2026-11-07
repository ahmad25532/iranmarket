"""
Corporate-action adjustment and tradability flags (Phase 2).

TSETMC publishes, for every trading day, `priceYesterday`: the reference price the day's
limits are computed from. On an ex-dividend or capital-increase day it is lowered from the
previous close; on every other day it equals the previous close. So

    event_factor(t) = yesterday(t) / close(t-1)

captures cash dividends, bonus shares and rights issues in one number, and the backward
cumulative product of those factors turns raw prices into a total-return series (dividends
reinvested), which is what returns and backtests must use.

Tradability: on Iranian boards a stock that opens at its limit and never trades at any other
price is a locked queue. A whole day at a single price above yesterday is a buy queue (cannot
buy), below yesterday a sell queue (cannot sell). Days without trades cannot be traded at all.
"""

import duckdb

# A single-price day this far from yesterday's reference price is treated as a locked queue.
QUEUE_MIN_MOVE = 0.015
# Factors outside this range are data errors (an unrecorded unit change, a typo), not corporate actions.
FACTOR_MIN, FACTOR_MAX = 0.05, 20.0

BUILD_SQL = f"""
CREATE OR REPLACE TABLE price_adj AS
WITH p AS (
    SELECT *, lag(close) OVER (PARTITION BY ins_code ORDER BY d) AS prev_close
    FROM daily_prices
),
e AS (
    SELECT *,
        CASE WHEN prev_close > 0 AND yesterday > 0 AND yesterday <> prev_close
                  AND yesterday / prev_close BETWEEN {FACTOR_MIN} AND {FACTOR_MAX}
             THEN yesterday / prev_close ELSE 1.0 END AS event_factor,
        prev_close > 0 AND yesterday > 0
            AND NOT (yesterday / prev_close BETWEEN {FACTOR_MIN} AND {FACTOR_MAX}) AS bad_factor
    FROM p
),
c AS (
    SELECT *,
        coalesce(exp(sum(ln(event_factor)) OVER (
            PARTITION BY ins_code ORDER BY d ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING
        )), 1.0) AS factor
    FROM e
)
SELECT
    ins_code, d, factor, event_factor, bad_factor,
    open * factor  AS adj_open,
    high * factor  AS adj_high,
    low * factor   AS adj_low,
    close * factor AS adj_close,
    CASE WHEN volume > 0 AND value > 0 THEN value / volume * factor END AS adj_vwap,
    value,
    coalesce(trades, 0) AS trades,
    coalesce(trades, 0) > 0 AND high = low AND yesterday > 0 AND last > yesterday * (1 + {QUEUE_MIN_MOVE}) AS buy_locked,
    coalesce(trades, 0) > 0 AND high = low AND yesterday > 0 AND last < yesterday * (1 - {QUEUE_MIN_MOVE}) AS sell_locked
FROM c
"""

# TSETMC's market watch ignores its paperTypes filter and returns the same list for every type, so
# the type fetched last (8) overwrites paper_type on every live instrument. Shares are recognised by
# ISIN instead: IRO1 (Bourse), IRO3 (Fara Bourse), IRO7 (base market). Options and futures use other
# IRO prefixes. paper_type 1 or 2 only survives on rows that left the live list, all of them shares.
CLASS_SQL = """
CREATE OR REPLACE TABLE instrument_class AS
SELECT i.ins_code, i.ticker, i.name,
    CASE
        WHEN i.paper_type IN (1, 2) OR i.isin LIKE 'IRO1%' OR i.isin LIKE 'IRO3%' OR i.isin LIKE 'IRO7%' THEN 'stock'
        WHEN f.fund_type = 4 THEN 'etf_fixed_income'
        WHEN f.fund_type = 5 THEN 'etf_commodity'
        ELSE 'etf_equity'
    END AS asset_class
FROM instruments i LEFT JOIN fipiran_funds f USING (ins_code)
WHERE i.paper_type IN (1, 2, 8)
"""


# Fund names and Fipiran types do not reliably say what a fund holds (several gold funds are
# registered as generic "commodity" or not matched at all), so gold funds are identified by
# behaviour: over the last ~400 days their daily returns track 18k gold closely, with similar
# volatility. Silver funds correlate less (~0.5-0.7) and are more volatile.
GOLD_MIN_CORR = 0.76
GOLD_VOL_RATIO = (0.80, 1.15)
GOLD_SQL = f"""
CREATE OR REPLACE TABLE gold_funds AS
WITH g AS (
    SELECT d, close / lag(close) OVER (ORDER BY d) - 1 AS gr FROM macro_daily WHERE series = 'gold_18k_irr'
),
e AS (
    SELECT p.ins_code, p.d, p.adj_close / lag(p.adj_close) OVER (PARTITION BY p.ins_code ORDER BY p.d) - 1 AS r
    FROM price_adj p JOIN instruments i USING (ins_code)
    WHERE i.paper_type = 8 AND p.trades > 0 AND p.d >= (SELECT max(d) FROM price_adj) - INTERVAL 400 DAY
)
SELECT ins_code, count(*) AS days, corr(e.r, g.gr) AS gold_corr, stddev(e.r) / stddev(g.gr) AS vol_ratio
FROM e JOIN g USING (d)
GROUP BY ins_code
HAVING count(*) >= 60 AND corr(e.r, g.gr) >= {GOLD_MIN_CORR}
   AND stddev(e.r) / stddev(g.gr) BETWEEN {GOLD_VOL_RATIO[0]} AND {GOLD_VOL_RATIO[1]}
"""


def build(con: duckdb.DuckDBPyConnection) -> dict:
    """Rebuilds price_adj, instrument_class and gold_funds from the raw tables. Returns a short summary."""
    con.execute(BUILD_SQL)
    con.execute(CLASS_SQL)
    con.execute(GOLD_SQL)
    # Gold funds without a Fipiran match pay commodity-fund fees, not equity-fund fees.
    con.execute("UPDATE instrument_class SET asset_class = 'etf_commodity' WHERE ins_code IN (SELECT ins_code FROM gold_funds)")
    rows, events, bad, buy_q, sell_q = con.execute(
        """SELECT count(*), count(*) FILTER (WHERE event_factor <> 1), count(*) FILTER (WHERE bad_factor),
                  count(*) FILTER (WHERE buy_locked), count(*) FILTER (WHERE sell_locked) FROM price_adj"""
    ).fetchall()[0]
    return {"rows": rows, "events": events, "bad_factors": bad, "buy_queue_days": buy_q, "sell_queue_days": sell_q}


if __name__ == "__main__":
    import sys

    import datastore

    sys.stdout.reconfigure(encoding="utf-8")
    con = datastore.connect()
    print(build(con))
    con.close()
