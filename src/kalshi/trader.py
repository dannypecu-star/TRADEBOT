"""Demo/paper trading loop for the Kalshi value strategy.

This reads live markets, values each against a probability source, and places orders on
the DEMO sandbox (real API plumbing, fake money). It is built to be *boring and safe*:

  * **dry_run defaults to True** -- it logs the orders it *would* place without sending
    them, even on demo. Flip it off deliberately.
  * **position cap** -- never hold more than ``max_open_positions`` markets at once.
  * **daily loss stop** -- if the balance falls below the day's start by more than
    ``max_daily_loss_fraction``, it stops opening new positions.
  * **no double-dipping** -- skips markets you already hold a position in.

The client is injected, so the whole loop is unit-tested offline against a fake client
that records orders (see tests/test_trader.py). In your environment you pass a real
``KalshiClient`` from a ``LiveGate``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .economics import SizingConfig
from .strategy import ProbabilitySource, evaluate_market

log = logging.getLogger("kalshi.trader")


@dataclass
class RiskLimits:
    max_open_positions: int = 10
    max_daily_loss_fraction: float = 0.10
    dry_run: bool = True


@dataclass
class OrderIntent:
    ticker: str
    side: str
    price_cents: int
    count: int
    edge: float
    placed: bool  # False when dry_run or blocked by a limit


@dataclass
class TraderState:
    start_balance: float
    orders: list[OrderIntent] = field(default_factory=list)


def _cents(x) -> Optional[int]:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


class PaperTrader:
    def __init__(
        self,
        client,
        probability_source: ProbabilitySource,
        sizing: Optional[SizingConfig] = None,
        limits: Optional[RiskLimits] = None,
        fee_rate: float = 0.07,
    ):
        self.client = client
        self.source = probability_source
        self.sizing = sizing or SizingConfig()
        self.limits = limits or RiskLimits()
        self.fee_rate = fee_rate

    # -- helpers ---------------------------------------------------------------
    def _balance_dollars(self) -> float:
        bal = self.client.get_balance()
        return float(bal.get("balance", 0)) / 100.0

    def _held_tickers(self) -> set[str]:
        try:
            positions = self.client.get_positions().get("market_positions", [])
        except Exception:  # noqa: BLE001 - absence of positions must not crash the loop
            return set()
        return {p.get("ticker") for p in positions if p.get("position")}

    def _market_prices(self, market: dict) -> tuple[Optional[float], Optional[float], Optional[int], Optional[int]]:
        """Return (yes_price, no_price, yes_ask_cents, no_ask_cents) in dollars/cents.

        You buy Yes at the yes ask and No at the no ask. If no_ask is absent it is
        derived from the yes bid (no_ask = 100 - yes_bid).
        """
        yes_ask = _cents(market.get("yes_ask"))
        no_ask = _cents(market.get("no_ask"))
        if no_ask is None:
            yes_bid = _cents(market.get("yes_bid"))
            no_ask = (100 - yes_bid) if yes_bid is not None else None
        yes_price = yes_ask / 100.0 if yes_ask else None
        no_price = no_ask / 100.0 if no_ask else None
        return yes_price, no_price, yes_ask, no_ask

    # -- one pass over the watched markets -------------------------------------
    def run_once(self, markets: Optional[list[dict]] = None) -> TraderState:
        start_balance = self._balance_dollars()
        state = TraderState(start_balance=start_balance)
        loss_floor = start_balance * (1.0 - self.limits.max_daily_loss_fraction)

        if markets is None:
            markets = self.client.get_markets(limit=100, status="open").get("markets", [])

        held = self._held_tickers()
        open_positions = len(held)

        for m in markets:
            ticker = m.get("ticker")
            if not ticker or ticker in held:
                continue

            fair = self.source.fair_probability(ticker)
            if fair is None:
                continue

            yes_price, no_price, yes_ask, no_ask = self._market_prices(m)
            if yes_price is None or no_price is None:
                continue

            balance = self._balance_dollars()
            if balance <= loss_floor:
                log.warning("daily loss stop hit (balance %.2f <= floor %.2f); halting",
                            balance, loss_floor)
                break

            sig = evaluate_market(ticker, yes_price, fair, balance, self.sizing,
                                  self.fee_rate, no_price=no_price)
            if sig is None:
                continue

            if open_positions >= self.limits.max_open_positions:
                log.info("position cap reached (%d); skipping %s",
                         self.limits.max_open_positions, ticker)
                continue

            price_cents = yes_ask if sig.side == "yes" else no_ask
            placed = False
            if self.limits.dry_run:
                log.info("[DRY-RUN] would BUY %s %s x%d @ %d cents (edge $%.3f)",
                         sig.side, ticker, sig.contracts, price_cents, sig.edge)
            else:
                self.client.create_order(
                    ticker=ticker, side=sig.side, action="buy",
                    count=sig.contracts, type="limit", price_cents=price_cents,
                )
                placed = True
                open_positions += 1
                log.info("PLACED BUY %s %s x%d @ %d cents (edge $%.3f)",
                         sig.side, ticker, sig.contracts, price_cents, sig.edge)

            state.orders.append(OrderIntent(
                ticker=ticker, side=sig.side, price_cents=int(price_cents),
                count=sig.contracts, edge=sig.edge, placed=placed,
            ))

        return state
