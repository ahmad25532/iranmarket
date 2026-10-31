import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datastore  # noqa: E402
import quality  # noqa: E402
import sources  # noqa: E402

D = date(2026, 10, 28)


@pytest.fixture
def con():
    c = datastore.connect(":memory:")
    yield c
    c.close()


# ---------- parsers (fixtures copied from real API responses) ----------

def test_parse_deven():
    assert sources.parse_deven(20261028) == D
    assert sources.parse_deven(0) is None
    assert sources.parse_deven(None) is None


def test_is_normal_board():
    assert sources.is_normal_board("فملي")
    assert not sources.is_normal_board("فملي2")
    assert not sources.is_normal_board("فمليح")
    assert not sources.is_normal_board("")


def test_parse_watch_price_skips_untraded_rows():
    # ETF row seen mid-session: no trades today, pcl still shows the previous close.
    untraded = {"insCode": "34144395039913458", "ztt": 0.0, "pcl": 713062.0, "qtj": 0.0}
    assert sources.parse_watch_price(untraded, D) is None

    traded = {"insCode": "31879190587976736", "ztt": 163.0, "pcl": 17480.0, "pdv": 17490.0, "py": 18000.0,
              "pf": 18100.0, "pmx": 18100.0, "pmn": 17460.0, "qtj": 1265109.0, "qtc": 22111809080.0}
    row = sources.parse_watch_price(traded, D)
    assert row["close"] == 17480.0 and row["last"] == 17490.0 and row["trades"] == 163
    assert row["low"] <= row["close"] <= row["high"]


def test_parse_price_history():
    raw = {"priceMin": 711000.0, "priceMax": 715700.0, "priceYesterday": 713441.0, "priceFirst": 715200.0,
           "insCode": "34144395039913458", "dEven": 20261028, "pClosing": 713062.0, "pDrCotVal": 714100.0,
           "zTotTran": 50411.0, "qTotTran5J": 55157775.0, "qTotCap": 39330899377033.0}
    row = sources.parse_price_history(raw)
    assert row["d"] == D and row["close"] == 713062.0 and row["yesterday"] == 713441.0
    assert row["volume"] == 55157775.0 and row["trades"] == 50411
    assert sources.parse_price_history({**raw, "pClosing": 0}) is None


def test_parse_ct_history_and_snapshot_use_different_field_names():
    hist = sources.parse_ct_history({
        "recDate": 20261028, "insCode": "1", "buy_I_Volume": 10.0, "buy_N_Volume": 5.0, "buy_I_Value": 100.0,
        "buy_I_Count": 7.0, "sell_I_Count": 3, "sell_I_Volume": 12.0, "sell_N_Volume": 3.0,
    })
    assert hist["buy_i_count"] == 7 and hist["sell_i_count"] == 3 and hist["buy_i_value"] == 100.0
    snap = sources.parse_ct_snapshot({"insCode": "1", "buy_I_Volume": 10.0, "buy_CountI": 7, "sell_CountI": 3}, D)
    assert snap["buy_i_count"] == 7 and snap["sell_i_count"] == 3 and snap["buy_i_value"] is None


def test_parse_tgju_row():
    raw = ["2,701,950", "2,685,000", "2,715,100", "2,710,600",
           '<span class="high" dir="ltr">15600</span>', '<span class="high" dir="ltr">0.58%</span>',
           "2026/10/29", "1405/08/07"]
    row = sources.parse_tgju_row(raw, "usd_irr")
    assert row == {"series": "usd_irr", "d": date(2026, 10, 29), "open": 2701950.0,
                   "low": 2685000.0, "high": 2715100.0, "close": 2710600.0, "source": "history", "as_of": None}
    assert sources.parse_tgju_row(raw[:5], "usd_irr") is None


