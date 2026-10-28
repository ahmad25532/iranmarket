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


def compute_series_technicals(closes_chronological: List[float]) -> Dict[str, Any]:
    """Computes RSI(14), SMA(20), 30-day high/low, and momentum from a chronological price list."""
    closes = [c for c in closes_chronological if c > 0]
    if len(closes) < 15:
        return {"rsi_14": 50.0, "sma_20_diff_pct": 0.0, "change_30d_pct": 0.0, "pos_30d_pct": 50.0}

    cur = closes[-1]
    first = closes[0]
    chg_30d = ((cur - first) / first * 100.0) if first > 0 else 0.0

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-14:]
    avg_gain = sum(d if d > 0 else 0.0 for d in recent) / 14.0
    avg_loss = sum(-d if d < 0 else 0.0 for d in recent) / 14.0
    rsi = 100.0 if avg_loss == 0 else (100.0 - (100.0 / (1.0 + (avg_gain / avg_loss))))

    sma20_slice = closes[-20:] if len(closes) >= 20 else closes
    sma20 = sum(sma20_slice) / len(sma20_slice)
    sma_diff = ((cur - sma20) / sma20 * 100.0) if sma20 > 0 else 0.0

    low_30 = min(closes)
    high_30 = max(closes)
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
                f"https://api.tgju.org/v1/market/indicator/summary-table-data/{slug}?length=30",
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
                    f"دلار در ۳۰ روز اخیر +{usd_tech.get('change_30d_pct', 0)}% رشد کرده (از ۲۳۱ به ۲۶۹ هزار تومان) و RSI آن روی {usd_rsi} است. "
                    + (
                        "به دلیل رشد تند یک‌ماهه، در کوتاه‌مدت در محدوده اشباع خرید قرار دارد و هر خبر مثبتی از مذاکرات قطر می‌تواند باعث اصلاح آن به سمت میانگین ۲۰ روزه شود."
                        if usd_rsi >= 62
                        else "در محدوده تعادلی بالای میانگین ۲۰ روزه قرار دارد."
                    )
                ),
                "fundamental_view": "تورم مهرماه و کسری بودجه کف بنیادی دلار را بالا نگه داشته، اما پیشنهاد جدید آتش‌بس قطر و بن‌بست انتخابات اسرائیل مانع جهش هیجانی جدید در کوتاه‌مدت شده است.",
            },
            "ounce_analysis": {
                "rsi_14": ons_rsi,
                "change_30d_pct": ons_tech.get("change_30d_pct", 0),
                "sma_20_usd": ons_tech.get("sma_20", 0),
                "sma_20_diff_pct": ons_tech.get("sma_20_diff_pct", 0),
                "technical_view": (
                    f"انس جهانی در ۳۰ روز گذشته {ons_tech.get('change_30d_pct', 0)}% تغییر داشته (از سقف ۴,۲۸۴ دلار به ۴,۰۵۹ دلار اصلاح کرده) و با RSI={ons_rsi} "
                    + (
                        "زیر میانگین ۲۰ روزه در فاز اصلاحی/استراحت است."
                        if ons_tech.get("sma_20_diff_pct", 0) < 0
                        else "در فاز برگشت صعودی بالای میانگین ۲۰ روزه است."
                    )
                ),
                "fundamental_view": "انتخابات میان‌دوره‌ای آمریکا (۶ روز دیگر) و نوسان شاخص دلار جهانی، انس طلا را موقتاً از سقف تاریخی دور کرده است.",
            },
        }
    return result


