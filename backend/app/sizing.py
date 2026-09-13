"""Position sizing via fractional Kelly criterion, grounded in the backtested data we
actually trust -- and only that data.

The "even-money" Kelly simplification, f* = 2p - 1, using only a win rate, is the
primitive this module builds on. It's dimensionless -- no magnitude assumption to get
wrong, unlike a continuous or discrete-payoff Kelly formula fed noisy magnitude data
would be -- and win rate is a plain frequency count. It is deliberately a conservative
proxy, not a claim that any real payoff is truly even-money.

Satellite has no per-symbol backtest at all yet (only one aggregate walk-forward result
across the S&P-100-slice universe -- 65.7% hit rate, Sharpe ~1, concentrated in mega-cap
tech, one bull-market regime, small sample). It was first built scaling off "confidence
above a 0.5 coin-flip" -- and shipped an all-zero bug: live testing showed the model's
satellite confidence actually clusters around 0.40-0.50, since the synthesis prompt
explicitly tells it to rarely exceed 0.75. Every real thesis landed at or below the
assumed 0.5 baseline, so every suggested size came back zero. Confidence isn't a
calibrated absolute win-probability yet (that's what the journal is for) -- it's a
relative signal, and needs a baseline drawn from real data, not an assumed one.

Fixed version: the *aggregate backtested win rate* (65.7%) is the base probability fed to
the even-money Kelly formula, damped for limited trust. Individual thesis
confidence then scales that base up or down relative to REFERENCE_CONFIDENCE (roughly
where satellite confidence actually centers, empirically) rather than standing
in for win probability directly. Below MIN_SATELLITE_CONFIDENCE, the model has already
flagged real doubt in its own output, so sizing goes to zero regardless of the aggregate
baseline. Should be replaced by real per-bucket calibration from the journal once entries
mature -- confidence will mean something calibrated once that exists.

NOTE (post-migration from Claude to OpenAI): REFERENCE_CONFIDENCE and
MIN_SATELLITE_CONFIDENCE below were empirically fit to Claude's specific confidence
output distribution -- there's no guarantee the new model centers its confidence the same
way. Re-validate both against a batch of real syntheses from the new model before trusting
sizing output; until then, treat these two constants as a carried-over placeholder, not a
re-confirmed calibration.

Every output is fractional Kelly (quarter-Kelly by default) and hard-capped -- full Kelly
is well known to be too aggressive even when inputs are exactly right.
"""

KELLY_FRACTION = 0.25
MAX_POSITION_PCT = 0.15
SATELLITE_AGGREGATE_EDGE_MULTIPLIER = 0.6  # damping factor reflecting only moderate trust in the aggregate backtest
AGGREGATE_SATELLITE_WIN_RATE = 0.657  # from the walk-forward equity-momentum backtest
REFERENCE_CONFIDENCE = 0.45  # empirical center of the (Claude-era) satellite confidence output -- re-validate post-migration, see module docstring
MIN_SATELLITE_CONFIDENCE = 0.30  # below this, the model itself is flagging real doubt -- re-validate post-migration, see module docstring


def kelly_fraction_even_money(win_rate: float) -> float:
    """f* = 2p - 1. Never negative -- a losing edge just means no position."""
    return max(2 * win_rate - 1, 0.0)


def suggested_size_satellite(*, confidence: float) -> dict:
    if confidence < MIN_SATELLITE_CONFIDENCE:
        fractional = 0.0
        base_f_star = 0.0
    else:
        base_f_star = kelly_fraction_even_money(AGGREGATE_SATELLITE_WIN_RATE)
        confidence_scalar = confidence / REFERENCE_CONFIDENCE
        damped = max(base_f_star * confidence_scalar * SATELLITE_AGGREGATE_EDGE_MULTIPLIER, 0.0)
        fractional = min(damped * KELLY_FRACTION, MAX_POSITION_PCT)

    return {
        "suggested_position_pct": round(fractional, 4),
        "method": "confidence_heuristic",
        "kelly_full_fraction": round(base_f_star, 4),
        "rationale": (
            f"Heuristic (not Kelly — no per-symbol backtest yet): base rate from the "
            f"aggregate momentum backtest's {AGGREGATE_SATELLITE_WIN_RATE * 100:.0f}% hit rate, "
            f"scaled by this thesis's {confidence * 100:.0f}% confidence relative to "
            f"{REFERENCE_CONFIDENCE * 100:.0f}% (roughly where satellite confidence centers), "
            f"damped for limited trust, capped at {MAX_POSITION_PCT * 100:.0f}%."
        ),
    }
