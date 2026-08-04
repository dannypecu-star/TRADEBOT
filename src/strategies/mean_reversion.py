"""Mean-reversion strategy (long-only spot).

The idea
--------
Prices tend to oscillate around a moving fair value. When price is stretched far
*below* that fair value, it is statistically likely (in range-bound / choppy regimes)
to snap back toward it. We buy that stretch and sell as it reverts.

We measure "stretched" with a rolling z-score:

    z = (close - rolling_mean) / rolling_std

This is the same quantity that defines Bollinger Bands (a band at k standard
deviations is just ``|z| = k``), so this strategy is a Bollinger-band reversion in
disguise, expressed in standardized units.

Rules
-----
  * ENTER long when ``z <= -entry_z`` (price is ``entry_z`` std below its mean).
  * EXIT  when ``z >= -exit_z`` (price has reverted back toward the mean).
    ``exit_z`` is typically near 0, so we exit around the mean rather than waiting for
    an overshoot to the upside.
  * A long-term regime filter (price above ``trend_filter`` SMA) is optional and ON by
    default. Mean reversion's classic failure mode is "catching a falling knife" -- a
    market that is not reverting but genuinely collapsing. Requiring price to be above
    a slow SMA keeps us from buying dips inside a structural downtrend.

Honest note
-----------
Mean reversion shines in sideways, range-bound markets and *underperforms* in strong
one-way trends (it keeps selling winners early and buying into declines). That is the
mirror image of the trend strategies here -- which is exactly why running them side by
side is informative. Do not expect this to beat buy-and-hold in a raging bull market;
expect it to earn its keep when the market chops.
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy
from ._signal_state import hold_position
from .trend_momentum import atr  # reuse the shared ATR implementation for the stop


class MeanReversion(Strategy):
    name = "mean_reversion"

    def __init__(
        self,
        lookback: int = 20,
        entry_z: float = 2.0,
        exit_z: float = 0.5,
        trend_filter: int = 200,
        use_trend_filter: bool = True,
        atr_period: int = 14,
    ):
        if lookback < 2:
            raise ValueError("lookback must be >= 2")
        if entry_z <= exit_z:
            raise ValueError("entry_z must be greater than exit_z")
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.trend_filter = trend_filter
        self.use_trend_filter = use_trend_filter
        self.atr_period = atr_period

    def zscore(self, close: pd.Series) -> pd.Series:
        mean = close.rolling(self.lookback).mean()
        std = close.rolling(self.lookback).std(ddof=0)
        # Avoid div-by-zero on flat windows; a zero-std window has no signal anyway.
        return (close - mean) / std.replace(0.0, pd.NA)

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        z = self.zscore(close)

        enter = z <= -self.entry_z
        exit_ = z >= -self.exit_z

        if self.use_trend_filter:
            regime_ok = close > close.rolling(self.trend_filter).mean()
            # Only *initiate* new longs when the long-term regime is up. We still allow
            # the normal reversion exit to close positions regardless of regime.
            enter = enter & regime_ok.fillna(False)

        # NaNs during warmup must not count as events.
        enter = enter.fillna(False)
        exit_ = exit_.fillna(False)

        warmup = max(self.lookback, self.trend_filter if self.use_trend_filter else 0)
        return hold_position(enter, exit_, warmup=warmup)
