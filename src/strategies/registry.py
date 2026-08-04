"""Strategy registry: look up a strategy by name and build it from a config dict.

This lets every entry point (backtest, paper trader, platform adapters, the comparison
report) select a strategy with a single string -- ``--strategy mean_reversion`` -- and
keeps the list of available strategies in exactly one place.

Adding a new strategy is two lines: import its class and add it to ``STRATEGIES``.
"""
from __future__ import annotations

import inspect
from typing import Type

from .base import Strategy
from .mean_reversion import MeanReversion
from .trend_following import TrendFollowing
from .trend_momentum import TrendMomentum

# name -> class. The three the project advertises, plus trend_momentum as a second,
# distinct trend implementation for comparison.
STRATEGIES: dict[str, Type[Strategy]] = {
    TrendFollowing.name: TrendFollowing,       # "trend_following"  (Donchian breakout)
    TrendMomentum.name: TrendMomentum,         # "trend_momentum"   (EMA + momentum)
    MeanReversion.name: MeanReversion,         # "mean_reversion"   (z-score reversion)
}


def available() -> list[str]:
    return sorted(STRATEGIES)


def build_strategy(name: str, params: dict | None = None) -> Strategy:
    """Instantiate a strategy by name, passing only the params it actually accepts.

    Passing a shared config blob is convenient but risky: one strategy's parameter
    (``entry_z``) is meaningless to another (``fast``). We therefore filter the supplied
    params down to the target constructor's signature, so a single config section can
    hold settings for several strategies without cross-contamination.
    """
    if name not in STRATEGIES:
        raise KeyError(
            f"unknown strategy {name!r}; available: {', '.join(available())}"
        )
    cls = STRATEGIES[name]
    params = params or {}
    sig = inspect.signature(cls.__init__)
    accepted = {
        k: v for k, v in params.items()
        if k in sig.parameters and k != "self"
    }
    return cls(**accepted)
