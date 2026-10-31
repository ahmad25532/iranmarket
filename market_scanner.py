"""
Iran Market AI — Comprehensive Multi-Factor Scanner (Phase 1 + 2 + 3)
Combines:
1. Official TSETMC Market Indices & Exact PaperType Breadth (Last Price vs Closing Price, Bourse vs FaraBourse)
2. TGJU Live Gold, Coin, Global Ounce & USD + Bubble Calculator
3. Technical Analysis Engine (14-day RSI, SMA-20 Trend, 52-Week Price Position, Volume Surge vs 5-Day Avg)
4. Smart Money Order-Book Analysis (Per-Capita Buy/Sell, Buyer Power Ratio, Real Money Inflow)
5. Codal Financial Statements & Disclosures Tracker (Monthly Sales, Interim Reports, Material Disclosures)
6. Macro/Geopolitical News & Risk Engine (Dynamic Asset Allocation: Gold vs Fixed Income vs Stocks)
"""

import sys
import json
import urllib.request
import ssl
import re
from datetime import datetime
from typing import Dict, Any, List, Optional

from tsetmc_client import TSETMCClient


def clean_html_num(val: str) -> float:
    if not val:
        return 0.0
    txt = re.sub(r"<[^>]+>", "", str(val)).replace(",", "").strip()
    try:
        return float(txt)
    except ValueError:
        return 0.0


def compute_wilder_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Computes standard Wilder's Smoothed RSI (matching TradingView / Rahavard365)."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(max(d, 0.0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0.0) for d in deltas[:period]) / period

    for d in deltas[period:]:
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def adjust_historical_closes(history_newest_first: List[Dict[str, Any]]) -> List[float]:
    """
    Adjusts daily closing prices backwards for capital increases & cash dividends (DPS)
    using TSETMC's `priceYesterday` vs the actual previous day's `pClosing`, plus split-gap detection.
    """
    if not history_newest_first:
        return []
    # Reverse to chronological: index 0 is oldest, index -1 is newest
    days = [d for d in reversed(history_newest_first) if (d.get("pClosing") or d.get("pDrCotVal"))]
    if not days:
        return []

    raw_closes = [float(d.get("pDrCotVal") or d.get("pClosing") or 0) for d in days]
    # Work backwards from newest to oldest, accumulating adjustment factor
    n = len(days)
    adj_closes = [0.0] * n
    factor = 1.0
    adj_closes[n - 1] = raw_closes[n - 1]

    for i in range(n - 1, 0, -1):
        prev_close = float(days[i - 1].get("pClosing") or days[i - 1].get("pDrCotVal") or 0)
        declared_yesterday = float(days[i].get("priceYesterday") or 0)
        cur_close = raw_closes[i]

        if prev_close > 0 and declared_yesterday > 0 and abs(declared_yesterday - prev_close) / prev_close > 0.015:
            # Official TSETMC DPS / Capital-increase adjustment ratio
            factor *= declared_yesterday / prev_close
        elif prev_close > 0 and cur_close > 0 and (cur_close / prev_close) < 0.68:
            # Unrecorded split/capital increase (>32% single-day drop impossible under normal +-10% limits)
            factor *= cur_close / prev_close

        adj_closes[i - 1] = round(raw_closes[i - 1] * factor, 2)

    return [c for c in adj_closes if c > 0]


def compute_series_technicals(closes_chronological: List[float]) -> Dict[str, Any]:
    """Computes Wilder's RSI(14), SMA(20), 30-day high/low, and momentum from a chronological price list."""
    closes = [c for c in closes_chronological if c > 0]
    if len(closes) < 15:
        return {"rsi_14": 50.0, "sma_20_diff_pct": 0.0, "change_30d_pct": 0.0, "pos_30d_pct": 50.0}

    cur = closes[-1]
    window_30 = closes[-30:] if len(closes) >= 30 else closes
    first_30 = window_30[0]
    chg_30d = ((cur - first_30) / first_30 * 100.0) if first_30 > 0 else 0.0

    rsi = compute_wilder_rsi(closes, 14) or 50.0

    sma20_slice = closes[-20:] if len(closes) >= 20 else closes
    sma20 = sum(sma20_slice) / len(sma20_slice)
    sma_diff = ((cur - sma20) / sma20 * 100.0) if sma20 > 0 else 0.0

    low_30 = min(window_30)
    high_30 = max(window_30)
    pos_30 = ((cur - low_30) / (high_30 - low_30) * 100.0) if high_30 > low_30 else 50.0

    return {
        "rsi_14": round(rsi, 1),
        "sma_20": round(sma20, 2),
        "sma_20_diff_pct": round(sma_diff, 2),
        "change_30d_pct": round(chg_30d, 1),
        "pos_30d_pct": round(pos_30, 1),
        "low_30d": low_30,
        "high_30d": high_30,
    }


