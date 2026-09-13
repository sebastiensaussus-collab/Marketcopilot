import { useEffect, useMemo, useState } from "react";
import {
  confirmPortfolioImport,
  fetchActionPlan,
  fetchJournal,
  fetchOpportunities,
  fetchPortfolio,
  fetchPortfolioRisk,
  importPortfolioCsv,
  importPortfolioDocument,
  recordManualTrade,
  triggerRefresh,
  updateBrokerCash,
} from "./api.js";
import { extractChips, extractSuggestedSize } from "./metrics.js";

function confidenceTier(confidence) {
  if (confidence >= 0.6) return "high";
  if (confidence >= 0.4) return "medium";
  return "low";
}

function ConfidencePill({ confidence }) {
  const pct = Math.round(confidence * 100);
  return <span className={`confidence-pill tier-${confidenceTier(confidence)}`}>{pct}%</span>;
}

function MetricChips({ rawMetrics }) {
  const chips = extractChips(rawMetrics);
  if (chips.length === 0) return null;
  return (
    <div className="chip-row">
      {chips.map((c, i) => (
        <span className="chip" key={i}>
          <span className="chip-label">{c.label}</span>
          <span className="chip-value">{c.value}</span>
        </span>
      ))}
    </div>
  );
}

function SizingBadge({ rawMetrics }) {
  const size = extractSuggestedSize(rawMetrics);
  if (!size) return null;
  return (
    <div className="sizing-badge" title={size.rationale}>
      <span className="sizing-badge-label">Suggested size</span>
      <span className="sizing-badge-value">{Math.round(size.pct * 1000) / 10}%</span>
      <span className="sizing-badge-method">{size.methodLabel}</span>
    </div>
  );
}

