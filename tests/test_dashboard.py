import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402
import datastore  # noqa: E402

D = date(2026, 10, 28)


@pytest.fixture
def con():
    c = datastore.connect(":memory:")
    datastore.upsert(c, "instruments", [
        {"ins_code": "g1", "ticker": "عيار"},
        {"ins_code": "g2", "ticker": "زر"},
        {"ins_code": "g3", "ticker": "كوچك"},
        {"ins_code": "s1", "ticker": "فملي"},
    ])
    c.execute("""CREATE TABLE instrument_class AS SELECT ins_code, ticker, ticker AS name,
                 CASE WHEN ins_code = 's1' THEN 'stock' ELSE 'etf_commodity' END AS asset_class FROM instruments""")
    c.execute("CREATE TABLE gold_funds AS SELECT ins_code FROM instruments WHERE ins_code LIKE 'g%'")
    rows = []
    # g1: large, +1% premium; g2: liquid, -1% premium; g3: thin, -3% premium; s1: a stock
    for code, close, value, trades in (("g1", 101.0, 2e12, 5000), ("g2", 99.0, 2e11, 500), ("g3", 97.0, 1e9, 5), ("s1", 1000.0, 1e11, 300)):
        rows.append({"ins_code": code, "d": D, "close": close, "last": close, "volume": value / close,
                     "value": value, "trades": trades})
    datastore.upsert(c, "daily_prices", rows)
    datastore.upsert(c, "etf_nav", [{"ins_code": g, "d": D, "nav_redeem": 100.0} for g in ("g1", "g2", "g3")])
    datastore.upsert(c, "macro_daily", [
        {"series": "usd_irr", "d": D, "close": 1_000_000.0},
        {"series": "gold_18k_irr", "d": D, "close": 50_000_000.0},
        {"series": "gold_ounce_usd", "d": D, "close": 2000.0},
        {"series": "gold_ounce_usd", "d": date(2026, 10, 30), "close": 2100.0},   # weekend ounce, no USD that day
    ])
    yield c
    c.close()


def test_bubble_uses_same_day_prices(con):
    m = dashboard.market_snapshot(con)
    assert m["bubble_date"] == D                                  # not the weekend ounce
    intrinsic = 2000 * 1_000_000 / 31.1034768 * 0.75
    assert m["gold18_bubble"] == pytest.approx(50_000_000 / intrinsic - 1)


def test_live_prices_override_older_stored_rows(con):
    from datetime import datetime
    live = [
        {"series": "gold_18k_irr", "d": date(2026, 10, 31), "close": 48_000_000.0, "as_of": datetime(2026, 10, 31, 17, 50)},
        {"series": "usd_irr", "d": date(2026, 10, 31), "close": 1_000_000.0, "as_of": datetime(2026, 10, 31, 17, 50)},
        {"series": "gold_ounce_usd", "d": date(2026, 10, 31), "close": 2000.0, "as_of": datetime(2026, 10, 31, 1, 50)},
        {"series": "coin_emami_irr", "d": date(2026, 10, 20), "close": 1.0, "as_of": datetime(2026, 10, 20, 12, 0)},  # older than stored? none stored -> used
    ]
    m = dashboard.market_snapshot(con, live)
    assert m["gold18"]["live"] and m["gold18"]["close"] == 48_000_000.0
    assert m["bubble_live"]
    intrinsic = 2000 * 1_000_000 / 31.1034768 * 0.75
    assert m["gold18_bubble"] == pytest.approx(48_000_000 / intrinsic - 1)
    # A stale live quote never replaces a newer stored close
    stale = [{"series": "gold_18k_irr", "d": date(2026, 10, 1), "close": 1.0, "as_of": datetime(2026, 10, 1)}]
    m2 = dashboard.market_snapshot(con, stale)
    assert m2["gold18"]["close"] == 50_000_000.0 and "live" not in m2["gold18"]
    assert not m2.get("bubble_live")


def test_portfolio_valuation_and_gaps(con):
    pf = dashboard.validate_portfolio(con, {
        "holdings": [{"ticker": "عیار", "qty": 100}, {"ticker": "فملی", "qty": 10}],   # Persian ی/ک are normalized
        "manual": {"cash_toman": 1000},
        "target": {"stocks": 50, "gold": 50},
    })
    v = dashboard.value_portfolio(con, pf, dashboard.market_snapshot(con))
    alloc = {a["key"]: a for a in v["allocation"]}
    assert v["total"] == pytest.approx(100 * 101 + 10 * 1000 + 10_000)
    assert alloc["gold"]["value"] == pytest.approx(10_100)
    assert alloc["cash"]["current_pct"] == pytest.approx(10_000 / 30_100 * 100)
    assert alloc["stocks"]["gap_value"] == pytest.approx(10_000 - 0.5 * 30_100)


def test_validation_errors(con):
    with pytest.raises(ValueError, match="پیدا نشد"):
        dashboard.validate_portfolio(con, {"holdings": [{"ticker": "XYZ", "qty": 1}], "target": {"stocks": 100}})
    with pytest.raises(ValueError, match="۱۰۰"):
        dashboard.validate_portfolio(con, {"target": {"stocks": 60, "gold": 50}})
    with pytest.raises(ValueError, match="عدد"):
        dashboard.validate_portfolio(con, {"holdings": [{"ticker": "عيار", "qty": "abc"}], "target": {"gold": 100}})


def test_portfolio_file_round_trip(tmp_path):
    path = str(tmp_path / "p.json")
    assert dashboard.load_portfolio(path)["target"]["stocks"] == 50       # default when missing
    data = {"holdings": [{"ticker": "عيار", "qty": 3.0}], "manual": {"gold18_grams": 2.0},
            "target": {"stocks": 40, "gold": 60}}
    dashboard.save_portfolio(data, path)
    loaded = dashboard.load_portfolio(path)
    assert loaded["holdings"] == data["holdings"] and loaded["manual"]["gold18_grams"] == 2.0
    assert loaded["target"]["gold"] == 60 and loaded["target"]["cash"] == 0