def fetch_tgju_macro() -> Dict[str, Any]:
    """Fetches latest + 30-day history of USD, 18K Gold, Coin, and Global Ounce from TGJU."""
    ctx = ssl._create_unverified_context()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx)
    )
    items = {
        "usd_irr": "price_dollar_rl",
        "gold_18k_irr": "geram18",
        "coin_emami_irr": "sekee",
        "gold_ounce_usd": "ons",
    }
    result = {}
    histories = {}
    for key, slug in items.items():
        try:
            req = urllib.request.Request(
                f"https://api.tgju.org/v1/market/indicator/summary-table-data/{slug}?length=60",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r = opener.open(req, timeout=8)
            js = json.loads(r.read().decode("utf-8"))
            rows = js.get("data", [])
            if rows:
                row = rows[0]
                closes_desc = [clean_html_num(rw[3]) for rw in rows if rw and len(rw) > 3]
                closes_chrono = [c for c in reversed(closes_desc) if c > 0]
                histories[key] = compute_series_technicals(closes_chrono)
                result[key] = {
                    "price": clean_html_num(row[3]),
                    "change_pct": re.sub(r"<[^>]+>", "", str(row[5])).strip(),
                    "date_jalali": row[7],
                    "tech": histories[key],
                }
        except Exception as e:
            result[key] = {"price": 0.0, "error": str(e)}

    usd = result.get("usd_irr", {}).get("price", 0)
    ons = result.get("gold_ounce_usd", {}).get("price", 0)
    coin_mkt = result.get("coin_emami_irr", {}).get("price", 0)
    gold18_mkt = result.get("gold_18k_irr", {}).get("price", 0)

    if usd > 0 and ons > 0:
        intrinsic_18k = (ons * usd / 31.1034768) * 0.750
        intrinsic_coin = (ons * usd / 31.1034768) * 0.900 * 8.133
        gold_bubble = round(((gold18_mkt - intrinsic_18k) / intrinsic_18k) * 100, 2) if intrinsic_18k else 0
        coin_bubble = round(((coin_mkt - intrinsic_coin) / intrinsic_coin) * 100, 2) if intrinsic_coin else 0

        usd_tech = histories.get("usd_irr", {})
        ons_tech = histories.get("gold_ounce_usd", {})

        usd_rsi = usd_tech.get("rsi_14", 50)
        ons_rsi = ons_tech.get("rsi_14", 50)

        downside_risk = 35
        if usd_rsi > 70:
            downside_risk += 20
        elif usd_rsi < 40:
            downside_risk -= 15
        if ons_rsi > 70:
            downside_risk += 15
        elif ons_tech.get("sma_20_diff_pct", 0) < -1.5:
            downside_risk += 12
        if gold_bubble > 2.5:
            downside_risk += 20

        downside_risk = max(15, min(85, downside_risk))
        upside_prob = 100 - downside_risk

        if downside_risk >= 58:
            gold_verdict = "🔴 احتیاط / سیو سود پله‌ای (احتمال اصلاح یا درجا زدن طلا در کوتاه‌مدت بیشتر از رشد فوری است)"
            gold_action = "توقف خرید طلا / انتقال بخشی از سود به صندوق درآمد ثابت (ریال امن)"
        elif downside_risk <= 40:
            gold_verdict = "🟢 کم‌ریسک و مستعد رشد (فرصت خرید پله‌ای صندوق طلا و نقره)"
            gold_action = "خرید پله‌ای طلا و نقره"
        else:
            gold_verdict = "🟡 تعادل بین ریسک و بازده (نگهداری سهمیه فعلی، پرهیز از خرید هیجانی)"
            gold_action = "نگهداری (Hold) بدون خرید هیجانی"

        result["derived"] = {
            "usd_toman": round(usd / 10),
            "gold_18k_toman": round(gold18_mkt / 10),
            "coin_emami_toman": round(coin_mkt / 10),
            "intrinsic_18k_toman": round(intrinsic_18k / 10),
            "gold_18k_bubble_pct": gold_bubble,
            "intrinsic_coin_toman": round(intrinsic_coin / 10),
            "coin_bubble_pct": coin_bubble,
            "implied_usd_toman_from_gold18": round(((gold18_mkt * 31.1034768) / (ons * 0.750)) / 10),
            "gold_upside_prob_pct": upside_prob,
            "gold_downside_risk_pct": downside_risk,
            "gold_verdict": gold_verdict,
            "gold_action": gold_action,
            "usd_analysis": {
                "rsi_14": usd_rsi,
                "change_30d_pct": usd_tech.get("change_30d_pct", 0),
                "sma_20_toman": round(usd_tech.get("sma_20", 0) / 10),
                "sma_20_diff_pct": usd_tech.get("sma_20_diff_pct", 0),
                "technical_view": (
                    f"دلار در ۳۰ روز اخیر {usd_tech.get('change_30d_pct', 0):+}% تغییر داشته "
                    f"(کف ۳۰ روزه: {round(usd_tech.get('low_30d', 0)/10):,} و سقف ۳۰ روزه: {round(usd_tech.get('high_30d', 0)/10):,} تومان) "
                    f"و RSI استاندارد وایلدر آن روی {usd_rsi} است. "
                    + (
                        "به دلیل قرارگیری در محدوده اشباع خرید کوتاه‌مدت، حساسیت به اخبار سیاسی بالاست و احتمال اصلاح به سمت میانگین ۲۰ روزه وجود دارد."
                        if usd_rsi >= 65
                        else (
                            "در محدوده اشباع فروش و کف قیمتی کوتاه‌مدت قرار دارد."
                            if usd_rsi <= 35
                            else "در محدوده تعادلی نسبت به میانگین ۲۰ روزه معامله می‌شود."
                        )
                    )
                ),
                "fundamental_view": (
                    f"شکاف بین دلار آزاد ({round(usd/10):,} ت) و دلار مستتر در طلای ۱۸ عیار ({round(((gold18_mkt * 31.1034768) / (ons * 0.750)) / 10):,} ت) "
                    f"معادل {gold_bubble:+}% است. روند میان‌مدت تابع انتظارات تورمی و خروجی مذاکرات دیپلماتیک است."
                ),
            },
            "ounce_analysis": {
                "rsi_14": ons_rsi,
                "change_30d_pct": ons_tech.get("change_30d_pct", 0),
                "sma_20_usd": ons_tech.get("sma_20", 0),
                "sma_20_diff_pct": ons_tech.get("sma_20_diff_pct", 0),
                "technical_view": (
                    f"انس جهانی در ۳۰ روز گذشته {ons_tech.get('change_30d_pct', 0):+}% تغییر داشته "
                    f"(کف ۳۰ روزه: {ons_tech.get('low_30d', 0):,} $ و سقف ۳۰ روزه: {ons_tech.get('high_30d', 0):,} $) و اکنون با RSI={ons_rsi} "
                    + (
                        f"زیر میانگین ۲۰ روزه ({ons_tech.get('sma_20', 0)} $) در فاز اصلاحی/استراحت است."
                        if ons_tech.get("sma_20_diff_pct", 0) < 0
                        else f"بالای میانگین ۲۰ روزه ({ons_tech.get('sma_20', 0)} $) در روند صعودی قرار دارد."
                    )
                ),
                "fundamental_view": (
                    f"موقعیت فعلی انس در {ons_tech.get('pos_30d_pct', 50)}% بازه یک‌ماهه خود قرار دارد و همبستگی مستقیم با نوسانات شاخص دلار جهانی و ریسک‌های ژئوپلیتیک دارد."
                ),
            },
        }
    return result


def compute_ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1.0 - k))
    return ema


