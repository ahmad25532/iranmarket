"""
Plain-language guidance for someone who knows nothing about investing.

Nothing here forecasts anything. It only turns what survived the out-of-sample tests into one
sentence about today, and it says so when the data cannot support even that:

- a fixed split between stocks and gold was the only rule with a positive dollar return and about
  half the drawdown of stocks in both periods (docs/phase3_findings.md);
- no exit signal, no timing rule and no stock or fund selection rule was confirmed, so none is used;
- moving back to the target only once a class has drifted 20 points was at least as good as trading
  more often, on weak evidence (docs/external_review.md);
- fixed-income funds lost 11 to 15% a year in dollars, so they are only for money needed soon.

The functions are pure (a valuation in, a plan out) so they can be tested without a database;
dashboard.py feeds them and serves the result to simple.html.
"""

import sys
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import numpy as np

TEDPIX = "32097828799138957"
RIALS_PER_TOMAN = 10.0
# Classes the tested plan speaks about. "other" (dollars, silver, ...) was never part of a tested
# mix, so unless the user gave it a target it is left out of the sums rather than called a mistake.
PLAN_CLASSES = ("stocks", "gold", "fixed", "cash")
CLASS_NAMES = {
    "stocks": "صندوق سهامی",
    "gold": "صندوق طلا",
    "fixed": "صندوق درآمد ثابت",
    "cash": "پول نقد",
    "other": "دارایی‌های دیگر",
}
# A step smaller than this share of the plan is noise next to fees and price moves.
MIN_STEP_SHARE = 0.01
MIN_STEP_RIAL = 1_000_000 * RIALS_PER_TOMAN
# With the last market day older than this the exchange is shut or the collector has not run;
# either way today's numbers are not something to trade on. Nowruz closes the market for 5-6 days.
MAX_DATA_AGE_DAYS = 7
STALE_PRICE_DAYS = 10
# The two research periods and the whole span. Stocks won the first and gold the second, which is
# the plain argument for holding both: nobody knew in advance which it would be.
EVIDENCE_PERIODS = (
    ("first", date(2014, 4, 1), date(2020, 12, 31)),
    ("second", date(2021, 1, 1), date(2100, 1, 1)),
    ("whole", date(2014, 4, 1), date(2100, 1, 1)),
)
# Fipiran fund types shown under "what to buy": index funds, plain equity funds, fixed income.
FUND_TYPE_INDEX, FUND_TYPE_EQUITY, FUND_TYPE_FIXED = 23, 6, 4
MIN_FUND_VALUE = 1e10        # rial a day (1 billion toman): enough for a retail order
LIQUID_GOLD_VALUE = 1e11     # the dashboard's "liquid" gold fund

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


# ---------------------------------------------------------------- wording

def fa_num(x: float, digits: int = 0) -> str:
    return f"{x:,.{digits}f}".translate(_FA_DIGITS).replace(",", "٬").replace(".", "٫")


def round_toman(rial: float) -> float:
    """Rounds to a figure a person would actually type into a broker app. Returns toman."""
    toman = abs(rial) / RIALS_PER_TOMAN
    step = 1_000_000 if toman >= 100_000_000 else 100_000 if toman >= 5_000_000 else 10_000
    return round(toman / step) * step


def toman_text(rial: float) -> str:
    toman = round_toman(rial)
    if toman >= 1e9:
        return f"{fa_num(toman / 1e9, 2 if toman < 1e10 else 1)} میلیارد تومان"
    if toman >= 1e6:
        return f"{fa_num(toman / 1e6, 1 if toman < 1e8 and toman % 1e6 else 0)} میلیون تومان"
    return f"{fa_num(toman)} تومان"


# ---------------------------------------------------------------- data health, in one sentence

def data_status(health: Dict[str, Any], today: date) -> Dict[str, Any]:
    """level ok | warn | stop, a sentence for the reader, and whether today's plan may be acted on."""
    last = health.get("last_market_date")
    if last is None:
        return {"level": "stop", "usable": False, "last_market_date": None, "age_days": None,
                "text": "هنوز داده‌ای جمع نشده است. فایل run_daily_collect.bat را یک بار اجرا کنید."}
    age = (today - last).days
    if age > MAX_DATA_AGE_DAYS:
        return {"level": "stop", "usable": False, "last_market_date": last, "age_days": age,
                "text": f"آخرین روز معاملاتی در داده‌ها {fa_num(age)} روز پیش است. یا بازار تعطیل است یا "
                        "جمع‌آوری داده اجرا نشده. تا به‌روز شدن داده‌ها بر اساس این صفحه معامله نکنید."}
    if health.get("worst") == "fail":
        return {"level": "warn", "usable": True, "last_market_date": last, "age_days": age,
                "text": "آخرین جمع‌آوری داده خطا داشته است. عددها ممکن است کامل نباشند؛ جزئیات در نمای پیشرفته."}
    return {"level": "ok", "usable": True, "last_market_date": last, "age_days": age, "text": "داده‌ها به‌روز است."}


