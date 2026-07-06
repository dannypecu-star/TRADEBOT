"""Value strategy: buy Kalshi contracts the market underprices relative to a fair
probability estimate.

The strategy itself is simple and correct by construction. **All of the difficulty --
and all of the actual edge -- lives in the probability estimate you feed it.** For
sports, that estimate comes from sharp sportsbook odds (devigged) or your own model;
for weather, from forecast APIs; for economics, from a nowcast model. This module does
not invent that number: it takes a ``ProbabilitySource`` and turns a good estimate into
correctly sized trades. Feed it a bad estimate and it will lose money efficiently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .economics import SizingConfig, contracts_to_buy, edge


class ProbabilitySource(Protocol):
    """Anything that can estimate the true Yes-probability of a market.

    Implement this against sportsbook odds, a model, or a forecast feed. It is the
    single most important component and the one you must validate hardest.
    """

    def fair_probability(self, market_ticker: str) -> float | None:
        ...


@dataclass
class Signal:
    ticker: str
    side: str          # "yes" or "no"
    price: float       # entry price in dollars (0..1)
    fair_prob: float
    edge: float        # net $ edge per contract
    contracts: int


def evaluate_market(
    ticker: str,
    yes_price: float,
    fair_prob: float,
    bankroll: float,
    sizing: SizingConfig,
    fee_rate: float = 0.07,
    no_price: float | None = None,
) -> Signal | None:
    """Decide whether (and how much) to trade a single market.

    Considers both sides: buying Yes at ``yes_price`` when the model thinks Yes is
    underpriced, or buying No when the model thinks Yes is overpriced. ``no_price``
    defaults to ``1 - yes_price``; pass the real No ask to account honestly for the
    bid/ask spread. Returns the better-edged actionable side, or None.
    """
    if no_price is None:
        no_price = 1.0 - yes_price

    yes_edge = edge(fair_prob, yes_price, fee_rate)
    no_edge = edge(1.0 - fair_prob, no_price, fee_rate)

    if yes_edge >= no_edge and yes_edge > 0:
        n = contracts_to_buy(bankroll, fair_prob, yes_price, sizing, fee_rate)
        if n > 0:
            return Signal(ticker, "yes", yes_price, fair_prob, yes_edge, n)
    elif no_edge > 0:
        n = contracts_to_buy(bankroll, 1.0 - fair_prob, no_price, sizing, fee_rate)
        if n > 0:
            return Signal(ticker, "no", no_price, fair_prob, no_edge, n)
    return None