def compute_macd_and_bollinger(closes: List[float]) -> Dict[str, Any]:
    """Computes standard MACD(12, 26, 9) and Bollinger Bands(20, 2) on adjusted closes."""
    out: Dict[str, Any] = {
        "macd_hist": None,
        "macd_state": "-",
        "bb_pos_pct": None,
        "bb_state": "-",
    }
    if len(closes) >= 26:
        ema12 = compute_ema(closes, 12)
        ema26 = compute_ema(closes, 26)
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        signal_line = compute_ema(macd_line, 9)
        hist = macd_line[-1] - signal_line[-1]
        prev_hist = (macd_line[-2] - signal_line[-2]) if len(macd_line) >= 2 else hist
        out["macd_hist"] = round(hist, 2)
        if hist > 0 and prev_hist <= 0:
            out["macd_state"] = "🟢 تقاطع صعودی طلایی (سیگنال خرید MACD)"
        elif hist > 0 and hist >= prev_hist:
            out["macd_state"] = "📈 فاز صعودی پرقدرت (MACD > 0)"
        elif hist > 0 and hist < prev_hist:
            out["macd_state"] = "🟡 مثبت اما در حال کاهش شتاب"
        elif hist < 0 and prev_hist >= 0:
            out["macd_state"] = "🔴 تقاطع نزولی (سیگنال خروج MACD)"
        else:
            out["macd_state"] = "🔻 فاز نزولی (MACD < 0)"

    if len(closes) >= 20:
        w20 = closes[-20:]
        mean20 = sum(w20) / 20.0
        variance = sum((x - mean20) ** 2 for x in w20) / 20.0
        std20 = variance ** 0.5
        upper = mean20 + 2.0 * std20
        lower = mean20 - 2.0 * std20
        cur = closes[-1]
        if upper > lower:
            pos = ((cur - lower) / (upper - lower)) * 100.0
            out["bb_pos_pct"] = round(pos, 1)
            if pos <= 15:
                out["bb_state"] = "🟢 کف باند بولینگر (حمایت قوی)"
            elif pos >= 90:
                out["bb_state"] = "⚠️ سقف باند بولینگر (احتمال عرضه)"
            else:
                out["bb_state"] = f"در {round(pos)}% کانال بولینگر"
    return out


def compute_technicals(history: List[Dict[str, Any]], current_price: float) -> Dict[str, Any]:
    """Computes Wilder's 14-day RSI, MACD(12,26,9), Bollinger Bands(20,2) & SMA-20 on adjusted closes."""
    if not history or len(history) < 15:
        return {"rsi_14": None, "sma_20_diff_pct": None, "tech_signal": "نامشخص", "macd_state": "-", "bb_state": "-"}

    closes = adjust_historical_closes(history)
    if current_price > 0:
        if not closes or abs(current_price - closes[-1]) / max(closes[-1], 1) <= 0.25:
            closes.append(current_price)

    if len(closes) < 15:
        return {"rsi_14": None, "sma_20_diff_pct": None, "tech_signal": "نامشخص", "macd_state": "-", "bb_state": "-"}

    rsi = compute_wilder_rsi(closes, 14)
    if rsi is None:
        return {"rsi_14": None, "sma_20_diff_pct": None, "tech_signal": "نامشخص", "macd_state": "-", "bb_state": "-"}

    sma20_window = closes[-20:] if len(closes) >= 20 else closes
    sma20 = sum(sma20_window) / len(sma20_window)
    sma20_diff_pct = ((closes[-1] - sma20) / sma20 * 100.0) if sma20 > 0 else 0.0

    mb = compute_macd_and_bollinger(closes)
    macd_hist = mb.get("macd_hist") or 0

    if rsi < 33:
        signal = "🟢 کف تکنیکالی (اشباع فروش)"
    elif 33 <= rsi <= 62 and sma20_diff_pct >= 0 and macd_hist >= 0:
        signal = "🚀 روند صعودی سالم (تأیید RSI + MACD)"
    elif 60 < rsi <= 70:
        signal = "📈 مومنتوم قوی (نزدیک مقاومت)"
    elif rsi > 70:
        signal = "⚠️ اشباع خرید (احتمال اصلاح کوتاه‌مدت)"
    else:
        signal = "⚖️ در حال نوسان / خنثی"

    return {
        "rsi_14": round(rsi, 1),
        "sma_20": round(sma20),
        "sma_20_diff_pct": round(sma20_diff_pct, 1),
        "tech_signal": signal,
        **mb,
    }


