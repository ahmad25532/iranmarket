"""
Point-in-time financial statements from Codal (codal.ir), for testing fundamental factors later.

Two steps, both resumable:
1. letters: the list of financial-statement letters per share (search.codal.ir), with the exact
   publish time. Amendments and audited versions are separate letters, so the table shows what was
   public on any day. Subsidiaries' statements (title ends in "(شرکت ...)") are skipped.
2. statements: for each letter, the income statement and balance sheet pages. The cells are stored
   compactly in codal_sheets so parsing can be redone without fetching again, and the key figures of
   the letter's own period go into codal_financials.

Codal cuts off clients that ask too fast (see docs/fundamentals_findings.md), so requests are slow
and the run stops at the first sign of blocking. It never tries to get past a captcha.
"""

import json
import re
import time
import urllib.parse
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import duckdb

import datastore
from freeusd import jalali_to_gregorian
from net import FetchError, HttpClient

SEARCH_HOST = "search.codal.ir"
SITE_HOST = "www.codal.ir"
SEARCH_PARAMS = {
    "LetterType": "6", "Category": "1", "Audited": "true", "NotAudited": "true", "Childs": "false",
    "Mains": "true", "Publisher": "false", "Consolidatable": "true", "NotConsolidatable": "true",
    "search": "true", "Length": "-1", "CompanyType": "1", "CompanyState": "0", "TracingNo": "-1",
    "AuditorRef": "-1",
}
DEFAULT_DELAY_S = 4.0
MAX_FAILURES = 3


class Blocked(RuntimeError):
    """Codal refused or stopped answering; the run must stop, not retry harder."""


# ---------------------------------------------------------------- text and dates

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def norm(s: str) -> str:
    """Arabic letters to Persian, digits to ASCII, no spaces, ZWNJ or punctuation: for label matching."""
    s = (s or "").translate(_DIGITS).replace("ي", "ی").replace("ك", "ک").replace("ى", "ی")
    return re.sub(r"[\s‌‏‎()\-–—:،,.]+", "", s)


def jalali_date(s: str) -> Optional[date]:
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", (s or "").translate(_DIGITS))
    return jalali_to_gregorian(*(int(x) for x in m.groups())) if m else None


def jalali_datetime(s: str) -> Optional[datetime]:
    s = (s or "").translate(_DIGITS)
    d = jalali_date(s)
    if d is None:
        return None
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", s)
    h, mi, se = (int(x) for x in m.groups()) if m else (0, 0, 0)
    return datetime(d.year, d.month, d.day, h, mi, se)


def parse_letter(letter: Dict[str, Any], ins_code: str) -> Optional[Dict[str, Any]]:
    """One codal_letters row, or None for a subsidiary's statement or a letter without an HTML page."""
    title = letter.get("Title") or ""
    t = norm(title)
    if re.search(r"\(شرکت", title) or not letter.get("HasHtml"):
        return None
    m = re.search(r"(\d+)ماهه", t)
    annual = "سالمالی" in t and not m
    return {
        "tracing_no": int(letter["TracingNo"]),
        "ins_code": ins_code,
        "symbol": letter.get("Symbol"),
        "title": title,
        "sent_at": jalali_datetime(letter.get("SentDateTime")),
        "published_at": jalali_datetime(letter.get("PublishDateTime")),
        "period_end": jalali_date(re.sub(r".*منتهی\s*به", "", title)),
        "period_months": 12 if annual else (int(m.group(1)) if m else None),
        "audited": "حسابرسیشده" in t,
        "consolidated": "تلفیقی" in t,
        "amendment": "اصلاحیه" in t,
        "url": letter.get("Url"),
    }


# ---------------------------------------------------------------- fetching

def _check_page(text: str, where: str) -> None:
    low = text.lower()
    if "captcha" in low or "isattacker\":true" in low.replace(" ", ""):
        raise Blocked(f"Codal asked for a captcha at {where}")


def search_page(http: HttpClient, symbol: str, page: int) -> Dict[str, Any]:
    q = urllib.parse.urlencode({**SEARCH_PARAMS, "Symbol": symbol, "PageNumber": str(page)})
    text = http.get_text(SEARCH_HOST, "/api/search/v2/q?" + q, timeout=30)
    _check_page(text, f"search {symbol}")
    data = json.loads(text)
    if data.get("IsAttacker"):
        raise Blocked("Codal flagged this client (IsAttacker)")
    return data