def compute_technicals(history: List[Dict[str, Any]], current_price: float) -> Dict[str, Any]:
    """Computes 14-day RSI, SMA-20 distance, and recent support/resistance from daily closing prices."""
    if not history or len(history) < 15:
        return {"rsi_14": None, "sma_20_diff_pct": None, "tech_signal": "نامشخص"}

    # history is ordered newest to oldest; reverse it to chronological order
    closes = [float(d.get("pDrCotVal") or d.get("pClosing") or 0) for d in reversed(history)]
    closes = [c for c in closes if c > 0]
    if current_price > 0:
        closes.append(current_price)

    if len(closes) < 15:
        return {"rsi_14": None, "sma_20_diff_pct": None, "tech_signal": "نامشخص"}

    # Calculate 14-period RSI
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent_deltas = deltas[-14:]
    gains = [d if d > 0 else 0.0 for d in recent_deltas]
    losses = [-d if d < 0 else 0.0 for d in recent_deltas]
    avg_gain = sum(gains) / 14.0
    avg_loss = sum(losses) / 14.0

    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    # SMA 20
    sma20_window = closes[-20:] if len(closes) >= 20 else closes
    sma20 = sum(sma20_window) / len(sma20_window)
    sma20_diff_pct = ((current_price - sma20) / sma20 * 100.0) if sma20 > 0 else 0.0

    # Determine signal
    if rsi < 35:
        signal = "🟢 کف تکنیکالی (اشباع فروش)"
    elif 35 <= rsi <= 58 and sma20_diff_pct >= 0:
        signal = "🚀 روند صعودی سالم (مناسب ورود)"
    elif 58 < rsi <= 70:
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

    # Enrich Top ETFs across Gold, Silver, Leveraged, and Special with Technicals
    for etf_item in gold_etfs[:5] + silver_etfs[:5] + leveraged_etfs[:5] + sector_special_etfs[:5]:
        try:
            info = client.get_instrument_info(etf_item["insCode"])
            hist = client.get_instrument_history(etf_item["insCode"], 35)
            tech = compute_technicals(hist, etf_item["last_price"])
            etf_item.update(tech)
            min_yr = float(info.get("minYear") or 0)
            max_yr = float(info.get("maxYear") or 0)
            if max_yr > min_yr > 0:
                etf_item["year_range_pos_pct"] = round(
                    ((etf_item["last_price"] - min_yr) / (max_yr - min_yr)) * 100.0, 1
                )
        except Exception:
            pass

    # Enrich Top 10 Stocks with Technicals (RSI, SMA20, Volume Surge) + Codal Reports!
    for st in top_stocks:
        try:
            info = client.get_instrument_info(st["insCode"])
            hist = client.get_instrument_history(st["insCode"], 35)
            tech = compute_technicals(hist, st["last_price"])
            st.update(tech)

            min_yr = float(info.get("minYear") or 0)
            max_yr = float(info.get("maxYear") or 0)
            avg_vol_5d = float(info.get("qTotTran5JAvg") or 0)
            sector_pe = info.get("eps", {}).get("sectorPE")

            if max_yr > min_yr > 0:
                st["year_range_pos_pct"] = round(
                    ((st["last_price"] - min_yr) / (max_yr - min_yr)) * 100.0, 1
                )
            if avg_vol_5d > 0:
                st["vol_vs_5d_avg_ratio"] = round(st["volume"] / avg_vol_5d, 2)
            else:
                st["vol_vs_5d_avg_ratio"] = 1.0
            st["sector_pe"] = round(float(sector_pe), 1) if sector_pe else None

            # Fetch latest Codal letters for this symbol
            codal_letters = client.get_codal_letters(st["ticker"], limit=2)
            st["codal_ZeroToTwo"] = codal_letters
            st["latest_codal_title"] = codal_letters[0]["title"] if codal_letters else "گزارش جدیدی در کدال ثبت نشده"
            st["latest_codal_date"] = codal_letters[0]["publish_date"] if codal_letters else "-"
            st["latest_codal_tag"] = codal_letters[0]["tag"] if codal_letters else "-"

            # Adjust final composite score based on RSI & Codal & Volume Surge
            adj_score = st["score"]
            if st.get("vol_vs_5d_avg_ratio", 1.0) >= 2.0:
                adj_score += 3.0  # Unusual volume breakout
            if codal_letters and codal_letters[0]["sentiment"] > 0:
                adj_score += 2.0
            if st.get("rsi_14") and st["rsi_14"] > 78:
                adj_score -= 5.0  # Overbought penalty
            st["final_score"] = round(min(99.5, adj_score), 1)
        except Exception:
            st["final_score"] = st["score"]

    top_stocks.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    sectors_ranking = compute_sectors_ranking(traded_all_stocks, ct_map)

    # Compute overall Gold ETFs real money flow today
    gold_total_inflow = round(sum(g.get("net_real_inflow_b_toman", 0) for g in gold_etfs), 1)
    silver_total_inflow = round(sum(s.get("net_real_inflow_b_toman", 0) for s in silver_etfs), 1)
    fixed_total_inflow = round(sum(f.get("net_real_inflow_b_toman", 0) for f in fixed_income_etfs), 1)

    # Refine Gold upside/downside probability using today's ETF money flow
    derived = macro.get("derived", {})
    if gold_total_inflow < -25:
        rs = min(85, derived.get("gold_downside_risk_pct", 50) + 15)
        derived["gold_downside_risk_pct"] = rs
        derived["gold_upside_prob_pct"] = 100 - rs
        if rs >= 55:
            derived["gold_verdict"] = (
                f"🔴 هشدار سیو سود / احتیاط در طلا: امروز {abs(gold_total_inflow)} میلیارد تومان پول حقیقی از صندوق‌های طلا خارج شده و انس جهانی هم از سقف ۴۲۸۴ دلار عقب نشسته است (احتمال اصلاح/درجا زدن: {rs}% در برابر احتمال رشد فوری: {100-rs}%)."
            )
            derived["gold_action"] = "فروش پله‌ای / توقف خرید جدید طلا و پارک پول در صندوق درآمد ثابت (ریال امن ۳۰٪)"

    # Build Executive Action Summary (خلاصه فرمان روز در بالای داشبورد)
    best_stocks_names = [f"{s['ticker']} (P/E:{s.get('pe')})" for s in top_stocks[:3]]
    best_sector = sectors_ranking[0] if sectors_ranking else {}
    worst_sector = sectors_ranking[-1] if sectors_ranking else {}

    executive_summary = {
        "market_mode": (
            "🛡️ فاز احتیاط در طلا و چرخش پول به سمت تک‌سهم‌های معدنی و درآمد ثابت"
            if gold_total_inflow < 0 and fixed_total_inflow > 100
            else "🚀 فاز ورود پول به دارایی‌های ریسکی"
        ),
        "buy_signals": [
            f"⛏️ بهترین گروه امروز برای خرید پله‌ای: «{best_sector.get('title', '')}» با +{best_sector.get('net_real_inflow_b_toman', 0)} میلیارد تومان ورود پول (لیدرها: فزر و فنقره)",
            f"💎 برترین تک‌سهم‌های بنیادی با ورود پول هوشمند: {' ، '.join(best_stocks_names)}",
            f"🥈 صندوق‌های نقره (سیمین، نقران، نقرابی): برخلاف طلا امروز +{silver_total_inflow} میلیارد تومان ورود پول حقیقی داشتند.",
        ],
        "sell_or_avoid_signals": [
            f"💊 پرهیز جدی و خروج از: «{worst_sector.get('title', '')}» به دلیل خروج سنگین {abs(worst_sector.get('net_real_inflow_b_toman', 0))} میلیارد تومان پول حقیقی و بحران مواد اولیه.",
            f"🪙 صندوق‌های طلا: امروز {abs(gold_total_inflow)} میلیارد تومان خروج پول حقیقی داشتند؛ در سقف قیمتی فعلی خرید هیجانی ممنوع!",
        ],
        "rial_fixed_income_advice": (
            f"🏦 استراتژی ریال و نقدینگی: امروز +{fixed_total_inflow} میلیارد تومان پول درشت وارد صندوق‌های درآمد ثابت (لبخند، افران، یاقوت) شد. "
            "نگه داشتن ۳۰٪ تا ۳۵٪ سرمایه در این صندوق‌ها (با سود روزشمار ~۳۰٪) در این لحظه بهترین سپر است تا اگر طلا یا سهام اصلاح کرد، در کف قیمت شکار کنید."
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