# ---------------------------------------------------------------- the plan

def _targets(alloc: Dict[str, Dict[str, Any]]) -> Tuple[List[str], Dict[str, float]]:
    keys = list(PLAN_CLASSES) + (["other"] if alloc["other"]["target_pct"] > 0 else [])
    total = sum(alloc[k]["target_pct"] for k in keys)
    if total <= 0:   # every target sits on a class outside the plan: fall back to the tested mix
        return keys, {k: (0.5 if k in ("stocks", "gold") else 0.0) for k in keys}
    return keys, {k: alloc[k]["target_pct"] / total for k in keys}


def _step(verb: str, key: str, rial: float) -> Dict[str, Any]:
    word = "بخرید" if verb == "buy" else "بفروشید"
    return {"verb": verb, "key": key, "label": CLASS_NAMES[key], "toman": round_toman(rial),
            "text": f"حدود {toman_text(rial)} {CLASS_NAMES[key]} {word}"}


def plan(valuation: Dict[str, Any], band_points: float, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    What to do today, from a dashboard valuation (values in rial).

    status: start      nothing entered yet
            blocked    the data or a missing price makes any figure unsafe to act on
            invest     idle cash goes to the classes below target; nothing is sold
            rebalance  a class is `band_points` or more from its target even after that
            hold       nothing to do
    """
    alloc = {a["key"]: a for a in valuation["allocation"]}
    keys, target = _targets(alloc)
    base = sum(alloc[k]["value"] for k in keys)
    outside = 0.0 if "other" in keys else alloc["other"]["value"]
    shares = [{"key": k, "label": CLASS_NAMES[k], "value": alloc[k]["value"],
               "current_pct": alloc[k]["value"] / base * 100 if base else 0.0, "target_pct": target[k] * 100}
              for k in keys if alloc[k]["value"] > 0 or target[k] > 0]
    out: Dict[str, Any] = {"base": base, "outside": outside, "shares": shares, "steps": [], "band": band_points,
                           "max_gap": 0.0, "warnings": []}

    missing = [i["ticker"] for i in valuation["items"] if "error" in i]
    stale = [i["ticker"] for i in valuation["items"] if i.get("stale")]
    if stale:
        out["warnings"].append("قیمت این نمادها قدیمی است (مدتی معامله نشده‌اند) و ارزش‌شان دقیق نیست: " + "، ".join(stale))
    if outside > 0:
        out["warnings"].append(f"{toman_text(outside)} دارایی دیگر (مثل دلار یا نقره) دارید که در این برنامه حساب نشده، "
                               "چون فقط ترکیب سهام و طلا آزمون شده است.")

    if data is not None and not data["usable"]:
        return dict(out, status="blocked", headline="امروز بر اساس این صفحه معامله نکنید", why=data["text"])
    if missing:
        return dict(out, status="blocked", headline="اول سبد را اصلاح کنید",
                    why="برای این نمادها قیمتی پیدا نشد، پس ارزش سبد و هر پیشنهادی اشتباه درمی‌آید: " + "، ".join(missing))
    if base <= 0:
        return dict(out, status="start", headline="اول بگویید چه دارید",
                    why="پول نقد، طلا و صندوق‌هایی که دارید را در بخش «دارایی من» وارد کنید تا بگویم با آن چه کنید.")

    floor = max(MIN_STEP_SHARE * base, MIN_STEP_RIAL)
    need = {k: target[k] * base - alloc[k]["value"] for k in keys}
    invested = [k for k in keys if k != "cash"]
    idle = max(0.0, -need["cash"])
    short = {k: need[k] for k in invested if need[k] > 0}
    deploy = min(idle, sum(short.values())) if idle >= floor else 0.0
    buys = {k: deploy * v / sum(short.values()) for k, v in short.items()} if deploy else {}
    left = {k: need[k] - buys.get(k, 0.0) for k in invested}
    out["max_gap"] = max((abs(v) / base * 100 for v in left.values()), default=0.0)

    if out["max_gap"] >= band_points:
        steps = [_step("sell", k, -need[k]) for k in invested if need[k] <= -floor]
        steps += [_step("buy", k, need[k]) for k in invested if need[k] >= floor]
        return dict(out, status="rebalance", steps=steps, headline="وقت متعادل‌کردن سبد است",
                    why=f"یکی از بخش‌های سبد {fa_num(out['max_gap'])} واحد از هدف دور شده است. در آزمون، برگرداندن سبد به "
                        f"هدف فقط وقتی فاصله به {fa_num(band_points)} واحد می‌رسد، دست‌کم به خوبیِ معامله زیاد بود "
                        "(شواهد ضعیف است، ولی هزینه کمتری دارد). اول بفروشید، بعد بخرید.")
    if deploy:
        steps = [_step("buy", k, v) for k, v in buys.items() if v >= floor / 2]
        return dict(out, status="invest", steps=steps, headline="پول نقدتان را این‌طور سرمایه‌گذاری کنید",
                    why="هیچ روشی برای پیدا کردن «زمان مناسب خرید» در آزمون‌ها تأیید نشد، پس منتظر ماندن مزیتی ندارد. "
                        "پول فقط به بخش‌هایی می‌رود که از هدف عقب‌اند و چیزی فروخته نمی‌شود.")
    gap = (f"بیشترین فاصله سبد با هدف {fa_num(out['max_gap'])} واحد است و تا {fa_num(band_points)} واحد نیازی به جابه‌جایی نیست. "
           if out["max_gap"] >= 1 else "سبد شما روی هدف است. ")
    return dict(out, status="hold", headline="امروز هیچ کاری لازم نیست",
                why=gap + "خرید و فروش بیشتر در آزمون‌ها فقط هزینه اضافه کرد. وقتی پول جدید داشتید، یا ماهی یک بار، سر بزنید.")


def split_new_money(valuation: Dict[str, Any], rial: float) -> List[Dict[str, Any]]:
    """Where fresh money goes: only to plan classes below their target, in proportion to the shortfall."""
    alloc = {a["key"]: a for a in valuation["allocation"]}
    keys, target = _targets(alloc)
    if rial <= 0:
        return []
    base = sum(alloc[k]["value"] for k in keys) + rial
    short = {k: target[k] * base - alloc[k]["value"] for k in keys if k != "cash"}
    short = {k: v for k, v in short.items() if v > 0}
    total = sum(short.values())
    if not total:
        return []
    spend = min(rial, total)
    return [_step("buy", k, spend * v / total) for k, v in short.items() if spend * v / total >= MIN_STEP_RIAL / 10]


# ---------------------------------------------------------------- what the past looked like

def _max_drawdown(curve: np.ndarray) -> float:
    return float(np.min(curve / np.maximum.accumulate(curve) - 1))


_evidence_cache: Dict[Any, Any] = {}


def evidence(con: duckdb.DuckDBPyConnection) -> List[Dict[str, Any]]:
    """
    What one unit of money became in each simple holding, bought once and left alone, in rial and
    in purchasing power (free-market dollars), with the worst fall on the way. No fees: a single
    purchase costs a fraction of a percent. Stocks are the total-return index, which cannot be
    bought directly; index funds tracked it closely in the test period (docs/external_review.md).
    """
    import backtest
    import research

    last = con.execute("SELECT max(d) FROM index_daily WHERE index_code = ?", [TEDPIX]).fetchone()[0]
    if last in _evidence_cache:
        return _evidence_cache[last]
    out = []
    for key, start, end in EVIDENCE_PERIODS:
        days = [r[0] for r in con.execute(
            "SELECT d FROM index_daily WHERE index_code = ? AND d BETWEEN ? AND ? ORDER BY d", [TEDPIX, start, end]).fetchall()]
        if len(days) < 250:
            continue
        ted = backtest._series(con, f"SELECT d, close FROM index_daily WHERE index_code = '{TEDPIX}'", days)
        gold = backtest._series(con, "SELECT d, close FROM macro_daily WHERE series = 'gold_18k_irr'", days)
        usd = backtest._series(con, "SELECT d, close FROM macro_daily WHERE series = 'usd_irr'", days)
        if np.isnan(ted[0]) or np.isnan(gold[0]) or np.isnan(usd[0]):
            continue
        curves = {
            "mix": 0.5 * ted / ted[0] + 0.5 * gold / gold[0],
            "stocks": ted / ted[0],
            "gold": gold / gold[0],
            "fixed": research.fixed_income_index(con, days),
            "usd": usd / usd[0],
        }
        rows = []
        for name, curve in curves.items():
            real = curve / (usd / usd[0])
            rows.append({"key": name, "rial_multiple": float(curve[-1] / curve[0]),
                         "real_multiple": float(real[-1] / real[0]), "worst_fall": _max_drawdown(real)})
        out.append({"key": key, "start": days[0], "end": days[-1],
                    "years": (days[-1] - days[0]).days / 365.25, "rows": rows})
    _evidence_cache.clear()
    _evidence_cache[last] = out
    return out


# ---------------------------------------------------------------- what can be bought

def instruments(con: duckdb.DuckDBPyConnection, gold_rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """
    The most traded funds of each kind, as facts about liquidity and not as picks: no tested rule
    chose a better fund, so being easy to buy and sell is the only thing that separates them.
    Leveraged and single-sector funds are left out because they do not behave like the market.
    """
    funds = con.execute(
        """WITH last AS (SELECT max(d) AS d FROM daily_prices),
                recent AS (
                    SELECT p.ins_code, avg(p.value) AS avg_value, max(p.d) AS last_trade, arg_max(p.close, p.d) AS price
                    FROM daily_prices p, last WHERE p.d >= last.d - INTERVAL 30 DAY AND p.trades > 0 GROUP BY 1)
           SELECT c.ticker, c.name, f.fund_type, r.avg_value, r.price, g.ins_code IS NOT NULL AS is_gold
           FROM recent r JOIN instrument_class c USING (ins_code) LEFT JOIN fipiran_funds f USING (ins_code)
           LEFT JOIN gold_funds g USING (ins_code), last
           WHERE r.last_trade >= last.d - INTERVAL 7 DAY AND r.avg_value >= ?
             AND (g.ins_code IS NOT NULL OR f.fund_type IN (?, ?, ?))
           ORDER BY r.avg_value DESC""", [MIN_FUND_VALUE, FUND_TYPE_INDEX, FUND_TYPE_EQUITY, FUND_TYPE_FIXED]).fetchall()
    unreliable = {r["ticker"] for r in gold_rows or [] if not r.get("reliable", True)}

    def rows(pick, n: int) -> List[Dict[str, Any]]:
        return [{"ticker": t, "name": name, "avg_value": v, "price": px}
                for t, name, kind, v, px, is_gold in funds if pick(t, kind, v, is_gold)][:n]

    return {
        "stocks_index": rows(lambda t, kind, v, g: kind == FUND_TYPE_INDEX and not g, 4),
        "stocks_equity": rows(lambda t, kind, v, g: kind == FUND_TYPE_EQUITY and not g, 3),
        "gold": rows(lambda t, kind, v, g: g and v >= LIQUID_GOLD_VALUE and t not in unreliable, 5),
        "fixed": rows(lambda t, kind, v, g: kind == FUND_TYPE_FIXED and not g, 4),
    }


# ---------------------------------------------------------------- text version

def as_text(state: Dict[str, Any]) -> str:
    p = state["plan"]
    lines = [p["headline"], ""]
    lines += [f"  {n}. {s['text']}" for n, s in enumerate(p["steps"], 1)]
    lines += ([""] if p["steps"] else []) + [p["why"]]
    for s in p["shares"]:
        lines.append(f"  {s['label']}: {fa_num(s['current_pct'])}٪ (هدف {fa_num(s['target_pct'])}٪)")
    lines += [f"  توجه: {w}" for w in p["warnings"]]
    lines += ["", state["data"]["text"], "این خروجی نتیجه آزمون داده‌های گذشته است و توصیه شخصی سرمایه‌گذاری نیست."]
    return "\n".join(lines)


def main() -> int:
    import dashboard   # imported here: dashboard imports this module

    sys.stdout.reconfigure(encoding="utf-8")
    try:
        con = duckdb.connect(dashboard.datastore.DEFAULT_DB_PATH, read_only=True)
    except duckdb.Error:
        print("داده‌ها در حال به‌روزرسانی هستند؛ چند دقیقه دیگر دوباره اجرا کنید.")
        return 1
    try:
        print(as_text(dashboard.simple_state(con, dashboard.load_portfolio())))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
