import json
import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import codal  # noqa: E402
import datastore  # noqa: E402
from net import FetchError  # noqa: E402

# Cells of two real فولاد letters fetched on 2026-11-10: 3 months to 1405/03/31 (template V9) and
# 6 months to 1395/06/31 (old template with a two-sided balance sheet).
FIX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "codal_folad.json"), encoding="utf-8"))


def sheets(prefix):
    return {"income": FIX[prefix + "_income"], "balance": FIX[prefix + "_balance"]}


def test_parse_v9_matches_the_published_statement():
    r = codal.parse_statement(1, sheets("v9"))
    assert r["period_end_ds"] == date(2026, 6, 21)
    assert r["net_income"] == 120356493
    assert r["eps_basic"] == 63 and r["eps_net"] == 62
    assert r["equity"] == 3382057475 and r["capital"] == 1935000000
    assert r["total_assets"] == 6278290305 and r["missing"] == ""


def test_parse_old_two_sided_balance_sheet():
    r = codal.parse_statement(1, sheets("old"))
    assert r["equity"] == 92046963 and r["capital"] == 75000000
    assert r["net_income"] == 6199687 and r["eps_net"] == 83
    assert r["eps_basic"] is None  # parts were all filed as 0 despite the profit
    assert "eps_basic" in r["missing"]


def test_eps_parts_are_added_when_total_is_empty():
    cells = [[0, 1, 1, "سود (زیان) خالص", ""], [0, 1, 2, "100", "1395/06/31"],
             [0, 2, 1, "سود (زیان) پایه هر سهم", ""],
             [0, 3, 1, "سود (زیان) پایه هر سهم ناشی از عملیات در حال تداوم- عملیاتی", ""], [0, 3, 2, "50", "1395/06/31"],
             [0, 4, 1, "سود (زیان) پایه هر سهم ناشی از عملیات در حال تداوم- غیرعملیاتی", ""], [0, 4, 2, "-8", "1395/06/31"]]
    assert codal.parse_income(cells, "1395/06/31")["eps_basic"] == 42


def test_only_the_letters_own_period_is_read():
    cells = [[0, 1, 1, "سود(زيان) خالص", ""], [0, 1, 2, "5", "1404/12/29"], [0, 1, 3, "7", "1405/03/31"]]
    assert codal.parse_income(cells, "1405/03/31")["net_income"] == 7


def test_pick_sheets_by_title():
    for p in ("v9", "old"):
        assert codal.pick_sheets({int(k): v for k, v in FIX[p + "_options"].items()}) == {"income": 1, "balance": 0}


def letter(title, tracing=1, html=True):
    return {"TracingNo": tracing, "Symbol": "فولاد", "Title": title, "HasHtml": html,
            "SentDateTime": "۱۴۰۵/۰۴/۳۰ ۲۲:۰۷:۳۵", "PublishDateTime": "۱۴۰۵/۰۴/۳۰ ۲۲:۰۷:۳۵",
            "Url": "/Reports/Decision.aspx?LetterSerial=abc%3d%3d&rt=0&let=6"}


def test_parse_letter():
    r = codal.parse_letter(letter("اطلاعات و صورت‌های مالی میاندوره‌ای  دوره ۳ ماهه منتهی به  ۱۴۰۵/۰۳/۳۱ (حسابرسی نشده)(اصلاحیه)"), "x")
    assert r["period_end"] == date(2026, 6, 21) and r["period_months"] == 3
    assert r["published_at"] == datetime(2026, 7, 21, 22, 7, 35)
    assert r["amendment"] and not r["audited"] and not r["consolidated"]
    a = codal.parse_letter(letter("صورت‌های مالی تلفیقی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹ (حسابرسی شده)"), "x")
    assert a["period_months"] == 12 and a["audited"] and a["consolidated"]


def test_subsidiary_and_image_only_letters_are_skipped():
    assert codal.parse_letter(letter("صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۰۳/۳۱ (حسابرسی شده) (شرکت فرهنگی)"), "x") is None
    assert codal.parse_letter(letter("صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۰۳/۳۱", html=False), "x") is None


def test_statement_path():
    assert codal.statement_path(letter("")["Url"], 0) == "/Reports/InterimStatement.aspx?LetterSerial=abc%3d%3d&sheetId=0"


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get_text(self, host, path, timeout=15):
        self.calls.append(path)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def con():
    c = datastore.connect(":memory:")
    yield c
    c.close()


def test_blocks_on_captcha_and_repeated_failures(con):
    col = codal.Collector(con, FakeHttp(["<div>captcha</div>"]), sleep=lambda s: None)
    with pytest.raises(codal.Blocked):
        col.statement(1, letter("")["Url"])
    col = codal.Collector(con, FakeHttp([FetchError("t")] * 3), sleep=lambda s: None)
    for _ in range(2):
        with pytest.raises(FetchError):
            col.statement(1, letter("")["Url"])
    with pytest.raises(codal.Blocked):
        col.statement(1, letter("")["Url"])


def test_letters_stop_at_a_known_page_and_wait_between_requests(con):
    page = lambda ids: json.dumps({"Total": 40, "Page": 2, "IsAttacker": False, "Letters": [
        letter("صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹", t) for t in ids]})
    waits = []
    http = FakeHttp([page([1, 2]), page([3])])
    assert codal.Collector(con, http, sleep=waits.append).letters("x", "فولاد") == 3
    assert waits == [codal.DEFAULT_DELAY_S]
    http = FakeHttp([page([1, 2])])
    assert codal.Collector(con, http, sleep=waits.append).letters("x", "فولاد") == 0
    assert len(http.calls) == 1


def test_statement_stores_cells_and_reparses(con):
    def html(sid, prefix):
        part = FIX[f"{prefix}_{'income' if sid == 1 else 'balance'}"]
        ds = {"title_En": "V9", "periodEndToDate": part["period_end"], "isConsolidated": False,
              "sheets": [{"code": sid, "tables": [{"cells": [
                  {"rowSequence": c[1], "columnSequence": c[2], "value": c[3], "periodEndToDate": c[4]}
                  for c in part["cells"]]}]}]}
        opts = "".join(f'<option value="{k}">{v}</option>' for k, v in FIX["v9_options"].items())
        return f"<select>{opts}</select><script>var datasource = {json.dumps(ds, ensure_ascii=False)};</script>"
    http = FakeHttp([html(1, "v9"), html(0, "v9")])
    row = codal.Collector(con, http, sleep=lambda s: None).statement(7, letter("")["Url"])
    assert row["eps_basic"] == 63 and row["equity"] == 3382057475
    assert http.calls[1].endswith("sheetId=0")
    con.execute("DELETE FROM codal_financials")
    assert codal.reparse(con) == 1
    assert con.execute("SELECT eps_net FROM codal_financials WHERE tracing_no = 7").fetchone()[0] == 62
