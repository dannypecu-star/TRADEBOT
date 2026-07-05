"""Kalshi trade API client with RSA-PSS request signing.

Kalshi authenticates every request with an RSA-PSS signature rather than a bearer
token. For each request you sign the string ``timestamp_ms + METHOD + path`` (path
includes ``/trade-api/v2`` but excludes the query string) using your private key, with
SHA-256, MGF1-SHA256, and a salt length equal to the digest length (32 bytes). The
signature and metadata go in three headers.

Environments
------------
* DEMO  -> https://demo-api.kalshi.co/trade-api/v2   (paper trading sandbox)
* PROD  -> https://api.elections.kalshi.com/trade-api/v2

Always run against DEMO until the bot is proven. ``KalshiClient`` defaults to DEMO and
requires an explicit ``env="prod"`` to touch real money.

The signing logic (``sign_request``) is deliberately a standalone function so it can be
unit-tested offline without any network access -- see tests/test_kalshi.py.

Credentials are read from the environment, never from a file:
    KALSHI_KEY_ID          -> your API key id
    KALSHI_PRIVATE_KEY_PATH -> path to the downloaded PEM private key
"""
from __future__ import annotations

import base64
import os
import time
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
API_PREFIX = "/trade-api/v2"


def load_private_key(path: str) -> rsa.RSAPrivateKey:
    with open(path, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Kalshi API key must be an RSA private key")
    return key


def sign_request(
    private_key: rsa.RSAPrivateKey,
    timestamp_ms: int,
    method: str,
    path: str,
) -> str:
    """Return the base64 RSA-PSS signature for one request.

    ``path`` must include the ``/trade-api/v2`` prefix and exclude the query string.
    """
    if "?" in path:
        path = path.split("?", 1)[0]
    message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


class KalshiClient:
    def __init__(
        self,
        env: str = "demo",
        key_id: Optional[str] = None,
        private_key: Optional[rsa.RSAPrivateKey] = None,
        session: Optional[requests.Session] = None,
    ):
        env = env.lower()
        if env not in ("demo", "prod"):
            raise ValueError("env must be 'demo' or 'prod'")
        self.env = env
        self.base = DEMO_BASE if env == "demo" else PROD_BASE
        self.key_id = key_id or os.environ.get("KALSHI_KEY_ID")
        self._private_key = private_key
        self.session = session or requests.Session()

    # -- authentication ---------------------------------------------------------
    @property
    def private_key(self) -> Optional[rsa.RSAPrivateKey]:
        if self._private_key is None:
            path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
            if path:
                self._private_key = load_private_key(path)
        return self._private_key

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.key_id or self.private_key is None:
            raise RuntimeError(
                "Missing credentials: set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH "
                "(or pass them to KalshiClient)."
            )
        ts = str(int(time.time() * 1000))
        sig = sign_request(self.private_key, int(ts), method, path)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    # -- low-level request ------------------------------------------------------
    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        """Perform a request. ``endpoint`` is relative to the API prefix, e.g. '/markets'."""
        full_path = f"{API_PREFIX}{endpoint}"
        url = f"{self.base}{endpoint}"
        headers = self._auth_headers(method, full_path) if auth else {}
        resp = self.session.request(
            method.upper(), url, params=params, json=json, headers=headers, timeout=30
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # -- public market data (no auth required) ----------------------------------
    def get_markets(self, **params) -> dict:
        return self.request("GET", "/markets", params=params, auth=False)

    def get_market(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}", auth=False)

    def get_events(self, **params) -> dict:
        return self.request("GET", "/events", params=params, auth=False)

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self.request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}, auth=False
        )

    def get_candlesticks(self, series: str, ticker: str, **params) -> dict:
        return self.request(
            "GET",
            f"/series/{series}/markets/{ticker}/candlesticks",
            params=params,
            auth=False,
        )

    # -- account & trading (auth required) --------------------------------------
    def get_balance(self) -> dict:
        return self.request("GET", "/portfolio/balance")

    def get_positions(self, **params) -> dict:
        return self.request("GET", "/portfolio/positions", params=params)

    def create_order(
        self,
        ticker: str,
        side: str,          # "yes" or "no"
        action: str,        # "buy" or "sell"
        count: int,
        type: str = "limit",
        price_cents: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Place an order. Prices are in integer cents (1..99)."""
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": type,
        }
        if price_cents is not None:
            # Kalshi uses yes_price / no_price in cents for limit orders.
            body[f"{side}_price"] = price_cents
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self.request("POST", "/portfolio/orders", json=body)
