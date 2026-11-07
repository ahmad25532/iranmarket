import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402
import datastore  # noqa: E402
import tracking  # noqa: E402

TODAY = date(2026, 11, 7)


def day(d, total, tedpix=1000.0, gold18=100.0, usd=10.0, positions=None):
    row = {"d": d, "total": float(total), "by_class": {}, "prices": {"tedpix": tedpix, "gold18": gold18, "usd": usd}}
    if positions is not None:
        row["positions"] = positions
    return row


def flow(d, toman, kind="deposit"):
    return {"id": d + kind, "d": d, "kind": kind, "amount_toman": toman, "note": ""}


def rial(toman):
    return toman * tracking.RIALS_PER_TOMAN


def row(report, key):
    return next(r for r in report["rows"] if r["key"] == key)


# ---------------------------------------------------------------- deposits, withdrawals, missing days

def test_no_flows_is_plain_growth():
    r = tracking.compute([day("2026-01-01", 100), day("2026-01-02", 110), day("2026-01-03", 121)], [])
    assert row(r, "you")["twr_rial"] == pytest.approx(0.21)
    assert r["profit"] == pytest.approx(21)


def test_deposit_is_not_profit():
    # 100 -> deposit of 900 with no market move -> 1000: no return at all
    days = [day("2026-01-01", rial(100)), day("2026-01-02", rial(1000))]
    r = tracking.compute(days, [flow("2026-01-02", 900)])
    assert row(r, "you")["twr_rial"] == pytest.approx(0)
    assert r["net_flows"] == pytest.approx(rial(900))
    assert r["profit"] == pytest.approx(0)


def test_withdrawal_is_not_a_loss():
    days = [day("2026-01-01", rial(1000)), day("2026-01-02", rial(400))]
    r = tracking.compute(days, [flow("2026-01-02", 600, "withdrawal")])
    assert row(r, "you")["twr_rial"] == pytest.approx(0)
    assert r["net_flows"] == pytest.approx(-rial(600))


def test_return_ignores_size_of_capital_at_each_step():
    # +10% on 100, then a large deposit, then +10% on 1000: time-weighted is 21%, whatever the sizes.
    days = [day("2026-01-01", rial(100)), day("2026-01-02", rial(110)),
            day("2026-01-03", rial(1000)), day("2026-01-04", rial(1100))]
    r = tracking.compute(days, [flow("2026-01-03", 890)])
    assert row(r, "you")["twr_rial"] == pytest.approx(0.21)


def test_withdraw_everything_then_start_again():
    days = [day("2026-01-01", rial(100)), day("2026-01-02", rial(110)), day("2026-01-03", 0),
            day("2026-01-04", rial(500)), day("2026-01-05", rial(550))]
    r = tracking.compute(days, [flow("2026-01-03", 110, "withdrawal"), flow("2026-01-04", 500)])
    assert row(r, "you")["twr_rial"] == pytest.approx(0.21)
    assert r["issues"] == []


def test_missing_days_do_not_change_the_result():
    full = [day(f"2026-01-0{i}", 100 + 7 * i, tedpix=1000 + 30 * i, gold18=100 + i, usd=10 + i / 10) for i in range(1, 8)]
    gappy = [full[0], full[3], full[6]]
    a, b = tracking.compute(full, []), tracking.compute(gappy, [])
    for key in ("you", "mix", "gold", "usd"):
        assert row(a, key)["twr_rial"] == pytest.approx(row(b, key)["twr_rial"])
        assert row(a, key)["twr_usd"] == pytest.approx(row(b, key)["twr_usd"])
    assert b["issues"] == []
    assert b["records_used"] == 3 and b["days"] == 6


def test_flow_inside_a_gap_uses_time_weight_and_is_marked_approximate():
    # 500 deposited halfway through 10 days counts for half the time; the gain is 100
    days = [day("2026-01-01", rial(1000)), day("2026-01-11", rial(1600))]
    r = tracking.compute(days, [flow("2026-01-06", 500)])
    assert row(r, "you")["twr_rial"] == pytest.approx(100 / (1000 + 500 * 0.5))
    assert [i["kind"] for i in r["issues"]] == ["approximate"]


