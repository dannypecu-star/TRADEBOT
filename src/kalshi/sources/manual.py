"""A hand-entered probability source.

The point: validate the entire paper-trading loop with **zero external APIs and zero
cost**. You write your own fair probabilities for a handful of tickers (from your own
read, or numbers you want to test), and the trader treats them exactly like it would a
sportsbook feed. Once the plumbing is proven, swap this for SportsbookProbabilitySource.

    {"KXNBA-25JUL10-LAL": 0.62, "KXMLB-25JUL10-NYY": 0.55}
"""
from __future__ import annotations

import json


class ManualProbabilitySource:
    def __init__(self, probabilities: dict[str, float]):
        self.probabilities = {k: float(v) for k, v in probabilities.items()}

    @classmethod
    def from_json(cls, path: str) -> "ManualProbabilitySource":
        with open(path, "r") as fh:
            return cls(json.load(fh))

    def fair_probability(self, market_ticker: str) -> float | None:
        return self.probabilities.get(market_ticker)
