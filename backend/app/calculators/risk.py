"""Risk metrics: Sharpe ratio, max drawdown, correlation to a benchmark."""

import numpy as np
import pandas as pd

from app.numeric import finite


def sharpe_ratio(close: pd.Series, risk_free_rate: float = 0.0, trading_days: int = 252) -> float | None:
    returns = close.pct_change().dropna()
    if len(returns) < 2 or returns.std() < 1e-12:
        return None
    excess_daily_rf = risk_free_rate / trading_days
    excess_returns = returns - excess_daily_rf
    return finite(float(excess_returns.mean() / returns.std() * np.sqrt(trading_days)))


def max_drawdown(close: pd.Series) -> float | None:
    if len(close) < 2:
        return None
    running_max = close.cummax()
    drawdown = close / running_max - 1
    return finite(float(drawdown.min()))


def correlation(close_a: pd.Series, close_b: pd.Series) -> float | None:
    a_returns = close_a.pct_change().dropna()
    b_returns = close_b.pct_change().dropna()
    aligned = pd.concat([a_returns, b_returns], axis=1, join="inner")
    if len(aligned) < 2:
        return None
    corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
    return finite(float(corr)) if corr is not None else None
