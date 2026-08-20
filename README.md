# Market Copilot

A personal, local research copilot: it scans free market data 24/7, runs it through
quant calculators, and uses Claude to synthesize structured research notes — a
**thesis, catalysts, risks, and a confidence level**, never a "buy now" directive.
It never places trades. You read the notes and trade manually via ING, Bolero, or IBKR.

Two sleeves:
- **Core** — safe/market-neutral funding-rate arbitrage opportunities in crypto perpetuals,
  filtered by a walk-forward backtest of each symbol's actual funding history (not just
  today's snapshot — see `app/backtest/crypto_funding.py`).
- **Satellite** — higher-conviction directional equity theses, meant to be sized smaller.

Also built:
- **Backtesting** (`app/backtest/`) — validates the screener's signals against historical
  data before trusting them live. Equity backtest is technicals-only (RSI/momentum/vol);
  fundamentals can't be backtested with free data since yfinance only exposes
  current-snapshot fundamentals, not point-in-time historical ones.
- **Outcome journal** (`app/journal.py`) — every synthesized thesis is snapshotted and
  checked at 14/30/90-day horizons against what actually happened, building a real
  confidence-calibration track record over time. Empty until entries age past 14 days —
  that's expected, not a bug.
- **Portfolio** (`app/portfolio.py`, `app/connectors/ibkr.py`) — read-only IBKR position
  sync plus manual CSV import for ING/Bolero, unified into one view. See IBKR setup below.
- **Position sizing** (`app/sizing.py`) — every opportunity gets a suggested size via
  fractional Kelly criterion. Core uses the backtested win rate (even-money Kelly
  simplification — see the module docstring for why two more "obvious" formulas were
  tried and rejected first). Satellite is an explicit heuristic, not Kelly, pending
  per-symbol calibration from the journal. Always capped, always fractional (quarter-Kelly).
- **Daily email reports** (`app/reports.py`, `app/notify.py`) — morning (full refresh),
  lunch and evening (free pulse-checks: funding-direction flips, price moves, live IBKR
  P&L) sent via Gmail SMTP on a schedule (`MORNING_REPORT_TIME` etc. in `.env`).
- **Portfolio risk** (`app/portfolio_risk.py`) — value-weighted concentration and
  correlation across everything actually held (IBKR + manual holdings, FX-converted to
  EUR). Correlation only covers symbols where yfinance actually returns historical price
  data — verified empirically that some European ETF listings return a live quote but no
  `.history()` series at all; those are excluded from correlation and reported as such,
  not silently dropped.
- **Insider signal** (`app/connectors/sec_edgar.py`, `app/calculators/insider_signal.py`)
  — SEC Form 4 filings, free and structured (not text to parse). Only genuine open-market
  buy/sell transactions count (option exercises, grants, gifts, tax withholding are
  filtered out); a cluster-buy signal fires when multiple distinct insiders buy on the
  open market with buying outweighing selling. Daily-cached, feeds both the screener's
  ranking and the satellite synthesis bundle.

Remaining ideas (merger arb, NAV discounts, pairs trading, portfolio-level correlation/risk)
are documented in conversation history — ask for the roadmap if you want the full list.

## Setup

### 1. Backend

```bash
cd backend
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

Edit `backend/.env`:
- `ANTHROPIC_API_KEY` — required for the synthesis layer (get one at
  https://console.anthropic.com/). Without it, the screener still runs and finds
  candidates, but no research notes get written — you'll see 0 opportunities.
- `FRED_API_KEY` — optional, free instant signup at
  https://fred.stlouisfed.org/docs/api/api_key.html. Adds macro context (rates, CPI,
  10y yield) to equity theses. Safe to leave blank.

Run the backend:

```bash
cd backend
./.venv/bin/uvicorn app.main:app --port 8000
```

Run the tests:

```bash
cd backend
./.venv/bin/python -m pytest -q
```

### 2. Frontend (web dev mode — works today, no extra setup)

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:1420. Click **Refresh** to trigger a scan — it takes ~30-60s
(scanning ~30 equities + ~50 crypto perpetuals against free APIs). Results land in the
Core / Satellite columns.

### 3. Native Mac app (Tauri) — needs Rust installed once

The Tauri shell (`frontend/src-tauri/`) is scaffolded but **not yet buildable** here —
Rust/cargo wasn't installed in this environment. To finish it on your machine:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Then, from `frontend/`:

```bash
npm run tauri dev
```

Before a real `.app` bundle (`npm run tauri build`), generate proper icons from your
own artwork (a 1024x1024 PNG is enough):

```bash
npm run tauri icon path/to/your-icon.png
```

Right now the Tauri shell just opens a window pointed at the frontend — you still start
the Python backend separately (step 1). Auto-spawning the backend as a bundled sidecar
process is the natural next step once you can actually build and test Rust code here.

### 4. IBKR portfolio sync (optional, read-only)

This app never places, modifies, or cancels an order — no code path does anything but
read. It also never sees your IBKR login: you authenticate yourself through IB Gateway or
TWS's own window, and this app only opens a local socket to that already-authenticated
process.

1. Install [IB Gateway](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php)
   (lighter than full TWS) or use TWS if you already have it.
2. Log in yourself, then enable API access: **Configure → API → Settings → Enable ActiveX
   and Socket Clients**. Leave "Read-Only API" checked if you want a second layer of
   protection beyond this app's own read-only connection.
3. Note the port shown there. Defaults: TWS paper `7497`, TWS live `7496`, Gateway paper
   `4002`, Gateway live `4001`. Set `ibkr_port` in `backend/app/settings.py` (or add
   `IBKR_PORT=...` to `backend/.env`) to match.
4. With Gateway/TWS running, `/portfolio` in the app will show `"connected": true` and
   your live positions. If it's not running, the dashboard just shows "IBKR not
   connected" — the rest of the app works fine either way.

ING and Bolero have no public API, so those holdings go in via CSV upload in the
Portfolio section instead (columns: `symbol,quantity,average_cost` — `average_cost` is
optional). Re-uploading for a broker fully replaces its prior holdings.

## How the cost control works

Market data is free. The only real ongoing cost is Claude API calls in the synthesis
layer (`app/synthesis.py`). Two things keep that bounded regardless of how big the scan
universe gets:

1. The screener (`app/screener.py`) only ever shortlists the top ~10 candidates per
   sleeve — Claude never sees the full universe.
2. Each candidate's synthesis is cached by a content hash of its data bundle
   (`app/store.py`) — rerunning a scan with materially unchanged inputs reuses the
   cached note instead of paying for a new one.

## Project structure

```
backend/
  app/
    connectors/     # free market data: Binance, Bybit, CoinGecko, yfinance, FRED, news RSS, IBKR
    calculators/     # pure quant functions: funding-rate arb, technicals, fundamentals, DCF, risk
    backtest/         # walk-forward validation of the screener's own signals
    screener.py       # ranks candidates, keeps a short list per sleeve
    synthesis.py       # Claude call -> structured research note, cached by content hash
    journal.py          # tracks thesis outcomes at 14/30/90d horizons for calibration
    portfolio.py          # unifies IBKR positions + manual CSV holdings
    store.py                # SQLite persistence
    main.py                  # FastAPI: refresh, opportunities, journal, portfolio, backtest
  tests/               # unit tests (calculators, backtest math, journal, CSV parsing)
frontend/
  src/                  # React dashboard (opportunities, journal, portfolio sections)
  src-tauri/             # Tauri shell (needs Rust to build)
```
