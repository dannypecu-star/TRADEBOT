#!/usr/bin/env python3
"""Prove your Kalshi credentials and the whole auth chain work end to end.

Run this in YOUR environment (this dev sandbox blocks Kalshi's servers). It performs a
harmless, read-only checkup against the DEMO sandbox:

  1. public endpoint       -> confirms basic connectivity (no auth)
  2. list a few markets    -> confirms you can read live market data
  3. signed balance call   -> confirms your API key + RSA signing are correct

Nothing here places an order. Set your credentials first:

    export KALSHI_KEY_ID=...            # your API key id
    export KALSHI_PRIVATE_KEY_PATH=...  # path to the downloaded RSA .pem

    python scripts/kalshi_smoke_test.py            # DEMO sandbox (default, recommended)
    python scripts/kalshi_smoke_test.py --env prod # only once demo looks right
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.client import KalshiClient


def _check(label: str, fn):
    try:
        result = fn()
        print(f"  [PASS] {label}")
        return result
    except Exception as e:  # noqa: BLE001 - we want to report any failure plainly
        print(f"  [FAIL] {label}\n         {type(e).__name__}: {str(e)[:180]}")
        return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env", choices=["demo", "prod"], default="demo")
    args = p.parse_args()

    print(f"\nKalshi smoke test  |  env = {args.env}\n" + "-" * 46)
    client = KalshiClient(env=args.env)

    print(" credentials:")
    print(f"   KALSHI_KEY_ID           {'set' if os.environ.get('KALSHI_KEY_ID') else 'MISSING'}")
    print(f"   KALSHI_PRIVATE_KEY_PATH {'set' if os.environ.get('KALSHI_PRIVATE_KEY_PATH') else 'MISSING'}")

    print("\n 1. public connectivity (no auth):")
    status = _check("GET /exchange/status", lambda: client.request(
        "GET", "/exchange/status", auth=False))
    if status:
        print(f"         trading_active={status.get('trading_active')}")

    print("\n 2. read market data (no auth):")
    markets = _check("GET /markets?limit=5&status=open",
                     lambda: client.get_markets(limit=5, status="open"))
    if markets:
        for m in markets.get("markets", [])[:5]:
            print(f"         {m.get('ticker'):<28} {str(m.get('title'))[:40]}")

    print("\n 3. signed request (auth -> verifies key + signing):")
    bal = _check("GET /portfolio/balance", client.get_balance)
    if bal is not None:
        cents = bal.get("balance")
        if cents is not None:
            print(f"         demo balance: ${cents / 100:,.2f}")

    print("\n" + "-" * 46)
    print(" If step 3 passed, your credentials and signing are correct and you're")
    print(" ready to paper trade on demo. If it failed with a 401, re-check the key")
    print(" id and that the .pem matches it.\n")


if __name__ == "__main__":
    main()
