# Market Copilot

A personal, local research copilot for equities: it scans free market data 24/7, runs it
through quant calculators, and uses an LLM to synthesize structured research notes — a
**thesis, catalysts, risks, and a confidence level**, never a "buy now" directive.
It never places trades. You read the notes and trade manually via ING, Bolero, or IBKR.

One sleeve — **satellite**: higher-conviction directional equity theses, meant to be
sized smaller than a market-neutral position would warrant. (An earlier crypto
funding-rate-arbitrage sleeve existed and was fully removed — not disabled, actually
deleted, including its connectors/backtest/tests — once the user decided against any
crypto exposure.)

Also built:
- **Backtesting** (`app/backtest/`) — validates the screener's signals against historical
  data before trusting them live. Technicals-only (RSI/momentum/vol); fundamentals can't
  be backtested with free data since yfinance only exposes current-snapshot fundamentals,
  not point-in-time historical ones.
- **Outcome journal** (`app/journal.py`) — every synthesized thesis is snapshotted and
  checked at 14/30/90-day horizons against what actually happened, building a real
  confidence-calibration track record over time. Empty until entries age past 14 days —
  that's expected, not a bug.
- **Portfolio** (`app/portfolio.py`, `app/connectors/ibkr.py`) — read-only IBKR position
  sync, unified with manually-tracked ING/Bolero holdings (CSV upload, or PDF/screenshot
  statement import with an editable review step before anything is saved — a model's
  read of a financial document is never auto-committed), plus manual buy/sell trade
  entries that update an existing holding in place. See IBKR setup below.
- **Cash-aware buy recommendations** (`app/action_plan.py`, `app/portfolio.py`) — a "buy"
  idea only counts as actionable if there's real deployable cash to fund it (IBKR's live
  cash balance, plus manually-declared cash per ING/Bolero broker). Cash is allocated
  greedily to the strongest ideas first; anything that can't be funded still shows up,
  clearly separated, as research rather than as something to act on today.
- **Position sizing** (`app/sizing.py`) — every opportunity gets a suggested size via
  fractional Kelly criterion, grounded in one aggregate walk-forward backtest (not
  per-symbol yet — see the module docstring for the full reasoning and a note on
  re-validating its confidence-calibration constants after any model swap). Always
  capped, always fractional (quarter-Kelly).
- **Daily email reports** (`app/reports.py`, `app/notify.py`) — morning (full refresh),
  lunch and evening (free pulse-checks: price moves, live IBKR P&L) sent via Gmail SMTP
  on a schedule (`MORNING_REPORT_TIME` etc. in `.env`).
- **Urgent alerts** (`app/alerts.py`) — a tighter-cadence (~20min) check, separate from
  the 3x/day digest, for the two conditions that genuinely can't wait: an invalidated
  thesis on something held, or a holding breaching the concentration threshold.
- **Consolidated daily view** (dashboard's "Needs attention" section) — the same urgent
  conditions the alert emails check are surfaced prominently on the dashboard itself, so
  opening the app tells you at a glance whether anything needs a look today.
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
  ranking and the synthesis bundle.
- **Dark mode** — a header toggle, persisted in `localStorage`; light is the deliberate
  default and it never auto-switches with the OS.

See `AGENTS.md` for the standing safety constraints (read-only IBKR, no DB table wipes,
no data fabrication) and the design decisions that aren't obvious from reading code alone
— required reading before making a nontrivial change here.

## Setup

### 1. Backend

```bash
cd backend
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

Edit `backend/.env`:
- `OPENAI_API_KEY` — required for the synthesis layer (get one at
  https://platform.openai.com/api-keys). Without it, the screener still runs and finds
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

Open http://localhost:1420. Click **Refresh** to trigger a scan — it takes roughly
30-60s against free APIs.

### 3. Native Mac app (Tauri)

Needs Rust/cargo installed once (`curl --proto '=https' --tlsv1.2 -sSf
https://sh.rustup.rs | sh`, then make sure `~/.cargo/bin` is on `PATH`). From `frontend/`:

```bash
npm run tauri dev
```

This opens a native window pointed at the Vite dev server — you still start the Python
backend separately (step 1). Auto-spawning the backend as a bundled sidecar process is
the natural next step for a real distributable `.app` (`npm run tauri build`); before
that, generate proper icons from your own artwork (a 1024x1024 PNG is enough):

```bash
npm run tauri icon path/to/your-icon.png
```

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
   your live positions, including live cash balance. If it's not running, the dashboard
   just shows "IBKR not connected" — the rest of the app works fine either way.

ING and Bolero have no public API, so those holdings go in via the Portfolio section's
CSV upload (`symbol,quantity,average_cost`), PDF/screenshot statement upload (reviewed
and editable before anything saves), or manual buy/sell trade entries afterward. Cash for
these two brokers is also self-declared there, since neither has a live feed for it.

## How the cost control works

Market data is free. The only real ongoing cost is the synthesis layer's LLM API calls
(`app/synthesis.py`). Two things keep that bounded regardless of how big the scan
universe gets:

1. The screener (`app/screener.py`) only ever shortlists the top ~10 candidates — the
   model never sees the full universe.
2. Each candidate's synthesis is cached by a content hash of its data bundle
   (`app/store.py`) — rerunning a scan with materially unchanged inputs reuses the
   cached note instead of paying for a new one.

## Project structure

```
backend/
  app/
    connectors/     # free market data: yfinance, FRED, SEC EDGAR, news RSS, FX, IBKR
    calculators/     # pure quant functions: technicals, fundamentals, DCF, risk, fees, insider signal
    backtest/         # walk-forward validation of the screener's own signals
    screener.py       # ranks candidates, keeps a short list
    synthesis.py       # model call -> structured research note, cached by content hash
    journal.py          # tracks thesis outcomes at 14/30/90d horizons for calibration
    action_plan.py        # buy/hold/sell shortlist netted against real holdings, fees, and available cash
    portfolio.py            # unifies IBKR positions + manual holdings; PDF/CSV import, manual trades, cash
    portfolio_risk.py         # concentration + correlation across everything held
    alerts.py                  # urgent-condition email checks, ~20min cadence
    reports.py                  # 3x/day digest emails
    store.py                     # SQLite persistence
    main.py                       # FastAPI: refresh, opportunities, journal, portfolio, action-plan, backtest
  tests/               # unit tests (calculators, backtest math, journal, portfolio, action plan)
frontend/
  src/                  # React dashboard (opportunities, action plan, portfolio, journal sections)
  src-tauri/             # Tauri shell (needs Rust to build)
```
