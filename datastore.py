"""
DuckDB storage for Iran Market AI.

All tables keep raw (unadjusted) values exactly as published, keyed by date, so that
later phases can rebuild adjusted series, indicators and backtests from one source.
"""

import os
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

import duckdb
import polars as pl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "market.duckdb")

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    ins_code     VARCHAR PRIMARY KEY,
    ticker       VARCHAR,
    name         VARCHAR,
    isin         VARCHAR,
    paper_type   INTEGER,
    sector_code  VARCHAR,
    first_seen   DATE,
    last_seen    DATE
);

-- One row per instrument per trading day. close = pClosing (weighted closing price),
-- last = pDrCotVal (last trade), yesterday = priceYesterday (used for corporate-action adjustment).
CREATE TABLE IF NOT EXISTS daily_prices (
    ins_code   VARCHAR,
    d          DATE,
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    last       DOUBLE,
    yesterday  DOUBLE,
    volume     DOUBLE,
    value      DOUBLE,
    trades     BIGINT,
    source     VARCHAR,   -- 'history' (official daily list) or 'snapshot' (end-of-day market watch)
    PRIMARY KEY (ins_code, d)
);

-- Individual (I) vs institutional (N) flows. Snapshot rows have volumes/counts only;
-- *_value columns are filled from the official history endpoint.
CREATE TABLE IF NOT EXISTS client_flows (
    ins_code      VARCHAR,
    d             DATE,
    buy_i_volume  DOUBLE, buy_n_volume  DOUBLE,
    sell_i_volume DOUBLE, sell_n_volume DOUBLE,
    buy_i_count   BIGINT, buy_n_count   BIGINT,
    sell_i_count  BIGINT, sell_n_count  BIGINT,
    buy_i_value   DOUBLE, buy_n_value   DOUBLE,
    sell_i_value  DOUBLE, sell_n_value  DOUBLE,
    source        VARCHAR,
    PRIMARY KEY (ins_code, d)
);

-- ETF NAV per unit (rial). 'tsetmc' rows are the daily snapshot; 'fipiran' rows are the
-- official end-of-day history and only fill days the snapshot missed.
CREATE TABLE IF NOT EXISTS etf_nav (
    ins_code     VARCHAR,
    d            DATE,
    nav_redeem   DOUBLE,
    nav_issue    DOUBLE,
    nav_time     INTEGER,  -- hEven of the published NAV (HHMMSS)
    close        DOUBLE,
    PRIMARY KEY (ins_code, d)
);
ALTER TABLE etf_nav ADD COLUMN IF NOT EXISTS source VARCHAR;

-- Maps exchange-traded funds (TSETMC insCode) to their Fipiran registration number.
CREATE TABLE IF NOT EXISTS fipiran_funds (
    ins_code   VARCHAR PRIMARY KEY,
    reg_no     VARCHAR,
    name       VARCHAR,
    fund_type  INTEGER
);

CREATE TABLE IF NOT EXISTS indices (
    index_code  VARCHAR PRIMARY KEY,
    name        VARCHAR
);

CREATE TABLE IF NOT EXISTS index_daily (
    index_code  VARCHAR,
    d           DATE,
    close       DOUBLE,
    low         DOUBLE,
    high        DOUBLE,
    PRIMARY KEY (index_code, d)
);

CREATE TABLE IF NOT EXISTS market_overview (
    market        INTEGER,   -- 1 = Bourse, 2 = FaraBourse
    d             DATE,
    index_value   DOUBLE,
    index_change  DOUBLE,
    eqw_value     DOUBLE,
    eqw_change    DOUBLE,
    trade_value   DOUBLE,
    trade_volume  DOUBLE,
    trades        BIGINT,
    market_value  DOUBLE,
    PRIMARY KEY (market, d)
);

-- Free-market macro series from TGJU (prices in rial, ounce in USD).
CREATE TABLE IF NOT EXISTS macro_daily (
    series  VARCHAR,
    d       DATE,
    open    DOUBLE,
    low     DOUBLE,
    high    DOUBLE,
    close   DOUBLE,
    PRIMARY KEY (series, d)
);
-- 'history' = TGJU's closed-day table; 'live' = intraday price (as_of = its timestamp), replaced
-- by the history row once TGJU publishes the day.
ALTER TABLE macro_daily ADD COLUMN IF NOT EXISTS source VARCHAR;
ALTER TABLE macro_daily ADD COLUMN IF NOT EXISTS as_of TIMESTAMP;

-- Global gold drivers (see globalgold.py): futures, DXY, GVZ and yields from Yahoo, the 10y real
-- yield from the US Treasury. Keyed by the exchange's own date.
CREATE TABLE IF NOT EXISTS global_daily (
    series  VARCHAR,
    d       DATE,
    close   DOUBLE,
    PRIMARY KEY (series, d)
);

-- CFTC disaggregated COT, COMEX gold. Positions are as of report_d (Tuesday) and public from released_d.
CREATE TABLE IF NOT EXISTS cot_gold (
    report_d       DATE PRIMARY KEY,
    released_d     DATE,
    open_interest  DOUBLE,
    mm_long  DOUBLE, mm_short  DOUBLE,   -- managed money (funds, CTAs)
    pm_long  DOUBLE, pm_short  DOUBLE,   -- producers / merchants
    sw_long  DOUBLE, sw_short  DOUBLE    -- swap dealers (bullion banks)
);

