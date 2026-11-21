# Iran Market AI

A personal investment research assistant for the Tehran Stock Exchange, funds, gold and the free-market USD.

It collects market data daily, tests signals on historical data using real Iranian market rules, and shows a dashboard that displays only results that held up in testing. **This tool is not buy or sell advice.**

## 🧭 Plain-language guide (home page)

The first page is written for someone who knows nothing about investing. Each time it opens it gives **one sentence**: "nothing to do today", "invest your cash like this" or "time to rebalance", with the amount of every buy and sell in toman.

- **Inputs:** cash, grams of gold, coins, the symbols and quantities of funds you hold, and one question: how much of this money you might need within the next year. That part stays in fixed income; the rest is split half stocks, half gold.
- **New money:** takes this month's savings and tells you how much to buy in each category.
- **What to buy:** the most heavily traded index and equity funds, gold and fixed income, with the price per unit. They are ordered by trading volume, because no fund-selection method was confirmed in testing. Leveraged and sector funds are excluded.
- **Why:** what would have happened if you had bought 100 million toman in one go, in toman and in purchasing power, with the worst drawdown along the way, for both the research period and the full period. It is computed from the database, not a fixed number.
- **When it cannot be trusted:** if the last trading day was more than 7 days ago, or the price of any asset cannot be found, the page gives no amount and says why.

There are no predictions and no individual stock is recommended; only rules that survived the out-of-sample test (`advisor.py`). The same output as text:

```bash
.venv\Scripts\python.exe advisor.py
```

## ✨ Advanced dashboard (`/pro`)

The dashboard shows only what was confirmed in the phase 3 test (on data not used in the design):

- **Your portfolio mix vs. target:** the current share of stocks, gold, fixed income and cash against percentages you set, each gap in toman, a comparison with a 20-point band, and how to split new money or a withdrawal across categories.
- **Gold funds today:** each fund's premium over redemption NAV, with a liquidity label and the lowest-premium liquid fund.
- **Market context (not a signal):** free-market USD, 18-karat gold, the ounce, the 18-karat premium, the total index in dollars.
- **Global gold:** ounce drivers (real yield, the dollar, implied volatility, gold/silver ratio, fund positioning in COT) and where they stand today relative to the last 5 years; a decomposition of 18-karat gold returns into the ounce, the free-market dollar and the premium; and the result of testing ounce timing signals (none confirmed).
- **Free-market USD:** the real dollar rate (adjusted for Iranian and US inflation), the gap to NIMA, Central Bank inflation, and the result of testing dollar timing signals (none confirmed).
- **Risk-free rate (not a signal):** yield to maturity of treasury bills, the daily median and the part up to one year.
- **Data health:** the last trading day, whether the day's snapshot is final, and the result of the quality checks.
- **Portfolio performance:** the time-weighted return of the portfolio in rials and in free-market dollars, against holding 50% total index and 50% 18-karat gold, gold only, and dollar only.

Your portfolio in `data/portfolio.json`, the daily history in `data/portfolio_history.json` and deposits and withdrawals in `data/portfolio_flows.json` are stored only on this computer and are not committed to git.

### How portfolio performance is recorded and computed

- **Daily recording:** `collector.py daily` records today's value each time it runs, so the dashboard does not need to be open. Opening the dashboard records only if today has not been recorded yet, and saving the portfolio refreshes today's row. If the price of any asset cannot be found, that day is not recorded.
- **Days without a record:** they are not guessed. The next record covers the whole gap and the result does not change, unless there was a deposit or withdrawal in that gap, in which case the period is marked approximate.
- **Deposits and withdrawals:** record any money coming in or going out, dated the day you update the portfolio. Cash dividends and switching between assets are not deposits or withdrawals. A change of more than 2% that prices and this list do not explain triggers a warning.
- **Comparisons:** each one is bought once at the first record and held without rebalancing, with no fees or bid-ask spread.

---

## 🚀 How to run on Windows

Run [`run_dashboard.bat`](run_dashboard.bat) or:

```bash
.venv\Scripts\python.exe dashboard.py
```

Then open in a browser: 👉 **http://127.0.0.1:8085** (plain-language guide) and **http://127.0.0.1:8085/pro** (advanced dashboard)

---

## 🗄️ Data layer (phase 1)

Data is stored in `data/market.duckdb` (raw daily prices, real/legal-entity money flow, fund NAV, indices, dollar/gold/coin/ounce). After every run a data-quality check is performed and its result is stored in the `quality_checks` table.

