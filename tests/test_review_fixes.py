"""Regressions for the problems found in the November 2026 project review."""

import http.client
import json
import os
import sys
import threading
from datetime import date, timedelta

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import adjust
import backup
import collector
import dashboard
import datastore
import quality
import research
import sources

DAYS = [date(2026, 11, 1) + timedelta(days=i) for i in range(6)]


@pytest.fixture
def con():
    c = datastore.connect(":memory:")
    yield c
    c.close()


# ---------------------------------------------------------------- catch-up marks only what it filled

def _daily(con, monkeypatch, failed):
    gap = DAYS[0]
    overview = {"marketActivityDEven": int(DAYS[-1].strftime("%Y%m%d")), "marketStateTitle": "closed"}
    datastore.upsert(con, "snapshots", [{"d": DAYS[-1], "taken_at": None, "final": True}])
    monkeypatch.setattr(collector.sources, "fetch_market_overview", lambda http, m: overview)
    monkeypatch.setattr(collector, "refresh_indices", lambda *a: 0)
    monkeypatch.setattr(collector, "refresh_tgju", lambda *a: None)
    monkeypatch.setattr(collector, "refresh_global", lambda *a, **k: None)
    monkeypatch.setattr(collector, "_trading_days", lambda *a: DAYS)
    monkeypatch.setattr(collector, "fill_prices_and_flows", lambda *a: failed)
    monkeypatch.setattr(collector, "fill_nav", lambda *a, **k: failed)
    monkeypatch.setattr(collector.adjust, "build", lambda c: {"rows": 0, "events": 0})
    monkeypatch.setattr(collector.dashboard, "portfolio_record_check", lambda c: ("portfolio_record", "ok", ""))
    monkeypatch.setattr(quality, "find_gaps", lambda *a, **k: [gap])
    collector.collect_daily(con, object(), allow_open=False, force=False)
    return con.execute("SELECT kind FROM filled_days ORDER BY kind").fetchall()


def test_failed_catch_up_leaves_the_day_to_be_retried(con, monkeypatch):
    assert _daily(con, monkeypatch, failed=0.5) == []


def test_successful_catch_up_marks_the_day(con, monkeypatch):
    assert _daily(con, monkeypatch, failed=0.0) == [("nav",), ("prices_flows",)]


def test_fill_reports_the_share_that_failed(con, monkeypatch):
    def history(http, code, days):
        if code == "bad":
            raise ValueError("down")
        return []
    monkeypatch.setattr(sources, "fetch_price_history", history)
    monkeypatch.setattr(sources, "fetch_client_type_history", lambda http, code: [])
    run = collector.Run(con, object())
    assert collector.fill_prices_and_flows(run, ["a", "bad", "c", "d"], 0, "x") == 0.25


# ---------------------------------------------------------------- signals are warm when a period starts

def test_signal_on_a_period_uses_the_history_before_it():
    n = 400
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    tedpix = np.concatenate([np.linspace(200, 300, 300), np.linspace(300, 100, 100)])
    one = np.ones(n)
    full = research.Panel(days, tedpix, one, one, one, one, one, one * 0, one * 0, one * 0)
    p = full.slice(days[300], days[-1])
    trend = lambda x: research.sig_trend(x, 200)
    assert trend(p)[:50].all()                  # the cut series has no average yet, so it reads "stay in"
    assert not research.aligned(trend, p, full)[:50].all()


# ---------------------------------------------------------------- classification and portfolio pricing

def _market(con):
    datastore.upsert(con, "instruments", [
        {"ins_code": "old", "ticker": "دوتايي", "isin": "IRO3AAAA0001"},
        {"ins_code": "new", "ticker": "دوتايي", "isin": "IRO1AAAA0001"},
        {"ins_code": "base", "ticker": "پايه", "isin": "IRO5BBBB0001"},
        {"ins_code": "bill", "ticker": "اخزا301", "isin": "IRB3TR260661"},
    ])
    row = {"open": 1.0, "high": 1.0, "low": 1.0, "last": 1.0, "yesterday": 1.0, "volume": 1.0, "value": 1.0, "trades": 1}
    datastore.upsert(con, "daily_prices", [
        {**row, "ins_code": "old", "d": date(2020, 1, 1), "close": 5.0},
        {**row, "ins_code": "new", "d": date(2026, 11, 1), "close": 7.0},
    ])
    datastore.upsert(con, "bill_yields", [{"ins_code": "bill", "d": date(2026, 11, 1), "close": 900.0}])
    adjust.build(con)


def test_share_classes_come_from_the_isin(con):
    _market(con)
    got = dict(con.execute("SELECT ins_code, asset_class FROM instrument_class").fetchall())
    assert got["base"] == "stock" and got["bill"] == "etf_fixed_income"


