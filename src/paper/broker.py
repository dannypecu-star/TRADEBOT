"""A simulated (paper) broker: fake money, but real prices and real costs.

The point of paper trading is to produce an *honest* track record before risking capital.
Honesty here means charging the same frictions a live account pays:

  * a taker **fee** on every fill, and
  * **slippage** -- the market moves against you between decision and fill, so buys fill a
    little above and sells a little below the quoted price.

Anything that skips these makes a strategy look better than it is. This broker refuses to.
It is long/flat spot only (no leverage, no shorting), matching the strategies it serves.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PaperFill:
    time: str
    side: str          # "buy" or "sell"
    price: float       # the price actually paid/received, after slippage
    units: float
    fee: float
    cash_after: float
    equity_after: float


@dataclass
class PaperBroker:
    cash: float = 10_000.0
    units: float = 0.0
    fee_rate: float = 0.001
    slippage: float = 0.0005
    fills: list[PaperFill] = field(default_factory=list)
    # Bookkeeping for realized-PnL / win-rate reporting.
    _entry_price: float = 0.0

    def equity(self, price: float) -> float:
        """Mark-to-market total value: idle cash plus the position valued at ``price``."""
        return self.cash + self.units * price

    def in_position(self) -> bool:
        return self.units > 0

    def buy(self, price: float, target_value: float, time: str) -> PaperFill | None:
        """Buy up to ``target_value`` of notional, capped by available cash (incl. fee).

        Returns the fill, or ``None`` if there was nothing to buy.
        """
        if self.in_position() or target_value <= 0:
            return None
        fill_price = price * (1 + self.slippage)
        spend = min(target_value, self.cash / (1 + self.fee_rate))
        if spend <= 0:
            return None
        units = spend / fill_price
        fee = spend * self.fee_rate
        self.cash -= spend + fee
        self.units += units
        self._entry_price = fill_price
        fill = PaperFill(
            time=time, side="buy", price=fill_price, units=units, fee=fee,
            cash_after=self.cash, equity_after=self.equity(price),
        )
        self.fills.append(fill)
        return fill

    def sell_all(self, price: float, time: str) -> PaperFill | None:
        """Liquidate the entire position at ``price`` (minus slippage and fee)."""
        if not self.in_position():
            return None
        fill_price = price * (1 - self.slippage)
        gross = self.units * fill_price
        fee = gross * self.fee_rate
        units = self.units
        self.cash += gross - fee
        self.units = 0.0
        fill = PaperFill(
            time=time, side="sell", price=fill_price, units=units, fee=fee,
            cash_after=self.cash, equity_after=self.equity(price),
        )
        self.fills.append(fill)
        return fill

    def realized_trades(self) -> list[float]:
        """Reconstruct per-round-trip PnL from the fill log (for win-rate/profit factor)."""
        pnls: list[float] = []
        entry: PaperFill | None = None
        for f in self.fills:
            if f.side == "buy":
                entry = f
            elif f.side == "sell" and entry is not None:
                cost = entry.units * entry.price + entry.fee
                proceeds = f.units * f.price - f.fee
                pnls.append(proceeds - cost)
                entry = None
        return pnls