function OpportunityCard({ opportunity, sleeve }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <article className={`card sleeve-${sleeve}`}>
      <header className="card-header">
        <div className="card-heading">
          <span className="symbol">{opportunity.symbol}</span>
          <h3>{opportunity.display_name || opportunity.symbol}</h3>
        </div>
        <div className="card-header-right">
          <ConfidencePill confidence={opportunity.confidence} />
          <SizingBadge rawMetrics={opportunity.raw_metrics} />
        </div>
      </header>

      <MetricChips rawMetrics={opportunity.raw_metrics} />

      <p className="thesis">{opportunity.thesis}</p>

      <button className="expand-toggle" onClick={() => setExpanded((v) => !v)}>
        {expanded ? "Hide detail" : "Show catalysts, risks & sizing"}
        <span className={`chevron ${expanded ? "open" : ""}`}>▾</span>
      </button>

      {expanded && (
        <div className="card-detail">
          <div className="detail-grid">
            {opportunity.catalysts?.length > 0 && (
              <div className="list-block catalysts-block">
                <h4>Catalysts</h4>
                <ul>
                  {opportunity.catalysts.map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </div>
            )}
            {opportunity.risks?.length > 0 && (
              <div className="list-block risks-block">
                <h4>Risks</h4>
                <ul>
                  {opportunity.risks.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          <p className="invalidation">
            <strong>Invalidated if</strong> — {opportunity.invalidation_condition}
          </p>
          <p className="sizing">{opportunity.sizing_note}</p>
        </div>
      )}

      <footer className="card-footer">
        Updated {new Date(opportunity.updated_at).toLocaleString()}
      </footer>
    </article>
  );
}

function Sleeve({ id, title, subtitle, opportunities, emptyHint }) {
  return (
    <section className={`sleeve sleeve-${id}`}>
      <div className="sleeve-header">
        <h2>{title}</h2>
        <p>{subtitle}</p>
      </div>
      {opportunities.length === 0 ? (
        <p className="empty-hint">{emptyHint}</p>
      ) : (
        <div className="card-list">
          {opportunities.map((o) => (
            <OpportunityCard key={o.id} opportunity={o} sleeve={id} />
          ))}
        </div>
      )}
    </section>
  );
}

function StatsBar({ data, lastRefreshStats }) {
  const stats = useMemo(() => {
    const all = data.satellite;
    const avgConfidence = all.length
      ? all.reduce((sum, o) => sum + o.confidence, 0) / all.length
      : null;
    const top = all.length
      ? all.reduce((best, o) => (o.confidence > best.confidence ? o : best), all[0])
      : null;
    return { total: all.length, avgConfidence, top };
  }, [data]);

  return (
    <div className="stats-bar">
      <div className="stat">
        <span className="stat-value">{data.satellite.length}</span>
        <span className="stat-label">opportunities</span>
      </div>
      <div className="stat">
        <span className="stat-value">
          {stats.avgConfidence !== null ? `${Math.round(stats.avgConfidence * 100)}%` : "—"}
        </span>
        <span className="stat-label">avg confidence</span>
      </div>
      <div className="stat">
        <span className="stat-value">{stats.top ? stats.top.symbol : "—"}</span>
        <span className="stat-label">top conviction</span>
      </div>
      {lastRefreshStats && (
        <div className="stat stat-scan">
          <span className="stat-value">{lastRefreshStats.equity_candidates_scanned}</span>
          <span className="stat-label">candidates scanned last run</span>
        </div>
      )}
    </div>
  );
}

const ACTION_LABELS = {
  exit: "Exit",
  trim: "Trim",
  trim_unmanaged: "Trim",
  add: "Add",
  new_entry: "New",
  hold: "Hold",
  hold_unmanaged: "Hold",
};

const MACRO_LABELS = {
  fed_funds_rate: "Fed funds",
  cpi_index: "CPI index",
  ten_year_yield: "10Y yield",
  unemployment_rate: "Unemployment",
};

const BUCKETS = [
  { id: "sell", title: "Sell", subtitle: "Trim or exit — invalidated theses first, then overweight positions.", empty: "Nothing to sell — no invalidated theses and nothing overweight." },
  { id: "buy", title: "Buy", subtitle: "New entries and adds, sized to model-suggested weight.", empty: "Nothing to buy — no new idea clears the bar right now." },
  { id: "hold", title: "Hold", subtitle: "Held at roughly the right weight, or outside the model's scan universe entirely.", empty: "Nothing currently held." },
];

function fmtUsd(x) {
  return x === null || x === undefined ? null : `$${x.toFixed(2)}`;
}

function MacroStrip({ macro }) {
  const entries = Object.entries(macro || {});
  return (
    <div className="macro-strip">
      {entries.length === 0 ? (
        <span className="macro-empty">
          Macro backdrop not configured — add FRED_API_KEY to see rates/CPI/unemployment context here.
        </span>
      ) : (
        entries.map(([key, obs]) => (
          <span className="macro-chip" key={key}>
            <span className="macro-chip-label">{MACRO_LABELS[key] || key}</span>
            <span className="macro-chip-value">{obs.value}</span>
            <span className="macro-chip-date">{obs.date}</span>
          </span>
        ))
      )}
    </div>
  );
}

function ActionLevels({ action }) {
  const chips = [
    { label: "Ref", value: fmtUsd(action.reference_price) },
    { label: "Limit", value: fmtUsd(action.limit_price) },
    { label: "Stop", value: action.stop_condition || fmtUsd(action.stop_price) },
    { label: "Target (DCF)", value: fmtUsd(action.target_price) },
  ].filter((c) => c.value);

  if (chips.length === 0) return null;

  return (
    <div className="action-levels">
      {chips.map((c) => (
        <span className="action-level-chip" key={c.label}>
          <span className="action-level-label">{c.label}</span>
          <span className="action-level-value">{c.value}</span>
        </span>
      ))}
      <span className="action-levels-disclaimer">suggested reference levels, not orders placed</span>
    </div>
  );
}

function CorrelationWarning({ correlatedHoldings }) {
  if (!correlatedHoldings || correlatedHoldings.length === 0) return null;

  return (
    <div className="correlation-warning">
      {correlatedHoldings.map((c) => (
        <span className="correlation-chip" key={c.symbol}>
          <span className="correlation-chip-icon" aria-hidden="true">⚠</span>
          compounds {c.symbol} ({Math.round(c.weight * 100)}% of portfolio, r={c.correlation.toFixed(2)})
        </span>
      ))}
    </div>
  );
}

function ActionCard({ action }) {
  return (
    <div className={`action-card action-${action.action}`}>
      <div className="action-card-top">
        <span className={`action-badge action-badge-${action.action}`}>{ACTION_LABELS[action.action]}</span>
        <span className="symbol">{action.symbol}</span>
        <span className="action-card-name">{action.display_name}</span>
        {action.confidence !== null && action.confidence !== undefined && (
          <ConfidencePill confidence={action.confidence} />
        )}
        <span className="action-card-eur">
          {action.suggested_eur !== null
            ? `€${Math.round(action.suggested_eur).toLocaleString()}`
            : action.held_eur
              ? `€${Math.round(action.held_eur).toLocaleString()} held`
              : "size n/a"}
        </span>
      </div>
      <p className="action-card-rationale">{action.rationale}</p>
      <CorrelationWarning correlatedHoldings={action.correlated_holdings} />
      {action.fee && (
        <p className="action-card-fee">
          est. via {action.fee.broker} · €{action.fee.fee_eur.toFixed(2)}
          {action.fee.tob_eur ? ` + €${action.fee.tob_eur.toFixed(2)} TOB` : ""} — estimated, not your actual terms
        </p>
      )}
      <ActionLevels action={action} />
    </div>
  );
}

function attentionKeyFigure(action) {
  // The one specific number worth a glance -- exit's real actionable price is the
  // at-market limit (see _price_levels' docstring: no buffer, the point is getting out).
  // trim_unmanaged has no per-share price at all (it's a diversification call, not a
  // thesis on the instrument), so the closest equivalent is how much to trim by.
  if (action.action === "exit" && action.limit_price != null) {
    return `exit ~${fmtUsd(action.limit_price)}`;
  }
  if (action.action === "trim_unmanaged" && action.suggested_eur != null) {
    const trimEur = Math.round(action.held_eur - action.suggested_eur);
    return `trim ~€${trimEur.toLocaleString()}`;
  }
  return null;
}

function NeedsAttentionSection({ actions }) {
  const urgent = actions.filter((a) => a.urgent);

  return (
    <div className="needs-attention">
      <div className="needs-attention-header">
        <h3>Needs attention today</h3>
      </div>
      {urgent.length === 0 ? (
        <p className="needs-attention-clear">✓ Nothing needs action today.</p>
      ) : (
        <div className="attention-list">
          {urgent.map((a) => (
            <div className="attention-row" key={`${a.sleeve}-${a.symbol}`}>
              <div className="attention-row-top">
                <span className={`action-badge action-badge-${a.action}`}>{ACTION_LABELS[a.action]}</span>
                <span className="symbol">{a.symbol}</span>
                <span className="attention-row-name">{a.display_name}</span>
                <span className="attention-row-figure">{attentionKeyFigure(a)}</span>
              </div>
              <p className="attention-row-reason">{a.rationale}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function WouldBuySection({ actions }) {
  const wouldBuy = actions.filter((a) => a.bucket === "buy_no_cash");
  if (wouldBuy.length === 0) return null;

  return (
    <div className="would-buy">
      <div className="would-buy-header">
        <h3>Would buy — no cash available</h3>
        <span>
          Clears the bar, but there's no cash registered to fund it — not actionable today. Add cash in the
          Portfolio section below to change that.
        </span>
      </div>
      <div className="attention-list">
        {wouldBuy.map((a) => (
          <div className="attention-row would-buy-row" key={`${a.sleeve}-${a.symbol}`}>
            <div className="attention-row-top">
              <span className={`action-badge action-badge-${a.action}`}>{ACTION_LABELS[a.action]}</span>
              <span className="symbol">{a.symbol}</span>
              <span className="attention-row-name">{a.display_name}</span>
              <span className="attention-row-figure">
                {a.suggested_eur != null ? `€${Math.round(a.suggested_eur).toLocaleString()}` : null}
              </span>
            </div>
            <p className="attention-row-reason">{a.rationale}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function ActionBucketSection({ bucket, actions }) {
  return (
    <div className="action-bucket">
      <div className="action-bucket-header">
        <h3>{bucket.title}</h3>
        <span>{bucket.subtitle}</span>
      </div>
      {actions.length === 0 ? (
        <p className="empty-hint-small">{bucket.empty}</p>
      ) : (
        <div className="action-card-list">
          {actions.map((a) => (
            <ActionCard action={a} key={`${a.sleeve}-${a.symbol}`} />
          ))}
        </div>
      )}
    </div>
  );
}

const ASSET_CLASS_COLORS = {
  equity: "var(--accent)",
  bond: "var(--brass)",
  commodity: "#9c6b3b",
};

const ASSET_CLASS_LABELS = {
  equity: "Equity",
  bond: "Bonds",
  commodity: "Commodities",
};

function AssetAllocationBar({ allocation }) {
  if (!allocation || !allocation.weights || Object.keys(allocation.weights).length === 0) return null;
  const entries = Object.entries(allocation.weights).sort((a, b) => b[1] - a[1]);

  return (
    <div className="asset-allocation">
      <div className="asset-allocation-header">
        <h4>Asset allocation</h4>
        <span>Real composition across everything priced — not a signal, just what you actually hold.</span>
      </div>
      <div className="asset-allocation-bar">
        {entries.map(([cls, weight]) => (
          <div
            key={cls}
            className="asset-allocation-segment"
            style={{ width: `${weight * 100}%`, background: ASSET_CLASS_COLORS[cls] || "var(--text-dim)" }}
            title={`${ASSET_CLASS_LABELS[cls] || cls}: ${Math.round(weight * 1000) / 10}%`}
          />
        ))}
      </div>
      <div className="asset-allocation-legend">
        {entries.map(([cls, weight]) => (
          <span className="asset-allocation-legend-item" key={cls}>
            <span className="asset-allocation-swatch" style={{ background: ASSET_CLASS_COLORS[cls] || "var(--text-dim)" }} />
            {ASSET_CLASS_LABELS[cls] || cls} {Math.round(weight * 1000) / 10}%
          </span>
        ))}
      </div>
    </div>
  );
}

function ActionPlanSection({ actionPlan }) {
  if (!actionPlan) return null;
  const {
    actions,
    nav_eur: navEur,
    unpriced_symbols: unpriced,
    macro,
    asset_allocation: assetAllocation,
    available_cash_eur: availableCashEur,
  } = actionPlan;
  const riskCount = actions.filter((a) => a.bucket === "sell").length;
  const ideaCount = actions.filter((a) => a.bucket === "buy").length;

  return (
    <section className="action-plan">
      <div className="sleeve-header">
        <h2>Today's actions</h2>
        <p>What to buy, hold, and sell — cross-referenced against what you actually hold, netted for estimated fees/taxes, with suggested reference levels. Still research context, not a directive.</p>
      </div>

      <NeedsAttentionSection actions={actions} />

      <MacroStrip macro={macro} />
      <AssetAllocationBar allocation={assetAllocation} />

      <div className="stats-bar action-plan-stats">
        <div className="stat">
          <span className="stat-value">€{Math.round(navEur).toLocaleString()}</span>
          <span className="stat-label">priced portfolio NAV</span>
        </div>
        <div className="stat">
          <span className="stat-value">{riskCount}</span>
          <span className="stat-label">flagged for exit/trim</span>
        </div>
        <div className="stat">
          <span className="stat-value">{ideaCount}</span>
          <span className="stat-label">adds / new ideas</span>
        </div>
        <div className="stat">
          <span className="stat-value">€{Math.round(availableCashEur || 0).toLocaleString()}</span>
          <span className="stat-label">cash available to deploy</span>
        </div>
      </div>

      {unpriced.length > 0 && (
        <p className="empty-hint-small">Couldn't price (excluded from sizing): {unpriced.join(", ")}</p>
      )}

      <div className="action-bucket-grid">
        {BUCKETS.map((bucket) => (
          <ActionBucketSection key={bucket.id} bucket={bucket} actions={actions.filter((a) => a.bucket === bucket.id)} />
        ))}
      </div>

      <WouldBuySection actions={actions} />
    </section>
  );
}

function JournalSection({ journal }) {
  if (!journal) return null;
  const { calibration } = journal;
  const buckets = Object.entries(calibration.calibration_buckets || {}).sort();

  return (
    <section className="journal-section">
      <div className="sleeve-header">
        <h2>Journal</h2>
        <p>Every thesis checked against what actually happened — evidence, not just narrative.</p>
      </div>

      <div className="stats-bar journal-stats">
        <div className="stat">
          <span className="stat-value">{calibration.total_entries}</span>
          <span className="stat-label">theses tracked</span>
        </div>
        <div className="stat">
          <span className="stat-value">{calibration.matured_entries}</span>
          <span className="stat-label">matured outcomes</span>
        </div>
      </div>

      {buckets.length === 0 ? (
        <p className="empty-hint">
          No matured outcomes yet — theses need to age past their first 14-day checkpoint before
          this becomes meaningful. First results land ~14 days after the earliest tracked thesis.
        </p>
      ) : (
        <div className="table-wrap">
          <table className="calibration-table">
            <thead>
              <tr>
                <th>Confidence bucket</th>
                <th>Avg. confidence</th>
                <th>Actual hit rate</th>
                <th>N</th>
              </tr>
            </thead>
            <tbody>
              {buckets.map(([bucket, stats]) => (
                <tr key={bucket}>
                  <td>{bucket}</td>
                  <td>{Math.round(stats.avg_confidence * 100)}%</td>
                  <td>{Math.round(stats.actual_hit_rate * 100)}%</td>
                  <td>{stats.num_entries}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function IBKRStatus({ ibkr, cashEur }) {
  return (
    <div className={`ibkr-status ${ibkr.connected ? "connected" : "disconnected"}`}>
      <span className="status-dot" />
      {ibkr.connected ? "IBKR connected" : "IBKR not connected"}
      {ibkr.connected && cashEur != null && (
        <span className="ibkr-cash">€{cashEur.toLocaleString()} cash (live)</span>
      )}
    </div>
  );
}

function PositionsTable({ positions }) {
  if (positions.length === 0) {
    return <p className="empty-hint-small">Connected, but no open positions.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="portfolio-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Qty</th>
            <th>Avg cost</th>
            <th>Price</th>
            <th>Mkt value</th>
            <th>Unrealized P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p, i) => (
            <tr key={i}>
              <td>
                {p.symbol}
                {p.name && p.name !== p.symbol && <span className="holding-name"> — {p.name}</span>}
              </td>
              <td>{p.position}</td>
              <td>{p.average_cost?.toFixed(2)}</td>
              <td>{p.market_price?.toFixed(2)}</td>
              <td>{p.market_value?.toFixed(2)}</td>
              <td className={p.unrealized_pnl >= 0 ? "positive" : "negative"}>
                {p.unrealized_pnl?.toFixed(2)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ManualHoldingsTable({ broker, holdings, cashEur, onReload }) {
  return (
    <div className="manual-broker-block">
      <h4>{broker}</h4>
      {holdings?.length > 0 ? (
        <div className="table-wrap">
          <table className="portfolio-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Qty</th>
                <th>Avg cost</th>
              </tr>
            </thead>
            <tbody>
              {holdings.map((h, i) => (
                <tr key={i}>
                  <td>{h.symbol}</td>
                  <td>{h.quantity}</td>
                  <td>{h.average_cost ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="empty-hint-small">No holdings imported yet.</p>
      )}
      <TradeForm broker={broker} onReload={onReload} />
      <CashControl broker={broker} cashEur={cashEur} onReload={onReload} />
    </div>
  );
}

function TradeForm({ broker, onReload }) {
  const [symbol, setSymbol] = useState("");
  const [action, setAction] = useState("buy");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!symbol || !quantity || !price) return;
    setBusy(true);
    setStatus(null);
    try {
      await recordManualTrade(broker, symbol, action, parseFloat(quantity), parseFloat(price));
      setStatus(`Recorded ${action} of ${quantity} ${symbol.toUpperCase()}.`);
      setSymbol("");
      setQuantity("");
      setPrice("");
      onReload();
    } catch (err) {
      setStatus(`Error: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="trade-form" onSubmit={handleSubmit}>
      <input
        type="text"
        placeholder="Symbol"
        value={symbol}
        onChange={(e) => setSymbol(e.target.value)}
        disabled={busy}
      />
      <select value={action} onChange={(e) => setAction(e.target.value)} disabled={busy}>
        <option value="buy">Buy</option>
        <option value="sell">Sell</option>
      </select>
      <input
        type="number"
        placeholder="Qty"
        value={quantity}
        onChange={(e) => setQuantity(e.target.value)}
        disabled={busy}
        step="any"
        min="0"
      />
      <input
        type="number"
        placeholder="Price"
        value={price}
        onChange={(e) => setPrice(e.target.value)}
        disabled={busy}
        step="any"
        min="0"
      />
      <button type="submit" disabled={busy}>
        {busy ? "Recording…" : "Record trade"}
      </button>
      {status && <span className="trade-form-status">{status}</span>}
    </form>
  );
}

function CashControl({ broker, cashEur, onReload }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(cashEur ?? 0);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(null);

  async function handleSave() {
    setBusy(true);
    setStatus(null);
    try {
      await updateBrokerCash(broker, parseFloat(value) || 0);
      setEditing(false);
      onReload();
    } catch (err) {
      setStatus(`Error: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }

  if (!editing) {
    return (
      <div className="cash-control">
        <span className="cash-control-label">Cash available:</span>
        <span className="cash-control-value">€{(cashEur ?? 0).toLocaleString()}</span>
        <button
          type="button"
          className="cash-control-edit"
          onClick={() => {
            setValue(cashEur ?? 0);
            setEditing(true);
          }}
        >
          Update
        </button>
        {status && <span className="cash-control-status">{status}</span>}
      </div>
    );
  }

  return (
    <div className="cash-control">
      <span className="cash-control-label">Cash available:</span>
      <input type="number" value={value} onChange={(e) => setValue(e.target.value)} disabled={busy} step="any" min="0" />
      <button type="button" onClick={handleSave} disabled={busy}>
        {busy ? "Saving…" : "Save"}
      </button>
      <button type="button" className="cash-control-cancel" onClick={() => setEditing(false)} disabled={busy}>
        Cancel
      </button>
    </div>
  );
}

function CsvImportForm({ onImported }) {
  const [broker, setBroker] = useState("ing");
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);

  async function handleFileChange(e) {
    const file = e.target.files[0];
    if (!file) return;
    setBusy(true);
    setStatus(null);
    try {
      const result = await importPortfolioCsv(broker, file);
      setStatus(`Imported ${result.holdings_imported} holdings for ${broker}.`);
      onImported();
    } catch (err) {
      setStatus(`Error: ${err.message}`);
    } finally {
      setBusy(false);
      e.target.value = "";
    }
  }

  return (
    <div className="csv-import">
      <select value={broker} onChange={(e) => setBroker(e.target.value)} disabled={busy}>
        <option value="ing">ING</option>
        <option value="bolero">Bolero</option>
      </select>
      <label className="file-input-label">
        {busy ? "Importing…" : "Upload CSV"}
        <input type="file" accept=".csv" onChange={handleFileChange} disabled={busy} hidden />
      </label>
      {status && <span className="csv-import-status">{status}</span>}
    </div>
  );
}

function DocumentImportForm({ onImported }) {
  const [broker, setBroker] = useState("ing");
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [review, setReview] = useState(null);

  async function handleFileChange(e) {
    const file = e.target.files[0];
    if (!file) return;
    setBusy(true);
    setStatus(null);
    setReview(null);
    try {
      const result = await importPortfolioDocument(broker, file);
      setReview(
        result.holdings.map((h) => ({
          name: h.name,
          isin: h.isin,
          symbol: h.symbol_guess,
          quantity: h.quantity,
          average_cost: h.average_cost ?? "",
          currency: h.currency,
        }))
      );
    } catch (err) {
      setStatus(`Error: ${err.message}`);
    } finally {
      setBusy(false);
      e.target.value = "";
    }
  }

  function updateRow(i, field, value) {
    setReview((rows) => rows.map((r, idx) => (idx === i ? { ...r, [field]: value } : r)));
  }

  function removeRow(i) {
    setReview((rows) => rows.filter((_, idx) => idx !== i));
  }

  async function handleConfirm() {
    setBusy(true);
    setStatus(null);
    try {
      const holdings = review.map((r) => ({
        symbol: r.symbol.toUpperCase(),
        quantity: parseFloat(r.quantity),
        average_cost: r.average_cost === "" ? null : parseFloat(r.average_cost),
      }));
      const result = await confirmPortfolioImport(broker, holdings);
      setStatus(`Imported ${result.holdings_imported} holdings for ${broker}.`);
      setReview(null);
      onImported();
    } catch (err) {
      setStatus(`Error: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="document-import">
      {!review && (
        <div className="csv-import">
          <select value={broker} onChange={(e) => setBroker(e.target.value)} disabled={busy}>
            <option value="ing">ING</option>
            <option value="bolero">Bolero</option>
          </select>
          <label className="file-input-label">
            {busy ? "Reading document…" : "Upload statement (PDF/image)"}
            <input
              type="file"
              accept="application/pdf,image/png,image/jpeg,image/webp"
              onChange={handleFileChange}
              disabled={busy}
              hidden
            />
          </label>
          {status && <span className="csv-import-status">{status}</span>}
        </div>
      )}

      {review && (
        <div className="document-import-review">
          <p className="document-import-review-hint">
            Review before importing — confirm each symbol is correct (the model's best guess from the
            statement, not a verified ticker), and convert any avg cost flagged as non-EUR before
            confirming (holdings here are tracked in EUR).
          </p>
          <div className="table-wrap">
            <table className="portfolio-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Symbol</th>
                  <th>Qty</th>
                  <th>Avg cost (EUR)</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {review.map((r, i) => (
                  <tr key={i}>
                    <td className="document-import-name">
                      {r.name}
                      {r.isin && <span className="holding-name"> — {r.isin}</span>}
                    </td>
                    <td>
                      <input type="text" value={r.symbol} onChange={(e) => updateRow(i, "symbol", e.target.value)} />
                    </td>
                    <td>
                      <input
                        type="number"
                        value={r.quantity}
                        onChange={(e) => updateRow(i, "quantity", e.target.value)}
                        step="any"
                      />
                    </td>
                    <td>
                      <input
                        type="number"
                        value={r.average_cost}
                        onChange={(e) => updateRow(i, "average_cost", e.target.value)}
                        step="any"
                      />
                      {r.currency && r.currency !== "EUR" && (
                        <span className="document-import-currency-warn" title="Holdings are tracked in EUR — convert before confirming.">
                          {r.currency}, not EUR
                        </span>
                      )}
                    </td>
                    <td>
                      <button type="button" className="row-remove" onClick={() => removeRow(i)}>
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="document-import-actions">
            <button onClick={handleConfirm} disabled={busy || review.length === 0}>
              {busy ? "Importing…" : "Confirm import"}
            </button>
            <button className="secondary" onClick={() => setReview(null)} disabled={busy}>
              Cancel
            </button>
          </div>
          {status && <span className="csv-import-status">{status}</span>}
        </div>
      )}
    </div>
  );
}

function PortfolioRiskSection({ risk }) {
  if (!risk) return null;
  const { concentration, unpriced_symbols: unpriced, correlation, names } = risk;
  const weightRows = Object.entries(concentration.weights).sort((a, b) => b[1] - a[1]);

  return (
    <div className="portfolio-risk">
      <h4>Concentration &amp; correlation</h4>

      {concentration.concentration_flags.length > 0 && (
        <div className="risk-flags">
          {concentration.concentration_flags.map((f) => (
            <div className="risk-flag" key={f.symbol}>
              ⚠ <b>{f.symbol}</b> ({names?.[f.symbol] || f.symbol}) is {Math.round(f.weight * 100)}% of priced
              portfolio value
            </div>
          ))}
        </div>
      )}

      <div className="table-wrap">
        <table className="portfolio-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Weight</th>
            </tr>
          </thead>
          <tbody>
            {weightRows.map(([symbol, weight]) => (
              <tr key={symbol}>
                <td>
                  {symbol}
                  {names?.[symbol] && names[symbol] !== symbol && (
                    <span className="holding-name"> — {names[symbol]}</span>
                  )}
                </td>
                <td>{Math.round(weight * 1000) / 10}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="empty-hint-small">Total priced value: €{concentration.total_value_eur.toFixed(2)}</p>

      {unpriced.length > 0 && (
        <p className="empty-hint-small">Couldn't price (excluded from weights): {unpriced.join(", ")}</p>
      )}

      <p className="empty-hint-small">
        Avg pairwise correlation:{" "}
        {correlation.avg_pairwise_correlation !== null ? correlation.avg_pairwise_correlation : "n/a"}
        {correlation.symbols_excluded_no_history.length > 0 &&
          ` (${correlation.symbols_excluded_no_history.join(", ")} excluded — no free historical data available)`}
      </p>
      {correlation.highly_correlated_pairs.length > 0 && (
        <div className="risk-flags">
          {correlation.highly_correlated_pairs.map((p, i) => (
            <div className="risk-flag" key={i}>
              ⚠ {p.symbol_a} / {p.symbol_b} move together (correlation {p.correlation})
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function PortfolioSection({ portfolio, risk, onReload }) {
  if (!portfolio) return null;
  const brokers = Object.keys(portfolio.manual);

  return (
    <section className="portfolio-section">
      <div className="sleeve-header">
        <h2>Portfolio</h2>
        <p>Unified view across IBKR (live, read-only) and manually-imported ING/Bolero holdings.</p>
      </div>

      <IBKRStatus ibkr={portfolio.ibkr} cashEur={portfolio.cash?.by_broker?.ibkr} />

      {portfolio.ibkr.connected ? (
        <PositionsTable positions={portfolio.ibkr.positions} />
      ) : (
        <p className="empty-hint">
          Not connected. Install &amp; run IB Gateway or TWS, log into it yourself with your
          own IBKR credentials, and enable API access under Configure → API → Settings — this
          app only talks to that already-authenticated local connection, and only ever reads
          from it.
        </p>
      )}

      <div className="manual-holdings">
        {brokers.length === 0 ? (
          <p className="empty-hint-small">No manual holdings imported yet.</p>
        ) : (
          brokers.map((broker) => (
            <ManualHoldingsTable
              key={broker}
              broker={broker}
              holdings={portfolio.manual[broker]}
              cashEur={portfolio.cash?.by_broker?.[broker]}
              onReload={onReload}
            />
          ))
        )}
        <DocumentImportForm onImported={onReload} />
        <CsvImportForm onImported={onReload} />
      </div>

      <PortfolioRiskSection risk={risk} />
    </section>
  );
}

export default function App() {
  const [data, setData] = useState({ satellite: [] });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [lastRefreshStats, setLastRefreshStats] = useState(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState(null);
  const [journal, setJournal] = useState(null);
  const [portfolio, setPortfolio] = useState(null);
  const [portfolioRisk, setPortfolioRisk] = useState(null);
  const [actionPlan, setActionPlan] = useState(null);
  // Deliberately light-first (see index.css) -- only ever switches to dark via this
  // toggle, never from OS preference, and remembers the choice across reloads.
  const [theme, setTheme] = useState(() => localStorage.getItem("theme") || "light");

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("theme", theme);
  }, [theme]);

  async function loadOpportunities() {
    try {
      const result = await fetchOpportunities();
      setData(result);
      setLastUpdatedAt(new Date());
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }

  async function loadJournal() {
    try {
      setJournal(await fetchJournal());
    } catch {
      // journal is supplementary -- don't surface its errors over the main dashboard error banner
    }
  }

  async function loadActionPlan() {
    try {
      setActionPlan(await fetchActionPlan());
    } catch {
      // supplementary -- same reasoning as journal/portfolio above
    }
  }

  async function loadPortfolio() {
    try {
      setPortfolio(await fetchPortfolio());
    } catch {
      // portfolio is supplementary -- same reasoning as the journal above
    }
    try {
      setPortfolioRisk(await fetchPortfolioRisk());
    } catch {
      // risk view is supplementary too -- it can be slow (FX + price lookups), never
      // let it block the rest of the dashboard from rendering
    }
  }

  async function handleRefresh() {
    setLoading(true);
    setError(null);
    try {
      const stats = await triggerRefresh();
      setLastRefreshStats(stats);
      await Promise.all([loadOpportunities(), loadJournal(), loadActionPlan()]);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handlePortfolioReload() {
    await Promise.all([loadPortfolio(), loadActionPlan()]);
  }

  useEffect(() => {
    loadOpportunities();
    loadJournal();
    loadPortfolio();
    loadActionPlan();
  }, []);

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <div className="brand-mark">MC</div>
          <div>
            <h1>Market Copilot</h1>
            <p className="tagline">Research decision-support, not a directive — you place every trade.</p>
          </div>
        </div>
        <div className="app-header-actions">
          {lastUpdatedAt && (
            <span className="last-updated">Loaded {lastUpdatedAt.toLocaleTimeString()}</span>
          )}
          <button
            className="theme-toggle"
            onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            aria-label="Toggle color theme"
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
          <button onClick={handleRefresh} disabled={loading}>
            {loading ? (
              <>
                <span className="spinner" /> Scanning…
              </>
            ) : (
              "Refresh"
            )}
          </button>
        </div>
      </header>

      <StatsBar data={data} lastRefreshStats={lastRefreshStats} />

      {error && <div className="error-banner">Error: {error}</div>}

      <ActionPlanSection actionPlan={actionPlan} />

      <main className="columns columns-single">
        <Sleeve
          id="satellite"
          title="Satellite"
          subtitle="Conviction — directional equity theses, sized smaller"
          opportunities={data.satellite}
          emptyHint="No satellite opportunities yet. Click Refresh (needs OPENAI_API_KEY set on the backend)."
        />
      </main>

      <PortfolioSection portfolio={portfolio} risk={portfolioRisk} onReload={handlePortfolioReload} />
      <JournalSection journal={journal} />
    </div>
  );
}