def test_flows_outside_the_window_do_not_count():
    days = [day("2026-01-02", rial(100)), day("2026-01-03", rial(110))]
    flows = [flow("2026-01-01", 50), flow("2026-01-02", 50), flow("2026-01-04", 999)]
    r = tracking.compute(days, flows)
    assert row(r, "you")["twr_rial"] == pytest.approx(0.10)
    assert r["net_flows"] == 0
    assert {f["d"]: f["status"] for f in r["flows"]} == {
        "2026-01-01": "before_start", "2026-01-02": "before_start", "2026-01-04": "pending"}


def test_flow_logged_but_not_in_holdings_cannot_wreck_the_chain():
    days = [day("2026-01-01", rial(100)), day("2026-01-02", rial(100)), day("2026-01-03", rial(110))]
    r = tracking.compute(days, [flow("2026-01-02", 500)])
    assert [i["kind"] for i in r["issues"]] == ["broken"]
    assert row(r, "you")["twr_rial"] == pytest.approx(0.10)


# ---------------------------------------------------------------- unlogged money

def position(key, qty, price):
    return {"key": key, "qty": qty, "price": price}


def test_money_added_without_a_log_entry_is_flagged():
    days = [day("2026-01-01", 1000, positions=[position("a", 10, 100)]),
            day("2026-01-02", 2000, positions=[position("a", 20, 100)])]
    r = tracking.compute(days, [])
    assert r["issues"] == [{"kind": "unexplained", "d": "2026-01-02", "amount": pytest.approx(1000)}]


def test_logged_money_and_trades_between_holdings_are_not_flagged():
    days = [day("2026-01-01", rial(1000), positions=[position("a", 10, rial(100))]),
            day("2026-01-02", rial(1500), positions=[position("a", 10, rial(100)), position("b", 5, rial(100))]),
            # sold all of a, bought more b, prices unchanged
            day("2026-01-03", rial(1500), positions=[position("b", 15, rial(100))])]
    assert tracking.compute(days, [flow("2026-01-02", 500)])["issues"] == []


def test_a_price_move_alone_is_not_flagged():
    days = [day("2026-01-01", 1000, positions=[position("a", 10, 100)]),
            day("2026-01-02", 1500, positions=[position("a", 10, 150)])]
    assert tracking.compute(days, [])["issues"] == []


# ---------------------------------------------------------------- comparisons

def test_comparisons_in_rial_and_dollars():
    days = [day("2026-01-01", 100, tedpix=1000, gold18=100, usd=10),
            day("2026-01-02", 150, tedpix=1500, gold18=120, usd=20)]
    r = tracking.compute(days, [])
    assert row(r, "you") == {"key": "you", "twr_rial": pytest.approx(0.5), "twr_usd": pytest.approx(-0.25)}
    assert row(r, "mix") == {"key": "mix", "twr_rial": pytest.approx(0.35), "twr_usd": pytest.approx(-0.325)}
    assert row(r, "gold") == {"key": "gold", "twr_rial": pytest.approx(0.2), "twr_usd": pytest.approx(-0.4)}
    assert row(r, "usd") == {"key": "usd", "twr_rial": pytest.approx(1.0), "twr_usd": pytest.approx(0)}
    end = r["series"][-1]
    assert end["you"] == pytest.approx(1.5) and end["usd_view"]["you"] == pytest.approx(0.75)
    assert end["usd_view"]["usd"] == pytest.approx(1)


def test_mix_is_held_not_rebalanced():
    days = [day("2026-01-01", 100, tedpix=100, gold18=100), day("2026-01-02", 100, tedpix=200, gold18=100),
            day("2026-01-03", 100, tedpix=100, gold18=200)]
    # Holding: half of 1x + half of 2x. Rebalancing at the middle record would give 1.5 * 1.25 instead.
    assert row(tracking.compute(days, []), "mix")["twr_rial"] == pytest.approx(0.5)


def test_comparison_starts_at_the_first_record_with_every_price():
    days = [day("2026-01-01", 100, usd=None), day("2026-01-02", 100, usd=10), day("2026-01-03", 120, usd=10)]
    r = tracking.compute(days, [])
    assert r["start"] == "2026-01-02" and row(r, "you")["twr_rial"] == pytest.approx(0.2)