def test_parse_tgju_live():
    current = {
        "geram18": {"p": "252,562,000", "h": "262,256,000", "l": "251,662,000", "ts": "2026-10-31 17:50:50"},
        "price_dollar_rl": {"p": "2,704,650", "ts": "2026-10-31 17:50:50"},
        "ons": {"p": "3,875.84", "ts": "2026-10-31 01:50:16"},
        "sekee": {"p": "", "ts": "2026-10-31 17:50:50"},          # no price: skipped
    }
    rows = {r["series"]: r for r in sources.parse_tgju_live(current)}
    assert set(rows) == {"gold_18k_irr", "usd_irr", "gold_ounce_usd"}
    assert rows["gold_18k_irr"]["close"] == 252_562_000.0 and rows["gold_18k_irr"]["source"] == "live"
    assert rows["gold_ounce_usd"]["d"] == date(2026, 10, 31)


def test_live_macro_never_overwrites_closed_day(con):
    import collector
    from datetime import datetime
    datastore.upsert(con, "macro_daily", [{"series": "usd_irr", "d": D, "close": 100.0, "source": "history"}])
    live = [
        {"series": "usd_irr", "d": D, "close": 999.0, "source": "live", "as_of": datetime(2026, 10, 28, 12)},
        {"series": "usd_irr", "d": date(2026, 10, 31), "close": 101.0, "source": "live", "as_of": datetime(2026, 10, 31, 12)},
    ]
    collector.store_live_macro(con, live)
    rows = dict(con.execute("SELECT d, close FROM macro_daily ORDER BY d").fetchall())
    assert rows == {D: 100.0, date(2026, 10, 31): 101.0}
    # When TGJU later publishes the closed day, it replaces the live row
    datastore.upsert(con, "macro_daily", [{"series": "usd_irr", "d": date(2026, 10, 31), "close": 102.0, "source": "history"}])
    assert con.execute("SELECT close, source FROM macro_daily WHERE d = '2026-10-31'").fetchall() == [(102.0, "history")]


def test_parse_etf_nav():
    raw = {"insCode": "34144395039913458", "deven": 20261031, "hEven": 115810,
           "pRedTran": 709191.0, "pSubTran": 712415.0}
    row = sources.parse_etf_nav(raw, 714100.0)
    assert row["d"] == date(2026, 10, 31) and row["nav_redeem"] == 709191.0 and row["nav_time"] == 115810
    assert sources.parse_etf_nav({**raw, "pRedTran": 0}, None) is None


def test_market_state():
    assert not sources.is_market_closed({"marketState": "S", "marketStateTitle": "باز"})
    assert sources.is_market_closed({"marketState": "F", "marketStateTitle": "بسته"})


# ---------- storage ----------

def test_upsert_replaces_by_primary_key(con):
    row = {"ins_code": "1", "d": D, "close": 100.0, "source": "snapshot"}
    datastore.upsert(con, "daily_prices", [row])
    datastore.upsert(con, "daily_prices", [{**row, "close": 101.0}])
    assert con.execute("SELECT count(*), max(close) FROM daily_prices").fetchone() == (1, 101.0)


def test_snapshot_does_not_wipe_history_flow_values(con):
    hist = {"ins_code": "1", "d": D, "buy_i_volume": 10.0, "buy_i_value": 1000.0, "source": "history"}
    datastore.upsert(con, "client_flows", [hist])
    snap = {"ins_code": "1", "d": D, "buy_i_volume": 11.0, "buy_i_value": None, "source": "snapshot"}
    datastore.upsert(con, "client_flows", [snap], preserve_existing=["buy_i_value"])
    assert con.execute("SELECT buy_i_volume, buy_i_value FROM client_flows").fetchone() == (11.0, 1000.0)


# ---------- quality checks ----------

def test_snapshot_date_check():
    assert quality.check_snapshot_date(D, date(2026, 10, 29))[1] == "ok"
    assert quality.check_snapshot_date(D, date(2026, 11, 10))[1] == "warn"
    assert quality.check_snapshot_date(D, date(2026, 10, 1))[1] == "fail"


