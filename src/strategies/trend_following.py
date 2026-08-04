"""Trend-following strategy: Donchian channel breakout (long-only spot).

This is the classic "Turtle"-style breakout trend follower, and it complements the
EMA-based :class:`~src.strategies.trend_momentum.TrendMomentum` by expressing the same
edge (ride persistent trends) through a different, largely uncorrelated mechanism:
price *breakouts* rather than moving-average *state*.

The idea
--------
New sustained trends announce themselves by breaking to new highs. If today's close is
the highest close of the last ``entry_lookback`` bars, an up-move has broken out of its
recent range -- we join it. We ride until the trend shows exhaustion, defined as price
falling to the lowest low of the last ``exit_lookback`` bars.

Using a *shorter* exit lookback than the entry lookback (e.g. enter on a 55-bar high,
exit on a 20-bar low) is deliberate: it lets winners run on the wide entry channel while
cutting losers quickly on the tight exit channel -- the asymmetry that gives trend
following its positive skew (many small losses, a few large wins).

Rules
-----
  * ENTER long when ``close`` >= highest ``close`` over the prior ``entry_lookback`` bars.
  * EXIT when ``close`` <= lowest ``low`` over the prior ``exit_lookback`` bars.

The channels are computed on data *excluding the current bar* (``shift(1)``) so the
comparison is "did we break the range that existed before this bar", never a trivially
true "is today's high the highest including today".
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy
from ._signal_state import hold_position
from .trend_momentum import atr  # noqa: F401  (re-exported for the risk manager's stop)


class TrendFollowing(Strategy):
    name = "trend_following"

    def __init__(
        self,
        entry_lookback: int = 55,
        exit_lookback: int = 20,
        atr_period: int = 14,
    ):
        if entry_lookback < 2 or exit_lookback < 2:
            raise ValueError("lookbacks must be >= 2")
        self.entry_lookback = entry_lookback
        self.exit_lookback = exit_lookback
        self.atr_period = atr_period

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        low = df["low"]

        # Prior-range channels: highest close / lowest low over the window that ends at
        # the *previous* bar. shift(1) is what excludes the current bar.
        upper = close.shift(1).rolling(self.entry_lookback).max()
        lower = low.shift(1).rolling(self.exit_lookback).min()

        enter = (close >= upper).fillna(False)
        exit_ = (close <= lower).fillna(False)

        warmup = max(self.entry_lookback, self.exit_lookback) + 1
        return hold_position(enter, exit_, warmup=warmup)