def test_price_missing_later_keeps_its_last_value():
    days = [day("2026-01-01", 100, usd=10), day("2026-01-02", 100, usd=None), day("2026-01-03", 100, usd=20)]
    r = tracking.compute(days, [])
    assert r["series"][1]["usd"] == pytest.approx(1)
    assert row(r, "usd")["twr_rial"] == pytest.approx(1)


def test_needs_two_records():
    assert not tracking.compute([], [])["ready"]
    one = tracking.compute([day("2026-01-01", 100)], [flow("2026-01-01", 5)])
    assert not one["ready"] and one["records"] == 1 and len(one["flows"]) == 1


# ---------------------------------------------------------------- flow input

@pytest.mark.parametrize("text,expected", [
    ("1405/08/16", date(2026, 11, 7)),
    ("۱۴۰۵/۰۸/۱۶", date(2026, 11, 7)),
    ("1404/01/01", date(2025, 3, 21)),
    ("1403/12/30", date(2025, 3, 20)),  # leap year
    ("1405-06-31", date(2026, 9, 22)),
    ("2026-11-07", date(2026, 11, 7)),
])
def test_parse_date(text, expected):
    assert tracking.parse_date(text) == expected


@pytest.mark.parametrize("text", ["", "فردا", "1405/13/01", "1405/07/31", "1404/12/30", "2026-02-30"])
def test_parse_date_rejects_what_does_not_exist(text):
    with pytest.raises(ValueError):
        tracking.parse_date(text)


def test_validate_flow_accepts_persian_input():
    f = tracking.validate_flow({"d": "۱۴۰۵/۰۸/۱۰", "kind": "deposit", "amount_toman": "۱۰,۰۰۰", "note": " حقوق "}, TODAY)
    assert f["d"] == "2026-11-01" and f["amount_toman"] == 10_000 and f["note"] == "حقوق" and f["id"]


@pytest.mark.parametrize("payload", [
    {"d": "1405/08/17", "kind": "deposit", "amount_toman": 1},       # tomorrow
    {"d": "1405/08/16", "kind": "gift", "amount_toman": 1},
    {"d": "1405/08/16", "kind": "deposit", "amount_toman": 0},
    {"d": "1405/08/16", "kind": "deposit", "amount_toman": -5},
    {"d": "1405/08/16", "kind": "deposit", "amount_toman": "abc"},
    {"d": "1405/08/16", "kind": "deposit", "amount_toman": "inf"},
])
def test_validate_flow_rejects(payload):
    with pytest.raises(ValueError):
        tracking.validate_flow(payload, TODAY)


# ---------------------------------------------------------------- files