def analyze_client_flow(s: Dict[str, Any], ct: Dict[str, Any]) -> Dict[str, Any]:
    """Calculates Smart Money metrics (Haghighi Buy Power, Net Real Money Inflow, Per-Capita Buy)."""
    price = float(s.get("pcl") or s.get("pdv") or 0)
    buy_i_vol = float(ct.get("buy_I_Volume", 0))
    sell_i_vol = float(ct.get("sell_I_Volume", 0))
    buy_i_cnt = int(ct.get("buy_CountI", 0))
    sell_i_cnt = int(ct.get("sell_CountI", 0))
    tot_vol = float(s.get("qtj", 0))

    per_capita_buy_toman = ((buy_i_vol * price) / buy_i_cnt / 10.0) if buy_i_cnt > 0 else 0.0
    per_capita_sell_toman = ((sell_i_vol * price) / sell_i_cnt / 10.0) if sell_i_cnt > 0 else 0.0
    power_ratio = (
        (per_capita_buy_toman / per_capita_sell_toman) if per_capita_sell_toman > 0 else 0.0
    )
    net_real_inflow_btoman = ((buy_i_vol - sell_i_vol) * price) / 1e10
    ind_buy_pct = (buy_i_vol / tot_vol * 100.0) if tot_vol > 0 else 0.0

    return {
        "per_capita_buy_m_toman": round(per_capita_buy_toman / 1e6, 2),
        "per_capita_sell_m_toman": round(per_capita_sell_toman / 1e6, 2),
        "buyer_power_ratio": round(power_ratio, 2),
        "net_real_inflow_b_toman": round(net_real_inflow_btoman, 2),
        "ind_buy_pct": round(ind_buy_pct, 1),
    }


SECTOR_DEFINITIONS = {
    "pharma": {
        "title": "💊 دارویی و مواد اولیه دارو",
        "codes": {"43"},
        "tickers": set(),
    },
    "cement": {
        "title": "🧱 سیمان، آهک و گچ",
        "codes": {"53"},
        "tickers": set(),
    },
    "gold_silver_mining": {
        "title": "⛏️ استخراج طلا، نقره، سرب و روی",
        "codes": set(),
        "tickers": {
            "فزر", "فنقره", "كاما", "فاسمين", "كيميا", "فزرين", "فروي", "فسرب",
            "فرآور", "فتوسا", "زنگان", "كروي", "فگستر", "تكنار", "فملي", "فباهنر", "كبافق"
        },
    },
    "metals_mining": {
        "title": "🏗️ فلزات اساسی و معادن سنگ‌آهن",
        "codes": {"27", "13"},
        "tickers": set(),
    },
    "chemical_petro": {
        "title": "🧪 پتروشیمی و محصولات شیمیایی",
        "codes": {"44"},
        "tickers": set(),
    },
    "oil_refinery": {
        "title": "🛢️ فرآورده‌های نفتی و پالایشی",
        "codes": {"23"},
        "tickers": set(),
    },
    "auto": {
        "title": "🚗 خودرو و ساخت قطعات",
        "codes": {"34"},
        "tickers": set(),
    },
    "banks": {
        "title": "🏦 بانک‌ها و موسسات اعتباری",
        "codes": {"57"},
        "tickers": set(),
    },
    "food": {
        "title": "🌾 غذایی، قند و شکر و زراعت",
        "codes": {"42", "38", "01"},
        "tickers": set(),
    },
    "power": {
        "title": "⚡ عرضه برق، گاز و نیروگاه‌ها",
        "codes": {"40"},
        "tickers": set(),
    },
    "realestate": {
        "title": "🏢 انبوه‌سازی و املاک",
        "codes": {"70"},
        "tickers": set(),
    },
    "it_tech": {
        "title": "💻 رایانه و فناوری اطلاعات",
        "codes": {"72", "73"},
        "tickers": set(),
    },
}


