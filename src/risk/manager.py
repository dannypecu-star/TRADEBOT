"""Risk management: position sizing, stops, and account-level kill switches.

This module is deliberately conservative. The fastest way to blow up an automated
account is oversized positions and no downside cap, so those are handled here rather
than left to the strategy.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskConfig:
    # Fraction of equity to risk (distance to stop) on a single trade.
    risk_per_trade: float = 0.01
    # Hard cap on how much of equity a single position may occupy.
    max_position_fraction: float = 1.0
    # ATR multiple used to place the protective stop below entry.
    atr_stop_mult: float = 3.0
    # If equity draws down more than this from its peak, stop opening new trades.
    max_drawdown_stop: float = 0.30


class RiskManager:
    """Translates a directional signal + volatility into a concrete position size.

    Sizing uses the classic fixed-fractional rule: risk a fixed fraction of equity
    per trade, where "risk" is the distance from entry to the ATR-based stop. This
    automatically shrinks positions when volatility is high.
    """

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()

    def stop_price(self, entry_price: float, atr_value: float) -> float:
        return entry_price - self.config.atr_stop_mult * atr_value

    def position_fraction(self, entry_price: float, atr_value: float) -> float:
        """Return the fraction of equity to allocate to this long position."""
        stop = self.stop_price(entry_price, atr_value)
        risk_per_unit = entry_price - stop
        if risk_per_unit <= 0:
            return 0.0
        # equity * risk_per_trade = size_value * (risk_per_unit / entry_price)
        frac = self.config.risk_per_trade * entry_price / risk_per_unit
        return float(min(frac, self.config.max_position_fraction))

    def drawdown_halt(self, equity: float, peak_equity: float) -> bool:
        if peak_equity <= 0:
            return False
        drawdown = 1.0 - equity / peak_equity
        return drawdown >= self.config.max_drawdown_stop
