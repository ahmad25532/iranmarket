"""
Portfolio tracking: a daily record of value by class, a log of deposits and withdrawals, and a
time-weighted return compared with simple holdings.

Both files are private and live in data/ (ignored by git), next to portfolio.json:
- portfolio_history.json: one row per recorded day (last write of the day wins)
- portfolio_flows.json:   the deposits and withdrawals the user logs

Rules the numbers rest on:
- A flow dated d is assumed to be inside the value recorded for d, so it should carry the date the
  holdings were updated. A flow on or before the first record is already in the starting value.
- Between two records the return is Modified Dietz. With consecutive records this is the plain
  (V1 - F) / V0 - 1, so a missing day needs no guessing; a flow inside a gap is only approximate.
- Each comparison is bought once at the first record and held, without rebalancing, so it only
  depends on its start and end prices. Prices are stored with each record, so a portfolio and its
  comparisons are always measured at the same moment.
"""

import json
import os
import threading
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import datastore

HISTORY_PATH = os.path.join(datastore.BASE_DIR, "data", "portfolio_history.json")
FLOWS_PATH = os.path.join(datastore.BASE_DIR, "data", "portfolio_flows.json")
PRICE_KEYS = ("tedpix", "gold18", "usd")
RIALS_PER_TOMAN = 10.0
# A change this large (share of portfolio value) that prices and the log cannot explain gets a warning.
# Ordinary trades between holdings stay under it: they only differ from the close by fees and timing.
UNEXPLAINED_SHARE = 0.02
SHORT_PERIOD_DAYS = 30

_write_lock = threading.Lock()
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class TrackingFileError(ValueError):
    """A private file exists but cannot be used. It must be fixed by hand, never overwritten."""


# ---------------------------------------------------------------- files