| File | Purpose |
|---|---|
| `net.py` | HTTP connection with valid TLS certificates and a direct route for domestic sites |
| `sources.py` | Fetching and converting data from TSETMC, TGJU and Fipiran (NAV history) |
| `datastore.py` | DuckDB database schema |
| `quality.py` | Data-quality checks |
| `collector.py` | Daily run and history download |

Install (once):

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Commands:

```bash
.venv\Scripts\python.exe collector.py backfill   # full history (once, about 10 to 20 minutes)
.venv\Scripts\python.exe collector.py daily      # daily snapshot + fills in missed days (any time, any number of times)
.venv\Scripts\python.exe collector.py past-shares # delisted stocks and their history (once, about 10 minutes)
.venv\Scripts\python.exe backup.py                # backup of the database and portfolio files
.venv\Scripts\python.exe collector.py status     # data status and coverage of the last 10 trading days
.venv\Scripts\python.exe -m pytest -q tests      # tests
```

### Automatic runs and catching up after the computer was off

The "IranMarketAI Daily Collect" task in Windows Task Scheduler runs [`run_daily_collect.bat`](run_daily_collect.bat):
- at 16:00 Saturday to Wednesday, at 20:00 every day, and 5 minutes after every Windows login
- if the computer is off at the scheduled time, it runs at the first opportunity after it is turned on

Each time `daily` runs:
1. If today's closing snapshot has not been taken yet, it takes it (a snapshot taken before 14:00 does not count as final and is retaken).
2. Non-final snapshots of past days (for example when the internet was down) are replaced from the official history. All times are Tehran time, even if the Windows clock is set to another zone. It compares the trading days of the last 120 days with the total-index calendar and fills every incomplete day (for example because of a trip) from the official history: price and money flow from TSETMC, fund NAV from Fipiran.
3. If everything is complete, it finishes in under a minute.

A log for each day is saved in `data/logs`.

After every run `backup.py` keeps the last 3 days of backups in `data/backups`. This folder is on the same disk; to protect against disk failure, set the `IRANMARKET_BACKUP_DIR` environment variable to another drive or a synced folder.

Known limitations:
- Fipiran publishes some NAVs a day or two late; the system keeps retrying until those days are complete.
- If the computer is not turned on for more than 120 days, `backfill --refresh` must be run once.
- Delisted stocks were added once with `collector.py past-shares`, but a stock whose market has closed stays at its last price, and about 100 stocks outside the watchlist are updated only when missed days are being filled in.
- In the daily snapshot, the rial value of real/legal-entity flow is empty (only volume and counts); days filled in from history have the exact value.

### Fundamental data (EPS and P/E)

Every day `collector.py daily` stores the market watch's EPS and P/E in the `fundamentals_seen` table. The `seen_on` column is the actual day of retrieval, and a signal on day t may only use rows with `seen_on <= t`.

- **Available history:** the history starts only from the first run of this code, because TSETMC provides only today's value. A missed day is not back-filled later.
- **P/E test:** not feasible with this table for now. It needs either a source of real history or several years of collection. Codal is free and has the publication time of each financial statement, but its collector has not been built yet.
- **Meaning of EPS:** trailing 12-month profit from the latest financial statement. In a sample of 13 symbols, 8 matched Codal, 3 did not (all three with a recent capital increase) and 2 could not be checked.

Details: [`docs/fundamentals_findings.md`](docs/fundamentals_findings.md)

---

## 📐 Adjusted prices and backtest engine (phase 2)

| File | Purpose |
|---|---|
| `adjust.py` | Builds the `price_adj` table: prices adjusted for cash dividends and capital increases, plus buy/sell queue detection |
| `backtest.py` | Backtest engine with Tehran Stock Exchange rules |

**Price adjustment:** for every day the exchange publishes a reference "yesterday price". On the day of a shareholders' meeting or a capital increase this number is lower than the closing price of the last day with trades; the ratio of the two is that day's adjustment factor. A day with no trades does not move the series: a price of 1000 rials before the first trade is not a price, and a reference price change during a halt is applied on the reopening day. Multiplying the factors cumulatively turns the raw price into a "total return" series (with cash dividends reinvested). This table is rebuilt automatically after every `collector.py daily` run.

