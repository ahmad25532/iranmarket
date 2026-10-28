"""
Iran Market AI — Local Web Dashboard Server (RTL Persian UI)
Serves a modern, responsive Persian financial dashboard on http://127.0.0.1:8085
Features:
- One-click Live Market Scan (TSETMC + TGJU + Codal + News Risk)
- Instant Deep-Dive Stock / ETF Analyzer (Search any symbol on the fly)
- Dynamic Asset Allocation & Portfolio Calculator (Input your capital in Tomans and get exact allocation)
"""

import json
import os
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any

from market_scanner import (
    run_comprehensive_scan,
    compute_technicals,
    analyze_client_flow,
    SECTOR_DEFINITIONS,
)
from tsetmc_client import TSETMCClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCAN_FILE = os.path.join(BASE_DIR, "latest_scan.json")
PORTFOLIO_FILE = os.path.join(BASE_DIR, "my_portfolio.json")


def load_portfolio_items() -> list:
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_portfolio_items(items: list):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def evaluate_portfolio() -> Dict[str, Any]:
    """Evaluates every holding in the user's saved portfolio and issues HOLD / SELL / ADD signals."""
    items = load_portfolio_items()
    if not items:
        return {"items": [], "summary": "هنوز سهم یا صندوقی در پرتفوی خود ثبت نکرده‌اید."}

    client = TSETMCClient()
    mw = client.get_market_watch()
    ct_map = client.get_client_types_all()

    evaluated = []
    for it in items:
        sym = str(it.get("symbol", "")).strip()
        buy_price_toman = float(it.get("buy_price_toman") or 0)
        qty = float(it.get("quantity") or 0)
        q_norm = sym.replace("ی", "ي").replace("ک", "ك")

        target = None
        for s in mw:
            if str(s.get("lva", "")).strip() == q_norm:
                target = s
                break
        if not target:
            for s in mw:
                if q_norm in str(s.get("lva", "")).strip() or q_norm in str(s.get("lvc", "")).strip():
                    target = s
                    break

        if not target:
            evaluated.append({
                "symbol": sym,
                "buy_price_toman": buy_price_toman,
                "quantity": qty,
                "error": "نماد در تابلوی فعال یافت نشد",
            })
            continue

        ins_code = str(target.get("insCode", ""))
        ticker = str(target.get("lva", "")).strip()
        name = str(target.get("lvc", "")).strip()
        last_price_rial = float(target.get("pdv", 0))
        close_price_rial = float(target.get("pcl", 0))
        yesterday_rial = float(target.get("py", 0))
        cur_price_toman = round(last_price_rial / 10.0, 1)
        chg_today = round(((last_price_rial - yesterday_rial) / yesterday_rial) * 100, 2) if yesterday_rial > 0 else 0.0

        pnl_pct = (
            round(((cur_price_toman - buy_price_toman) / buy_price_toman) * 100, 2)
            if buy_price_toman > 0
            else None
        )
        cur_value_m_toman = round((cur_price_toman * qty) / 1e6, 2) if qty > 0 else None

        ct = ct_map.get(ins_code, {})
        flow = analyze_client_flow(target, ct)
        hist = client.get_instrument_history(ins_code, 35)
        tech = compute_technicals(hist, last_price_rial)
        rsi = tech.get("rsi_14")

        # Determine Exit / Hold / Add Decision
        action_badge = "🟡 نگهداری (Hold)"
        action_color = "badge-gold"
        reasons = []

        # Exit triggers (سیگنال خروج / سیو سود)
        if flow["net_real_inflow_b_toman"] < -10.0 and flow["buyer_power_ratio"] < 0.7:
            action_badge = "🔴 هشدار خروج / سیو سود (خروج پول هوشمند)"
            action_color = "badge-red"
            reasons.append(f"خروج سنگین پول حقیقی ({flow['net_real_inflow_b_toman']} میلیارد تومان) و برتری فروشندگان")
        elif rsi and rsi >= 76 and flow["buyer_power_ratio"] < 1.0:
            action_badge = "🔴 سیگنال سیو سود (اشباع خرید + ضعف خریدار)"
            action_color = "badge-red"
            reasons.append(f"چارت در اشباع خرید (RSI={rsi}) قرار گرفته و قدرت خریدار افت کرده است")
        elif pnl_pct is not None and pnl_pct <= -10.0 and flow["buyer_power_ratio"] < 0.8:
            action_badge = "🔴 هشدار حد ضرر / تعویض سهم"
            action_color = "badge-red"
            reasons.append(f"عبور از حد ضرر ({pnl_pct}%) همراه با ادامه فشار فروش در تابلو")
        elif flow["buyer_power_ratio"] >= 1.5 and flow["net_real_inflow_b_toman"] > 3.0 and (not rsi or rsi < 70):
            action_badge = "🟢 نگهداری قوی / مناسب اضافه کردن"
            action_color = "badge-green"
            reasons.append(f"ورود پول هوشمند (+{flow['net_real_inflow_b_toman']} م.ت) و قدرت خریدار {flow['buyer_power_ratio']} برابری")
        else:
            reasons.append(f"تابلو متعادل است (قدرت خریدار: {flow['buyer_power_ratio']}x، RSI: {rsi or '-'})")

        evaluated.append({
            "symbol": ticker,
            "name": name,
            "buy_price_toman": buy_price_toman,
            "cur_price_toman": cur_price_toman,
            "change_today_pct": chg_today,
            "pnl_pct": pnl_pct,
            "quantity": qty,
            "cur_value_m_toman": cur_value_m_toman,
            "inflow_b_toman": flow["net_real_inflow_b_toman"],
            "buyer_power": flow["buyer_power_ratio"],
            "rsi_14": rsi,
            "action_badge": action_badge,
            "action_color": action_color,
            "reason": " | ".join(reasons),
        })

    return {"items": evaluated}


def analyze_sector_deep_dive(sector_key: str) -> Dict[str, Any]:
    """Deep-dives an entire industry sector (e.g., Pharma, Cement, Gold/Silver Mining) across TSETMC + Codal."""
    sdef = SECTOR_DEFINITIONS.get(sector_key)
    if not sdef:
        return {"error": f"گروه صنعت «{sector_key}» یافت نشد."}

    client = TSETMCClient()
    stocks_main = client.get_market_watch_by_paper_types([1, 2], market=0, with_best_limits=False)
    ct_map = client.get_client_types_all()

    matched = []
    seen_codes = set()
    for s in stocks_main:
        code = str(s.get("insCode", ""))
        t = str(s.get("lva", "")).strip()
        csv = str(s.get("csv", "")).strip()
        if not code or code in seen_codes or not t or t.endswith("ح") or any(ch.isdigit() for ch in t):
            continue
        if s.get("qtj", 0) <= 0 or s.get("pdv", 0) <= 0:
            continue
        if t in sdef["tickers"] or csv in sdef["codes"]:
            seen_codes.add(code)
            matched.append(s)

    stocks_list = []
    tot_inflow = 0.0
    tot_val = 0.0
    pos_cnt = 0
    pes = []

    for s in matched:
        ins_code = str(s.get("insCode", ""))
        ticker = str(s.get("lva", "")).strip()
        name = str(s.get("lvc", "")).strip()
        last_price = float(s.get("pdv", 0))
        close_price = float(s.get("pcl", 0))
        yesterday_price = float(s.get("py", 0))
        val_rial = float(s.get("qtc", 0))
        pe_raw = s.get("pe")
        try:
            pe_val = float(pe_raw) if pe_raw is not None else None
            if pe_val and 1.0 < pe_val < 35.0:
                pes.append(pe_val)
        except ValueError:
            pe_val = None

        close_chg = round(((close_price - yesterday_price) / yesterday_price) * 100, 2) if yesterday_price > 0 else 0.0
        last_chg = round(((last_price - yesterday_price) / yesterday_price) * 100, 2) if yesterday_price > 0 else 0.0
        if close_chg > 0:
            pos_cnt += 1

        ct = ct_map.get(ins_code, {})
        flow = analyze_client_flow(s, ct)
        tot_inflow += flow["net_real_inflow_b_toman"]
        tot_val += val_rial / 1e10

        stocks_list.append({
            "ticker": ticker,
            "name": name,
            "insCode": ins_code,
            "last_price": int(last_price),
            "last_change_pct": last_chg,
            "close_change_pct": close_chg,
            "pe": pe_val,
            "trade_value_b_toman": round(val_rial / 1e10, 1),
            **flow,
        })

    # Sort by net real money inflow descending
    stocks_list.sort(key=lambda x: x["net_real_inflow_b_toman"], reverse=True)

    # Enrich top 4 inflow leaders and bottom 2 outflow laggards with Codal & RSI
    sample_for_codal = stocks_list[:4] + (stocks_list[-2:] if len(stocks_list) > 5 else [])
    sector_codal_feed = []
    for st in sample_for_codal:
        try:
            hist = client.get_instrument_history(st["insCode"], 35)
            tech = compute_technicals(hist, st["last_price"])
            st.update(tech)
            letters = client.get_codal_letters(st["ticker"], limit=2)
            if letters:
                st["latest_codal"] = letters[0]["title"]
                for l in letters:
                    sector_codal_feed.append(l)
        except Exception:
            pass

    avg_pe = round(sum(pes) / len(pes), 1) if pes else None
    pos_pct = round((pos_cnt / len(stocks_list)) * 100, 1) if stocks_list else 0.0

    # Generate Plain-Persian AI Verdict on the Sector
    if tot_inflow >= 25.0 and pos_pct >= 55:
        verdict = "🟢 حکم هوش مصنوعی: ورود پول هوشمند و اقبال قوی به این صنعت! (لیدرهای بالای جدول مستعد رشد هستند)"
    elif tot_inflow <= -25.0 and pos_pct <= 45:
        verdict = "🔴 حکم هوش مصنوعی: خروج پول حقیقی و ضعف تقاضا در کلیت این صنعت! (فقط تک‌سهم‌های استثنا با گزارش کدال عالی بررسی شوند)"
    else:
        verdict = "🟡 حکم هوش مصنوعی: وضعیت صنعت متعادل و دوقطبی است؛ پول هوشمند فقط وارد ۲ الی ۳ تک‌سهم بنیادی صدر جدول شده و از بقیه خارج شده است."

    return {
        "sector_key": sector_key,
        "title": sdef["title"],
        "stocks_count": len(stocks_list),
        "positive_pct": pos_pct,
        "avg_pe": avg_pe,
        "total_trade_value_b_toman": round(tot_val, 1),
        "total_net_inflow_b_toman": round(tot_inflow, 1),
        "verdict": verdict,
        "stocks": stocks_list[:18],
        "codal_feed": sector_codal_feed[:8],
    }


