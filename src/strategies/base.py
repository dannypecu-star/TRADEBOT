"""Strategy interface.

A strategy's only job is to turn a price history into a *desired position* series.
It must never look into the future: the value at bar ``i`` may depend only on data
up to and including bar ``i``'s close. The backtest engine is responsible for the
one-bar execution delay, so strategies do not need to shift signals themselves.

Positions are expressed as a target in the range [0, 1] for long-only spot trading
(0 = flat, 1 = fully allocated). Short selling is intentionally out of scope for the
first strategy to keep the account requirements and risk profile simple.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class Strategy(ABC):
    #: human-readable name used in reports
    name: str = "base"

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """Return a Series (aligned to ``df.index``) of desired positions in [0, 1].

        The value at each timestamp is the position you would want to *hold going
        into the next bar*, decided using only information available at that bar's
        close.
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Strategy {self.name}>"
