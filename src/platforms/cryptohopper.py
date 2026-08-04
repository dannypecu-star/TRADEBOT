"""Cryptohopper adapter -- drive a Cryptohopper "hopper" via its webhook signal.

How Cryptohopper works
----------------------
Cryptohopper runs your bot ("hopper") in its cloud and connects to your exchange. External
strategies reach it two ways:

  * the **REST API** (``https://api.cryptohopper.com/v1``) with an API key, or
  * a **webhook / external signal** URL that accepts buy/sell instructions for a coin.

This adapter targets the webhook route because, like 3Commas, it keeps exchange keys on
the platform side -- we only send buy/sell intents. We map our neutral :class:`Signal`:

  * ``enter_long`` -> ``{"action": "buy"}``
  * ``exit``       -> ``{"action": "sell"}``
  * ``hold``       -> nothing sent

Set ``CRYPTOHOPPER_WEBHOOK_URL`` and ``CRYPTOHOPPER_API_TOKEN`` (or ``hopper_id`` +
``api_key`` for the REST route) in the environment. Nothing transmits unless ``dry_run``
is explicitly disabled.
"""
from __future__ import annotations

import os

from .base import PlatformAdapter, Signal


class CryptohopperAdapter(PlatformAdapter):
    name = "cryptohopper"

    def __init__(
        self,
        webhook_url: str | None = None,
        api_token: str | None = None,
        hopper_id: str | None = None,
        dry_run: bool = True,
    ):
        super().__init__(dry_run=dry_run)
        self.webhook_url = webhook_url or os.environ.get("CRYPTOHOPPER_WEBHOOK_URL", "")
        self.api_token = api_token or os.environ.get("CRYPTOHOPPER_API_TOKEN", "")
        self.hopper_id = hopper_id or os.environ.get("CRYPTOHOPPER_HOPPER_ID", "")

    def build_payload(self, signal: Signal) -> dict | None:
        if signal.action == "hold":
            return None
        action = "buy" if signal.action == "enter_long" else "sell"
        return {
            "hopper_id": self.hopper_id,
            "coin": _to_coin(signal.symbol),
            "action": action,
            # Percentage of allocated funds to use; 100 = full allocation for this coin.
            "amount_percentage": round(min(max(signal.strength, 0.0), 1.0) * 100, 2),
        }

    def send(self, signal: Signal) -> dict:
        payload = self.build_payload(signal)
        if payload is None:
            return {"platform": self.name, "action": "hold", "sent": False}

        if self.dry_run:
            return {"platform": self.name, "sent": False, "dry_run": True, "payload": payload}

        if not self.webhook_url or not self.api_token:
            raise RuntimeError(
                "Cryptohopper webhook_url / api_token missing -- set "
                "CRYPTOHOPPER_WEBHOOK_URL and CRYPTOHOPPER_API_TOKEN."
            )
        import requests  # lazy import

        headers = {"access-token": self.api_token}
        resp = requests.post(self.webhook_url, json=payload, headers=headers, timeout=15)
        return {
            "platform": self.name,
            "sent": True,
            "status_code": resp.status_code,
            "action": payload["action"],
        }


def _to_coin(symbol: str) -> str:
    """Cryptohopper signals reference the base coin, e.g. 'BTC/USDT' -> 'BTC'."""
    return symbol.split("/")[0] if "/" in symbol else symbol