def analyze_single_symbol(query: str) -> Dict[str, Any]:
    """Deep-dives any symbol requested by the user across TSETMC + Codal."""
    client = TSETMCClient()
    mw = client.get_market_watch()
    ct_map = client.get_client_types_all()
    q = query.strip().replace("ی", "ي").replace("ک", "ك")

    target = None
    for s in mw:
        lva = str(s.get("lva", "")).strip()
        if lva == q:
            target = s
            break
    if not target:
        for s in mw:
            lva = str(s.get("lva", "")).strip()
            lvc = str(s.get("lvc", "")).strip()
            if q in lva or q in lvc:
                target = s
                break

    if not target:
        return {"error": f"نماد «{query}» در فهرست نمادهای فعال بازار یافت نشد."}

    ins_code = str(target.get("insCode", ""))
    ticker = str(target.get("lva", "")).strip()
    name = str(target.get("lvc", "")).strip()
    last_price = float(target.get("pdv", 0))
    close_price = float(target.get("pcl", 0))
    yesterday_price = float(target.get("py", 0))
    vol = float(target.get("qtj", 0))
    val_rial = float(target.get("qtc", 0))
    pe_raw = target.get("pe")
    try:
        pe_val = float(pe_raw) if pe_raw is not None else None
    except ValueError:
        pe_val = None

    last_change_pct = (
        round(((last_price - yesterday_price) / yesterday_price) * 100, 2)
        if yesterday_price > 0
        else 0.0
    )
    close_change_pct = (
        round(((close_price - yesterday_price) / yesterday_price) * 100, 2)
        if yesterday_price > 0
        else 0.0
    )

    ct = ct_map.get(ins_code, {})
    flow = analyze_client_flow(target, ct)
    info = client.get_instrument_info(ins_code)
    hist = client.get_instrument_history(ins_code, 40)
    tech = compute_technicals(hist, last_price)
    codal = client.get_codal_letters(ticker, limit=4)

    min_yr = float(info.get("minYear") or 0)
    max_yr = float(info.get("maxYear") or 0)
    avg_vol_5d = float(info.get("qTotTran5JAvg") or 0)
    year_pos = (
        round(((last_price - min_yr) / (max_yr - min_yr)) * 100.0, 1)
        if max_yr > min_yr > 0
        else None
    )
    vol_surge = round(vol / avg_vol_5d, 2) if avg_vol_5d > 0 else 1.0

    # Generate Plain-Persian AI Verdict
    pros = []
    cons = []
    score = 50

    if flow["buyer_power_ratio"] >= 1.5:
        pros.append(f"قدرت خریدار حقیقی قوی ({flow['buyer_power_ratio']} برابر فروشنده)")
        score += 15
    elif flow["buyer_power_ratio"] < 0.8:
        cons.append(f"فشار فروش حقیقی (قدرت خریدار به فروشنده: {flow['buyer_power_ratio']})")
        score -= 12

    if flow["net_real_inflow_b_toman"] > 2.0:
        pros.append(f"ورود پول هوشمند حقیقی (+{flow['net_real_inflow_b_toman']} میلیارد تومان)")
        score += 12
    elif flow["net_real_inflow_b_toman"] < -2.0:
        cons.append(f"خروج پول حقیقی ({flow['net_real_inflow_b_toman']} میلیارد تومان)")
        score -= 12

    if pe_val:
        if 2.0 < pe_val <= 6.8:
            pros.append(f"ارزندگی بنیادی عالی (P/E = {pe_val})")
            score += 15
        elif pe_val > 14.0:
            cons.append(f"نسبت P/E بالا ({pe_val}) و ریسک حباب قیمتی")
            score -= 10

    rsi = tech.get("rsi_14")
    if rsi:
        if rsi < 35:
            pros.append(f"قرارگیری در کف تکنیکالی و اشباع فروش (RSI = {rsi})")
            score += 12
        elif rsi > 75:
            cons.append(f"اشباع خرید در کوتاه‌مدت (RSI = {rsi})؛ احتمال استراحت قیمتی")
            score -= 8

    if vol_surge >= 1.8:
        pros.append(f"حجم معاملات مشکوک و بالا ({vol_surge} برابر میانگین ۵ روزه)")
        score += 8

    score = max(10, min(99, score))
    if score >= 75:
        verdict = "🟢 سیگنال کلی: مثبت و مستعد رشد (مناسب خرید پله‌ای)"
    elif score >= 50:
        verdict = "🟡 سیگنال کلی: متعادل / نیازمند رصد (نگهداری یا ورود کم‌حجم)"
    else:
        verdict = "🔴 سیگنال کلی: پرریسک / فشار فروش (دست نگه دارید)"

    return {
        "ticker": ticker,
        "name": name,
        "insCode": ins_code,
        "last_price": int(last_price),
        "close_price": int(close_price),
        "last_change_pct": last_change_pct,
        "close_change_pct": close_change_pct,
        "pe": pe_val,
        "trade_value_b_toman": round(val_rial / 1e10, 1),
        "year_range_pos_pct": year_pos,
        "vol_vs_5d_avg_ratio": vol_surge,
        "flow": flow,
        "tech": tech,
        "codal": codal,
        "score": score,
        "verdict": verdict,
        "pros": pros,
        "cons": cons,
    }


