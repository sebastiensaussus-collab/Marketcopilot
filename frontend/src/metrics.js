// Pulls a handful of the underlying quant metrics out of raw_metrics for display as
// chips on each card — makes the numbers behind the thesis visible, not just the prose.

function pct(x, digits = 1) {
  if (x === null || x === undefined) return null;
  return `${(x * 100).toFixed(digits)}%`;
}

function num(x, digits = 2) {
  if (x === null || x === undefined) return null;
  return x.toFixed(digits);
}

export function extractChips(rawMetrics) {
  if (!rawMetrics) return [];

  if (rawMetrics.type === "equity_directional") {
    const m = rawMetrics.metrics || {};
    const insider = m.insider || {};
    // Only surface an insider chip when there's an actual open-market buy to report --
    // most names most of the time have none, and that's the point (see
    // app/calculators/insider_signal.py), so a chip on every card would just be noise.
    const insiderChip =
      insider.num_buy_transactions > 0
        ? {
            label: insider.cluster_buy_signal ? "Insider cluster buy" : "Insider buying",
            value: `${insider.num_distinct_buyers} buyer${insider.num_distinct_buyers === 1 ? "" : "s"}`,
          }
        : null;
    return [
      { label: "1w change", value: pct(m.pct_change_1w) },
      { label: "RSI(14)", value: num(m.rsi_14, 0) },
      { label: "FCF yield", value: pct(m.fcf_yield) },
      { label: "Sharpe", value: num(m.sharpe_ratio) },
      { label: "Off 52w high", value: pct(m.pct_off_52w_high) },
      insiderChip,
    ].filter((c) => c && c.value !== null && c.value !== undefined);
  }

  return [];
}

const SIZING_METHOD_LABELS = {
  confidence_heuristic: "heuristic",
};

export function extractSuggestedSize(rawMetrics) {
  if (!rawMetrics || rawMetrics.suggested_position_pct === undefined || rawMetrics.suggested_position_pct === null) {
    return null;
  }
  return {
    pct: rawMetrics.suggested_position_pct,
    methodLabel: SIZING_METHOD_LABELS[rawMetrics.sizing_method] || rawMetrics.sizing_method,
    rationale: rawMetrics.sizing_rationale,
  };
}
