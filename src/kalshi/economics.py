"""The math of trading binary event contracts: fees, edge, and position sizing.

A Kalshi contract costs some price ``p`` dollars (0..1) and pays $1 if the event
resolves Yes, $0 otherwise. If you believe the true probability is ``q``:

  * expected value per contract = q * 1 - p = q - p   (before fees)
  * you have an *edge* only when q - p exceeds the round-trip fee per contract.

Position sizing uses fractional Kelly, which is the mathematically correct way to size
bets to maximize long-run growth without risking ruin. We scale it down (quarter Kelly
by default) because Kelly assumes you know ``q`` exactly -- and you never do.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def fee_per_contract(price: float, fee_rate: float = 0.07) -> float:
    """Kalshi's general trading fee, in dollars per contract.

    Kalshi's published general formula is fee = ceil(fee_rate * P * (1 - P)) rounded up
    to the next cent, where P is the price in dollars. The 0.07 coefficient is the
    standard schedule; some markets differ, so treat this as an estimate and verify the
    current schedule for the specific market before trading real money.
    """
    raw = fee_rate * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0  # round up to the next whole cent


def edge(true_prob: float, price: float, fee_rate: float = 0.07) -> float:
    """Net expected profit per contract after the (entry) fee, in dollars."""
    return (true_prob - price) - fee_per_contract(price, fee_rate)


def kelly_fraction(true_prob: float, price: float) -> float:
    """Full-Kelly fraction of bankroll for a Yes contract at ``price``.

    Buying a contract costs ``price`` to win ``1 - price`` (prob ``true_prob``) or lose
    ``price`` (prob ``1 - true_prob``). The Kelly-optimal fraction simplifies to
    (true_prob - price) / (1 - price). Returns 0 when there is no positive edge.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    f = (true_prob - price) / (1.0 - price)
    return max(0.0, f)


@dataclass
class SizingConfig:
    kelly_fraction: float = 0.25       # quarter-Kelly for safety against model error
    max_bankroll_fraction: float = 0.05  # never stake more than 5% of bankroll on one market
    min_edge: float = 0.02             # require >= 2 cents of net edge to bother trading


def contracts_to_buy(
    bankroll: float,
    true_prob: float,
    price: float,
    config: SizingConfig,
    fee_rate: float = 0.07,
) -> int:
    """How many contracts to buy given an edge, or 0 if the trade isn't worth it."""
    if edge(true_prob, price, fee_rate) < config.min_edge:
        return 0
    frac = min(config.kelly_fraction * kelly_fraction(true_prob, price),
               config.max_bankroll_fraction)
    stake = frac * bankroll
    if price <= 0:
        return 0
    return int(stake // price)  # each contract costs `price` dollars
