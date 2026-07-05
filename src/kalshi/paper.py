"""Safety gating for Kalshi live/paper trading.

Kalshi provides a real demo environment, so "paper trading" here means pointing the
client at DEMO -- actual order plumbing against fake money. Going to PROD is gated the
same way the crypto side is: multiple explicit switches so real orders are never sent
by accident.
"""
from __future__ import annotations

from dataclasses import dataclass

from .client import KalshiClient


@dataclass
class LiveGate:
    enabled: bool = False        # must be true to trade at all
    env: str = "demo"            # "demo" (paper) or "prod" (real money)
    confirm_prod: bool = False   # must be literally true to touch PROD

    def preflight(self) -> None:
        if not self.enabled:
            raise RuntimeError("Kalshi trading is disabled (enabled=false).")
        if self.env == "prod" and not self.confirm_prod:
            raise RuntimeError(
                "env is 'prod' but confirm_prod is not true -- refusing to send real "
                "orders. Prove the strategy on demo first."
            )

    def client(self) -> KalshiClient:
        self.preflight()
        return KalshiClient(env=self.env)
