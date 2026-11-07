import os
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adjust  # noqa: E402
import collector  # noqa: E402
import datastore  # noqa: E402
import quality  # noqa: E402
import sources  # noqa: E402

SEEN = datetime(2026, 11, 7, 16, 5)


def watch_row(code, ticker, isin, eps, pe):
    """EPS and P/E values are copied from the live market watch of 2026-11-07."""
    return {"insCode": code, "lva": ticker, "lvc": ticker, "insID": isin, "csv": "27 ", "eps": eps, "pe": pe,
            "ztt": 10.0, "pcl": 4410.0, "pdv": 4410.0, "py": 4290.0, "pf": 4300.0, "pmx": 4410.0, "pmn": 4300.0,
            "qtj": 5.0, "qtc": 22050.0}


WATCH = [
    watch_row("profit", "فولاد", "IRO1FOLD0001", 693.0, "6.4"),
    watch_row("loss", "خساپا", "IRO1SIPA0001", -321.0, "-1.9"),
    watch_row("fund", "سرو", "IRT1SARV0001", 0.0, None),
    watch_row("option", "ضهرم", "IRO9AHRM0F01", 0.0, None),
    watch_row("block", "فولاد2", "IRO1FOLD0002", 693.0, "6.4"),
    watch_row("rights", "فولادح", "IRO1FOLD0091", 693.0, "6.4"),
]


@pytest.fixture
def con():
    c = datastore.connect(":memory:")
    yield c
    c.close()


def stored(con):
    return {r[0]: r[1:] for r in con.execute(
        "SELECT ins_code, seen_on, seen_at, eps, pe FROM fundamentals_seen ORDER BY ins_code").fetchall()}


# ---------- parser ----------

def test_pe_string_is_parsed_and_negative_values_are_kept():
    row = sources.parse_watch_fundamentals(WATCH[1], SEEN)
    assert row == {"ins_code": "loss", "seen_on": date(2026, 11, 7), "seen_at": SEEN, "eps": -321.0, "pe": -1.9}


def test_row_without_any_figure_is_skipped():
    assert sources.parse_watch_fundamentals(WATCH[2], SEEN) is None
    # The live feed had one row with pe '-': same meaning as null.
    assert sources.parse_watch_fundamentals({"insCode": "x", "eps": 0.0, "pe": "-"}, SEEN) is None


def test_eps_is_kept_when_pe_is_unusable():
    row = sources.parse_watch_fundamentals({"insCode": "x", "eps": 12.0, "pe": "-"}, SEEN)
    assert row["eps"] == 12.0 and row["pe"] is None


# ---------- storage ----------

def test_only_normal_boards_with_figures_are_stored(con):
    assert collector.store_fundamentals(con, WATCH, SEEN) == 2
    assert set(stored(con)) == {"profit", "loss"}


def test_as_of_query_cannot_see_a_value_before_it_was_fetched(con):
    collector.store_fundamentals(con, WATCH, SEEN)
    newer = [watch_row("profit", "فولاد", "IRO1FOLD0001", 800.0, "5.5")]
    collector.store_fundamentals(con, newer, SEEN + timedelta(days=1))
    sql = """SELECT eps FROM fundamentals_seen WHERE ins_code = 'profit' AND seen_on <= ?
             ORDER BY seen_on DESC LIMIT 1"""
    assert con.execute(sql, [date(2026, 11, 6)]).fetchone() is None
    assert con.execute(sql, [date(2026, 11, 7)]).fetchone()[0] == 693.0
    assert con.execute(sql, [date(2026, 11, 8)]).fetchone()[0] == 800.0


def test_rerun_on_the_same_day_replaces_the_row(con):
    collector.store_fundamentals(con, WATCH, SEEN)
    later = SEEN + timedelta(hours=4)
    collector.store_fundamentals(con, [watch_row("profit", "فولاد", "IRO1FOLD0001", 800.0, "5.5")], later)
    assert stored(con)["profit"] == (date(2026, 11, 7), later, 800.0, 5.5)


# ---------- quality check ----------

def test_check_fails_when_a_day_has_no_rows(con):
    assert quality.check_fundamentals(con, SEEN.date())[1] == "fail"


def test_check_passes_on_first_day_and_warns_on_a_sharp_drop(con):
    many = [watch_row(str(i), "فولاد", "IRO1FOLD0001", 10.0, "1.0") for i in range(10)]
    collector.store_fundamentals(con, many, SEEN)
    assert quality.check_fundamentals(con, SEEN.date())[1] == "ok"
    next_day = SEEN + timedelta(days=1)
    collector.store_fundamentals(con, many[:3], next_day)
    assert quality.check_fundamentals(con, next_day.date())[1] == "warn"


# ---------- a whole daily run ----------

class FakeHttp:
    """Answers every endpoint a daily run calls; only the market watch carries data."""
    source_ip = None

    def __init__(self, market_date):
        self.market_date = market_date

    def get_json(self, host, path, timeout=15, direct=True, body=None):
        answers = {
            "GetMarketOverview": {"marketOverview": {
                "marketActivityDEven": self.market_date, "marketState": "F", "marketStateTitle": "بسته"}},
            "GetMarketWatch": {"marketwatch": WATCH},
            "GetClientTypeAll": {"clientTypeAllDto": []},
            "GetClosingPriceDailyList": {"closingPriceDaily": []},
            "GetClientTypeHistory": {"clientType": []},
            "GetETFByInsCode": {"etf": {}},
            "GetIndexB1LastAll": {"indexB1": []},
            "GetIndexB2History": {"indexB2": []},
            "fundcompare": {"items": []},
            "summary-table-data": {"data": []},
            "ajax.json": {"current": {}},
        }
        for key, answer in answers.items():
            if key in path:
                return answer
        raise AssertionError(f"unexpected request: {path}")


def test_daily_run_fills_the_table_dated_by_the_run_not_the_market(con, monkeypatch):
    # The market last traded days ago (computer was off): anything published since then is in today's
    # feed, so dating these rows by the market date would be looking ahead.
    monkeypatch.setattr(adjust, "build", lambda c: {"rows": 0, "events": 0})
    market_date = date.today() - timedelta(days=3)
    collector.collect_daily(con, FakeHttp(int(market_date.strftime("%Y%m%d"))), allow_open=False, force=False)
    rows = stored(con)
    assert set(rows) == {"profit", "loss"}
    assert rows["profit"][0] == date.today() != market_date
    assert rows["profit"][2:] == (693.0, 6.4)
    checks = con.execute("SELECT severity FROM quality_checks WHERE check_name = 'fundamentals'").fetchall()
    assert checks == [("ok",)]
