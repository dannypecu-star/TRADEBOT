"""3Commas adapter -- drive a 3Commas bot via its TradingView-style webhook.

How 3Commas works
------------------
On 3Commas you create a **bot** connected to your exchange account. The bot can be started
or closed by posting a small JSON message to a fixed webhook URL. This is the same channel
3Commas documents for "TradingView custom signals", so any external brain (like this
project's strategies) can drive it without holding your exchange keys -- 3Commas holds
those, we only send start/close messages.

The message must include your ``message_token`` (proves the signal is from you) and the
bot's ``bot_id``, plus an action. We map our neutral :class:`Signal`:

  * ``enter_long`` -> ``{"action": "start_deal"}``   (open a new deal)
  * ``exit``       -> ``{"action": "close_at_market_price"}``
  * ``hold``       -> no message sent

Secrets (``message_token``) come from the environment, never from code or config files.
Set ``THREECOMMAS_WEBHOOK_URL``, ``THREECOMMAS_MESSAGE_TOKEN``, ``THREECOMMAS_BOT_ID``.
"""
from __future__ import annotations

import os

from .base import PlatformAdapter, Signal

WEBHOOK_DEFAULT = "https://app.3commas.io/trade_signal/trading_view"


class ThreeCommasAdapter(PlatformAdapter):
    name = "3commas"

    def __init__(
        self,
        bot_id: str | None = None,
        message_token: str | None = None,
        webhook_url: str | None = None,
        email_token: str | None = None,
        dry_run: bool = True,
    ):
        super().__init__(dry_run=dry_run)
        self.bot_id = bot_id or os.environ.get("THREECOMMAS_BOT_ID", "")
        self.message_token = message_token or os.environ.get("THREECOMMAS_MESSAGE_TOKEN", "")
        self.webhook_url = webhook_url or os.environ.get(
            "THREECOMMAS_WEBHOOK_URL", WEBHOOK_DEFAULT
        )
        # Some bot types require a per-deal email_token as an idempotency key.
        self.email_token = email_token or os.environ.get("THREECOMMAS_EMAIL_TOKEN", "")

    def build_payload(self, signal: Signal) -> dict | None:
        """Translate a Signal into a 3Commas webhook body (or None for 'hold')."""
        if signal.action == "hold":
            return None
        action = "start_deal" if signal.action == "enter_long" else "close_at_market_price"
        payload = {
            "message_type": "bot",
            "bot_id": self.bot_id,
            "message_token": self.message_token,
            "pair": _to_3c_pair(signal.symbol),
            "action": action,
        }
        if self.email_token:
            payload["email_token"] = self.email_token
        return payload

    def send(self, signal: Signal) -> dict:
        payload = self.build_payload(signal)
        if payload is None:
            return {"platform": self.name, "action": "hold", "sent": False}

        if self.dry_run:
            # Never transmit in dry-run; return the exact body that *would* be posted,
            # with the token redacted so it is safe to log.
            safe = dict(payload, message_token="***redacted***")
            return {"platform": self.name, "sent": False, "dry_run": True, "payload": safe}

        if not self.bot_id or not self.message_token:
            raise RuntimeError(
                "3Commas bot_id / message_token missing -- set THREECOMMAS_BOT_ID and "
                "THREECOMMAS_MESSAGE_TOKEN in the environment."
            )
        import requests  # lazy import; dry-run/tests need no network stack

        resp = requests.post(self.webhook_url, json=payload, timeout=15)
        return {
            "platform": self.name,
            "sent": True,
            "status_code": resp.status_code,
            "action": payload["action"],
        }


def _to_3c_pair(symbol: str) -> str:
    """Convert 'BTC/USDT' to 3Commas' 'USDT_BTC' quote_base convention."""
    if "/" in symbol:
        base, quote = symbol.split("/")
        return f"{quote}_{base}"
    return symbol