-- Monthly price indexes and inflation. A value covers the month ending period_end and is public from
-- released_d. Iranian months are Jalali, so their period_end falls around the 20th-22nd.
CREATE TABLE IF NOT EXISTS monthly_macro (
    series      VARCHAR,
    period_end  DATE,
    value       DOUBLE,
    released_d  DATE,
    PRIMARY KEY (series, period_end)
);

-- One row per market date for which an end-of-day snapshot was taken (final = market was closed).
CREATE TABLE IF NOT EXISTS snapshots (
    d         DATE PRIMARY KEY,
    taken_at  TIMESTAMP,
    final     BOOLEAN
);

-- Trading days already re-filled from official history. A day that is still thin afterwards
-- (e.g. a near-empty session) is genuine, so it is not re-fetched on every run.
CREATE TABLE IF NOT EXISTS filled_days (
    d         DATE,
    kind      VARCHAR,   -- 'prices_flows' | 'nav'
    filled_at TIMESTAMP,
    PRIMARY KEY (d, kind)
);

CREATE TABLE IF NOT EXISTS backfill_status (
    ins_code     VARCHAR,
    kind         VARCHAR,   -- 'prices' | 'flows' | 'nav'
    rows         INTEGER,
    completed_at TIMESTAMP,
    PRIMARY KEY (ins_code, kind)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    run_id      VARCHAR,
    logged_at   TIMESTAMP DEFAULT current_timestamp,
    source      VARCHAR,
    target      VARCHAR,
    status      VARCHAR,   -- 'ok' | 'error'
    rows        INTEGER,
    error       VARCHAR,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS quality_checks (
    run_id   VARCHAR,
    logged_at TIMESTAMP DEFAULT current_timestamp,
    d        DATE,
    check_name VARCHAR,
    severity VARCHAR,     -- 'ok' | 'warn' | 'fail'
    detail   VARCHAR
);
"""

TABLE_KEYS = {
    "instruments": ["ins_code"],
    "daily_prices": ["ins_code", "d"],
    "client_flows": ["ins_code", "d"],
    "etf_nav": ["ins_code", "d"],
    "fipiran_funds": ["ins_code"],
    "indices": ["index_code"],
    "index_daily": ["index_code", "d"],
    "market_overview": ["market", "d"],
    "macro_daily": ["series", "d"],
    "global_daily": ["series", "d"],
    "cot_gold": ["report_d"],
    "monthly_macro": ["series", "period_end"],
    "backfill_status": ["ins_code", "kind"],
    "snapshots": ["d"],
    "filled_days": ["d", "kind"],
}


def connect(db_path: str = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = duckdb.connect(db_path)
    con.execute(SCHEMA)
    return con


def upsert(
    con: duckdb.DuckDBPyConnection,
    table: str,
    rows: Iterable[Dict[str, Any]],
    preserve_existing: Optional[List[str]] = None,
    keep_existing: bool = False,
) -> int:
    """
    Inserts rows, replacing any existing row with the same primary key.
    Columns listed in preserve_existing keep their stored value when the new value is NULL
    (e.g. a snapshot must not wipe flow values that came from the official history).
    keep_existing=True only inserts missing keys and returns how many were added.
    """
    rows = list(rows)
    if not rows:
        return 0
    cols = [c[0] for c in con.execute(f"DESCRIBE {table}").fetchall()]
    df = pl.DataFrame([{c: r.get(c) for c in cols} for r in rows], infer_schema_length=None)
    keys = TABLE_KEYS[table]
    df = df.unique(subset=keys, keep="last")
    con.register("_incoming", df.to_arrow())
    try:
        if keep_existing:
            col_list = ", ".join(cols)
            before = con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]
            con.execute(f"INSERT OR IGNORE INTO {table} ({col_list}) SELECT {col_list} FROM _incoming")
            return con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0] - before
        if preserve_existing:
            sel = ", ".join(
                f"COALESCE(i.{c}, t.{c}) AS {c}" if c in preserve_existing else f"i.{c}" for c in cols
            )
            join = " AND ".join(f"i.{k} = t.{k}" for k in keys)
            con.execute(
                f"CREATE OR REPLACE TEMP TABLE _merged AS SELECT {sel} FROM _incoming i LEFT JOIN {table} t ON {join}"
            )
            con.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM _merged")
            con.execute("DROP TABLE _merged")
        else:
            col_list = ", ".join(cols)
            con.execute(f"INSERT OR REPLACE INTO {table} ({col_list}) SELECT {col_list} FROM _incoming")
    finally:
        con.unregister("_incoming")
    return df.height


def log_fetch(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    source: str,
    target: str,
    status: str,
    rows: int = 0,
    error: Optional[str] = None,
    duration_ms: int = 0,
) -> None:
    con.execute(
        "INSERT INTO fetch_log (run_id, source, target, status, rows, error, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [run_id, source, target, status, rows, (error or "")[:500], duration_ms],
    )


def record_check(
    con: duckdb.DuckDBPyConnection, run_id: str, d: Optional[date], name: str, severity: str, detail: str
) -> None:
    con.execute(
        "INSERT INTO quality_checks (run_id, d, check_name, severity, detail) VALUES (?, ?, ?, ?, ?)",
        [run_id, d, name, severity, detail],
    )
