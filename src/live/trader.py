"""Live / paper trading skeleton -- SAFETY GATED, not yet wired to send orders.

This is deliberately a skeleton. Going live is the last step, and only after a green
backtest, walk-forward validation, and a paper run. The gating below makes it
impossible to accidentally send a real order:

  * ``live.enabled`` must be true to run at all.
  * ``live.mode`` must be "live" (not "paper") to touch real balances.
  * ``live.confirm_live`` must be literally true as a second explicit acknowledgement.
  * API credentials come only from environment variables, never from a file.

When we reach this stage we will fill in ``_place_order`` for the chosen exchange and
add reconnection, order-state reconciliation, and alerting. Until then this raises
rather than pretends to trade.
"""
from __future__ import annotations

import os

from ..strategies.trend_momentum import TrendMomentum


class LiveTrader:
    def __init__(self, config: dict):
        self.config = config
        live = config.get("live", {})
        self.enabled = bool(live.get("enabled", False))
        self.mode = live.get("mode", "paper")
        self.confirm_live = bool(live.get("confirm_live", False))
        self.strategy = TrendMomentum(**config["strategy"])

    def _credentials(self) -> tuple[str, str]:
        key = os.environ.get("TRADEBOT_API_KEY", "")
        secret = os.environ.get("TRADEBOT_API_SECRET", "")
        if not key or not secret:
            raise RuntimeError(
                "Missing TRADEBOT_API_KEY / TRADEBOT_API_SECRET environment variables."
            )
        return key, secret

    def preflight(self) -> None:
        """Fail loudly unless every safety gate is satisfied."""
        if not self.enabled:
            raise RuntimeError("live.enabled is false -- refusing to trade.")
        if self.mode == "live" and not self.confirm_live:
            raise RuntimeError(
                "live.mode is 'live' but live.confirm_live is not true -- refusing to "
                "send real orders."
            )

    def run(self) -> None:
        self.preflight()
        raise NotImplementedError(
            "Live execution is intentionally not implemented yet. Complete backtesting, "
            "validation, and paper trading first; then we wire the exchange client here."
        )

    def _place_order(self, *args, **kwargs):  # pragma: no cover - placeholder
        raise NotImplementedError