@pytest.fixture
def files(tmp_path, monkeypatch):
    """Points every private file at a temp folder, so tests can never reach the real ones."""
    monkeypatch.setattr(dashboard, "PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(tracking, "HISTORY_PATH", str(tmp_path / "portfolio_history.json"))
    monkeypatch.setattr(tracking, "FLOWS_PATH", str(tmp_path / "portfolio_flows.json"))
    return tmp_path


def test_last_write_of_a_day_wins_and_days_stay_sorted(files):
    tracking.upsert_day(day("2026-01-02", 1))
    tracking.upsert_day(day("2026-01-01", 2))
    tracking.upsert_day(day("2026-01-02", 3))
    assert [(r["d"], r["total"]) for r in tracking.load_history()] == [("2026-01-01", 2), ("2026-01-02", 3)]


def test_flow_log_add_and_delete(files):
    f = tracking.add_flow({"d": "1405/08/16", "kind": "deposit", "amount_toman": 5}, TODAY)
    assert [r["id"] for r in tracking.load_flows()] == [f["id"]]
    with pytest.raises(ValueError):
        tracking.delete_flow("nope")
    tracking.delete_flow(f["id"])
    assert tracking.load_flows() == []


@pytest.mark.parametrize("content", ["{broken", '{"a": 1}', '[{"d": "not a date", "total": 1}]'])
def test_damaged_file_is_reported_and_never_overwritten(files, content):
    open(tracking.HISTORY_PATH, "w", encoding="utf-8").write(content)
    assert "error" in tracking.report()
    with pytest.raises(tracking.TrackingFileError):
        tracking.upsert_day(day("2026-01-01", 1))
    assert open(tracking.HISTORY_PATH, encoding="utf-8").read() == content


# ---------------------------------------------------------------- recording

@pytest.fixture
def con():
    c = datastore.connect(":memory:")
    datastore.upsert(c, "instruments", [{"ins_code": "s1", "ticker": "فملي"}])
    c.execute("CREATE TABLE instrument_class AS SELECT ins_code, ticker, ticker AS name, 'stock' AS asset_class FROM instruments")
    c.execute("CREATE TABLE gold_funds (ins_code VARCHAR)")
    datastore.upsert(c, "daily_prices", [{"ins_code": "s1", "d": date(2026, 11, 4), "close": 1000.0}])
    datastore.upsert(c, "macro_daily", [
        {"series": "usd_irr", "d": date(2026, 11, 4), "close": 1_000_000.0},
        {"series": "gold_18k_irr", "d": date(2026, 11, 4), "close": 50_000_000.0},
    ])
    datastore.upsert(c, "index_daily", [{"index_code": dashboard.TEDPIX, "d": date(2026, 11, 4), "close": 3_000_000.0}])
    yield c
    c.close()


def write_portfolio(holdings=(), **manual):
    dashboard.save_portfolio({"holdings": list(holdings), "manual": manual, "target": {"stocks": 50, "gold": 50}})


def test_record_stores_value_by_class_and_prices(files, con):
    write_portfolio([{"ticker": "فملي", "qty": 10}], gold18_grams=2)
    assert dashboard.record_today(con, "collector", today=TODAY) == "recorded"
    r = tracking.load_history()[0]
    assert r["d"] == "2026-11-07" and r["source"] == "collector"
    assert r["total"] == pytest.approx(10_000 + 100_000_000)
    assert r["by_class"]["stocks"] == pytest.approx(10_000) and r["by_class"]["gold"] == pytest.approx(100_000_000)
    assert r["prices"] == {"tedpix": 3_000_000.0, "gold18": 50_000_000.0, "usd": 1_000_000.0}
    assert {p["key"] for p in r["positions"]} == {"فملي", "طلای ۱۸ عیار (گرم)"}
    json.dumps(r)


def test_record_overwrite_rules(files, con):
    write_portfolio([{"ticker": "فملي", "qty": 10}])
    assert dashboard.record_today(con, "dashboard", overwrite=False, today=TODAY) == "recorded"
    write_portfolio([{"ticker": "فملي", "qty": 20}])
    assert dashboard.record_today(con, "dashboard", overwrite=False, today=TODAY) == "exists"
    assert tracking.load_history()[0]["total"] == pytest.approx(10_000)
    assert dashboard.record_today(con, "collector", today=TODAY) == "recorded"
    assert tracking.load_history()[0]["total"] == pytest.approx(20_000)


def test_no_record_without_a_readable_portfolio(files, con):
    tracking.upsert_day(day("2026-11-06", 100))
    assert dashboard.record_today(con, "collector", today=TODAY) == "no_portfolio"
    open(dashboard.PORTFOLIO_PATH, "w", encoding="utf-8").write("{broken")
    # A damaged file must not read as an empty portfolio, which would look like a total loss.
    assert dashboard.record_today(con, "collector", today=TODAY) == "no_portfolio"
    assert len(tracking.load_history()) == 1


def test_no_record_when_a_price_is_missing(files, con):
    write_portfolio([{"ticker": "ناموجود", "qty": 1}])
    assert dashboard.record_today(con, "collector", today=TODAY) == "incomplete"
    write_portfolio(coin_emami=1)  # no coin price stored: must not silently count as zero
    assert dashboard.record_today(con, "collector", today=TODAY) == "incomplete"
    assert tracking.load_history() == []


def test_empty_portfolio_starts_no_history_but_can_end_one(files, con):
    write_portfolio()
    assert dashboard.record_today(con, "collector", today=TODAY) == "empty"
    assert tracking.load_history() == []
    tracking.upsert_day(day("2026-11-06", 100))
    assert dashboard.record_today(con, "collector", today=TODAY) == "recorded"
    assert tracking.load_history()[-1]["total"] == 0


def test_record_problem_becomes_a_warning_not_a_crash(files, con):
    write_portfolio([{"ticker": "فملي", "qty": 1}])
    open(tracking.HISTORY_PATH, "w", encoding="utf-8").write("{broken")
    name, severity, _ = dashboard.portfolio_record_check(con)
    assert (name, severity) == ("portfolio_record", "warn")
    assert open(tracking.HISTORY_PATH, encoding="utf-8").read() == "{broken"


def test_collector_records_the_day_without_the_dashboard(files, tmp_path, monkeypatch):
    import collector

    db = str(tmp_path / "market.duckdb")
    c = datastore.connect(db)
    datastore.upsert(c, "instruments", [{"ins_code": "s1", "ticker": "فملي", "isin": "IRO1MSMI0001", "paper_type": 1}])
    datastore.upsert(c, "daily_prices", [{"ins_code": "s1", "d": TODAY, "close": 1000.0, "last": 1000.0, "yesterday": 1000.0,
                                          "volume": 1.0, "value": 1000.0, "trades": 1}])
    write_portfolio([{"ticker": "فملي", "qty": 3}])
    overview = {"marketActivityDEven": int(TODAY.strftime("%Y%m%d")), "marketStateTitle": "closed"}
    monkeypatch.setattr(collector.sources, "fetch_market_overview", lambda http, m: overview)
    monkeypatch.setattr(collector.sources, "is_market_closed", lambda ov: True)
    monkeypatch.setattr(collector.sources, "parse_overview", lambda ov, m: {"market": m, "d": TODAY})
    monkeypatch.setattr(collector, "refresh_indices", lambda run, codes: 0)
    monkeypatch.setattr(collector, "take_snapshot", lambda run, d, final: ({}, 0))
    monkeypatch.setattr(collector, "snapshot_checks", lambda *a: [])
    monkeypatch.setattr(collector, "refresh_tgju", lambda run, n: None)
    monkeypatch.setattr(collector, "_new_instruments", lambda con: [])
    collector.collect_daily(c, None, allow_open=False, force=False)
    rows = c.execute("SELECT severity FROM quality_checks WHERE check_name = 'portfolio_record'").fetchall()
    c.close()
    assert rows == [("ok",)]
    assert tracking.load_history()[0]["total"] == pytest.approx(3000) and tracking.load_history()[0]["source"] == "collector"


# ---------------------------------------------------------------- HTTP

@pytest.fixture
def server(files, tmp_path, monkeypatch):
    db = str(tmp_path / "market.duckdb")
    c = datastore.connect(db)
    datastore.upsert(c, "instruments", [{"ins_code": "s1", "ticker": "فملي"}])
    c.execute("CREATE TABLE instrument_class AS SELECT ins_code, ticker, ticker AS name, 'stock' AS asset_class FROM instruments")
    c.execute("CREATE TABLE gold_funds (ins_code VARCHAR)")
    c.execute("CREATE TABLE price_adj (ins_code VARCHAR, d DATE, adj_close DOUBLE, adj_vwap DOUBLE, factor DOUBLE, trades BIGINT)")
    datastore.upsert(c, "daily_prices", [{"ins_code": "s1", "d": date(2026, 11, 4), "close": 1000.0}])
    c.close()
    monkeypatch.setattr(dashboard.Handler, "db_path", db)
    monkeypatch.setattr(dashboard, "get_live_prices", lambda: None)
    srv = dashboard.ExclusiveServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def test_http_opening_records_saving_replaces_and_flows_round_trip(server):
    s = call(server + "/api/state")
    assert s["tracking"]["record_status"] == "no_portfolio" and not tracking.load_history()

    s = call(server + "/api/portfolio", {"holdings": [{"ticker": "فملی", "qty": 2}], "target": {"stocks": 100}})
    assert s["tracking"]["record_status"] == "recorded" and s["tracking"]["records"] == 1
    assert call(server + "/api/state")["tracking"]["record_status"] == "exists"

    s = call(server + "/api/flows", {"d": date.today().isoformat(), "kind": "deposit", "amount_toman": "1000"})
    entry = s["tracking"]["flows"][0]
    assert entry["amount_toman"] == 1000
    s = call(server + "/api/flows/delete", {"id": entry["id"]})
    assert s["tracking"]["flows"] == []


def test_http_rejects_a_bad_flow_with_a_persian_message(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        call(server + "/api/flows", {"d": "1405/13/40", "kind": "deposit", "amount_toman": 1})
    assert e.value.code == 400
    assert "تاریخ" in json.load(e.value)["error"]
    assert tracking.load_flows() == []
