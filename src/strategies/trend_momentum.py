"""Trend / momentum strategy (long-only spot).

Why this as the first strategy:
  * Trend following is one of the few edges with decades of out-of-sample evidence
    across asset classes, and crypto has historically trended strongly.
  * The rules are simple and few, which means fewer parameters to overfit.
  * It is long/flat only, so it needs no margin, no shorting, and no leverage --
    matching a first live account.

Rules:
  * Go long when the fast EMA is above the slow EMA (uptrend) AND price is above the
    slow EMA (regime filter to avoid chop), AND momentum over ``mom_lookback`` bars
    is positive.
  * Otherwise stay flat.

The ATR column is attached for the risk manager to size stops; the strategy itself
only decides direction.
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


class TrendMomentum(Strategy):
    name = "trend_momentum"

    def __init__(
        self,
        fast: int = 20,
        slow: int = 50,
        mom_lookback: int = 24,
        atr_period: int = 14,
    ):
        if fast >= slow:
            raise ValueError("fast EMA span must be shorter than slow EMA span")
        self.fast = fast
        self.slow = slow
        self.mom_lookback = mom_lookback
        self.atr_period = atr_period

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        fast_ema = ema(close, self.fast)
        slow_ema = ema(close, self.slow)
        momentum = close.pct_change(self.mom_lookback)

        long_ok = (fast_ema > slow_ema) & (close > slow_ema) & (momentum > 0)
        signal = long_ok.astype(float)

        # Indicators need warmup; force flat until the slowest window is populated.
        warmup = max(self.slow, self.mom_lookback)
        signal.iloc[:warmup] = 0.0
        return signal
