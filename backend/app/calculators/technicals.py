"""Pure technical indicator functions, operating on a pandas Series of closing prices."""

import numpy as np
import pandas as pd

from app.numeric import finite as _finite


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.fillna(100)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "histogram": histogram})


def annualized_realized_vol(close: pd.Series, trading_days: int = 252) -> float:
    returns = close.pct_change().dropna()
    if len(returns) < 2:
        return 0.0
    return float(returns.std() * np.sqrt(trading_days))


def latest_snapshot(close: pd.Series) -> dict:
    """Convenience bundle of the latest values for the synthesis prompt."""
    rsi_series = rsi(close)
    macd_df = macd(close)
    return {
        "rsi_14": _finite(float(rsi_series.iloc[-1])) if len(rsi_series) else None,
        "macd_histogram": _finite(float(macd_df["histogram"].iloc[-1])) if len(macd_df) else None,
        "annualized_realized_vol": _finite(annualized_realized_vol(close)),
        "pct_change_1w": _finite(_pct_change_over(close, 5)),
        "pct_change_1m": _finite(_pct_change_over(close, 21)),
        "pct_change_3m": _finite(_pct_change_over(close, 63)),
    }


def _pct_change_over(close: pd.Series, lookback: int) -> float | None:
    if len(close) <= lookback:
        return None
    return float(close.iloc[-1] / close.iloc[-lookback - 1] - 1)