def test_price_sanity_flags_close_outside_range(con):
    datastore.upsert(con, "instruments", [{"ins_code": "1", "ticker": "الف"}, {"ins_code": "2", "ticker": "ب"}])
    datastore.upsert(con, "daily_prices", [
        # thin day: closing price stays at yesterday's level, outside the range, which is legitimate
        {"ins_code": "1", "d": D, "low": 90.0, "high": 110.0, "close": 85.0, "last": 100.0, "yesterday": 85.0},
        {"ins_code": "2", "d": D, "low": 90.0, "high": 110.0, "close": 100.0, "last": 150.0, "yesterday": 100.0},
    ])
    name, sev, detail = quality.check_price_sanity(con, D)
    assert sev == "warn" and "ب" in detail and detail.startswith("1 rows")


def test_flow_volume_consistency(con):
    datastore.upsert(con, "daily_prices", [{"ins_code": "1", "d": D, "close": 1.0, "volume": 100.0}])
    datastore.upsert(con, "client_flows", [{"ins_code": "1", "d": D, "buy_i_volume": 60.0, "buy_n_volume": 40.0}])
    assert quality.check_flow_volume_consistency(con, D)[1] == "ok"
    datastore.upsert(con, "client_flows", [{"ins_code": "1", "d": D, "buy_i_volume": 10.0, "buy_n_volume": 40.0}])
    assert quality.check_flow_volume_consistency(con, D)[1] == "warn"


def test_fetch_errors_fail_above_ten_percent(con):
    for i in range(8):
        datastore.log_fetch(con, "r", "s", str(i), "ok")
    datastore.log_fetch(con, "r", "s", "x", "error", error="HTTP 500")
    datastore.log_fetch(con, "r", "s", "y", "error", error="timeout")
    assert quality.check_fetch_errors(con, "r")[1] == "fail"
    assert quality.check_fetch_errors(con, "nothing")[1] == "fail"


def test_parse_fipiran_nav():
    row = sources.parse_fipiran_nav(
        {"date": "2026-10-27T00:00:00", "issueNav": 711317.0, "cancelNav": 708098.0, "statisticalNav": 708098.0},
        "34144395039913458",
    )
    assert row["d"] == date(2026, 10, 27) and row["nav_redeem"] == 708098.0 and row["nav_issue"] == 711317.0
    assert row["source"] == "fipiran"
    assert sources.parse_fipiran_nav({"date": "bad", "cancelNav": 1.0}, "1") is None


def test_keep_existing_does_not_overwrite_snapshot_nav(con):
    datastore.upsert(con, "etf_nav", [{"ins_code": "1", "d": D, "nav_redeem": 100.0, "source": "tsetmc"}])
    added = datastore.upsert(con, "etf_nav", [
        {"ins_code": "1", "d": D, "nav_redeem": 99.0, "source": "fipiran"},
        {"ins_code": "1", "d": date(2026, 10, 27), "nav_redeem": 98.0, "source": "fipiran"},
    ], keep_existing=True)
    assert added == 1
    assert con.execute("SELECT nav_redeem, source FROM etf_nav WHERE d = ?", [D]).fetchone() == (100.0, "tsetmc")


def test_find_gaps(con):
    days = [date(2026, 10, x) for x in (25, 26, 27, 28)]
    rows = [{"ins_code": str(i), "d": d, "close": 1.0} for d in days if d != date(2026, 10, 27) for i in range(10)]
    rows += [{"ins_code": "0", "d": date(2026, 10, 27), "close": 1.0}]  # thin day: 1 row vs 10
    datastore.upsert(con, "daily_prices", rows)
    assert quality.find_gaps(con, "daily_prices", days + [date(2026, 10, 29)]) == [date(2026, 10, 27), date(2026, 10, 29)]
    # A table that starts later is not reported as missing its whole past
    datastore.upsert(con, "etf_nav", [{"ins_code": "1", "d": date(2026, 10, 28), "nav_redeem": 1.0}])
    assert quality.find_gaps(con, "etf_nav", days) == []
    assert quality.find_gaps(con, "client_flows", days) == days


def test_compare_closes():
    assert quality.compare_closes(100.0, 100.4)
    assert not quality.compare_closes(100.0, 102.0)
