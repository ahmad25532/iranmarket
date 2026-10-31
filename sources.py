"""
Fetchers and pure parsers for TSETMC and TGJU.

Fetch functions only do I/O and raise on failure; parse functions turn one raw API row
into one storage row and are covered by tests.
"""

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from net import HttpClient

TSETMC = "cdn.tsetmc.com"
TGJU = "api.tgju.org"
FIPIRAN = "www.fipiran.com"

PAPER_STOCKS = 1
PAPER_BASE_MARKET = 2
PAPER_ETF = 8

MAIN_INDICES = {
    "32097828799138957": "شاخص کل بورس",
    "67130298613737946": "شاخص کل هم‌وزن",
    "43685683301327984": "شاخص کل فرابورس",
}

TGJU_SERIES = {
    "usd_irr": "price_dollar_rl",
    "gold_18k_irr": "geram18",
    "coin_emami_irr": "sekee",
    "gold_ounce_usd": "ons",
}


# ---------- helpers ----------

def parse_deven(v: Any) -> Optional[date]:
    """TSETMC dEven (e.g. 20261028) -> date. Returns None for 0/empty."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return date(n // 10000, (n // 100) % 100, n % 100)


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    f = _f(v)
    return int(f) if f is not None else None


def clean_num(v: Any) -> Optional[float]:
    """Strips HTML tags, thousands separators and % from a TGJU cell."""
    if v is None:
        return None
    txt = re.sub(r"<[^>]+>", "", str(v)).replace(",", "").replace("%", "").strip()
    return _f(txt)


def is_normal_board(ticker: str) -> bool:
    """False for block-trade boards (ticker contains a digit) and rights issues (ends with 'ح')."""
    t = (ticker or "").strip()
    return bool(t) and not t.endswith("ح") and not any(ch.isdigit() for ch in t)


# ---------- market overview ----------

def fetch_market_overview(http: HttpClient, market: int) -> Dict[str, Any]:
    data = http.get_json(TSETMC, f"/api/MarketData/GetMarketOverview/{market}")
    ov = data.get("marketOverview")
    if not ov:
        raise ValueError(f"empty marketOverview for market {market}")
    return ov


def is_market_closed(ov: Dict[str, Any]) -> bool:
    """TSETMC marks the open session with state 'S' / title 'باز'; anything else is treated as closed."""
    title = str(ov.get("marketStateTitle") or "")
    return ov.get("marketState") != "S" and "باز" not in title


def parse_overview(ov: Dict[str, Any], market: int) -> Dict[str, Any]:
    return {
        "market": market,
        "d": parse_deven(ov.get("marketActivityDEven")),
        "index_value": _f(ov.get("indexLastValue")),
        "index_change": _f(ov.get("indexChange")),
        "eqw_value": _f(ov.get("indexEqualWeightedLastValue")),
        "eqw_change": _f(ov.get("indexEqualWeightedChange")),
        "trade_value": _f(ov.get("marketActivityQTotCap")),
        "trade_volume": _f(ov.get("marketActivityQTotTran")),
        "trades": _i(ov.get("marketActivityZTotTran")),
        "market_value": _f(ov.get("marketValue")),
    }


# ---------- market watch (end-of-day snapshot) ----------

def fetch_market_watch(http: HttpClient, paper_type: int) -> List[Dict[str, Any]]:
    path = (
        f"/api/ClosingPrice/GetMarketWatch?market=0&industrialGroup=&paperTypes%5B0%5D={paper_type}"
        f"&showTraded=false&withBestLimits=false&hEven=0&RefID=0"
    )
    rows = http.get_json(TSETMC, path, timeout=30).get("marketwatch")
    if rows is None:
        raise ValueError(f"no 'marketwatch' key for paperType {paper_type}")
    return rows


def parse_watch_instrument(row: Dict[str, Any], paper_type: int, d: date) -> Dict[str, Any]:
    return {
        "ins_code": str(row.get("insCode")),
        "ticker": str(row.get("lva") or "").strip(),
        "name": str(row.get("lvc") or "").strip(),
        "isin": row.get("insID"),
        "paper_type": paper_type,
        "sector_code": str(row.get("csv") or "").strip() or None,
        "first_seen": d,
        "last_seen": d,
    }


def parse_watch_price(row: Dict[str, Any], d: date) -> Optional[Dict[str, Any]]:
    """
    Market-watch rows carry no per-row date and keep showing the previous session's price
    for instruments that did not trade, so only rows with trades today are stored.
    """
    trades = _i(row.get("ztt")) or 0
    close = _f(row.get("pcl")) or 0.0
    if trades <= 0 or close <= 0:
        return None
    return {
        "ins_code": str(row.get("insCode")),
        "d": d,
        "open": _f(row.get("pf")),
        "high": _f(row.get("pmx")),
        "low": _f(row.get("pmn")),
        "close": close,
        "last": _f(row.get("pdv")),
        "yesterday": _f(row.get("py")),
        "volume": _f(row.get("qtj")),
        "value": _f(row.get("qtc")),
        "trades": trades,
        "source": "snapshot",
    }


# ---------- client types (individual vs institutional) ----------

def fetch_client_types_all(http: HttpClient) -> List[Dict[str, Any]]:
    rows = http.get_json(TSETMC, "/api/ClientType/GetClientTypeAll", timeout=30).get("clientTypeAllDto")
    if rows is None:
        raise ValueError("no 'clientTypeAllDto' key")
    return rows


def parse_ct_snapshot(row: Dict[str, Any], d: date) -> Dict[str, Any]:
    return {
        "ins_code": str(row.get("insCode")),
        "d": d,
        "buy_i_volume": _f(row.get("buy_I_Volume")),
        "buy_n_volume": _f(row.get("buy_N_Volume")),
        "sell_i_volume": _f(row.get("sell_I_Volume")),
        "sell_n_volume": _f(row.get("sell_N_Volume")),
        "buy_i_count": _i(row.get("buy_CountI")),
        "buy_n_count": _i(row.get("buy_CountN")),
        "sell_i_count": _i(row.get("sell_CountI")),
        "sell_n_count": _i(row.get("sell_CountN")),
        "buy_i_value": None,
        "buy_n_value": None,
        "sell_i_value": None,
        "sell_n_value": None,
        "source": "snapshot",
    }


def fetch_client_type_history(http: HttpClient, ins_code: str) -> List[Dict[str, Any]]:
    return http.get_json(TSETMC, f"/api/ClientType/GetClientTypeHistory/{ins_code}", timeout=30).get("clientType") or []


def parse_ct_history(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d = parse_deven(row.get("recDate"))
    if d is None:
        return None
    return {
        "ins_code": str(row.get("insCode")),
        "d": d,
        "buy_i_volume": _f(row.get("buy_I_Volume")),
        "buy_n_volume": _f(row.get("buy_N_Volume")),
        "sell_i_volume": _f(row.get("sell_I_Volume")),
        "sell_n_volume": _f(row.get("sell_N_Volume")),
        "buy_i_count": _i(row.get("buy_I_Count")),
        "buy_n_count": _i(row.get("buy_N_Count")),
        "sell_i_count": _i(row.get("sell_I_Count")),
        "sell_n_count": _i(row.get("sell_N_Count")),
        "buy_i_value": _f(row.get("buy_I_Value")),
        "buy_n_value": _f(row.get("buy_N_Value")),
        "sell_i_value": _f(row.get("sell_I_Value")),
        "sell_n_value": _f(row.get("sell_N_Value")),
        "source": "history",
    }


# ---------- price history ----------

def fetch_price_history(http: HttpClient, ins_code: str, days: int = 0) -> List[Dict[str, Any]]:
    """days=0 returns the full history (newest first)."""
    return (
        http.get_json(TSETMC, f"/api/ClosingPrice/GetClosingPriceDailyList/{ins_code}/{days}", timeout=30).get(
            "closingPriceDaily"
        )
        or []
    )


def parse_price_history(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d = parse_deven(row.get("dEven"))
    close = _f(row.get("pClosing")) or 0.0
    if d is None or close <= 0:
        return None
    return {
        "ins_code": str(row.get("insCode")),
        "d": d,
        "open": _f(row.get("priceFirst")),
        "high": _f(row.get("priceMax")),
        "low": _f(row.get("priceMin")),
        "close": close,
        "last": _f(row.get("pDrCotVal")),
        "yesterday": _f(row.get("priceYesterday")),
        "volume": _f(row.get("qTotTran5J")),
        "value": _f(row.get("qTotCap")),
        "trades": _i(row.get("zTotTran")),
        "source": "history",
    }


# ---------- ETF NAV ----------

def fetch_etf_nav(http: HttpClient, ins_code: str) -> Dict[str, Any]:
    return http.get_json(TSETMC, f"/api/Fund/GetETFByInsCode/{ins_code}", timeout=15).get("etf") or {}


def parse_etf_nav(raw: Dict[str, Any], close: Optional[float]) -> Optional[Dict[str, Any]]:
    d = parse_deven(raw.get("deven"))
    red = _f(raw.get("pRedTran")) or 0.0
    if d is None or red <= 0:
        return None
    return {
        "ins_code": str(raw.get("insCode")),
        "d": d,
        "nav_redeem": red,
        "nav_issue": _f(raw.get("pSubTran")),
        "nav_time": _i(raw.get("hEven")),
        "close": close,
        "source": "tsetmc",
    }


# ---------- Fipiran (NAV history; fills days the daily snapshot missed) ----------

def fetch_fipiran_funds(http: HttpClient) -> List[Dict[str, Any]]:
    """All funds registered with SEO, with regNo and (for exchange-traded ones) insCode."""
    data = http.get_json(
        FIPIRAN, "/services/fund/fundcompare/", timeout=60, body={"regNos": [], "showMarketMakers": False}
    )
    items = data.get("items")
    if items is None:
        raise ValueError("no 'items' key in Fipiran fund list")
    return items


def fetch_fipiran_nav_history(http: HttpClient, reg_no: str, show_all: bool = True) -> List[Dict[str, Any]]:
    path = f"/services/chart/getfundchart?regno={reg_no}&groupId=0&showAll={'true' if show_all else 'false'}"
    data = http.get_json(FIPIRAN, path, timeout=60)
    if not isinstance(data, list):
        raise ValueError(f"unexpected Fipiran NAV payload for regNo {reg_no}")
    return data


def parse_fipiran_nav(row: Dict[str, Any], ins_code: str) -> Optional[Dict[str, Any]]:
    """Fipiran end-of-day NAV per unit (rial). cancelNav = redemption NAV, issueNav = subscription NAV."""
    try:
        d = datetime.fromisoformat(str(row.get("date"))).date()
    except ValueError:
        return None
    red = _f(row.get("cancelNav")) or 0.0
    if red <= 0:
        return None
    return {
        "ins_code": ins_code,
        "d": d,
        "nav_redeem": red,
        "nav_issue": _f(row.get("issueNav")),
        "nav_time": None,
        "close": None,
        "source": "fipiran",
    }


# ---------- indices ----------

def fetch_index_list(http: HttpClient) -> List[Dict[str, Any]]:
    """Sector + main indices of the Bourse (market 1) with their codes and names."""
    return http.get_json(TSETMC, "/api/Index/GetIndexB1LastAll/All/1").get("indexB1") or []


def fetch_index_history(http: HttpClient, index_code: str) -> List[Dict[str, Any]]:
    return http.get_json(TSETMC, f"/api/Index/GetIndexB2History/{index_code}", timeout=30).get("indexB2") or []


def parse_index_history(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d = parse_deven(row.get("dEven"))
    close = _f(row.get("xNivInuClMresIbs"))
    if d is None or not close:
        return None
    return {
        "index_code": str(row.get("insCode")),
        "d": d,
        "close": close,
        "low": _f(row.get("xNivInuPbMresIbs")),
        "high": _f(row.get("xNivInuPhMresIbs")),
    }


# ---------- TGJU ----------

def fetch_tgju(http: HttpClient, slug: str, length: int = 30) -> List[List[Any]]:
    path = f"/v1/market/indicator/summary-table-data/{slug}?length={length}"
    rows = http.get_json(TGJU, path, timeout=30, direct=False).get("data")
    if rows is None:
        raise ValueError(f"no 'data' key for TGJU {slug}")
    return rows


def parse_tgju_row(row: List[Any], series: str) -> Optional[Dict[str, Any]]:
    """Columns: open, low, high, close, change, change%, gregorian date (YYYY/MM/DD), jalali date."""
    if not row or len(row) < 7:
        return None
    try:
        d = datetime.strptime(str(row[6]).strip(), "%Y/%m/%d").date()
    except ValueError:
        return None
    close = clean_num(row[3])
    if not close:
        return None
    return {
        "series": series,
        "d": d,
        "open": clean_num(row[0]),
        "low": clean_num(row[1]),
        "high": clean_num(row[2]),
        "close": close,
    }
