"""The market watch ignores its paper-type filter: one request, and funds are recognised by ISIN."""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adjust  # noqa: E402
import collector  # noqa: E402
import datastore  # noqa: E402
import sources  # noqa: E402
from net import FetchError  # noqa: E402

D = date(2026, 11, 4)


def watch_row(code, ticker, isin, trades=10):
    return {"insCode": code, "lva": ticker, "lvc": ticker, "insID": isin, "cs": "27", "ztt": trades,
            "pcl": 100.0, "pdv": 100.0, "py": 100.0, "pf": 100.0, "pmx": 100.0, "pmn": 100.0, "qtj": 5.0, "qtc": 500.0}


# What the API now returns whatever type is asked for.
WATCH = [
    watch_row("share", "فملي", "IRO1MSMI0001"),
    watch_row("basemkt", "شلرد", "IRO7SHLP0001"),
    watch_row("option", "ضفلا", "IRO9FOLD1234"),
    watch_row("future", "جفلا", "IROFFOLD0001"),
    watch_row("bond", "اخزا", "IRB3TB000001"),
    watch_row("equity_fund", "آگاس", "IRT1AGAS0001"),
    watch_row("fixed_fund", "كمند", "IRT3KMND0001"),
    watch_row("gold_fund", "عيار", "IRTKMOFD0001", trades=0),   # untraded funds still have a NAV
    watch_row("energy_fund", "سينرژي", "IRTEETFD0001"),
    watch_row("fund_block", "آگاس2", "IRT1AGAS0002"),
    watch_row("fund_rights", "آگاسح", "IRT1AGAS0003"),
]
FUNDS = ["energy_fund", "equity_fund", "fixed_fund", "gold_fund"]


class Calls:
    def __init__(self):
        self.watch = 0
        self.nav = []


@pytest.fixture
def con():
    c = datastore.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def api(monkeypatch):
    calls = Calls()

    def watch(http):
        calls.watch += 1
        return WATCH

    def nav(http, code):
        calls.nav.append(code)
        return {"insCode": code, "deven": 20261104, "pRedTran": 100.0, "pSubTran": 101.0, "hEven": 170000}

    monkeypatch.setattr(sources, "fetch_market_watch", watch)
    monkeypatch.setattr(sources, "fetch_etf_nav", nav)
    monkeypatch.setattr(sources, "fetch_client_types_all", lambda http: [])
    monkeypatch.setattr(sources, "fetch_index_list", lambda http: [])
    return calls


def test_snapshot_fetches_once_and_asks_nav_only_for_funds(con, api):
    prices, etf_count = collector.take_snapshot(collector.Run(con, None), D, final=True)
    assert api.watch == 1
    assert sorted(api.nav) == FUNDS and etf_count == len(FUNDS)
    assert {r[0] for r in con.execute("SELECT ins_code FROM etf_nav").fetchall()} == set(FUNDS)
    # Price collection is unchanged: every traded normal-board instrument, whatever it is.
    assert set(prices) == {"share", "basemkt", "option", "future", "bond", "equity_fund", "fixed_fund", "energy_fund"}
    assert con.execute("SELECT count(*) FROM instruments").fetchone()[0] == len(WATCH)
    assert con.execute("SELECT final FROM snapshots").fetchone()[0] is True


def test_snapshot_skips_delisted_funds(con, api):
    datastore.upsert(con, "instruments", [{"ins_code": "gone", "ticker": "رفته", "isin": "IRT1GONE0001"}])
    collector.take_snapshot(collector.Run(con, None), D, final=True)
    assert "gone" not in api.nav


def test_failed_market_watch_is_not_a_final_snapshot(con, api, monkeypatch):
    def down(http):
        raise FetchError("down")

    monkeypatch.setattr(sources, "fetch_market_watch", down)
    prices, etf_count = collector.take_snapshot(collector.Run(con, None), D, final=True)
    assert prices == {} and etf_count == 0 and api.nav == []
    assert con.execute("SELECT final FROM snapshots").fetchone()[0] is False


def test_market_watch_request_names_every_type():
    # A restored filter must return all types, as the three separate requests did.
    class Http:
        def get_json(self, host, path, timeout):
            self.path = path
            return {"marketwatch": []}

    http = Http()
    assert sources.fetch_market_watch(http) == []
    for i, t in enumerate((1, 2, 8)):
        assert f"paperTypes%5B{i}%5D={t}" in http.path


