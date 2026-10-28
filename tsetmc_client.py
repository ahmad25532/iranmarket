"""
Direct Network Adapter & TSETMC / Codal / RSS Client for Iran Market AI
Automatically detects local physical IPv4 interface to bypass VPN/TUN adapters
when connecting to domestic Iranian financial servers (TSETMC, Codal, TGJU, News).
"""

import socket
import ssl
import http.client
import urllib.parse
import json
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional


def get_physical_ipv4() -> Optional[str]:
    """
    Detects the local physical IPv4 address (e.g. 192.168.x.x) to bypass
    VPN TUN adapters (like 172.19.x.x or 10.x.x.x) for Iranian websites.
    """
    try:
        hostname = socket.gethostname()
        _, _, ips = socket.gethostbyname_ex(hostname)
        for ip in ips:
            if ip.startswith("192.168."):
                try:
                    ctx = ssl._create_unverified_context()
                    conn = http.client.HTTPSConnection(
                        "cdn.tsetmc.com", source_address=(ip, 0), timeout=3, context=ctx
                    )
                    conn.request(
                        "GET", "/api/StaticData/GetTime", headers={"User-Agent": "Mozilla/5.0"}
                    )
                    res = conn.getresponse()
                    conn.close()
                    if res.status in (200, 404):
                        return ip
                except Exception:
                    continue
        for ip in ips:
            if ip.startswith("192.168."):
                return ip
    except Exception:
        pass
    return None


class DirectHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, source_ip=None, **kwargs):
        if source_ip:
            kwargs["source_address"] = (source_ip, 0)
        super().__init__(host, **kwargs)


