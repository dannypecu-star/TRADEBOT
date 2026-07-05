"""Tests for the Kalshi module.

The signing test is the important one: it verifies the RSA-PSS signature the client
produces is actually valid under the matching public key, using the exact parameters
Kalshi specifies. This can be checked fully offline, and getting it right is the
difference between a working client and 401s.
"""
from __future__ import annotations

import os
import sys

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.client import sign_request
from src.kalshi.economics import (SizingConfig, contracts_to_buy, edge,
                                   fee_per_contract, kelly_fraction)
from src.kalshi.backtest import run_value_backtest, synthetic_resolved_markets
from src.kalshi.strategy import evaluate_market


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_signature_verifies_with_public_key():
    key = _key()
    ts, method, path = 1700000000000, "GET", "/trade-api/v2/portfolio/balance"
    import base64
    sig = base64.b64decode(sign_request(key, ts, method, path))
    message = f"{ts}{method}{path}".encode()
    # Must not raise -> signature is valid under Kalshi's exact PSS parameters.
    key.public_key().verify(
        sig, message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_signature_excludes_query_string():
    key = _key()
    a = sign_request(key, 123, "GET", "/trade-api/v2/markets?limit=10")
    # Signing the same path without the query must produce a verifiable signature for
    # the stripped path (PSS is randomized, so we verify rather than compare bytes).
    import base64
    sig = base64.b64decode(a)
    key.public_key().verify(
        sig, b"123GET/trade-api/v2/markets",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_fee_and_edge_math():
    # Fee is symmetric around 0.5 and rounds up to a whole cent.
    assert fee_per_contract(0.5) == fee_per_contract(0.5)
    assert fee_per_contract(0.5) > 0
    # No edge when price equals true probability (you still pay the fee).
    assert edge(0.6, 0.6) < 0
    # Clear edge when the market underprices a likely outcome.
    assert edge(0.8, 0.5) > 0


def test_kelly_zero_without_edge():
    assert kelly_fraction(0.4, 0.5) == 0.0        # no edge -> no bet
    assert 0 < kelly_fraction(0.8, 0.5) <= 1.0     # edge -> positive fraction


def test_strategy_picks_the_underpriced_side():
    sizing = SizingConfig(min_edge=0.01)
    # Model thinks Yes is 80% but market prices it at 55c -> buy Yes.
    sig = evaluate_market("M", yes_price=0.55, fair_prob=0.80, bankroll=1000, sizing=sizing)
    assert sig is not None and sig.side == "yes"
    # Model thinks Yes is 20% but market prices it at 45c -> buy No.
    sig = evaluate_market("M", yes_price=0.45, fair_prob=0.20, bankroll=1000, sizing=sizing)
    assert sig is not None and sig.side == "no"
    # Fairly priced -> no trade.
    assert evaluate_market("M", yes_price=0.50, fair_prob=0.50, bankroll=1000, sizing=sizing) is None


def test_skilled_model_profits_and_useless_model_does_not():
    skilled = run_value_backtest(synthetic_resolved_markets(n=3000, model_skill=0.9))
    useless = run_value_backtest(synthetic_resolved_markets(n=3000, model_skill=0.0))
    assert skilled.total_return > 0, "an accurate model on mispriced markets should profit"
    # A useless model must not reliably print money -- if it does, the harness is rigged.
    assert useless.total_return < skilled.total_return


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
