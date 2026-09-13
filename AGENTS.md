# Agent notes for Market Copilot

Read this before making a nontrivial change. It captures constraints and decisions that
aren't obvious from reading the code cold — the human-facing "what is this app" doc is
`README.md`; this is the "how to safely work in this codebase" doc.

## What this is

A personal, local research/decision-support app for one person's own equity investing.
FastAPI + SQLite backend, React/Vite frontend (Tauri shell for a native Mac window). It
scans free market data, synthesizes structured research notes via an LLM API, tracks a
real portfolio (live read-only IBKR + manually-declared ING/Bolero holdings), and emails
daily digest reports. **It never executes a trade of any kind.** The user reads its
output and acts manually in his own brokerage accounts.

## Non-negotiable constraints

These aren't style preferences — violating them breaks the reason this app is trusted
with real financial decisions.

- **Never place, modify, or cancel a trade.** No code path anywhere does this, including
  the IBKR connector (`app/connectors/ibkr.py`) — it only ever reads positions/account
  summary. Do not add an order-placement code path, even behind a flag, even "just for
  testing."
- **Never wipe a whole table.** Deletions are always a targeted `WHERE` clause (see
  `store.py`'s `replace_manual_holdings` — deletes only that one broker's rows — and any
  historical one-off cleanup scripts in git history). This is real financial tracking
  data; a blanket `DELETE FROM` or dropped table is not recoverable by the user.
- **Never let an LLM's read of a financial document get auto-committed.** The PDF/
  screenshot portfolio-statement import (`portfolio.py`'s `extract_holdings_from_document`)
  deliberately returns extracted data for a *review step* (editable in the frontend)
  before anything reaches the DB — because a wrong ticker guess would silently mis-price
  a real position. If you touch this path, keep the extract-then-review-then-commit
  split; don't collapse it into one auto-saving call.
- **Never fabricate a number.** Every synthesis/extraction system prompt in this repo
  explicitly instructs the model to ground claims only in the provided data and say "I
  don't know" (lower confidence, omit the field) rather than invent one. Keep that
  instruction if you touch either prompt.

## Architecture map

```
backend/app/
  connectors/       free data sources: yfinance, FRED, SEC EDGAR, news RSS, FX, IBKR (read-only)
  calculators/      pure functions: technicals, fundamentals, DCF, risk, fees/TOB, insider signal
  backtest/         walk-forward validation of the screener's own signals
  screener.py       ranks candidates cheaply, keeps a short list before any paid API call
  synthesis.py      the one function that spends LLM API cost — structured thesis, cached by content hash
  journal.py        snapshots every thesis, checks it at 14/30/90d for confidence calibration
  sizing.py         fractional Kelly position sizing (see its own docstring for the full history)
  action_plan.py    buy/hold/sell shortlist: opportunities netted against real holdings + fees + available cash
  portfolio.py       unifies IBKR + manual holdings; CSV/PDF import, manual trades, cash tracking, model extraction calls
  portfolio_risk.py   concentration + correlation across everything held
  watchdog.py          computable thesis-invalidation proxy (price move vs. a threshold), shared by reports + action_plan
  alerts.py             urgent-only email checks (~20min cadence) — reuses watchdog + portfolio_risk, no new detection logic
  reports.py             3x/day digest emails; morning is the only one that spends synthesis cost
  store.py                SQLite models + CRUD (SQLModel)
  settings.py               env-driven config (pydantic-settings, reads backend/.env)
  main.py                    FastAPI app, all routes
frontend/src/
  App.jsx           all components (single file by convention here, not yet split)
  api.js            fetch wrappers, one per backend route
  metrics.js         chip/sizing extraction helpers for the opportunity cards
  index.css            theme tokens (light default + toggled dark), all component styles
```

## Running things

```bash
cd backend && ./.venv/bin/uvicorn app.main:app --port 8000   # backend
cd backend && ./.venv/bin/python -m pytest -q                # tests
cd frontend && npm run dev                                    # web dev server, localhost:1420
cd frontend && npm run tauri dev                               # native Mac window (needs ~/.cargo/bin on PATH)
```

No `CLAUDE.md`/other agent-instruction file exists besides this one. Full setup
(env vars, IBKR pairing, etc.) is in `README.md` — don't duplicate it here.

## Testing convention actually used in this repo

No test anywhere mocks the LLM client or touches the real DB via `store.py` directly —
confirmed by grep, not an oversight to "fix." The pattern is: pure-logic functions (CSV
parsing, trade-math, schema validators like `_is_valid_analysis`/`_is_valid_extraction`,
`action_plan.py`'s classification logic via injectable `priced`/`macro_snapshot`/
`available_cash` args) get real unit tests; anything that hits a live API or the DB gets
verified with a running server and `curl`/browser checks instead. Match this — don't
introduce a new DB-fixture or API-mocking pattern for one new test file.

## Standing design decisions (not obvious from the code alone)

- **Crypto was fully removed, not flagged off.** An earlier "core" sleeve did
  funding-rate arbitrage across Binance/Bybit; the user decided against any crypto
  exposure and it was deleted outright (connectors, backtest, tests, UI, DB rows) rather
  than hidden behind a setting. If crypto ever comes back, treat it as a new feature, not
  a revert.
- **Manual-holding symbols are never auto-trusted from a document.** A broker PDF states
  a security's name/ISIN, not its trading ticker — a wrong ticker guess would silently
  mis-price a different instrument. `symbol_guess` from the extraction call is explicitly
  labeled a guess and must survive a human review step before being saved as `symbol`.
- **Buy-cash gating uses greedy allocation, not a proportional haircut.** When available
  cash can't fund every buy idea, the strongest ideas (by confidence/priority) get fully
  funded first and the rest are shown separately as non-actionable research — chosen
  specifically to avoid ending up with a pile of fee-inefficient micro-positions.
- **`sizing.py`'s `REFERENCE_CONFIDENCE`/`MIN_SATELLITE_CONFIDENCE`** were empirically fit
  to the previous model's (Claude's) confidence output distribution. They were carried
  over as-is in the OpenAI migration — re-validate them against a batch of real syntheses
  from the current model before trusting sizing output; see the module docstring.
  This is a general pattern worth remembering: if the synthesis/extraction model is ever
  swapped again, treat any empirically-derived constant tied to the old model's specific
  behavior as needing re-validation, not a free carry-over.
- **Dark mode is deliberately light-first and never auto-switches with the OS** — only
  the header toggle changes it, persisted in `localStorage`. This was a conscious choice,
  not an oversight to "fix" by adding `prefers-color-scheme` support back.
- **Git remote pushes may fail from an agent sandbox with no cached GitHub credentials.**
  If `git push` fails with an auth error here, that's usually an environment limitation
  (no credential helper, no SSH key), not a problem with the commit itself — tell the
  user to push from their own terminal rather than trying to work around it.