HTML_PAGE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>دستیار هوشمند سرمایه‌گذاری و بورس ایران | Iran Market AI</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/Vazirmatn-font-face.css" rel="stylesheet" type="text/css" />
  <style>
    :root {
      --bg: #f8fafc;
      --card: #ffffff;
      --card-hover: #f1f5f9;
      --border: #e2e8f0;
      --text: #0f172a;
      --muted: #64748b;
      --accent: #2563eb;
      --accent-soft: #eff6ff;
      --green: #059669;
      --green-bg: #d1fae5;
      --red: #e11d48;
      --red-bg: #ffe4e6;
      --gold: #d97706;
      --gold-bg: #fef3c7;
    }
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: 'Vazirmatn', 'IRANSans', 'Segoe UI', Tahoma, sans-serif;
    }
    body {
      background: var(--bg);
      color: var(--text);
      line-height: 1.75;
      padding: 24px;
      max-width: 1440px;
      margin: 0 auto;
      -webkit-font-smoothing: antialiased;
    }
    header {
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
      background: #ffffff;
      border: 1px solid var(--border); border-radius: 18px; padding: 22px 28px; margin-bottom: 24px;
      box-shadow: 0 4px 20px rgba(15, 23, 42, 0.04);
    }
    .brand h1 { font-size: 1.45rem; font-weight: 800; color: #0f172a; display: flex; align-items: center; gap: 10px; letter-spacing: -0.3px; }
    .brand p { color: var(--muted); font-size: 0.9rem; margin-top: 4px; font-weight: 500; }
    .actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    button {
      background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
      color: #ffffff; font-weight: 600; border: none;
      padding: 11px 20px; border-radius: 12px; cursor: pointer; font-size: 0.92rem;
      transition: all 0.2s ease; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.2);
    }
    button:hover { transform: translateY(-1px); box-shadow: 0 6px 16px rgba(37, 99, 235, 0.3); }
    button:disabled { opacity: 0.6; cursor: wait; }
    .grid-4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 18px; margin-bottom: 24px; }
    .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); gap: 20px; margin-bottom: 24px; }
    .card {
      background: var(--card); border: 1px solid var(--border); border-radius: 16px;
      padding: 20px 22px; box-shadow: 0 2px 12px rgba(15, 23, 42, 0.03);
      transition: box-shadow 0.2s ease;
    }
    .card:hover { box-shadow: 0 6px 20px rgba(15, 23, 42, 0.06); }
    .card h2 { font-size: 1.08rem; font-weight: 700; margin-bottom: 16px; color: #1e293b; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #f1f5f9; padding-bottom: 10px; }
    .stat-label { font-size: 0.85rem; color: var(--muted); font-weight: 500; }
    .stat-val { font-size: 1.5rem; font-weight: 800; margin: 8px 0; color: #0f172a; letter-spacing: -0.5px; }
    .badge {
      display: inline-block; padding: 4px 10px; border-radius: 8px; font-size: 0.8rem; font-weight: 700;
    }
    .badge-green { background: var(--green-bg); color: var(--green); }
    .badge-red { background: var(--red-bg); color: var(--red); }
    .badge-gold { background: var(--gold-bg); color: var(--gold); }
    table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
    th, td { padding: 12px 10px; text-align: right; border-bottom: 1px solid var(--border); }
    th { color: #475569; font-weight: 700; font-size: 0.82rem; background: #f8fafc; }
    tr:hover { background: var(--card-hover); }
    .pos { color: var(--green); font-weight: 700; }
    .neg { color: var(--red); font-weight: 700; }
    .search-box { display: flex; gap: 10px; margin-bottom: 14px; }
    input[type="text"], input[type="number"] {
      background: #f8fafc; border: 1px solid #cbd5e1; color: #0f172a;
      padding: 11px 16px; border-radius: 12px; font-size: 0.94rem; flex: 1; font-weight: 500;
      transition: border-color 0.2s;
    }
    input[type="text"]:focus, input[type="number"]:focus {
      outline: none; border-color: var(--accent); background: #ffffff;
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
    }
    .alloc-bar {
      display: flex; height: 30px; border-radius: 10px; overflow: hidden; margin: 14px 0;
      box-shadow: inset 0 1px 3px rgba(0,0,0,0.08);
    }
    .alloc-seg { display: flex; align-items: center; justify-content: center; font-size: 0.82rem; font-weight: 700; color: #ffffff; }
    .news-list { list-style: none; max-height: 280px; overflow-y: auto; }
    .news-list li { padding: 10px 4px; border-bottom: 1px dashed var(--border); font-size: 0.88rem; display: flex; justify-content: space-between; align-items: center; gap: 10px; }
    .section-mb { margin-bottom: 24px; }
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <h1>🤖 سامانه هوشمند تحلیل و سرمایه‌گذاری بورس و طلای ایران</h1>
      <p id="last-update">متصل به سرورهای TSETMC، شبکه طلا و ارز (TGJU)، کدال و خبرخوان سیاسی-اقتصادی</p>
    </div>
    <div class="actions">
      <button id="btn-refresh" onclick="triggerLiveScan()">🔄 اسکن زنده کل بازار (همین لحظه)</button>
    </div>
  </header>

  <!-- بنر فرمانده هوشمند روز (خلاصه وضعیت خرید، فروش/خروج و استراتژی ریال در یک نگاه) -->
  <div class="card section-mb" style="border: 2px solid var(--accent); background: linear-gradient(135deg, #ffffff 0%, #eff6ff 100%);">
    <h2>
      <span style="font-size:1.18rem;color:#1e3a8a;">⚡ خلاصه فرمان روز هوش مصنوعی (چه بخریم؟ چه بفروشیم؟ با ریال چه کنیم؟)</span>
      <span class="badge badge-gold" id="exec-mode-badge">در حال تحلیل...</span>
    </h2>
    <div class="grid-2" style="margin-bottom:10px;">
      <div style="background:#ffffff;padding:14px;border-radius:12px;border:1px solid #a7f3d0;">
        <div style="color:var(--green);font-weight:800;margin-bottom:6px;">🟢 سیگنال‌های خرید و ورود پول امروز:</div>
        <ul id="exec-buy-list" style="padding-right:18px;font-size:0.88rem;"></ul>
      </div>
      <div style="background:#ffffff;padding:14px;border-radius:12px;border:1px solid #fecdd3;">
        <div style="color:var(--red);font-weight:800;margin-bottom:6px;">🔴 سیگنال‌های فروش، سیو سود و پرهیز امروز:</div>
        <ul id="exec-sell-list" style="padding-right:18px;font-size:0.88rem;"></ul>
      </div>
    </div>
    <div id="exec-rial-advice" style="background:#ffffff;padding:12px 16px;border-radius:10px;border-right:4px solid var(--accent);font-size:0.89rem;font-weight:600;color:#1e293b;"></div>
  </div>

  <!-- ردیف اول: شاخص‌ها، طلا و ارز، و دماسنج ریسک کلان -->
  <div class="grid-4" id="top-cards"></div>

  <!-- بخش تحلیل تخصصی ۳ جانبه طلا، دلار و انس جهانی -->
  <div class="card section-mb">
    <h2>
      <span>👑 کالبدشکافی عمیق طلا، دلار آزاد و انس جهانی (تکنیکال ۳۰ روزه + بنیادی + احتمال صعود/سقوط)</span>
      <span class="badge badge-gold" id="gold-action-badge">...</span>
    </h2>
    <div id="gold-verdict-banner" style="padding:12px 16px;border-radius:10px;background:#fffbeb;border:1px solid #fde68a;font-weight:700;font-size:0.92rem;margin-bottom:16px;color:#78350f;"></div>
    <div class="grid-2" style="margin-bottom:0;" id="gold-deep-cards"></div>
  </div>

  <!-- بخش مدیریت پرتفوی شخصی کاربر و دستیار خروج به موقع -->
  <div class="card section-mb" style="border: 1px solid #cbd5e1;">
    <h2>
      <span>💼 پرتفوی شخصی من و رادار هشدار خروج (سیو سود / حد ضرر خودکار)</span>
      <button onclick="loadPortfolio()" style="padding:6px 14px;font-size:0.82rem;">🔄 بروزرسانی وضعیت سهم‌های من</button>
    </h2>
    <p style="font-size:0.86rem;color:var(--muted);margin-bottom:12px;">
      سهم‌ها یا صندوق‌هایی که در کارگزاری دارید را اینجا وارد کنید تا هوش مصنوعی هر روز تابلوی آن‌ها را رصد کند و هر زمان پول هوشمند در حال فرار بود یا به اشباع خرید رسید، به شما <b>سیگنال خروج</b> بدهد:
    </p>
    <div class="search-box" style="flex-wrap:wrap;">
      <input type="text" id="pf-sym" placeholder="نام نماد (مثلاً: عیار، شسپا، فولاد، سیمین)" style="min-width:180px;" />
      <input type="number" id="pf-price" placeholder="قیمت خرید شما به تومان (اختیاری - مثلاً: 71000)" style="min-width:200px;" />
      <input type="number" id="pf-qty" placeholder="تعداد سهم (اختیاری)" style="min-width:140px;" />
      <button onclick="addPortfolioItem()">➕ افزودن به پرتفوی من</button>
    </div>
    <div style="overflow-x:auto;">
      <table id="portfolio-table">
        <thead>
          <tr>
            <th>نماد</th>
            <th>نام شرکت/صندوق</th>
            <th>قیمت خرید شما</th>
            <th>قیمت فعلی (تغییر امروز)</th>
            <th>سود/زیان شما</th>
            <th>ورود/خروج پول امروز</th>
            <th>قدرت خریدار</th>
            <th>RSI چارت</th>
            <th>فرمان هوش مصنوعی (نگهداری یا خروج؟)</th>
            <th>علت تحلیلی</th>
            <th>حذف</th>
          </tr>
        </thead>
        <tbody id="portfolio-tbody">
          <tr><td colspan="11" style="text-align:center;color:var(--muted);">در حال بارگذاری پرتفوی...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ردیف دوم: ماشین حساب تقسیم سرمایه هوشمند + رادار اخبار کلان/جنگ -->
  <div class="grid-2">
    <div class="card">
      <h2><span>🛡️ استراتژیست هوشمند سبد شما (ضد ریزش و ضد تورم)</span> <span class="badge badge-gold" id="regime-badge">...</span></h2>
      <p id="strategic-advice" style="font-size: 0.9rem; color: #334155; margin-bottom: 14px; font-weight: 500;"></p>
      <div class="alloc-bar" id="alloc-bar"></div>
      <div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border);">
        <label class="stat-label">💰 مبلغ کل سرمایه شما (به میلیون تومان) را وارد کنید تا سیستم دقیقاً تقسیم کند:</label>
        <div class="search-box" style="margin-top: 8px;">
          <input type="number" id="capital-input" value="100" min="1" oninput="updateCalculator()" />
          <button onclick="updateCalculator()">محاسبه سبد</button>
        </div>
        <div id="calc-output" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-top: 10px;"></div>
      </div>
    </div>

    <div class="card">
      <h2><span>🌍 رادار اخبار کلان، سیاسی و اقتصادی (تأثیرگذار بر کل بورس)</span></h2>
      <ul class="news-list" id="news-list"></ul>
    </div>
  </div>

  <!-- ردیف دوم-ب: ماتریس ۵ کانون ژئوپلیتیک و کلان (انتخابات اسرائیل، آمریکا، میانجی‌گری قطر/آتش‌بس، انرژی، تورم) -->
  <div class="card section-mb">
    <h2>
      <span>♟️ ماتریس هوشمند سناریوهای ژئوپلیتیک و سیاسی (تحلیل اثر مستقیم بر طلا، نقره و بورس ایران)</span>
      <span class="stat-label">رصد همزمان ۵ کانون سیاسی اثرگذار از رسانه‌های جهانی</span>
    </h2>
    <div id="geo-matrix" style="display:grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 14px;"></div>
  </div>

  <!-- ردیف سوم: کالبدشکافی اختصاصی هر نماد (جستجوی زنده) -->
  <div class="card section-mb">
    <h2><span>🔍 کالبدشکافی فوری هر سهم یا صندوق (تابلوخوانی + چارت + کدال)</span></h2>
    <div class="search-box">
      <input type="text" id="sym-input" placeholder="نام نماد را بنویسید (مثلاً: عیار، سیمین، اهرم، سافرون، کلید، فولاد، شسپا)..." onkeydown="if(event.key==='Enter') analyzeSymbol()" />
      <button id="btn-sym" onclick="analyzeSymbol()">بررسی کامل نماد</button>
    </div>
    <div id="sym-result" style="display:none; margin-top:14px; padding:18px; background:#f8fafc; border-radius:14px; border:1px solid var(--border);"></div>
  </div>

  <!-- ردیف سوم-ب: رادار و کالبدشکافی صنایع بورس (راست‌آزمایی سیگنال کانال‌ها برای دارو، سیمان، استخراج طلا/نقره و...) -->
  <div class="card section-mb">
    <h2>
      <span>🏭 رادار هوشمند صنایع و راست‌آزمایی تحلیل‌ها (دارویی، سیمان، استخراج طلا/نقره، پتروشیمی و...)</span>
      <span class="stat-label">روی هر صنعت کلیک کنید تا تمام سهم‌ها و گزارش‌های کدال آن صنعت کالبدشکافی شود</span>
    </h2>
    <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px;">
      <button onclick="analyzeSector('pharma')" style="background:#f1f5f9;color:#0f172a;box-shadow:none;border:1px solid #cbd5e1;padding:8px 14px;font-size:0.84rem;">💊 بررسی صنعت دارو</button>
      <button onclick="analyzeSector('cement')" style="background:#f1f5f9;color:#0f172a;box-shadow:none;border:1px solid #cbd5e1;padding:8px 14px;font-size:0.84rem;">🧱 بررسی صنعت سیمان</button>
      <button onclick="analyzeSector('gold_silver_mining')" style="background:#fef3c7;color:#92400e;box-shadow:none;border:1px solid #fde68a;padding:8px 14px;font-size:0.84rem;">⛏️ شرکت‌های استخراج طلا، نقره و روی (فزر، فنقره، کاما...)</button>
      <button onclick="analyzeSector('chemical_petro')" style="background:#f1f5f9;color:#0f172a;box-shadow:none;border:1px solid #cbd5e1;padding:8px 14px;font-size:0.84rem;">🧪 پتروشیمی و شیمیایی</button>
      <button onclick="analyzeSector('metals_mining')" style="background:#f1f5f9;color:#0f172a;box-shadow:none;border:1px solid #cbd5e1;padding:8px 14px;font-size:0.84rem;">🏗️ فلزات و معادن</button>
      <button onclick="analyzeSector('oil_refinery')" style="background:#f1f5f9;color:#0f172a;box-shadow:none;border:1px solid #cbd5e1;padding:8px 14px;font-size:0.84rem;">🛢️ پالایشی و نفتی</button>
      <button onclick="analyzeSector('banks')" style="background:#f1f5f9;color:#0f172a;box-shadow:none;border:1px solid #cbd5e1;padding:8px 14px;font-size:0.84rem;">🏦 بانکی</button>
      <button onclick="analyzeSector('auto')" style="background:#f1f5f9;color:#0f172a;box-shadow:none;border:1px solid #cbd5e1;padding:8px 14px;font-size:0.84rem;">🚗 خودرویی</button>
    </div>
    <div id="sector-result" style="display:none; margin-bottom:18px; padding:18px; background:#f8fafc; border-radius:14px; border:2px solid var(--accent);"></div>
    <div style="overflow-x: auto;">
      <table id="sectors-table">
        <thead>
          <tr>
            <th>گروه صنعت</th>
            <th>تعداد نماد فعال</th>
            <th>درصد نمادهای مثبت</th>
            <th>میانگین P/E صنعت</th>
            <th>ارزش معاملات امروز</th>
            <th>خالص ورود/خروج پول حقیقی</th>
            <th>لیدرهای ورود پول امروز</th>
            <th>وضعیت کلی صنعت</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- ردیف چهارم: دیده‌بان فلزات گرانبها (طلا و نقره در کنار هم به عنوان گزینه‌های اصلی پوشش تورم و ریسک) -->
  <div class="grid-2">
    <div class="card">
      <h2><span>🪙 دیده‌بان صندوق‌های طلای بورس (پوشش ریسک دلار و انس طلا)</span> <span class="badge badge-gold">دارایی استراتژیک ۱</span></h2>
      <div style="overflow-x: auto;">
        <table id="gold-table">
          <thead>
            <tr>
              <th>نماد</th>
              <th>قیمت (تومان)</th>
              <th>تغییر</th>
              <th>ارزش معاملات</th>
              <th>ورود/خروج پول</th>
              <th>قدرت خریدار</th>
              <th>وضعیت تکنیکال</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h2><span>🥈 دیده‌بان صندوق‌های نقره بورس (پتانسیل نوسان و رشد دلاری بالا)</span> <span class="badge" style="background:#e2e8f0;color:#334155">دارایی استراتژیک ۲</span></h2>
      <div style="overflow-x: auto;">
        <table id="silver-table">
          <thead>
            <tr>
              <th>نماد</th>
              <th>قیمت (تومان)</th>
              <th>تغییر</th>
              <th>ارزش معاملات</th>
              <th>ورود/خروج پول</th>
              <th>قدرت خریدار</th>
              <th>وضعیت تکنیکال</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ردیف پنجم: دیده‌بان صندوق‌های اهرمی + صندوق‌های خاص (زعفران، املاک و مسکن) -->
  <div class="grid-2">
    <div class="card">
      <h2><span>🚀 دیده‌بان صندوق‌های اهرمی (مخصوص نوسان‌گیری و رالی‌های صعودی بورس)</span></h2>
      <div style="overflow-x: auto;">
        <table id="leveraged-table">
          <thead>
            <tr>
              <th>نماد</th>
              <th>قیمت (تومان)</th>
              <th>تغییر امروز</th>
              <th>ارزش معاملات</th>
              <th>ورود/خروج پول حقیقی</th>
              <th>قدرت خریدار</th>
              <th>وضعیت تکنیکال</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h2><span>🌸🏠 دیده‌بان صندوق‌های خاص: زعفران + املاک و مستغلات (سودهای فصلی و ملکی)</span></h2>
      <div style="overflow-x: auto;">
        <table id="special-table">
          <thead>
            <tr>
              <th>نماد</th>
              <th>حوزه دارایی</th>
              <th>قیمت (تومان)</th>
              <th>تغییر امروز</th>
              <th>ارزش معاملات</th>
              <th>ورود/خروج پول</th>
              <th>قدرت خریدار</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ردیف ششم: برترین سهام شکار شده امروز (ورود پول هوشمند + بنیادی + کدال + تکنیکال) -->
  <div class="card section-mb">
    <h2>
      <span>💎 شکارچی سهام: برترین تک‌سهم‌های بازار امروز (غربال‌شده از بین کل نمادهای بورس و فرابورس)</span>
      <span class="stat-label">ترکیب: ورود پول حقیقی + P/E ارزنده + تکنیکال + کدال</span>
    </h2>
    <div style="overflow-x: auto;">
      <table id="stocks-table">
        <thead>
          <tr>
            <th>نماد</th>
            <th>نام شرکت</th>
            <th>امتیاز هوش مصنوعی</th>
            <th>قیمت پایانی (تغییر)</th>
            <th>نسبت P/E</th>
            <th>ورود پول حقیقی</th>
            <th>قدرت خریدار</th>
            <th>سرانه خرید</th>
            <th>حجم به میانگین</th>
            <th>RSI (چارت)</th>
            <th>آخرین اطلاعیه کدال</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- ردیف هفتم: صندوق‌های درآمد ثابت -->
  <div class="card section-mb">
    <h2><span>🏦 دیده‌بان صندوق‌های درآمد ثابت (سنگر امن نقدینگی با ~۳۰٪ سود روزشمار بدون مالیات)</span></h2>
    <div style="overflow-x: auto;">
      <table id="fixed-table">
        <thead>
          <tr>
            <th>نماد</th>
            <th>نام صندوق</th>
            <th>قیمت (ریال)</th>
            <th>تغییر امروز</th>
            <th>ارزش معاملات</th>
            <th>ورود پول حقیقی</th>
            <th>قدرت خریدار</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

<script>
let currentData = null;

function fmt(n) {
  if (n === null || n === undefined || isNaN(n)) return '-';
  return Number(n).toLocaleString('fa-IR');
}

async function loadData() {
  const res = await fetch('/api/data');
  currentData = await res.json();
  renderAll();
}

async function triggerLiveScan() {
  const btn = document.getElementById('btn-refresh');
  btn.disabled = true;
  btn.innerText = '⏳ در حال اسکن زنده TSETMC، کدال و طلا (حدود ۱۰ ثانیه)...';
  try {
    const res = await fetch('/api/scan', { method: 'POST' });
    currentData = await res.json();
    renderAll();
  } catch (e) {
    alert('خطا در بروزرسانی: ' + e);
  } finally {
    btn.disabled = false;
    btn.innerText = '🔄 اسکن زنده کل بازار (همین لحظه)';
  }
}

function renderAll() {
  if (!currentData) return;
  document.getElementById('last-update').innerText = 'آخرین بروزرسانی زنده از سرورهای بورس و طلا: ' + currentData.timestamp;

  const idx = currentData.indices || {};
  const br = currentData.market_breadth || {};
  const mac = (currentData.macro && currentData.macro.derived) || {};
  const ons = (currentData.macro && currentData.macro.gold_ounce_usd) || {};
  const nr = currentData.news_and_risk || {};
  const ex = currentData.executive_summary || {};

  // Executive Action Banner
  document.getElementById('exec-mode-badge').innerText = ex.market_mode || 'تحلیل زنده';
  document.getElementById('exec-buy-list').innerHTML = (ex.buy_signals || []).map(x => `<li>${x}</li>`).join('');
  document.getElementById('exec-sell-list').innerHTML = (ex.sell_or_avoid_signals || []).map(x => `<li>${x}</li>`).join('');
  document.getElementById('exec-rial-advice').innerText = ex.rial_fixed_income_advice || '';

  // Deep Gold, USD & Global Ounce Section
  document.getElementById('gold-action-badge').innerText = mac.gold_action || '';
  document.getElementById('gold-verdict-banner').innerHTML = `
    ${mac.gold_verdict || ''}
    <div style="display:flex;height:20px;border-radius:6px;overflow:hidden;margin-top:8px;">
      <div style="width:${mac.gold_upside_prob_pct || 45}%;background:#10b981;color:#fff;font-size:0.75rem;display:flex;align-items:center;justify-content:center;">احتمال صعود فوری: ${mac.gold_upside_prob_pct || 45}%</div>
      <div style="width:${mac.gold_downside_risk_pct || 55}%;background:#e11d48;color:#fff;font-size:0.75rem;display:flex;align-items:center;justify-content:center;">احتمال اصلاح / درجا زدن: ${mac.gold_downside_risk_pct || 55}%</div>
    </div>
  `;
  const usdAn = mac.usd_analysis || {};
  const onsAn = mac.ounce_analysis || {};
  document.getElementById('gold-deep-cards').innerHTML = `
    <div style="background:#f8fafc;padding:14px;border-radius:12px;border:1px solid var(--border);">
      <div style="font-weight:800;color:#1e293b;margin-bottom:8px;display:flex;justify-content:space-between;">
        <span>💵 تحلیل تکنیکال و بنیادی دلار آزاد (${fmt(mac.usd_toman)} ت)</span>
        <span class="badge badge-gold">RSI(14): ${usdAn.rsi_14 || '-'} | بازده ۳۰ روزه: +${usdAn.change_30d_pct || 0}%</span>
      </div>
      <p style="font-size:0.84rem;margin-bottom:6px;"><b>📊 تکنیکال و چارت:</b> ${usdAn.technical_view || '-'} (میانگین ۲۰ روزه: ${fmt(usdAn.sma_20_toman)} تومان)</p>
      <p style="font-size:0.84rem;color:var(--muted);"><b>🏛️ فاندامنتال و سیاسی:</b> ${usdAn.fundamental_view || '-'}</p>
    </div>
    <div style="background:#f8fafc;padding:14px;border-radius:12px;border:1px solid var(--border);">
      <div style="font-weight:800;color:#1e293b;margin-bottom:8px;display:flex;justify-content:space-between;">
        <span>🌎 تحلیل تکنیکال و بنیادی انس جهانی طلا (${ons.price || '-'} $)</span>
        <span class="badge badge-gold">RSI(14): ${onsAn.rsi_14 || '-'} | تغییر ۳۰ روزه: ${onsAn.change_30d_pct || 0}%</span>
      </div>
      <p style="font-size:0.84rem;margin-bottom:6px;"><b>📊 تکنیکال و چارت:</b> ${onsAn.technical_view || '-'} (میانگین ۲۰ روزه: ${onsAn.sma_20_usd || '-'} $)</p>
      <p style="font-size:0.84rem;color:var(--muted);"><b>🏛️ فاندامنتال جهانی:</b> ${onsAn.fundamental_view || '-'}</p>
    </div>
  `;

  // Top 4 cards
  const topHtml = `
    <div class="card">
      <div class="stat-label">📈 شاخص کل بورس و وضعیت نمادها</div>
      <div class="stat-val">${fmt(Math.round(idx.tedpix_value || 0))}</div>
      <div>
        <span class="badge ${idx.tedpix_change >= 0 ? 'badge-green' : 'badge-red'}">
          ${idx.tedpix_change >= 0 ? '+' : ''}${fmt(Math.round(idx.tedpix_change || 0))} واحد
        </span>
      </div>
      <div style="margin-top: 10px; font-size: 0.83rem; color: var(--muted);">
        مثبت پایانی (بورس/فرابورس): <b style="color:var(--text)">${fmt(br.main_positive_close_price)}</b> |
        مثبت آخرین معامله (کل): <b style="color:var(--text)">${fmt(br.all_positive_last_price)}</b> از ${fmt(br.all_stocks_with_base_traded)}
      </div>
    </div>

    <div class="card">
      <div class="stat-label">💵 دلار آزاد و انس جهانی طلا</div>
      <div class="stat-val">${fmt(mac.usd_toman)} <span style="font-size:0.9rem;color:var(--muted)">تومان</span></div>
      <div>
        <span class="badge badge-gold">انس جهانی: ${ons.price || '-'} $ (${ons.change_pct || ''})</span>
      </div>
      <div style="margin-top: 10px; font-size: 0.83rem; color: var(--muted);">
        دلار مستتر در طلای ۱۸ عیار: <b style="color:var(--text)">${fmt(mac.implied_usd_toman_from_gold18)} تومان</b>
      </div>
    </div>

    <div class="card">
      <div class="stat-label">🪙 طلای ۱۸ عیار و حباب ذاتی</div>
      <div class="stat-val">${fmt(mac.gold_18k_toman)} <span style="font-size:0.9rem;color:var(--muted)">تومان</span></div>
      <div>
        <span class="badge ${mac.gold_18k_bubble_pct <= 1 ? 'badge-green' : 'badge-red'}">
          حباب طلای ۱۸ عیار: ${mac.gold_18k_bubble_pct}%
        </span>
        <span class="badge badge-gold" style="margin-right:4px;">
          حباب سکه: ${mac.coin_bubble_pct}%
        </span>
      </div>
      <div style="margin-top: 10px; font-size: 0.83rem; color: var(--muted);">
        ارزش ذاتی هر گرم ۱۸ عیار: <b style="color:var(--text)">${fmt(mac.intrinsic_18k_toman)} تومان</b>
      </div>
    </div>

    <div class="card">
      <div class="stat-label">🌊 جریان پول حقیقی و دماسنج ریسک کلان</div>
      <div class="stat-val" style="color:${br.total_real_money_flow_b_toman >= 0 ? 'var(--green)' : 'var(--red)'}">
        ${br.total_real_money_flow_b_toman >= 0 ? '+' : ''}${fmt(br.total_real_money_flow_b_toman)} <span style="font-size:0.9rem">میلیارد تومان</span>
      </div>
      <div>
        <span class="badge ${nr.risk_score >= 65 ? 'badge-red' : 'badge-green'}">
          امتیاز ریسک سیاسی/کلان: ${fmt(nr.risk_score)} از ۱۰۰
        </span>
      </div>
      <div style="margin-top: 10px; font-size: 0.83rem; color: var(--muted);">
        شاخص هم‌وزن: <b style="color:var(--text)">${fmt(Math.round(idx.equal_weight_value || 0))}</b> (${idx.equal_weight_change >= 0 ? '+' : ''}${fmt(Math.round(idx.equal_weight_change || 0))})
      </div>
    </div>
  `;
  document.getElementById('top-cards').innerHTML = topHtml;

  // Allocation & Risk
  document.getElementById('regime-badge').innerText = nr.regime || '';
  document.getElementById('strategic-advice').innerText = nr.strategic_advice || '';
  const al = nr.recommended_allocation_pct || {gold_etf: 30, silver_etf: 15, fixed_income: 30, stocks: 25};
  const silverPct = al.silver_etf || 15;
  document.getElementById('alloc-bar').innerHTML = `
    <div class="alloc-seg" style="width:${al.gold_etf}%; background:#d97706;">طلا ${al.gold_etf}%</div>
    <div class="alloc-seg" style="width:${silverPct}%; background:#64748b;">نقره ${silverPct}%</div>
    <div class="alloc-seg" style="width:${al.fixed_income}%; background:#2563eb;">درآمد ثابت ${al.fixed_income}%</div>
    <div class="alloc-seg" style="width:${al.stocks}%; background:#059669;">سهام/اهرم ${al.stocks}%</div>
  `;
  updateCalculator();

  // News list
  const newsEl = document.getElementById('news-list');
  newsEl.innerHTML = (nr.headlines || []).map(h => {
    let badge = '<span class="badge" style="background:#f1f5f9;color:#475569">خنثی</span>';
    if (h.sentiment === 'risk') badge = '<span class="badge badge-red">ریسک/تنش</span>';
    if (h.sentiment === 'positive') badge = '<span class="badge badge-green">دیپلماسی/توافق</span>';
    if (h.sentiment === 'mixed') badge = '<span class="badge badge-gold">مذاکره/بحران</span>';
    if (h.sentiment === 'geopolitical') badge = '<span class="badge" style="background:#eff6ff;color:#1d4ed8">ژئوپلیتیک/انتخابات</span>';
    const isEn = /^[A-Za-z0-9"']/.test(h.title.trim());
    const dirStyle = isEn ? 'dir="ltr" style="text-align:left; font-family: Segoe UI, sans-serif; font-size:0.84rem;"' : '';
    return `<li><div ${dirStyle}><b style="color:#2563eb; font-family: Vazirmatn, sans-serif;">[${h.source}]</b> <a href="${h.link || '#'}" target="_blank" style="color:var(--text); text-decoration:none;">${h.title}</a></div> ${badge}</li>`;
  }).join('');

  // Geopolitical Matrix
  const geoEl = document.getElementById('geo-matrix');
  if (geoEl) {
    geoEl.innerHTML = (nr.geopolitical_matrix || []).map(g => {
      const hitsHtml = (g.hits || []).map(ht => `<li style="font-size:0.78rem;color:#334155;margin-bottom:4px;">• ${ht}</li>`).join('') || '<li style="font-size:0.78rem;color:var(--muted)">در ۲۴ ساعت اخیر خبر فوری جدیدی ثبت نشده</li>';
      return `
        <div style="background:#f8fafc;border:1px solid var(--border);border-radius:12px;padding:14px;display:flex;flex-direction:column;justify-content:space-between;">
          <div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
              <b style="font-size:0.92rem;color:#0f172a;">${g.title}</b>
              <span class="badge badge-gold" style="font-size:0.72rem;">${g.status}</span>
            </div>
            <p style="font-size:0.81rem;color:#1e293b;background:#ffffff;padding:8px 10px;border-radius:8px;border-right:3px solid var(--accent);margin-bottom:10px;">
              <b>اثر بر بازار ایران:</b> ${g.market_impact}
            </p>
          </div>
          <div>
            <div style="font-size:0.75rem;color:var(--muted);font-weight:700;margin-bottom:4px;">آخرین سیگنال‌های خبری دریافت‌شده:</div>
            <ul style="list-style:none;padding:0;">${hitsHtml}</ul>
          </div>
        </div>
      `;
    }).join('');
  }

  // Top Stocks Table
  const stBody = document.querySelector('#stocks-table tbody');
  stBody.innerHTML = (currentData.top_smart_money_stocks || []).map(s => `
    <tr onclick="document.getElementById('sym-input').value='${s.ticker}'; analyzeSymbol();" style="cursor:pointer" title="برای کالبدشکافی کلیک کنید">
      <td><b style="color:var(--accent)">${s.ticker}</b></td>
      <td>${s.name}</td>
      <td><span class="badge badge-green">${s.final_score || s.score} / 100</span></td>
      <td class="${s.close_change_pct >= 0 ? 'pos' : 'neg'}">${fmt(Math.round(s.last_price/10))} ت (${s.close_change_pct >= 0 ? '+' : ''}${s.close_change_pct}%)</td>
      <td><b>${s.pe || '-'}</b></td>
      <td class="pos">+${fmt(s.net_real_inflow_b_toman)} میلیارد ت</td>
      <td><span class="badge badge-gold">${s.buyer_power_ratio}x</span></td>
      <td>${fmt(s.per_capita_buy_m_toman)} م.ت</td>
      <td>${s.vol_vs_5d_avg_ratio || 1}x</td>
      <td>${s.rsi_14 ? s.rsi_14 + ' (' + s.tech_signal + ')' : '-'}</td>
      <td style="font-size:0.78rem">${s.latest_codal_tag || '-'} <br><span style="color:var(--muted)">${s.latest_codal_date || ''}</span></td>
    </tr>
  `).join('');

  // Gold ETFs Table
  const gBody = document.querySelector('#gold-table tbody');
  gBody.innerHTML = (currentData.gold_etfs || []).map(g => `
    <tr onclick="document.getElementById('sym-input').value='${g.ticker}'; analyzeSymbol();" style="cursor:pointer" title="کلیک برای کالبدشکافی">
      <td><b style="color:var(--gold)">${g.ticker}</b></td>
      <td>${fmt(Math.round(g.last_price/10))} ت</td>
      <td class="${g.last_change_pct >= 0 ? 'pos' : 'neg'}">${g.last_change_pct >= 0 ? '+' : ''}${g.last_change_pct}%</td>
      <td>${fmt(g.trade_value_b_toman)} میلیارد ت</td>
      <td class="${g.net_real_inflow_b_toman >= 0 ? 'pos' : 'neg'}">${g.net_real_inflow_b_toman >= 0 ? '+' : ''}${fmt(g.net_real_inflow_b_toman)} میلیارد ت</td>
      <td>${g.buyer_power_ratio}x</td>
      <td style="font-size:0.78rem">${g.tech_signal || '-'}</td>
    </tr>
  `).join('');

  // Silver ETFs Table
  const sBody = document.querySelector('#silver-table tbody');
  sBody.innerHTML = (currentData.silver_etfs || []).map(s => `
    <tr onclick="document.getElementById('sym-input').value='${s.ticker}'; analyzeSymbol();" style="cursor:pointer" title="کلیک برای کالبدشکافی">
      <td><b style="color:#475569">🥈 ${s.ticker}</b></td>
      <td>${fmt(Math.round(s.last_price/10))} ت</td>
      <td class="${s.last_change_pct >= 0 ? 'pos' : 'neg'}">${s.last_change_pct >= 0 ? '+' : ''}${s.last_change_pct}%</td>
      <td>${fmt(s.trade_value_b_toman)} میلیارد ت</td>
      <td class="${s.net_real_inflow_b_toman >= 0 ? 'pos' : 'neg'}">${s.net_real_inflow_b_toman >= 0 ? '+' : ''}${fmt(s.net_real_inflow_b_toman)} میلیارد ت</td>
      <td>${s.buyer_power_ratio}x</td>
      <td style="font-size:0.78rem">${s.tech_signal || '-'}</td>
    </tr>
  `).join('');

  // Leveraged ETFs Table
  const lBody = document.querySelector('#leveraged-table tbody');
  lBody.innerHTML = (currentData.leveraged_etfs || []).map(l => `
    <tr onclick="document.getElementById('sym-input').value='${l.ticker}'; analyzeSymbol();" style="cursor:pointer" title="کلیک برای کالبدشکافی">
      <td><b style="color:#7c3aed">🚀 ${l.ticker}</b></td>
      <td>${fmt(Math.round(l.last_price/10))} ت</td>
      <td class="${l.last_change_pct >= 0 ? 'pos' : 'neg'}">${l.last_change_pct >= 0 ? '+' : ''}${l.last_change_pct}%</td>
      <td>${fmt(l.trade_value_b_toman)} میلیارد ت</td>
      <td class="${l.net_real_inflow_b_toman >= 0 ? 'pos' : 'neg'}">${l.net_real_inflow_b_toman >= 0 ? '+' : ''}${fmt(l.net_real_inflow_b_toman)} میلیارد ت</td>
      <td>${l.buyer_power_ratio}x</td>
      <td style="font-size:0.78rem">${l.tech_signal || '-'}</td>
    </tr>
  `).join('');

  // Special ETFs (Saffron & Real Estate) Table
  const spBody = document.querySelector('#special-table tbody');
  spBody.innerHTML = (currentData.sector_special_etfs || []).map(sp => `
    <tr onclick="document.getElementById('sym-input').value='${sp.ticker}'; analyzeSymbol();" style="cursor:pointer" title="کلیک برای کالبدشکافی">
      <td><b style="color:#be185d">${sp.ticker}</b></td>
      <td><span class="badge" style="background:#fdf2f8;color:#be185d">${sp.category || 'خاص'}</span></td>
      <td>${fmt(Math.round(sp.last_price/10))} ت</td>
      <td class="${sp.last_change_pct >= 0 ? 'pos' : 'neg'}">${sp.last_change_pct >= 0 ? '+' : ''}${sp.last_change_pct}%</td>
      <td>${fmt(sp.trade_value_b_toman)} میلیارد ت</td>
      <td class="${sp.net_real_inflow_b_toman >= 0 ? 'pos' : 'neg'}">${sp.net_real_inflow_b_toman >= 0 ? '+' : ''}${fmt(sp.net_real_inflow_b_toman)} میلیارد ت</td>
      <td>${sp.buyer_power_ratio}x</td>
    </tr>
  `).join('');

  // Fixed Income Table
  const fBody = document.querySelector('#fixed-table tbody');
  fBody.innerHTML = (currentData.fixed_income_etfs || []).map(f => `
    <tr>
      <td><b style="color:var(--accent)">${f.ticker}</b></td>
      <td>${f.name}</td>
      <td>${fmt(f.last_price)}</td>
      <td class="${f.last_change_pct >= 0 ? 'pos' : 'neg'}">${f.last_change_pct >= 0 ? '+' : ''}${f.last_change_pct}%</td>
      <td>${fmt(f.trade_value_b_toman)} میلیارد ت</td>
      <td class="${f.net_real_inflow_b_toman >= 0 ? 'pos' : 'neg'}">${f.net_real_inflow_b_toman >= 0 ? '+' : ''}${fmt(f.net_real_inflow_b_toman)} میلیارد ت</td>
      <td>${f.buyer_power_ratio}x</td>
    </tr>
  `).join('');

  // Sectors Ranking Table
  const secBody = document.querySelector('#sectors-table tbody');
  if (secBody) {
    secBody.innerHTML = (currentData.sectors_ranking || []).map(sec => `
      <tr onclick="analyzeSector('${sec.key}')" style="cursor:pointer" title="برای کالبدشکافی کامل این صنعت کلیک کنید">
        <td><b style="color:var(--accent)">${sec.title}</b></td>
        <td>${fmt(sec.stocks_count)} نماد</td>
        <td class="${sec.positive_pct >= 50 ? 'pos' : 'neg'}">${sec.positive_pct}%</td>
        <td><b>${sec.avg_pe || '-'}</b></td>
        <td>${fmt(sec.trade_value_b_toman)} میلیارد ت</td>
        <td class="${sec.net_real_inflow_b_toman >= 0 ? 'pos' : 'neg'}">${sec.net_real_inflow_b_toman >= 0 ? '+' : ''}${fmt(sec.net_real_inflow_b_toman)} میلیارد ت</td>
        <td style="font-size:0.8rem;direction:rtl">${sec.top_leaders || '-'}</td>
        <td style="font-size:0.82rem;font-weight:700">${sec.status}</td>
      </tr>
    `).join('');
  }
}

async function analyzeSector(sectorKey) {
  const box = document.getElementById('sector-result');
  box.style.display = 'block';
  box.innerHTML = '⏳ در حال کالبدشکافی زنده تمام نمادهای صنعت و استخراج آخرین گزارش‌های کدال...';
  try {
    const res = await fetch('/api/sector?key=' + encodeURIComponent(sectorKey));
    const d = await res.json();
    if (d.error) {
      box.innerHTML = '<span class="neg">' + d.error + '</span>';
      return;
    }
    const rowsHtml = (d.stocks || []).map(s => `
      <tr onclick="document.getElementById('sym-input').value='${s.ticker}'; analyzeSymbol(); window.scrollTo({top:250, behavior:'smooth'});" style="cursor:pointer">
        <td><b style="color:var(--accent)">${s.ticker}</b></td>
        <td>${s.name}</td>
        <td class="${s.close_change_pct >= 0 ? 'pos' : 'neg'}">${fmt(Math.round(s.last_price/10))} ت (${s.close_change_pct >= 0 ? '+' : ''}${s.close_change_pct}%)</td>
        <td><b>${s.pe || '-'}</b></td>
        <td>${fmt(s.trade_value_b_toman)} م.ت</td>
        <td class="${s.net_real_inflow_b_toman >= 0 ? 'pos' : 'neg'}">${s.net_real_inflow_b_toman >= 0 ? '+' : ''}${fmt(s.net_real_inflow_b_toman)} میلیارد ت</td>
        <td>${s.buyer_power_ratio}x</td>
        <td>${fmt(s.per_capita_buy_m_toman)} م.ت</td>
        <td style="font-size:0.78rem">${s.rsi_14 ? 'RSI: ' + s.rsi_14 + ' (' + s.tech_signal + ')' : '-'}</td>
      </tr>
    `).join('');
    const codalHtml = (d.codal_feed || []).map(c => `<li><b style="color:var(--accent)">[${c.symbol}]</b> <b>${c.tag}:</b> ${c.title} <span style="color:var(--muted)">(${c.publish_date})</span></li>`).join('') || '<li>گزارش جدیدی یافت نشد</li>';

    box.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:14px;">
        <div>
          <span style="font-size:1.25rem;font-weight:800;color:#0f172a">${d.title}</span>
          <span class="badge badge-gold" style="margin-right:8px;">میانگین P/E صنعت: ${d.avg_pe || '-'}</span>
          <span class="badge ${d.total_net_inflow_b_toman >= 0 ? 'badge-green' : 'badge-red'}" style="margin-right:6px;">
            خالص پول حقیقی کل صنعت: ${d.total_net_inflow_b_toman >= 0 ? '+' : ''}${fmt(d.total_net_inflow_b_toman)} میلیارد تومان
          </span>
        </div>
        <button onclick="document.getElementById('sector-result').style.display='none'" style="background:#e2e8f0;color:#0f172a;padding:6px 12px;font-size:0.8rem;box-shadow:none;">بستن ✕</button>
      </div>
      <div style="margin-bottom:14px;font-weight:700;font-size:0.94rem;color:#1e293b;">${d.verdict}</div>
      <div style="overflow-x:auto;margin-bottom:16px;">
        <table>
          <thead>
            <tr>
              <th>نماد</th>
              <th>نام شرکت</th>
              <th>قیمت و تغییر پایانی</th>
              <th>P/E</th>
              <th>ارزش معاملات</th>
              <th>ورود/خروج پول حقیقی</th>
              <th>قدرت خریدار</th>
              <th>سرانه خرید</th>
              <th>چارت (تکنیکال)</th>
            </tr>
          </thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
      <div style="background:#ffffff;padding:14px;border-radius:12px;border:1px solid var(--border);">
        <div style="color:var(--accent);font-weight:800;margin-bottom:8px;">📑 آخرین گزارش‌ها و صورت‌های مالی کدال برای لیدرهای این صنعت:</div>
        <ul style="padding-right:18px;font-size:0.85rem;">${codalHtml}</ul>
      </div>
    `;
  } catch (e) {
    box.innerHTML = 'خطا در بررسی صنعت: ' + e;
  }
}

function updateCalculator() {
  if (!currentData || !currentData.news_and_risk) return;
  const cap = parseFloat(document.getElementById('capital-input').value) || 0;
  const al = currentData.news_and_risk.recommended_allocation_pct;
  const silverPct = al.silver_etf || 15;
  const gAmt = ((cap * al.gold_etf) / 100).toFixed(1);
  const silAmt = ((cap * silverPct) / 100).toFixed(1);
  const fAmt = ((cap * al.fixed_income) / 100).toFixed(1);
  const sAmt = ((cap * al.stocks) / 100).toFixed(1);
  document.getElementById('calc-output').innerHTML = `
    <div style="background:#fffbeb;padding:12px;border-radius:12px;border:1px solid #fde68a;">
      <div class="stat-label" style="color:#b45309;font-weight:700;">🪙 صندوق طلا (عیار / طلا)</div>
      <div style="font-size:1.12rem;font-weight:800;margin-top:4px;color:#78350f;">${fmt(gAmt)} میلیون تومان</div>
    </div>
    <div style="background:#f1f5f9;padding:12px;border-radius:12px;border:1px solid #cbd5e1;">
      <div class="stat-label" style="color:#334155;font-weight:700;">🥈 صندوق نقره (سیمین / نقران)</div>
      <div style="font-size:1.12rem;font-weight:800;margin-top:4px;color:#0f172a;">${fmt(silAmt)} میلیون تومان</div>
    </div>
    <div style="background:#eff6ff;padding:12px;border-radius:12px;border:1px solid #bfdbfe;">
      <div class="stat-label" style="color:#1d4ed8;font-weight:700;">🏦 درآمد ثابت (لبخند / افران)</div>
      <div style="font-size:1.12rem;font-weight:800;margin-top:4px;color:#1e3a8a;">${fmt(fAmt)} میلیون تومان</div>
    </div>
    <div style="background:#ecfdf5;padding:12px;border-radius:12px;border:1px solid #a7f3d0;">
      <div class="stat-label" style="color:#047857;font-weight:700;">💎 سهام / اهرمی / زعفران</div>
      <div style="font-size:1.12rem;font-weight:800;margin-top:4px;color:#064e3b;">${fmt(sAmt)} میلیون تومان</div>
    </div>
  `;
}

async function analyzeSymbol() {
  const q = document.getElementById('sym-input').value.trim();
  if (!q) return;
  const btn = document.getElementById('btn-sym');
  const box = document.getElementById('sym-result');
  btn.disabled = true;
  btn.innerText = 'در حال استخراج از بورس و کدال...';
  box.style.display = 'block';
  box.innerHTML = '⏳ در حال بررسی تابلو، چارت ۳۰ روزه و اطلاعیه‌های کدال برای <b>' + q + '</b>...';
  try {
    const res = await fetch('/api/symbol?q=' + encodeURIComponent(q));
    const d = await res.json();
    if (d.error) {
      box.innerHTML = '<span class="neg">' + d.error + '</span>';
      return;
    }
    const codalHtml = (d.codal || []).map(c => `<li><b>${c.tag}:</b> ${c.title} <span style="color:var(--muted)">(${c.publish_date})</span></li>`).join('') || '<li>اطلاعیه جدیدی یافت نشد</li>';
    box.innerHTML = `
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;border-bottom:1px solid var(--border);padding-bottom:10px;margin-bottom:12px;">
        <div>
          <span style="font-size:1.25rem;font-weight:bold;color:var(--accent)">${d.ticker}</span>
          <span style="color:var(--muted);margin-right:8px;">(${d.name})</span>
          <span class="badge badge-gold" style="margin-right:10px;">امتیاز کل: ${d.score} از ۱۰۰</span>
        </div>
        <div style="font-weight:bold;">${d.verdict}</div>
      </div>
      <div class="grid-4" style="margin-bottom:12px;">
        <div><span class="stat-label">آخرین قیمت / پایانی:</span><br><b>${fmt(Math.round(d.last_price/10))} ت (${d.last_change_pct}%) / پایانی: ${d.close_change_pct}%</b></div>
        <div><span class="stat-label">ورود/خروج پول حقیقی:</span><br><b class="${d.flow.net_real_inflow_b_toman>=0?'pos':'neg'}">${d.flow.net_real_inflow_b_toman} میلیارد تومان</b> (قدرت خریدار: ${d.flow.buyer_power_ratio}x)</div>
        <div><span class="stat-label">ارزش‌گذاری و حجم:</span><br><b>P/E: ${d.pe || '-'} | حجم به میانگین ۵ روزه: ${d.vol_vs_5d_avg_ratio}x</b></div>
        <div><span class="stat-label">چارت و تکنیکال:</span><br><b>RSI(14): ${d.tech.rsi_14 || '-'} | ${d.tech.tech_signal || '-'}</b></div>
      </div>
      <div class="grid-2" style="margin-bottom:0;">
        <div>
          <div style="color:var(--green);font-weight:bold;margin-bottom:4px;">✅ نقاط قوت فعلی نماد:</div>
          <ul style="padding-right:18px;font-size:0.86rem;">${(d.pros.map(p=>'<li>'+p+'</li>').join('')) || '<li>مورد خاصی ثبت نشده</li>'}</ul>
          <div style="color:var(--red);font-weight:bold;margin-top:8px;margin-bottom:4px;">⚠️ ریسک‌ها و نکات منفی:</div>
          <ul style="padding-right:18px;font-size:0.86rem;">${(d.cons.map(c=>'<li>'+c+'</li>').join('')) || '<li>ریسک تابلویی خاصی دیده نمی‌شود</li>'}</ul>
        </div>
        <div>
          <div style="color:var(--accent);font-weight:bold;margin-bottom:4px;">📑 آخرین گزارش‌ها و اسناد شرکت در سایت کدال:</div>
          <ul style="padding-right:18px;font-size:0.84rem;">${codalHtml}</ul>
        </div>
      </div>
    `;
  } catch (e) {
    box.innerHTML = 'خطا در بررسی نماد: ' + e;
  } finally {
    btn.disabled = false;
    btn.innerText = 'بررسی کامل نماد';
  }
}

async function loadPortfolio() {
  const tbody = document.getElementById('portfolio-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:var(--muted);">⏳ در حال بررسی لحظه‌ای تابلو و چارت سهم‌های پرتفوی شما...</td></tr>';
  try {
    const res = await fetch('/api/portfolio');
    const data = await res.json();
    const items = data.items || [];
    if (items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:var(--muted);padding:18px;">هنوز سهم یا صندوقی در پرتفوی خود ثبت نکرده‌اید. از کادر بالا نمادهای خود را اضافه کنید تا سیستم زمان مناسب خروج را به شما هشدار دهد.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map((it, idx) => {
      if (it.error) {
        return `<tr><td><b>${it.symbol}</b></td><td colspan="9" class="neg">${it.error}</td><td><button onclick="removePortfolioItem(${idx})" style="background:#ffe4e6;color:#e11d48;padding:4px 10px;font-size:0.78rem;box-shadow:none;">حذف</button></td></tr>`;
      }
      const pnlStr = (it.pnl_pct !== null && it.pnl_pct !== undefined)
        ? `<span class="${it.pnl_pct >= 0 ? 'pos' : 'neg'}">${it.pnl_pct >= 0 ? '+' : ''}${it.pnl_pct}%</span>`
        : '-';
      return `
        <tr>
          <td><b style="color:var(--accent);cursor:pointer;" onclick="document.getElementById('sym-input').value='${it.symbol}';analyzeSymbol();">${it.symbol}</b></td>
          <td>${it.name}</td>
          <td>${it.buy_price_toman ? fmt(it.buy_price_toman) + ' ت' : '-'}</td>
          <td class="${it.change_today_pct >= 0 ? 'pos' : 'neg'}">${fmt(it.cur_price_toman)} ت (${it.change_today_pct >= 0 ? '+' : ''}${it.change_today_pct}%)</td>
          <td>${pnlStr}</td>
          <td class="${it.inflow_b_toman >= 0 ? 'pos' : 'neg'}">${it.inflow_b_toman >= 0 ? '+' : ''}${fmt(it.inflow_b_toman)} م.ت</td>
          <td>${it.buyer_power}x</td>
          <td>${it.rsi_14 || '-'}</td>
          <td><span class="badge ${it.action_color}">${it.action_badge}</span></td>
          <td style="font-size:0.8rem;color:#334155;">${it.reason}</td>
          <td><button onclick="removePortfolioItem(${idx})" style="background:#ffe4e6;color:#e11d48;padding:4px 10px;font-size:0.78rem;box-shadow:none;">✕</button></td>
        </tr>
      `;
    }).join('');
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="11" class="neg">خطا در بارگذاری پرتفوی: ' + e + '</td></tr>';
  }
}

async function addPortfolioItem() {
  const sym = document.getElementById('pf-sym').value.trim();
  const price = parseFloat(document.getElementById('pf-price').value) || 0;
  const qty = parseFloat(document.getElementById('pf-qty').value) || 0;
  if (!sym) {
    alert('لطفاً نام نماد را وارد کنید.');
    return;
  }
  await fetch('/api/portfolio', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'add', symbol: sym, buy_price_toman: price, quantity: qty})
  });
  document.getElementById('pf-sym').value = '';
  document.getElementById('pf-price').value = '';
  document.getElementById('pf-qty').value = '';
  loadPortfolio();
}

async function removePortfolioItem(idx) {
  await fetch('/api/portfolio', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'remove', index: idx})
  });
  loadPortfolio();
}

loadData();
loadPortfolio();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/data":
            if os.path.exists(SCAN_FILE):
                with open(SCAN_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = run_comprehensive_scan()
                with open(SCAN_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            self._send_json(data)
        elif parsed.path == "/api/symbol":
            qs = urllib.parse.parse_qs(parsed.query)
            q = qs.get("q", [""])[0]
            res = analyze_single_symbol(q)
            self._send_json(res)
        elif parsed.path == "/api/sector":
            qs = urllib.parse.parse_qs(parsed.query)
            k = qs.get("key", [""])[0]
            res = analyze_sector_deep_dive(k)
            self._send_json(res)
        elif parsed.path == "/api/portfolio":
            res = evaluate_portfolio()
            self._send_json(res)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/scan":
            data = run_comprehensive_scan()
            with open(SCAN_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._send_json(data)
        elif self.path == "/api/portfolio":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            payload = json.loads(raw)
            items = load_portfolio_items()
            act = payload.get("action")
            if act == "add":
                items.append({
                    "symbol": payload.get("symbol", ""),
                    "buy_price_toman": payload.get("buy_price_toman", 0),
                    "quantity": payload.get("quantity", 0),
                })
                save_portfolio_items(items)
            elif act == "remove":
                idx = int(payload.get("index", -1))
                if 0 <= idx < len(items):
                    items.pop(idx)
                    save_portfolio_items(items)
            self._send_json({"ok": True, "count": len(items)})
        else:
            self.send_error(404)


if __name__ == "__main__":
    port = 8085
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Iran Market AI Dashboard running on http://127.0.0.1:{port}")
    server.serve_forever()