def statement_path(url: str, sheet: Optional[int] = None) -> str:
    """Decision.aspx redirects to InterimStatement.aspx with the same LetterSerial."""
    serial = re.search(r"LetterSerial=([^&]+)", url or "")
    if not serial:
        raise ValueError(f"no LetterSerial in {url!r}")
    path = f"/Reports/InterimStatement.aspx?LetterSerial={serial.group(1)}"
    return path if sheet is None else f"{path}&sheetId={sheet}"


def extract_datasource(html: str) -> Optional[Dict[str, Any]]:
    i = html.find("var datasource = ")
    if i < 0:
        return None
    ds, _ = json.JSONDecoder().raw_decode(html[i + len("var datasource = "):])
    return ds


def sheet_options(html: str) -> Dict[int, str]:
    return {int(v): t.strip() for v, t in re.findall(r'<option[^>]*value="(\d+)"[^>]*>\s*([^<]+?)\s*</option>', html)}


def pick_sheets(options: Dict[int, str]) -> Dict[str, int]:
    """Sheet ids of the income statement and balance sheet, by title (ids differ between templates)."""
    out: Dict[str, int] = {}
    for sid, title in options.items():
        t = norm(title)
        if "income" not in out and "سودوزیان" in t and "جامع" not in t:
            out["income"] = sid
        if "balance" not in out and ("وضعیتمالی" in t or "ترازنامه" in t):
            out["balance"] = sid
    return out


def compact_cells(ds: Dict[str, Any], sheet_code: int) -> List[List[Any]]:
    """[table, row, column, value, period_end] for every non-empty cell of the sheet."""
    cells = []
    for sh in ds.get("sheets") or []:
        if sh.get("code") != sheet_code:
            continue
        for ti, t in enumerate(sh.get("tables") or []):
            for c in t.get("cells") or []:
                if c.get("value") not in (None, ""):
                    cells.append([ti, c["rowSequence"], c["columnSequence"], c["value"], c.get("periodEndToDate") or ""])
    return cells


# ---------------------------------------------------------------- parsing