**Backtest engine rules:**
- A signal on day t is executed on day t+1 (no look-ahead).
- Buys and sells happen at the day's volume-weighted average price (traded value ÷ volume) plus price slippage (default 0.1%).
- On a buy-queue day buying is impossible; on a sell-queue day selling is impossible; on a day with no trades neither is. The order waits and is retried on later days.
- Each order fills at most 10% of the symbol's traded value that day (low-volume symbols cannot absorb a large order).
- Separate fees for stocks, equity funds, commodity funds (gold/silver) and fixed-income funds.
- Daily interest on cash is supported (for example equal to a fixed-income fund).
- Returns are reported in rials and in free-market dollars, next to the total index, the dollar and 18-karat gold.

Default fee rates (changeable in `DEFAULT_FEES`; check them with your broker):

| Type | Buy | Sell |
|---|---|---|
| Stocks | 0.3712% | 0.8812% (includes 0.5% tax) |
| Equity / leveraged / mixed funds | 0.116% | 0.1875% |
| Gold / silver / commodity funds | 0.12% | 0.12% |
| Fixed-income funds | 0.0375% | 0.0375% |

Trying the engine with buy-and-hold of one symbol:

```bash
.venv\Scripts\python.exe backtest.py عیار --start 2021-03-21
```

Limitations:
- The total index includes cash dividends, but it cannot be bought and has no fund costs.
- Queue detection only catches days where the whole day traded at a single price; queues that opened for part of the day are assumed tradable.
- The historical price limit (daily range) is not stored; a symbol being locked is inferred from the data itself.
- About 40 funds are not on Fipiran; their type is read from the exchange name and ISIN (`IRTK` commodity; "درآمد ثابت" or the suffix "-د" and "ثابت" fixed income). A fund with an unusual name may stay in the equity category.

---

## 🔬 Signal testing (phase 3)

`research.py` tests all signals on two separate periods (design: 1393 to 1399, test: 1399 to 1405 in the Iranian calendar, roughly 2014 to 2020 and 2020 to 2026) with fees, price slippage and market-wide queue locks:

```bash
.venv\Scripts\python.exe research.py
```

Results and conclusions: [`docs/phase3_findings.md`](docs/phase3_findings.md)

---

## 🌍 Global gold

`globalgold.py` fetches global gold data from Yahoo Finance, the US Treasury (real yield) and the CFTC (COT report); all free and without an API key. `collector.py daily` updates them each time. Commands:

```bash
.venv\Scripts\python.exe collector.py global   # full download of global data history (once, about one minute)
.venv\Scripts\python.exe globalgold.py         # signal test report
```

Results and conclusions: [`docs/global_gold_findings.md`](docs/global_gold_findings.md)

---

## 💵 Free-market USD

`freeusd.py` fetches the NIMA and Sana rates from TGJU, monthly inflation from the Central Bank, the Statistical Centre's price index from the IMF (DBnomics) and US inflation from the BLS; all free and without an API key. `collector.py daily` and `collector.py global` update them too. Report command:

```bash
.venv\Scripts\python.exe freeusd.py            # dollar signal test report
```

Results and conclusions: [`docs/free_usd_findings.md`](docs/free_usd_findings.md)

---

## 🎯 Stock selection

`stockpick.py` tests ten price and money-flow factors on 150 liquid stocks, with fees, price slippage and queues, on two separate periods. A stock list is printed only for a rule that was confirmed in the test period; for now no rule has been confirmed.

```bash
.venv\Scripts\python.exe stockpick.py          # full test (about 2 minutes)
.venv\Scripts\python.exe stockpick.py today    # test, plus today's list if a rule has been confirmed
```

Results and conclusions: [`docs/stock_selection_findings.md`](docs/stock_selection_findings.md)

---

## ⚖️ Gold fund switching and the rebalancing band

`structural.py` tests switching between gold funds based on premium (not confirmed) and the timing of rebalancing a 50/50 portfolio. The dashboard compares the portfolio's gap to target with a 20-point band and splits new money or a withdrawal across categories.

```bash
.venv\Scripts\python.exe structural.py
```

Results and the review by another AI: [`docs/external_review.md`](docs/external_review.md)

---

## 🏦 Treasury bill yields

`treasury.py` computes the yield to maturity of treasury bills (اخزا) from prices that had trades and keeps it in `bill_yields` (each bill) and `bill_yield_daily` (daily median). `collector.py daily` updates it and the rows of matured bills are kept. This series is for context only and is not a signal.

```bash
.venv\Scripts\python.exe collector.py bills     # update bill yields only
.venv\Scripts\python.exe treasury.py             # descriptive report against the total index
```

Results and limitations: [`docs/treasury_findings.md`](docs/treasury_findings.md)