class TSETMCClient:
    def __init__(self):
        self.source_ip = get_physical_ipv4()
        self.ssl_ctx = ssl._create_unverified_context()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
        }

    def get_raw(self, host: str, path: str, timeout: int = 10) -> str:
        conn = DirectHTTPSConnection(
            host, source_ip=self.source_ip, timeout=timeout, context=self.ssl_ctx
        )
        try:
            conn.request("GET", path, headers=self.headers)
            res = conn.getresponse()
            if res.status != 200:
                raise RuntimeError(f"HTTP {res.status} for {host}{path}")
            return res.read().decode("utf-8", errors="ignore")
        finally:
            conn.close()

    def get_json(self, host: str, path: str, timeout: int = 10) -> Any:
        raw = self.get_raw(host, path, timeout=timeout)
        return json.loads(raw)

    def get_market_overview(self, market_id: int = 1) -> Dict[str, Any]:
        """Fetches official TSETMC Market Overview (1 = Bourse, 2 = FaraBourse)."""
        data = self.get_json("cdn.tsetmc.com", f"/api/MarketData/GetMarketOverview/{market_id}")
        return data.get("marketOverview", {})

    def get_market_watch_by_paper_types(
        self, paper_types: List[int], market: int = 0, with_best_limits: bool = True
    ) -> List[Dict[str, Any]]:
        """Fetches symbols filtered by official TSETMC paperTypes (1=Bourse/FaraBourse Stocks, 2=Base Market, 8=ETFs)."""
        pt_query = "&".join(
            f"paperTypes%5B{idx}%5D={pt}" for idx, pt in enumerate(paper_types)
        )
        bl_str = "true" if with_best_limits else "false"
        path = (
            f"/api/ClosingPrice/GetMarketWatch?market={market}&industrialGroup="
            f"&{pt_query}&showTraded=false&withBestLimits={bl_str}&hEven=0&RefID=0"
        )
        data = self.get_json("cdn.tsetmc.com", path)
        return data.get("marketwatch", [])

    def get_market_watch(self) -> List[Dict[str, Any]]:
        """Fetches all active symbols, ETFs, and certificates from TSETMC."""
        return self.get_market_watch_by_paper_types([1, 2, 3, 4, 5, 6, 7, 8])

    def get_client_types_all(self) -> Dict[str, Dict[str, Any]]:
        """Fetches Individual vs Institutional (Haghighi/Hoghooghi) volume & counts for all symbols."""
        data = self.get_json("cdn.tsetmc.com", "/api/ClientType/GetClientTypeAll")
        items = data.get("clientTypeAllDto", [])
        return {str(item["insCode"]): item for item in items if "insCode" in item}

    def get_instrument_history(self, ins_code: str, days: int = 60) -> List[Dict[str, Any]]:
        """Fetches daily closing price history for technical analysis (RSI, SMA, etc.)."""
        data = self.get_json(
            "cdn.tsetmc.com", f"/api/ClosingPrice/GetClosingPriceDailyList/{ins_code}/{days}"
        )
        return data.get("closingPriceDaily", [])

    def get_instrument_info(self, ins_code: str) -> Dict[str, Any]:
        """Fetches detailed metadata, NAV, sector P/E, and 5-day average volume."""
        data = self.get_json("cdn.tsetmc.com", f"/api/Instrument/GetInstrumentInfo/{ins_code}")
        return data.get("instrumentInfo", {})

    def get_codal_letters(self, symbol: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """Fetches latest financial statements & disclosures from Codal.ir for a symbol or whole market."""
        sym_param = f"&Symbol={urllib.parse.quote(symbol)}" if symbol else ""
        path = (
            f"/api/search/v2/q?&Audited=true&AuditorRef=-1&Category=-1&Childs=true"
            f"&CompanyState=-1&CompanyType=-1&Consolidatable=true&IsNotAudited=false"
            f"&Length=-1&LetterType=-1&Mains=true&NotAudited=true&NotConsolidatable=true"
            f"&PageNumber=1&Publisher=false&TracingNo=-1&search=false{sym_param}"
        )
        try:
            data = self.get_json("search.codal.ir", path, timeout=7)
            letters = data.get("Letters", [])
            cleaned = []
            for l in letters[:limit]:
                title = l.get("Title", "")
                # Score fundamental positivity of Codal letter
                tag = "خنثی/عمومی"
                sentiment = 0
                if "افشای اطلاعات بااهمیت" in title and "گروه الف" in title:
                    tag = "🔥 افشای الف (بسیار مهم)"
                    sentiment = 2
                elif "افشای اطلاعات بااهمیت" in title:
                    tag = "📢 افشای اطلاعات (گروه ب)"
                    sentiment = 1
                elif "گزارش فعالیت ماهانه" in title:
                    tag = "📊 گزارش فروش ماهانه"
                    sentiment = 1
                elif "صورت‌های مالی" in title:
                    tag = "📑 صورت مالی دوره‌ای"
                    sentiment = 1
                elif "آگهی دعوت به مجمع" in title:
                    tag = "🏛️ دعوت به مجمع"
                    sentiment = 1
                cleaned.append({
                    "symbol": l.get("Symbol", ""),
                    "company": l.get("CompanyName", ""),
                    "title": title,
                    "publish_date": l.get("PublishDateTime", ""),
                    "tag": tag,
                    "sentiment": sentiment,
                    "url": "https://codal.ir" + str(l.get("Url", "")),
                })
            return cleaned
        except Exception:
            return []

    def get_macro_news_and_risk(self) -> Dict[str, Any]:
        """
        Fetches live international geopolitical & macroeconomic headlines regarding Iran, Middle East,
        Sanctions, War/Ceasefire, and Oil from global tier-1 outlets (Reuters, WSJ, CNBC, Bloomberg via
        Google News Intl, BBC World/Persian, Al Jazeera) via the system VPN/Proxy, combined with domestic
        economic indicators, to compute an accurate Geopolitical & Macro Risk Index (0 to 100).
        """
        import urllib.request

        intl_feeds = [
            (
                "رسانه‌های بین‌المللی (Reuters / WSJ / CNBC / Bloomberg)",
                "https://news.google.com/rss/search?q=Iran+war+OR+ceasefire+OR+sanctions+OR+deal+OR+oil+OR+hormuz&hl=en-US&gl=US&ceid=US:en",
                True,
            ),
            (
                "تحولات اسرائیل و انتخابات آمریکا (Reuters / AP / BBC)",
                "https://news.google.com/rss/search?q=(Israel+election+OR+Netanyahu+OR+Eisenkot)+OR+(US+midterms+Trump+Iran)&hl=en-US&gl=US&ceid=US:en",
                True,
            ),
            (
                "بی‌بی‌سی فارسی (جهان و ایران)",
                "https://feeds.bbci.co.uk/persian/rss.xml",
                True,
            ),
            (
                "بی‌بی‌سی جهانی (خاورمیانه)",
                "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
                True,
            ),
            (
                "الجزیره انگلیسی (خاورمیانه و انرژی)",
                "https://www.aljazeera.com/xml/rss/all.xml",
                True,
            ),
            (
                "اقتصاد آنلاین (اقتصاد داخلی)",
                "https://www.eghtesadonline.com/fa/rss/allnews",
                False,
            ),
        ]

        high_risk_keywords = [
            "war", "strike", "attack", "military", "missile", "sanctions", "escalation",
            "hormuz", "oil spill", "threat", "conflict", "warning", "warns", "bomb",
            "reject", "collapse", "inflation", "crisis", "hardline", "retaliation",
            "جنگ", "حمله", "نظامی", "موشک", "پدافند", "تحریم", "تنش", "تحرکات تازه نظامی",
            "رد کرد", "تورم", "بحران", "درگیری", "تهدید", "نفتکش"
        ]
        deescalation_keywords = [
            "deal to end", "ceasefire", "peace", "mediators", "diplomacy", "agreement",
            "talks", "negotiation", "lifting sanctions", "de-escalation", "truce",
            "no clear winner", "difficult paths to power", "deadlock",
            "توافق", "آتش‌بس", "میانجی‌گری", "مذاکره", "صلح", "رفع تحریم", "گشایش", "پایان جنگ"
        ]

        # 5 Geopolitical & Macro Scenarios Tracker
        geo_axes = {
            "israel_politics": {
                "title": "🇮🇱 انتخابات و کابینه اسرائیل",
                "keywords": ["israel", "netanyahu", "eisenkot", "knesset", "exit poll", "اسرائیل", "نتانیاهو", "آیزنکوت", "تل‌آویو"],
                "hits": [],
                "status": "در حال رصد",
                "market_impact": "بن‌بست انتخاباتی بین نتانیاهو و آیزنکوت، تصمیم‌گیری برای عملیات نظامی بزرگ را موقتاً دشوار می‌کند؛ سیگنال کاهش هیجان کاذب در حباب سکه و طلا.",
            },
            "us_midterms": {
                "title": "🇺🇸 انتخابات میان‌دوره‌ای و کاخ سفید",
                "keywords": ["midterm", "trump", "washington", "congress", "white house", "u.s. warns", "ترامپ", "واشینگتن", "آمریکا", "میانجی"],
                "hits": [],
                "status": "⚠️ حساسیت بالا",
                "market_impact": "نزدیکی به انتخابات میان‌دوره‌ای آمریکا و هشدارها درباره تغییر فاز پس از انتخابات؛ محرک اصلی تقاضای پوششی در صندوق‌های طلا، نقره و سهم‌های «فزر» و «فنقره».",
            },
            "diplomacy_mediators": {
                "title": "🇶🇦 میانجی‌گری قطر/عمان و پرونده آتش‌بس",
                "keywords": ["qatar", "oman", "mediator", "deal to end", "ceasefire", "talks", "قطر", "عمان", "میانجی", "مذاکره", "آتش‌بس", "توافق"],
                "hits": [],
                "status": "🔄 در جریان (پیشنهاد جدید)",
                "market_impact": "هرگونه خبر رسمی از پذیرش طرح میانجی‌های قطری، باعث ریزش فوری ۵ تا ۱۰ درصدی طلا و جهش شارپ صندوق‌های اهرمی (اهرم، موج، توان) و سهام خواهد شد.",
            },
            "energy_hormuz": {
                "title": "🛢️ امنیت انرژی، نفت و تنگه هرمز",
                "keywords": ["hormuz", "oil", "tanker", "opec", "energy", "gas", "هرمز", "نفت", "انرژی", "پالایش", "بنزین"],
                "hits": [],
                "status": "⚖️ نوسانی",
                "market_impact": "حساسیت در تنگه هرمز و قیمت جهانی نفت و فلزات؛ حمایت مستقیم از حاشیه سود شرکت‌های معدنی، فلزی و روانکارها (مثل شسپا و فجر).",
            },
            "domestic_economy": {
                "title": "🏦 تورم داخلی، ارز و سیاست بانک مرکزی",
                "keywords": ["تورم", "بانک مرکزی", "نرخ بهره", "ارز", "دلار", "مالیات", "بودجه", "کدال", "دارو", "برنج", "گرانی"],
                "hits": [],
                "status": "📈 فشار تورمی",
                "market_impact": "تورم بالای کالاهای اساسی و کمبود نقدینگی صنایع مصرفی (مثل دارو)؛ برتری مطلق شرکت‌های استخراج طلا/نقره و صندوق‌های کالایی نسبت به صنایع ریالی.",
            },
        }

        headlines = []
        risk_score = 45
        risk_triggers = []
        positive_triggers = []

        for source_name, url, use_vpn in intl_feeds:
            try:
                if use_vpn:
                    req = urllib.request.Request(url, headers=self.headers)
                    with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=8) as r:
                        xml_str = r.read().decode("utf-8", errors="ignore")
                else:
                    parsed = urllib.parse.urlparse(url)
                    xml_str = self.get_raw(parsed.netloc, parsed.path, timeout=6)

                root = ET.fromstring(xml_str)
                for item in root.findall(".//item")[:12]:
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    if not title:
                        continue

                    title_lower = title.lower()
                    if "الجزیره" in source_name:
                        if not any(
                            k in title_lower
                            for k in ["iran", "hormuz", "israel", "oil", "us ", "trump", "gulf", "qatar", "war", "market", "midterm"]
                        ):
                            continue

                    # Match against the 5 Geopolitical Axes
                    matched_axis_title = None
                    for ax_key, ax_data in geo_axes.items():
                        if any(kw in title_lower for kw in ax_data["keywords"]):
                            if title not in ax_data["hits"] and len(ax_data["hits"]) < 3:
                                ax_data["hits"].append(title)
                            matched_axis_title = ax_data["title"]
                            break

                    sentiment = "neutral"
                    has_risk = any(kw in title_lower for kw in high_risk_keywords)
                    has_peace = any(kw in title_lower for kw in deescalation_keywords)

                    if has_risk and not has_peace:
                        sentiment = "risk"
                        risk_score += 4
                        if len(risk_triggers) < 6:
                            risk_triggers.append(title)
                    elif has_peace and not has_risk:
                        sentiment = "positive"
                        risk_score -= 4
                        if len(positive_triggers) < 6:
                            positive_triggers.append(title)
                    elif has_risk and has_peace:
                        sentiment = "mixed"
                        risk_score += 2
                        if len(positive_triggers) < 6:
                            positive_triggers.append(title)
                    elif matched_axis_title:
                        sentiment = "geopolitical"

                    if len(headlines) < 18 and not any(h["title"] == title for h in headlines):
                        headlines.append({
                            "source": source_name,
                            "axis": matched_axis_title or "عمومی",
                            "title": title,
                            "link": link,
                            "sentiment": sentiment,
                        })
            except Exception:
                continue

        risk_score = max(10, min(95, risk_score))
        if risk_score >= 65:
            regime = "ریسک ژئوپلیتیک و نظامی بالا (حالت تدافعی و حفظ سرمایه)"
            alloc = {"gold_etf": 35, "silver_etf": 15, "fixed_income": 30, "stocks": 20}
            advice = (
                "رادار رسانه‌های بین‌المللی (Reuters, WSJ, CNBC, BBC) تیترهای مهمی از تنش‌های نظامی/ژئوپلیتیک و در عین حال تحرکات میانجی‌گرانه را نشان می‌دهد. "
                "پیشنهاد سیستم: ۵۰٪ سبد در فلزات گرانبها (۳۵٪ صندوق طلا + ۱۵٪ صندوق نقره برای پوشش ریسک جهش انس و دلار)، ۳۰٪ در صندوق درآمد ثابت (نقدینگی آماده شکار) و ۲۰٪ در سهام بنیادی یا صندوق‌های خاص."
            )
        elif risk_score <= 38:
            regime = "کاهش تنش بین‌المللی / سیگنال توافق و آتش‌بس (حالت تهاجمی بورس)"
            alloc = {"gold_etf": 15, "silver_etf": 10, "fixed_income": 20, "stocks": 55}
            advice = (
                "در رسانه‌های جهانی سیگنال‌های قوی از توافق، آتش‌بس یا کاهش تنش دیده می‌شود. در چنین شرایطی بورس و صندوق‌های اهرمی پتانسیل رالی صعودی سنگین دارند. "
                "پیشنهاد سیستم: افزایش وزن سهام بنیادی و صندوق‌های اهرمی به ۵۵٪."
            )
        else:
            regime = "وضعیت متعادل ژئوپلیتیک (سبد متوازن چهارگانه)"
            alloc = {"gold_etf": 25, "silver_etf": 15, "fixed_income": 25, "stocks": 35}
            advice = (
                "توازن بین ریسک‌های بین‌المللی و اخبار دیپلماتیک برقرار است. بهترین استراتژی، تقسیم متوازن بین صندوق طلا (۲۵٪)، صندوق نقره (۱۵٪)، درآمد ثابت (۲۵٪) و سهام/اهرم (۳۵٪) است."
            )

        return {
            "risk_score": risk_score,
            "regime": regime,
            "recommended_allocation_pct": alloc,
            "strategic_advice": advice,
            "risk_triggers": risk_triggers[:4],
            "positive_triggers": positive_triggers[:4],
            "headlines": headlines[:14],
            "geopolitical_matrix": list(geo_axes.values()),
        }