def test_universe_keeps_all_normal_board_instruments_and_funds_by_isin(con, api):
    # paper_type is stale on stored rows and must not matter either way.
    collector.take_snapshot(collector.Run(con, None), D, final=True)
    con.execute("UPDATE instruments SET paper_type = 8 WHERE ins_code IN ('share', 'option')")
    con.execute("UPDATE instruments SET paper_type = 1 WHERE ins_code = 'fixed_fund'")
    assert collector._universe(con, funds_only=True) == FUNDS
    assert set(collector._universe(con)) == {r["insCode"] for r in WATCH} - {"fund_block", "fund_rights"}


def test_backfill_seeds_once_and_fills_nav_only_for_funds(con, api, monkeypatch):
    nav_targets = []
    monkeypatch.setattr(collector, "fill_prices_and_flows", lambda *a: None)
    monkeypatch.setattr(collector, "fill_nav", lambda run, codes, show_all: nav_targets.extend(codes))
    monkeypatch.setattr(collector, "refresh_tgju", lambda *a: None)
    monkeypatch.setattr(collector, "refresh_indices", lambda *a: 0)
    monkeypatch.setattr(collector, "refresh_global", lambda *a, **k: None, raising=False)
    collector.backfill(con, None, limit=None, refresh=False)
    assert api.watch == 1
    assert nav_targets == FUNDS


def test_instruments_are_stored_without_paper_type(con, api):
    datastore.upsert(con, "instruments", [{"ins_code": "share", "ticker": "فملي", "isin": "IRO1MSMI0001", "paper_type": 8}])
    collector.take_snapshot(collector.Run(con, None), D, final=True)
    assert con.execute("SELECT count(paper_type) FROM instruments").fetchone()[0] == 0


@pytest.mark.parametrize("isin,paper_type,expected", [
    ("IRO1FOLD0001", 8, "stock"),          # Bourse, with the stale type from the old collector
    ("IRO3ATAP0001", None, "stock"),       # Fara Bourse
    ("IRO7LZIN0001", None, "stock"),       # base market
    ("IRO1ABAD0003", 1, "stock"),          # left the live list while the filter still worked
    ("IRT1AHRM0001", None, "etf_equity"),
    ("IRO9SHPN1234", None, "etf_equity"),  # options keep the catch-all, never "stock"
])
def test_asset_class_comes_from_isin_not_paper_type(con, isin, paper_type, expected):
    datastore.upsert(con, "instruments", [{"ins_code": "x", "ticker": "x", "isin": isin, "paper_type": paper_type}])
    adjust.build(con)
    assert con.execute("SELECT asset_class FROM instrument_class WHERE ins_code = 'x'").fetchone()[0] == expected


def test_fund_types_classify_funds_without_paper_type(con):
    datastore.upsert(con, "instruments", [
        {"ins_code": "fx", "ticker": "fx", "isin": "IRT3KMND0001"},
        {"ins_code": "cm", "ticker": "cm", "isin": "IRTKMOFD0001"},
    ])
    datastore.upsert(con, "fipiran_funds", [
        {"ins_code": "fx", "reg_no": "1", "fund_type": 4},
        {"ins_code": "cm", "reg_no": "2", "fund_type": 5},
    ])
    adjust.build(con)
    assert dict(con.execute("SELECT ins_code, asset_class FROM instrument_class").fetchall()) == {
        "fx": "etf_fixed_income", "cm": "etf_commodity",
    }


def test_gold_funds_are_funds_only_and_ignore_paper_type(con):
    # Three instruments with identical gold-like returns: only the fund counts, whatever paper_type says.
    days = [D - timedelta(days=100 - i) for i in range(100)]
    closes = [100.0]
    for i in range(1, 100):
        closes.append(closes[-1] * (1 + (0.01 if i % 3 else -0.012)))
    datastore.upsert(con, "instruments", [
        {"ins_code": "fund", "ticker": "زر", "isin": "IRTKZARF0001", "paper_type": None},
        {"ins_code": "share", "ticker": "فزر", "isin": "IRO1FZAR0001", "paper_type": 8},
        {"ins_code": "option", "ticker": "ضزر", "isin": "IRO9ZARF1234", "paper_type": 8},
    ])
    datastore.upsert(con, "macro_daily", [
        {"series": "gold_18k_irr", "d": d, "close": c} for d, c in zip(days, closes)
    ])
    datastore.upsert(con, "daily_prices", [
        {"ins_code": code, "d": d, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "last": c,
         "yesterday": prev, "volume": 1.0, "value": c, "trades": 10}
        for code in ("fund", "share", "option")
        for d, c, prev in zip(days, closes, [closes[0]] + closes[:-1])
    ])
    adjust.build(con)
    assert [r[0] for r in con.execute("SELECT ins_code FROM gold_funds").fetchall()] == ["fund"]