def _number(v: Any) -> Optional[float]:
    s = str(v).translate(_DIGITS).replace(",", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def labelled_values(cells: Iterable[List[Any]], period: str) -> List[Tuple[str, Optional[float]]]:
    """
    (normalized label, value in the column for `period`) pairs. A label is a cell without a period;
    its value is the first cell to its right, before the next label, whose period is `period`. This
    reads both one-sided statements and the old two-sided balance sheet.
    """
    rows: Dict[Tuple[int, int], List[List[Any]]] = {}
    for c in cells:
        rows.setdefault((c[0], c[1]), []).append(c)
    out = []
    for key in sorted(rows):
        label = None
        found = False
        for c in sorted(rows[key], key=lambda c: c[2]):
            if not c[4]:
                if label is not None and not found:
                    out.append((label, None))
                label = norm(str(c[3])) if _number(c[3]) is None and str(c[3]).strip() != "--" else None
                found = False
            elif label is not None and not found and c[4] == period:
                out.append((label, _number(c[3])))
                found = True
        if label is not None and not found:
            out.append((label, None))
    return out


def _first(pairs: List[Tuple[str, Optional[float]]], test: Callable[[str], bool]) -> Optional[float]:
    for label, v in pairs:
        if test(label) and v is not None:
            return v
    return None


def parse_income(cells: Iterable[List[Any]], period: str) -> Dict[str, Optional[float]]:
    pairs = labelled_values(cells, period)
    has_eps = lambda l: "هرسهم" in l and "تقلیل" not in l  # noqa: E731
    eps_basic = _first(pairs, lambda l: re.fullmatch(r"سود(زیان)?پایههرسهم", l) is not None)
    if eps_basic is None:  # old templates leave the total empty: add its parts
        parts = [v for l, v in pairs if has_eps(l) and "پایههرسهمناشیاز" in l and v is not None]
        eps_basic = sum(parts) if parts else None
    net_income = _first(pairs, lambda l: l == "سودزیانخالص")
    if eps_basic == 0 and net_income:  # some filers left every part as 0 although there was a profit
        eps_basic = None
    return {
        "revenue": _first(pairs, lambda l: l.startswith("درآمدهایعملیاتی") or l == "فروشخالص"),
        "net_income": net_income,
        "eps_basic": eps_basic,
        "eps_net": _first(pairs, lambda l: has_eps(l) and re.match(r"سود(زیان)?خالصهرسهم", l) is not None),
        "capital_is": _first(pairs, lambda l: l == "سرمایه"),
    }


def parse_balance(cells: Iterable[List[Any]], period: str) -> Dict[str, Optional[float]]:
    pairs = labelled_values(cells, period)
    return {
        "equity": _first(pairs, lambda l: l in ("جمعحقوقمالکانه", "جمعحقوقصاحبانسهام")),
        "total_assets": _first(pairs, lambda l: l == "جمعداراییها"),
        "capital_bs": _first(pairs, lambda l: l == "سرمایه"),
    }


def parse_statement(tracing_no: int, sheets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """codal_financials row from the stored sheets of one letter."""
    period = next((s["period_end"] for s in sheets.values() if s.get("period_end")), "")
    inc = parse_income(sheets["income"]["cells"], period) if "income" in sheets else {}
    bal = parse_balance(sheets["balance"]["cells"], period) if "balance" in sheets else {}
    row = {"tracing_no": tracing_no, "period_end_ds": jalali_date(period), **inc, **bal}
    cap_bs, cap_is = row.pop("capital_bs", None), row.pop("capital_is", None)
    row["capital"] = cap_bs if cap_bs is not None else cap_is
    missing = [k for k in ("net_income", "eps_basic", "equity", "capital") if row.get(k) is None]
    row["missing"] = ",".join(missing)
    return row


# ---------------------------------------------------------------- collection

class Collector:
    def __init__(self, con: duckdb.DuckDBPyConnection, http: HttpClient, delay_s: float = DEFAULT_DELAY_S,
                 max_requests: Optional[int] = None, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print):
        self.con, self.http, self.delay_s = con, http, delay_s
        self.max_requests, self.sleep, self.log = max_requests, sleep, log
        self.requests = 0
        self.failures = 0

    def _call(self, fn: Callable[[], Any]) -> Any:
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise StopIteration
        if self.requests:
            self.sleep(self.delay_s)
        self.requests += 1
        try:
            out = fn()
        except FetchError as e:
            self.failures += 1
            if self.failures >= MAX_FAILURES:
                raise Blocked(f"{self.failures} failures in a row, last: {e}")
            raise
        self.failures = 0
        return out

    def letters(self, ins_code: str, symbol: str) -> int:
        """Fetches every letter page for one share. Returns the number of new letters."""
        added, page, pages = 0, 1, 1
        while page <= pages:
            data = self._call(lambda: search_page(self.http, symbol, page))
            pages = int(data.get("Page") or 1)
            rows = [r for r in (parse_letter(l, ins_code) for l in data.get("Letters") or []) if r]
            n = datastore.upsert(self.con, "codal_letters", rows, keep_existing=True)
            added += n
            if rows and n == 0:  # newest first: once a page is all known, the rest is too
                break
            page += 1
        datastore.upsert(self.con, "codal_progress", [{"ins_code": ins_code, "letters_at": datetime.now()}])
        return added

    def statement(self, tracing_no: int, url: str) -> Dict[str, Any]:
        first = self._call(lambda: self.http.get_text(SITE_HOST, statement_path(url), timeout=40))
        _check_page(first, f"letter {tracing_no}")
        ds = extract_datasource(first)
        wanted = pick_sheets(sheet_options(first))
        rows = []
        for kind, sid in wanted.items():
            if ds is not None and any(sh.get("code") == sid for sh in ds.get("sheets") or []):
                page_ds = ds
            else:
                html = self._call(lambda: self.http.get_text(SITE_HOST, statement_path(url, sid), timeout=40))
                _check_page(html, f"letter {tracing_no} sheet {sid}")
                page_ds = extract_datasource(html)
            if page_ds is None:
                continue
            rows.append({
                "tracing_no": tracing_no, "sheet": kind, "fetched_at": datetime.now(),
                "template": page_ds.get("title_En"), "period_end": page_ds.get("periodEndToDate"),
                "consolidated": page_ds.get("isConsolidated"),
                "cells": json.dumps(compact_cells(page_ds, sid), ensure_ascii=False),
            })
        if not rows:  # mark it so it is not fetched again; old letters can be scanned images only
            rows.append({"tracing_no": tracing_no, "sheet": "none", "fetched_at": datetime.now(), "cells": "[]"})
        datastore.upsert(self.con, "codal_sheets", rows)
        return store_financials(self.con, tracing_no)


def load_sheets(con: duckdb.DuckDBPyConnection, tracing_no: int) -> Dict[str, Dict[str, Any]]:
    out = {}
    for sheet, period_end, cells in con.execute(
        "SELECT sheet, period_end, cells FROM codal_sheets WHERE tracing_no = ? AND sheet <> 'none'", [tracing_no]
    ).fetchall():
        out[sheet] = {"period_end": period_end, "cells": json.loads(cells)}
    return out


def store_financials(con: duckdb.DuckDBPyConnection, tracing_no: int) -> Dict[str, Any]:
    sheets = load_sheets(con, tracing_no)
    row = parse_statement(tracing_no, sheets) if sheets else {"tracing_no": tracing_no, "missing": "no sheets"}
    datastore.upsert(con, "codal_financials", [row])
    return row


def reparse(con: duckdb.DuckDBPyConnection) -> int:
    """Re-parses every stored letter after a parser change, without fetching."""
    ids = [r[0] for r in con.execute("SELECT DISTINCT tracing_no FROM codal_sheets").fetchall()]
    for t in ids:
        store_financials(con, t)
    return len(ids)


def share_universe(con: duckdb.DuckDBPyConnection, limit: Optional[int]) -> List[Tuple[str, str]]:
    """Normal-board shares, most traded over the last year first."""
    sql = """
        SELECT i.ins_code, i.ticker
        FROM instruments i LEFT JOIN daily_prices p ON p.ins_code = i.ins_code
             AND p.d >= current_date - INTERVAL 365 DAY
        WHERE regexp_matches(i.isin, '^IRO[13].{4}0001$') AND i.ticker IS NOT NULL
        GROUP BY 1, 2 ORDER BY median(p.value) DESC NULLS LAST, 1
    """
    rows = con.execute(sql).fetchall()
    return rows[:limit] if limit else rows


def run(con: duckdb.DuckDBPyConnection, http: HttpClient, symbols: Optional[List[str]] = None,
        limit: Optional[int] = None, delay_s: float = DEFAULT_DELAY_S, max_requests: Optional[int] = None,
        since: Optional[date] = date(2013, 3, 21)) -> int:
    """Letters first for each share, then its statements. Returns 0, or 2 if Codal blocked the run."""
    col = Collector(con, http, delay_s, max_requests)
    universe = share_universe(con, None)
    if symbols:
        wanted = {norm(s) for s in symbols}
        universe = [u for u in universe if norm(u[1]) in wanted]
    elif limit:
        universe = universe[:limit]
    done_letters = {r[0] for r in con.execute("SELECT ins_code FROM codal_progress").fetchall()}
    try:
        for ins_code, symbol in universe:
            if ins_code not in done_letters or symbols:
                n = col.letters(ins_code, symbol)
                print(f"  {symbol}: {n} new letters")
            todo = con.execute(
                """SELECT l.tracing_no, l.url FROM codal_letters l
                   WHERE l.ins_code = ? AND (? IS NULL OR l.published_at >= ?)
                     AND NOT EXISTS (SELECT 1 FROM codal_sheets s WHERE s.tracing_no = l.tracing_no)
                   ORDER BY l.published_at DESC""",
                [ins_code, since, since],
            ).fetchall()
            for tracing_no, url in todo:
                try:
                    row = col.statement(tracing_no, url)
                except FetchError as e:
                    print(f"    {tracing_no}: {e}")
                    continue
                print(f"    {symbol} {tracing_no}: eps {row.get('eps_basic')} equity {row.get('equity')}"
                      + (f" (missing {row['missing']})" if row.get("missing") else ""))
    except StopIteration:
        print(f"  stopped after {col.requests} requests (--max-requests)")
    except Blocked as e:
        print(f"  STOPPED: {e}. Wait at least an hour before running again.")
        return 2
    print(f"  {col.requests} requests")
    return 0