def compute_sectors_ranking(
    traded_stocks: List[Dict[str, Any]], ct_map: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Aggregates real-money flow, positive breadth, P/E, and top leaders for each key sector."""
    results = []
    for key, sdef in SECTOR_DEFINITIONS.items():
        matched = []
        for s in traded_stocks:
            csv = str(s.get("csv", "")).strip()
            ticker = str(s.get("lva", "")).strip()
            if ticker in sdef["tickers"] or csv in sdef["codes"]:
                matched.append(s)

        if not matched:
            continue

        tot_val_btoman = 0.0
        tot_inflow_btoman = 0.0
        pos_cnt = 0
        pes = []
        leaders = []

        for s in matched:
            ins_code = str(s.get("insCode", ""))
            ticker = str(s.get("lva", "")).strip()
            last_price = float(s.get("pdv", 0))
            close_price = float(s.get("pcl", 0))
            yesterday_price = float(s.get("py", 0))
            val_rial = float(s.get("qtc", 0))
            if last_price <= 0 or yesterday_price <= 0:
                continue

            close_chg = round(((close_price - yesterday_price) / yesterday_price) * 100, 2)
            if close_chg > 0:
                pos_cnt += 1

            ct = ct_map.get(ins_code, {})
            flow = analyze_client_flow(s, ct)
            tot_val_btoman += val_rial / 1e10
            tot_inflow_btoman += flow["net_real_inflow_b_toman"]

            try:
                pe_v = float(s.get("pe")) if s.get("pe") is not None else None
                if pe_v and 1.0 < pe_v < 35.0:
                    pes.append(pe_v)
            except ValueError:
                pass

            leaders.append({
                "ticker": ticker,
                "inflow": flow["net_real_inflow_b_toman"],
                "power": flow["buyer_power_ratio"],
                "chg": close_chg,
            })

        leaders.sort(key=lambda x: x["inflow"], reverse=True)
        top_3_tickers = [f"{x['ticker']} ({x['inflow']:+} م.ت)" for x in leaders[:3]]
        avg_pe = round(sum(pes) / len(pes), 1) if pes else None
        pos_pct = round((pos_cnt / len(matched)) * 100, 1)

        if tot_inflow_btoman > 15 and pos_pct >= 55:
            status = "🟢 ورود پول هوشمند و اقبال بازار"
        elif tot_inflow_btoman < -15 and pos_pct < 45:
            status = "🔴 خروج پول و فشار فروش"
        else:
            status = "⚖️ متعادل / چرخش تک‌سهمی"

        results.append({
            "key": key,
            "title": sdef["title"],
            "stocks_count": len(matched),
            "positive_pct": pos_pct,
            "avg_pe": avg_pe,
            "trade_value_b_toman": round(tot_val_btoman, 1),
            "net_real_inflow_b_toman": round(tot_inflow_btoman, 1),
            "top_leaders": " ، ".join(top_3_tickers),
            "status": status,
        })

    results.sort(key=lambda x: x["net_real_inflow_b_toman"], reverse=True)
    return results


def run_comprehensive_scan() -> Dict[str, Any]:
    client = TSETMCClient()

    # 1. Official Indices (Bourse & FaraBourse)
    ov_bourse = client.get_market_overview(1)
    ov_farabourse = client.get_market_overview(2)

    # 2. Exact PaperType Watchlists for Accurate Broker-Grade Counts
    # paperType 1 = Main Bourse & FaraBourse Stocks
    # paperType 2 = Base Market (Bazar Payeh)
    # paperType 8 = ETFs
    stocks_main = client.get_market_watch_by_paper_types([1], market=0, with_best_limits=False)
    stocks_base = client.get_market_watch_by_paper_types([2], market=0, with_best_limits=False)
    etfs_all = client.get_market_watch_by_paper_types([8], market=0, with_best_limits=False)

    ct_map = client.get_client_types_all()
    macro = fetch_tgju_macro()
    news_risk = client.get_macro_news_and_risk()

    # Exclude block-trade boards (tickers ending with '2', '3', '4') and rights issues ('ح') and deduplicate by insCode
    def dedup_normal_board(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        out = []
        for s in items:
            code = str(s.get("insCode", ""))
            t = str(s.get("lva", "")).strip()
            if not code or code in seen or not t or t.endswith("ح") or any(ch.isdigit() for ch in t):
                continue
            if s.get("qtj", 0) > 0:
                seen.add(code)
                out.append(s)
        return out

    traded_main = dedup_normal_board(stocks_main)
    traded_base = [s for s in dedup_normal_board(stocks_base) if str(s.get("insCode", "")) not in {str(x.get("insCode", "")) for x in traded_main}]
    traded_all_stocks = traded_main + traded_base
    etfs_all = [s for i, s in enumerate(etfs_all) if str(s.get("insCode", "")) not in {str(x.get("insCode", "")) for x in etfs_all[:i]}]

    pos_main_close = sum(1 for s in traded_main if s.get("pcl", 0) > s.get("py", 0))
    pos_main_last = sum(1 for s in traded_main if s.get("pdv", 0) > s.get("py", 0))
    pos_all_close = sum(1 for s in traded_all_stocks if s.get("pcl", 0) > s.get("py", 0))
    pos_all_last = sum(1 for s in traded_all_stocks if s.get("pdv", 0) > s.get("py", 0))

    gold_etfs = []
    silver_etfs = []
    leveraged_etfs = []
    sector_special_etfs = []
    fixed_income_etfs = []
    stock_candidates = []

    famous_gold_tickers = {
        "عيار", "كهربا", "طلا", "گوهر", "مثقال", "زر", "زرفام", "تابش", "آلتون", "ناب", "گلدا", "ريتون"
    }
    famous_silver_tickers = {
        "سيمين", "سيلور", "نقرابي", "نقران", "نقراتي", "نقرين", "سيلوا", "نقرآمد", "نقراط", "نقرسا", "پلاتا"
    }
    famous_leveraged_tickers = {
        "اهرم", "موج", "بيدار", "نارنج اهرم", "دوايكس", "توان", "جهش", "شتاب", "پيشران"
    }
    famous_special_tickers = {
        "سافرون": "🌸 زعفران",
        "نهال": "🌸 زعفران/کشاورزی",
        "كليد": "🏠 املاک و مسکن",
        "ارزش مسكن": "🏠 املاک و مسکن",
        "امين شهر": "🏠 املاک و مسکن",
        "دانيك": "🏠 املاک و مسکن",
        "مالك آتيه": "🏠 املاک و مسکن",
        "كاشانه": "🏠 املاک و مسکن",
        "كاخ": "🏠 املاک و مسکن",
    }
    famous_fixed_tickers = {"افران", "كاردان", "لبخند", "اعتماد", "كمند", "پارند", "صائن", "ياقوت", "سپر"}

    for s in etfs_all:
        ins_code = str(s.get("insCode", ""))
        ticker = str(s.get("lva", "")).strip()
        name = str(s.get("lvc", "")).strip()
        last_price = float(s.get("pdv", 0))
        close_price = float(s.get("pcl", 0))
        yesterday_price = float(s.get("py", 0))
        vol = float(s.get("qtj", 0))
        val_rial = float(s.get("qtc", 0))

        if last_price <= 0 or yesterday_price <= 0 or vol <= 0:
            continue

        last_change_pct = round(((last_price - yesterday_price) / yesterday_price) * 100, 2)
        close_change_pct = round(((close_price - yesterday_price) / yesterday_price) * 100, 2)
        ct = ct_map.get(ins_code, {})
        flow = analyze_client_flow(s, ct)

        item_data = {
            "ticker": ticker,
            "name": name,
            "insCode": ins_code,
            "last_price": int(last_price),
            "last_change_pct": last_change_pct,
            "close_change_pct": close_change_pct,
            "trade_value_b_toman": round(val_rial / 1e10, 1),
            **flow,
        }
        if ticker in famous_gold_tickers:
            gold_etfs.append(item_data)
        elif ticker in famous_silver_tickers:
            silver_etfs.append(item_data)
        elif ticker in famous_leveraged_tickers:
            leveraged_etfs.append(item_data)
        elif ticker in famous_special_tickers:
            item_data["category"] = famous_special_tickers[ticker]
            sector_special_etfs.append(item_data)
        elif ticker in famous_fixed_tickers:
            fixed_income_etfs.append(item_data)

    total_real_money_stocks_btoman = 0.0

    for s in traded_all_stocks:
        ins_code = str(s.get("insCode", ""))
        ticker = str(s.get("lva", "")).strip()
        name = str(s.get("lvc", "")).strip()
        last_price = float(s.get("pdv", 0))
        close_price = float(s.get("pcl", 0))
        yesterday_price = float(s.get("py", 0))
        vol = float(s.get("qtj", 0))
        val_rial = float(s.get("qtc", 0))

        if last_price <= 0 or yesterday_price <= 0 or vol <= 0:
            continue

        last_change_pct = round(((last_price - yesterday_price) / yesterday_price) * 100, 2)
        close_change_pct = round(((close_price - yesterday_price) / yesterday_price) * 100, 2)
        ct = ct_map.get(ins_code, {})
        flow = analyze_client_flow(s, ct)
        total_real_money_stocks_btoman += flow["net_real_inflow_b_toman"]

        pe_raw = s.get("pe")
        try:
            pe_val = float(pe_raw) if pe_raw is not None else None
        except ValueError:
            pe_val = None

        trade_val_btoman = val_rial / 1e10

        # Candidate selection criteria
        if (
            trade_val_btoman >= 15.0
            and flow["buyer_power_ratio"] >= 1.8
            and flow["net_real_inflow_b_toman"] >= 8.0
            and flow["per_capita_buy_m_toman"] >= 45.0
        ):
            # Initial Score
            score = 45.0
            score += min(20.0, (flow["buyer_power_ratio"] - 1.0) * 3.5)
            score += min(18.0, flow["net_real_inflow_b_toman"] * 0.4)
            if pe_val and 2.0 < pe_val <= 6.5:
                score += 17.0
            elif pe_val and 6.5 < pe_val <= 9.5:
                score += 10.0

            stock_candidates.append({
                "ticker": ticker,
                "name": name,
                "insCode": ins_code,
                "last_price": int(last_price),
                "last_change_pct": last_change_pct,
                "close_change_pct": close_change_pct,
                "pe": pe_val,
                "volume": int(vol),
                "trade_value_b_toman": round(trade_val_btoman, 1),
                "score": round(min(99.0, score), 1),
                **flow,
            })

    gold_etfs.sort(key=lambda x: x["trade_value_b_toman"], reverse=True)
    silver_etfs.sort(key=lambda x: x["trade_value_b_toman"], reverse=True)
    leveraged_etfs.sort(key=lambda x: x["trade_value_b_toman"], reverse=True)
    sector_special_etfs.sort(key=lambda x: x["trade_value_b_toman"], reverse=True)
    fixed_income_etfs.sort(key=lambda x: x["trade_value_b_toman"], reverse=True)
    stock_candidates.sort(key=lambda x: (x["score"], x["net_real_inflow_b_toman"]), reverse=True)

    top_stocks = stock_candidates[:10]

    # Enrich Top ETFs across Gold, Silver, Leveraged, and Special with Technicals + Official Redemption NAV Bubble!
    for etf_item in gold_etfs[:6] + silver_etfs[:6] + leveraged_etfs[:6] + sector_special_etfs[:6]:
        try:
            info = client.get_instrument_info(etf_item["insCode"])
            hist = client.get_instrument_history(etf_item["insCode"], 90)
            tech = compute_technicals(hist, etf_item["last_price"])
            etf_item.update(tech)
            min_yr = float(info.get("minYear") or 0)
            max_yr = float(info.get("maxYear") or 0)
            if max_yr > min_yr > 0:
                etf_item["year_range_pos_pct"] = round(
                    ((etf_item["last_price"] - min_yr) / (max_yr - min_yr)) * 100.0, 1
                )

            # Official ETF NAV from /api/Fund/GetETFByInsCode/{insCode}
            etf_nav_data = client.get_etf_nav(etf_item["insCode"])
            nav_red = float(etf_nav_data.get("pRedTran") or info.get("nav") or 0)
            if nav_red > 0 and etf_item["last_price"] > 0:
                nav_bubble = round(((etf_item["last_price"] - nav_red) / nav_red) * 100.0, 2)
                etf_item["nav_redemption"] = int(nav_red)
                etf_item["nav_bubble_pct"] = nav_bubble
                if nav_bubble > 3.5:
                    etf_item["tech_signal"] = f"⚠️ حباب بالا ({nav_bubble:+}%) | {etf_item.get('tech_signal', '')}"
        except Exception:
            pass

    # Enrich Top 10 Stocks with Technicals (Wilder RSI, Adjusted Prices) + Multi-Day Flow + OrderBook Queue + Codal!
    for st in top_stocks:
        try:
            info = client.get_instrument_info(st["insCode"])
            hist = client.get_instrument_history(st["insCode"], 90)
            tech = compute_technicals(hist, st["last_price"])
            st.update(tech)

            min_yr = float(info.get("minYear") or 0)
            max_yr = float(info.get("maxYear") or 0)
            avg_vol_5d = float(info.get("qTotTran5JAvg") or 0)
            sector_pe = info.get("eps", {}).get("sectorPE")
            static_thresh = info.get("staticThreshold", {}) or {}
            p_max_thresh = float(static_thresh.get("psGelStaMax") or 0)
            p_min_thresh = float(static_thresh.get("psGelStaMin") or 0)

            if max_yr > min_yr > 0:
                st["year_range_pos_pct"] = round(
                    ((st["last_price"] - min_yr) / (max_yr - min_yr)) * 100.0, 1
                )
            if avg_vol_5d > 0:
                st["vol_vs_5d_avg_ratio"] = round(st["volume"] / avg_vol_5d, 2)
            else:
                st["vol_vs_5d_avg_ratio"] = 1.0
            st["sector_pe"] = round(float(sector_pe), 1) if sector_pe else None

            # 1. Check 5-level Order Book (BestLimits) for Buy Queue / Sell Queue status
            bl = client.get_best_limits(st["insCode"])
            queue_status = "🔄 روان و قابل معامله"
            is_locked_buy_queue = False
            if bl:
                row1 = bl[0]
                bid_p = float(row1.get("pMeDem") or 0)
                bid_v = float(row1.get("qTitMeDem") or 0)
                ask_p = float(row1.get("pMeOf") or 0)
                ask_v = float(row1.get("qTitMeOf") or 0)
                if ask_v == 0 and bid_v > 0 and (p_max_thresh == 0 or bid_p >= p_max_thresh):
                    queue_val_bt = round((bid_v * bid_p) / 1e10, 1)
                    queue_status = f"🔒 صف خرید ({queue_val_bt} م.ت)"
                    is_locked_buy_queue = True
                elif bid_v == 0 and ask_v > 0 and (p_min_thresh == 0 or ask_p <= p_min_thresh):
                    queue_val_bt = round((ask_v * ask_p) / 1e10, 1)
                    queue_status = f"🔻 صف فروش ({queue_val_bt} م.ت)"
            st["queue_status"] = queue_status

            # 2. Check Multi-Day Smart Money Continuity (Last 5 trading days from GetClientTypeHistory)
            ct_hist = client.get_client_type_history(st["insCode"], days=5)
            pos_inflow_days = 0
            five_day_net_inflow_bt = 0.0
            for day_ct in ct_hist:
                b_val = float(day_ct.get("buy_I_Value") or 0)
                s_val = float(day_ct.get("sell_I_Value") or 0)
                net_bt = (b_val - s_val) / 1e10
                five_day_net_inflow_bt += net_bt
                if net_bt > 0:
                    pos_inflow_days += 1
            st["inflow_positive_days_of_5"] = pos_inflow_days
            st["five_day_net_inflow_b_toman"] = round(five_day_net_inflow_bt, 1)

            # 3. Fetch latest Codal letters for this symbol
            codal_letters = client.get_codal_letters(st["ticker"], limit=2)
            st["codal_ZeroToTwo"] = codal_letters
            st["latest_codal_title"] = codal_letters[0]["title"] if codal_letters else "گزارش جدیدی در کدال ثبت نشده"
            st["latest_codal_date"] = codal_letters[0]["publish_date"] if codal_letters else "-"
            st["latest_codal_tag"] = codal_letters[0]["tag"] if codal_letters else "-"

            # 4. Adjust final composite score scientifically
            adj_score = st["score"]
            if st.get("vol_vs_5d_avg_ratio", 1.0) >= 2.0:
                adj_score += 3.0
            if pos_inflow_days >= 3 and five_day_net_inflow_bt > 0:
                adj_score += 5.0  # Multi-day smart money continuity bonus
            elif pos_inflow_days <= 1 and five_day_net_inflow_bt < 0:
                adj_score -= 6.0  # Single-day spike against 5-day outflow (potential fake code-to-code)

            if codal_letters:
                if codal_letters[0]["sentiment"] > 0:
                    adj_score += 3.0
                elif codal_letters[0]["sentiment"] < 0:
                    adj_score -= 10.0  # Negative Codal disclosure penalty

            if st.get("rsi_14") and st["rsi_14"] > 72:
                adj_score -= 6.0  # Overbought penalty
            if (st["last_change_pct"] - st["close_change_pct"]) > 2.5:
                adj_score -= 3.0  # Daily ceiling gap risk
            if is_locked_buy_queue:
                adj_score -= 2.0  # Harder to execute immediately due to locked buy queue

            st["final_score"] = round(max(10.0, min(99.5, adj_score)), 1)
        except Exception:
            st["final_score"] = st["score"]

    top_stocks.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    sectors_ranking = compute_sectors_ranking(traded_all_stocks, ct_map)

    # Compute overall ETFs real money flow & average Gold ETF NAV bubble today
    gold_total_inflow = round(sum(g.get("net_real_inflow_b_toman", 0) for g in gold_etfs), 1)
    silver_total_inflow = round(sum(s.get("net_real_inflow_b_toman", 0) for s in silver_etfs), 1)
    fixed_total_inflow = round(sum(f.get("net_real_inflow_b_toman", 0) for f in fixed_income_etfs), 1)
    gold_nav_bubbles = [g["nav_bubble_pct"] for g in gold_etfs if g.get("nav_bubble_pct") is not None]
    avg_gold_etf_bubble = round(sum(gold_nav_bubbles) / len(gold_nav_bubbles), 2) if gold_nav_bubbles else 0.0

    # Refine Gold upside/downside probability dynamically using today's ETF money flow & NAV bubble
    derived = macro.get("derived", {})
    derived["avg_gold_etf_nav_bubble_pct"] = avg_gold_etf_bubble
    ons_high_30 = macro.get("gold_ounce_usd", {}).get("tech", {}).get("high_30d", 0)
    if gold_total_inflow < -25 or avg_gold_etf_bubble > 3.0:
        rs = min(85, derived.get("gold_downside_risk_pct", 50) + (12 if gold_total_inflow < -25 else 0) + (10 if avg_gold_etf_bubble > 3.0 else 0))
        derived["gold_downside_risk_pct"] = rs
        derived["gold_upside_prob_pct"] = 100 - rs
        if rs >= 55:
            derived["gold_verdict"] = (
                f"🔴 هشدار سیو سود / احتیاط در طلا: امروز {abs(gold_total_inflow)} میلیارد تومان پول حقیقی از صندوق‌های طلا خارج شده، "
                f"میانگین حباب NAV صندوق‌های طلا {avg_gold_etf_bubble:+}% است و انس جهانی از سقف ۳۰ روزه ({ons_high_30} $) فاصله دارد "
                f"(احتمال اصلاح/درجا زدن: {rs}% در برابر احتمال رشد فوری: {100-rs}%)."
            )
            derived["gold_action"] = "فروش پله‌ای / توقف خرید جدید طلا و پارک پول در صندوق درآمد ثابت (ریال امن ۳۰٪)"

    # Build 100% Dynamic Executive Action Summary (خلاصه فرمان روز در بالای داشبورد)
    best_stocks_names = [
        f"{s['ticker']} (P/E:{s.get('pe')}, تداوم پول ۵روزه:{s.get('inflow_positive_days_of_5', 1)}/5)"
        for s in top_stocks[:3]
    ]
    best_sector = sectors_ranking[0] if sectors_ranking else {}
    worst_sector = sectors_ranking[-1] if sectors_ranking else {}
    top_fixed_names = "، ".join(f["ticker"] for f in fixed_income_etfs[:3]) or "درآمد ثابت"
    top_silver_names = "، ".join(s["ticker"] for s in silver_etfs[:3]) or "نقره"

    buy_signals = []
    if best_sector and best_sector.get("net_real_inflow_b_toman", 0) > 0:
        buy_signals.append(
            f"🏆 برترین صنعت امروز از نظر ورود پول: «{best_sector.get('title', '')}» با {best_sector.get('net_real_inflow_b_toman', 0):+} میلیارد تومان خالص خرید حقیقی (لیدرها: {best_sector.get('top_leaders', '-')})"
        )
    if best_stocks_names:
        buy_signals.append(
            f"💎 برترین تک‌سهم‌های غربال‌شده (تعدیل‌شده + تداوم پول): {' ، '.join(best_stocks_names)}"
        )
    if silver_total_inflow > 0:
        buy_signals.append(
            f"🥈 صندوق‌های نقره ({top_silver_names}): امروز +{silver_total_inflow} میلیارد تومان ورود پول حقیقی ثبت کردند."
        )

    sell_signals = []
    if worst_sector and worst_sector.get("net_real_inflow_b_toman", 0) < 0:
        sell_signals.append(
            f"⚠️ ضعیف‌ترین صنعت امروز (پرهیز/خروج): «{worst_sector.get('title', '')}» به دلیل خروج سنگین {abs(worst_sector.get('net_real_inflow_b_toman', 0))} میلیارد تومان پول حقیقی (میانگین P/E گروه: {worst_sector.get('avg_pe', '-')})."
        )
    if gold_total_inflow < 0:
        sell_signals.append(
            f"🪙 صندوق‌های طلا: امروز {abs(gold_total_inflow)} میلیارد تومان خروج پول حقیقی داشتند (میانگین حباب NAV: {avg_gold_etf_bubble:+}%)؛ از خرید هیجانی در سقف خودداری کنید."
        )
    if silver_total_inflow < 0:
        sell_signals.append(
            f"🥈 صندوق‌های نقره: امروز {abs(silver_total_inflow)} میلیارد تومان خروج پول حقیقی داشتند."
        )

    executive_summary = {
        "market_mode": (
            "🛡️ فاز احتیاط در طلا و چرخش هوشمند نقدینگی به سمت صنایع پیشرو و درآمد ثابت"
            if gold_total_inflow < 0 and fixed_total_inflow > 50
            else "🚀 فاز ورود پول به دارایی‌های ریسکی"
        ),
        "buy_signals": buy_signals,
        "sell_or_avoid_signals": sell_signals,
        "rial_fixed_income_advice": (
            f"🏦 استراتژی ریال و نقدینگی: امروز {fixed_total_inflow:+} میلیارد تومان پول حقیقی در صندوق‌های درآمد ثابت ({top_fixed_names}) جابجا شد. "
            "نگه داشتن ۲۵٪ تا ۳۵٪ سرمایه در این صندوق‌ها (با سود روزشمار ~۳۰٪ بدون شکست نرخ) بهترین سپر نقدینگی برای شکار در اصلاح‌هاست."
        ),
        "flows_summary": {
            "gold_etfs_inflow_b_toman": gold_total_inflow,
            "silver_etfs_inflow_b_toman": silver_total_inflow,
            "fixed_income_inflow_b_toman": fixed_total_inflow,
            "stocks_inflow_b_toman": round(total_real_money_stocks_btoman, 1),
        },
    }

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "executive_summary": executive_summary,
        "indices": {
            "tedpix_value": ov_bourse.get("indexLastValue"),
            "tedpix_change": ov_bourse.get("indexChange"),
            "equal_weight_value": ov_bourse.get("indexEqualWeightedLastValue"),
            "equal_weight_change": ov_bourse.get("indexEqualWeightedChange"),
            "farabourse_value": ov_farabourse.get("indexLastValue"),
            "farabourse_change": ov_farabourse.get("indexChange"),
        },
        "market_breadth": {
            "main_bourse_farabourse_traded": len(traded_main),
            "main_positive_close_price": pos_main_close,
            "main_positive_last_price": pos_main_last,
            "all_stocks_with_base_traded": len(traded_all_stocks),
            "all_positive_close_price": pos_all_close,
            "all_positive_last_price": pos_all_last,
            "positive_ratio_pct": round(
                (pos_main_close / len(traded_main) * 100.0) if traded_main else 0, 1
            ),
            "total_real_money_flow_b_toman": round(total_real_money_stocks_btoman, 1),
        },
        "macro": macro,
        "news_and_risk": news_risk,
        "gold_etfs": gold_etfs,
        "silver_etfs": silver_etfs,
        "leveraged_etfs": leveraged_etfs,
        "sector_special_etfs": sector_special_etfs,
        "fixed_income_etfs": fixed_income_etfs,
        "top_smart_money_stocks": top_stocks,
        "sectors_ranking": sectors_ranking,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    report = run_comprehensive_scan()
    out_path = r"C:\Users\rzmd\.gemini\antigravity\scratch\iran-market-ai\latest_scan.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("Scan completed successfully. Top stocks count:", len(report["top_smart_money_stocks"]))
    print("Indices:", json.dumps(report["indices"], ensure_ascii=False))
    print("Breadth:", json.dumps(report["market_breadth"], ensure_ascii=False))
    print("Macro Risk:", report["news_and_risk"]["risk_score"], report["news_and_risk"]["regime"])
    for st in report["top_smart_money_stocks"][:5]:
        print(
            f"  * {st['ticker']} | Score: {st.get('final_score')} | P/E: {st.get('pe')} | RSI: {st.get('rsi_14')} | VolSurge: {st.get('vol_vs_5d_avg_ratio')}x | Codal: {st.get('latest_codal_tag')} ({st.get('latest_codal_date')})"
        )