def _read_list(path: str, label: str) -> List[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        raise TrackingFileError(f"فایل {label} خوانده نشد: {e}")
    if not isinstance(data, list):
        raise TrackingFileError(f"فایل {label} باید یک فهرست باشد")
    return data


def _write_list(path: str, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_history(path: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = _read_list(path or HISTORY_PATH, "تاریخچه سبد")
    try:
        for r in rows:
            date.fromisoformat(r["d"])
            float(r["total"])
    except (KeyError, TypeError, ValueError) as e:
        raise TrackingFileError(f"ردیف نامعتبر در تاریخچه سبد: {e}")
    return sorted(rows, key=lambda r: r["d"])


def load_flows(path: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = _read_list(path or FLOWS_PATH, "واریز و برداشت")
    try:
        for r in rows:
            date.fromisoformat(r["d"])
            if r["kind"] not in ("deposit", "withdrawal") or float(r["amount_toman"]) <= 0:
                raise ValueError(r)
    except (KeyError, TypeError, ValueError) as e:
        raise TrackingFileError(f"ردیف نامعتبر در واریز و برداشت: {e}")
    return sorted(rows, key=lambda r: r["d"])


def has_day(d: date, path: Optional[str] = None) -> bool:
    return any(r["d"] == d.isoformat() for r in load_history(path))


def upsert_day(day: Dict[str, Any], path: Optional[str] = None) -> None:
    with _write_lock:
        rows = [r for r in load_history(path) if r["d"] != day["d"]]
        _write_list(path or HISTORY_PATH, sorted(rows + [day], key=lambda r: r["d"]))


def add_flow(payload: Dict[str, Any], today: Optional[date] = None, path: Optional[str] = None) -> Dict[str, Any]:
    flow = validate_flow(payload, today)
    with _write_lock:
        _write_list(path or FLOWS_PATH, load_flows(path) + [flow])
    return flow


def delete_flow(flow_id: str, path: Optional[str] = None) -> None:
    with _write_lock:
        rows = load_flows(path)
        kept = [r for r in rows if r.get("id") != flow_id]
        if len(kept) == len(rows):
            raise ValueError("این ردیف پیدا نشد")
        _write_list(path or FLOWS_PATH, kept)


# ---------------------------------------------------------------- input

def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
    """Converts a Jalali date; raises ValueError for a date that does not exist."""
    if not (1 <= jm <= 12 and 1 <= jd <= (31 if jm <= 6 else 30)):
        raise ValueError("invalid Jalali date")
    result = _jalali_days(jy, jm, jd)
    if jm == 12 and jd == 30 and result >= _jalali_days(jy + 1, 1, 1):
        raise ValueError("invalid Jalali date")  # 30 Esfand exists in leap years only
    return date.fromordinal(result)


def _jalali_days(jy: int, jm: int, jd: int) -> int:
    y = jy + 1595
    days = -355668 + 365 * y + (y // 33) * 8 + ((y % 33) + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        gy += 100 * ((days - 1) // 36524)
        days = (days - 1) % 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    return date(gy, 1, 1).toordinal() + days


def parse_date(text: Any) -> date:
    """Accepts 1405/08/16 (Jalali, Persian or Latin digits) or 2026-11-07 (Gregorian)."""
    parts = str(text or "").strip().translate(_DIGITS).replace("/", "-").replace(".", "-").split("-")
    try:
        y, m, d = (int(p) for p in parts)
        return date(y, m, d) if y >= 1700 else jalali_to_gregorian(y, m, d)
    except ValueError:
        raise ValueError("تاریخ را به شکل ۱۴۰۵/۰۸/۱۶ وارد کنید")


def validate_flow(payload: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """Returns a clean log entry or raises ValueError with a Persian message."""
    d = parse_date(payload.get("d"))
    if d > (today or date.today()):
        raise ValueError("تاریخ نمی‌تواند در آینده باشد")
    kind = payload.get("kind")
    if kind not in ("deposit", "withdrawal"):
        raise ValueError("نوع باید واریز یا برداشت باشد")
    raw = str(payload.get("amount_toman", "")).translate(_DIGITS).replace(",", "").replace("٬", "").strip()
    try:
        amount = float(raw)
    except ValueError:
        raise ValueError("مبلغ باید عدد باشد")
    if not 0 < amount < float("inf"):
        raise ValueError("مبلغ باید بزرگ‌تر از صفر باشد")
    return {"id": uuid.uuid4().hex, "d": d.isoformat(), "kind": kind, "amount_toman": amount,
            "note": str(payload.get("note") or "").strip()[:200]}


# ---------------------------------------------------------------- one day's record

def build_day(d: date, valuation: Dict[str, Any], market: Dict[str, Any], source: str) -> Dict[str, Any]:
    prices, price_dates = {}, {}
    for key in PRICE_KEYS:
        quote = market.get(key) or {}
        prices[key] = quote.get("close")
        price_dates[key] = str(quote["d"]) if quote.get("d") else None
    return {
        "d": d.isoformat(),
        "total": valuation["total"],
        "by_class": {a["key"]: a["value"] for a in valuation["allocation"]},
        "positions": [{"key": i["ticker"], "qty": i["qty"], "price": i["price"]}
                      for i in valuation["items"] if "value" in i],
        "prices": prices,
        "price_dates": price_dates,
        "source": source,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- returns

def _signed_rial(flow: Dict[str, Any]) -> float:
    amount = float(flow["amount_toman"]) * RIALS_PER_TOMAN
    return amount if flow["kind"] == "deposit" else -amount


def _unexplained(prev: Dict[str, Any], cur: Dict[str, Any], flow: float) -> Optional[float]:
    """Change in value not explained by last record's quantities at today's prices plus the logged flow."""
    if "positions" not in prev or "positions" not in cur:
        return None
    now = {p["key"]: p["price"] for p in cur["positions"]}
    expected = sum(p["qty"] * now.get(p["key"], p["price"]) for p in prev["positions"])
    return cur["total"] - expected - flow


def _filled_prices(days: List[Dict[str, Any]]) -> List[Dict[str, Optional[float]]]:
    """Prices per record; a price missing from one record keeps its last known value."""
    out, last = [], {k: None for k in PRICE_KEYS}
    for day in days:
        for k in PRICE_KEYS:
            v = (day.get("prices") or {}).get(k)
            if v and v > 0:
                last[k] = v
        out.append(dict(last))
    return out


def compute(days: List[Dict[str, Any]], flows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Time-weighted returns and comparisons, from the first record that has a value and all prices."""
    prices = _filled_prices(days)
    start = next((i for i, day in enumerate(days)
                  if day["total"] > 0 and all(prices[i][k] for k in PRICE_KEYS)), None)
    out: Dict[str, Any] = {"records": len(days), "ready": False, "issues": [], "series": [], "rows": []}
    start_d = date.fromisoformat(days[start]["d"]) if start is not None else None
    last_d = date.fromisoformat(days[-1]["d"]) if days else None

    def status(flow: Dict[str, Any]) -> str:
        fd = date.fromisoformat(flow["d"])
        if start_d is None or fd > last_d:
            return "pending"
        return "before_start" if fd <= start_d else "counted"

    out["flows"] = [dict(f, status=status(f)) for f in reversed(flows)]
    out["recent"] = [{k: day.get(k) for k in ("d", "total", "by_class", "source")} for day in reversed(days[-10:])]
    if start is None or start == len(days) - 1:
        return out

    p0 = prices[start]
    growth, net_flows = 1.0, 0.0

    def point(i: int) -> Dict[str, Any]:
        p = prices[i]
        tedpix, gold, usd = p["tedpix"] / p0["tedpix"], p["gold18"] / p0["gold18"], p["usd"] / p0["usd"]
        return {"d": days[i]["d"], "you": growth, "mix": 0.5 * tedpix + 0.5 * gold, "gold": gold, "usd": usd}

    series = [point(start)]
    for i in range(start + 1, len(days)):
        prev, cur = days[i - 1], days[i]
        d0, d1 = date.fromisoformat(prev["d"]), date.fromisoformat(cur["d"])
        span = (d1 - d0).days
        inside = [f for f in flows if d0 < date.fromisoformat(f["d"]) <= d1]
        flow = sum(_signed_rial(f) for f in inside)
        weighted = sum(_signed_rial(f) * (d1 - date.fromisoformat(f["d"])).days / span for f in inside)
        net_flows += flow
        v0, v1 = prev["total"], cur["total"]
        if v0 > 0:  # with nothing invested there is no return to measure, only new money
            base = v0 + weighted
            r = (v1 - v0 - flow) / base if base > 0 else None
            if r is None or r <= -1:
                # The log and the holdings cannot both be right; a wrecked chain would hide everything else.
                out["issues"].append({"kind": "broken", "d": cur["d"]})
                r = 0.0
            growth *= 1 + r
        if weighted:
            out["issues"].append({"kind": "approximate", "d": cur["d"]})
        gap = _unexplained(prev, cur, flow)
        if gap is not None and abs(gap) > UNEXPLAINED_SHARE * max(v0, v1):
            out["issues"].append({"kind": "unexplained", "d": cur["d"], "amount": gap})
        series.append(point(i))

    final, end_price = series[-1], prices[-1]
    to_usd = p0["usd"] / end_price["usd"]
    out["rows"] = [{"key": k, "twr_rial": final[k] - 1, "twr_usd": final[k] * to_usd - 1}
                   for k in ("you", "mix", "gold", "usd")]
    # Each point in dollars, from the same start
    out["series"] = [
        dict(s, usd_view={k: s[k] * p0["usd"] / prices[start + n]["usd"] for k in ("you", "mix", "gold", "usd")})
        for n, s in enumerate(series)
    ]
    days_span = (last_d - start_d).days
    out.update(
        ready=True, start=days[start]["d"], end=days[-1]["d"], days=days_span,
        short=days_span < SHORT_PERIOD_DAYS, records_used=len(days) - start,
        value_start=days[start]["total"], value_end=days[-1]["total"], net_flows=net_flows,
        profit=days[-1]["total"] - days[start]["total"] - net_flows,
    )
    return out


def report(history_path: Optional[str] = None, flows_path: Optional[str] = None) -> Dict[str, Any]:
    """What the dashboard shows; a damaged file becomes a message and is left as it is."""
    try:
        return compute(load_history(history_path), load_flows(flows_path))
    except TrackingFileError as e:
        return {"error": str(e)}