def test_portfolio_uses_the_current_code_and_prices_bills(con):
    _market(con)
    pf = {"holdings": [{"ticker": "دوتايي", "qty": 2}, {"ticker": "اخزا301", "qty": 1}],
          "manual": dict(dashboard.DEFAULT_PORTFOLIO["manual"]), "target": dict(dashboard.DEFAULT_PORTFOLIO["target"])}
    items = {i["ticker"]: i for i in dashboard.value_portfolio(con, pf, {})["items"]}
    assert items["دوتايي"]["value"] == 14.0 and items["دوتايي"]["cls"] == "stocks"
    assert items["اخزا301"]["value"] == 900.0 and items["اخزا301"]["cls"] == "fixed"


def test_official_list_keeps_only_ordinary_shares():
    rec = ["1", "IRO1KAFF0001", "KAFF1", "Kaf", "KAFF", " شكف ", "كف", "IRO1KAFF0004", "20140311", "1", "كف", "A", "N2", "NO", "5", "44 "]
    got = sources.parse_official_share(rec)
    assert (got["ticker"], got["last_seen"], got["sector_code"]) == ("شكف", date(2014, 3, 11), "44")
    assert sources.parse_official_share(["2", "IRB3TR260661"] + rec[2:]) is None
    assert sources.parse_official_share(["3", "IRO1KAFF0002"] + rec[2:]) is None


# ---------------------------------------------------------------- the dashboard only answers its own page

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    srv = dashboard.ExclusiveServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _post(port, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/nothing", body=json.dumps({}), headers=headers)
    return conn.getresponse().status


def test_another_site_cannot_post(server):
    assert _post(server, {"Origin": "https://evil.example"}) == 403
    assert _post(server, {"Origin": "null"}) == 403


def test_rebound_host_is_refused(server):
    assert _post(server, {"Host": f"evil.example:{server}"}) == 403


def test_own_page_is_served(server):
    assert _post(server, {"Origin": f"http://127.0.0.1:{server}"}) == 404   # past the check, unknown path


# ---------------------------------------------------------------- backup

def _db(path):
    c = datastore.connect(str(path))
    datastore.upsert(c, "instruments", [{"ins_code": "1", "ticker": "x", "isin": "y"}])
    c.execute("INSERT INTO daily_prices (ins_code, d, close) VALUES ('1', DATE '2026-11-01', 1.0)")
    c.close()


def test_backup_copies_and_keeps_only_recent_days(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _db(data / "market.duckdb")
    (data / "portfolio.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "copies"
    for i in range(5):
        folder = backup.backup(str(data / "market.duckdb"), str(dest), date(2026, 11, 1) + timedelta(days=i), keep=3)
    assert backup._day_folders(str(dest)) == ["2026-11-03", "2026-11-04", "2026-11-05"]
    assert backup._rows(os.path.join(folder, "market.duckdb")) == 1
    assert os.path.exists(os.path.join(folder, "portfolio.json"))


def test_backup_refuses_while_the_collector_writes(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _db(data / "market.duckdb")
    writer = datastore.connect(str(data / "market.duckdb"))
    try:
        with pytest.raises(Exception):
            backup.backup(str(data / "market.duckdb"), str(tmp_path / "copies"), keep=3)
    finally:
        writer.close()


# ---------------------------------------------------------------- Tehran clock and unsettled days

def test_clock_is_tehran_whatever_the_system_zone():
    from datetime import datetime, timezone
    gap = sources.tehran_now() - datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(gap.total_seconds() - 3.5 * 3600) < 5


def _unsettled(con, monkeypatch, failed):
    datastore.upsert(con, "snapshots", [{"d": DAYS[1], "taken_at": None, "final": False}])
    _daily(con, monkeypatch, failed)
    monkeypatch.setattr(quality, "find_gaps", lambda *a, **k: [])
    con.execute("DELETE FROM filled_days")
    collector.collect_daily(con, object(), allow_open=False, force=False)
    return con.execute("SELECT final FROM snapshots WHERE d = ?", [DAYS[1]]).fetchone()[0]


def test_past_snapshot_not_final_is_replaced_from_history(con, monkeypatch):
    assert _unsettled(con, monkeypatch, 0.0) is True


def test_past_snapshot_stays_open_when_the_refill_fails(con, monkeypatch):
    assert _unsettled(con, monkeypatch, 0.5) is False


def test_todays_unfinished_snapshot_is_left_for_the_normal_retake(con, monkeypatch):
    datastore.upsert(con, "snapshots", [{"d": DAYS[-1], "taken_at": None, "final": False}])
    assert con.execute("SELECT count(*) FROM snapshots WHERE NOT final AND d < ?", [DAYS[-1]]).fetchone()[0] == 0
