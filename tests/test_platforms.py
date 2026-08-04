"""Tests for platform adapters and the position->action mapping.

Everything runs in dry-run, so no network and no accounts are needed. We assert the
adapters build the *correct payload* and never transmit unless explicitly told to.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.platforms.base import Signal, signal_from_position
from src.platforms.cryptohopper import CryptohopperAdapter
from src.platforms.mt5_adapter import MT5Adapter
from src.platforms.threecommas import ThreeCommasAdapter


def test_position_to_action_mapping():
    assert signal_from_position("BTC/USDT", 0.0, 1.0, 100, "s").action == "enter_long"
    assert signal_from_position("BTC/USDT", 1.0, 0.0, 100, "s").action == "exit"
    assert signal_from_position("BTC/USDT", 1.0, 1.0, 100, "s").action == "hold"
    assert signal_from_position("BTC/USDT", 0.0, 0.0, 100, "s").action == "hold"


def test_3commas_payload_and_redaction():
    adapter = ThreeCommasAdapter(bot_id="123", message_token="SECRET", dry_run=True)
    enter = Signal("BTC/USDT", "enter_long", strategy="trend_following")
    out = adapter.send(enter)
    assert out["sent"] is False and out["dry_run"] is True
    # Secret must never appear in the returned/logged payload.
    assert out["payload"]["message_token"] == "***redacted***"
    assert out["payload"]["action"] == "start_deal"
    assert out["payload"]["pair"] == "USDT_BTC"

    exit_out = adapter.send(Signal("BTC/USDT", "exit"))
    assert exit_out["payload"]["action"] == "close_at_market_price"

    hold_out = adapter.send(Signal("BTC/USDT", "hold"))
    assert hold_out["sent"] is False and hold_out.get("action") == "hold"


def test_cryptohopper_payload():
    adapter = CryptohopperAdapter(hopper_id="h1", dry_run=True)
    out = adapter.send(Signal("ETH/USDT", "enter_long", strength=1.0))
    assert out["payload"]["action"] == "buy"
    assert out["payload"]["coin"] == "ETH"
    assert out["payload"]["amount_percentage"] == 100.0
    sell = adapter.send(Signal("ETH/USDT", "exit"))
    assert sell["payload"]["action"] == "sell"


def test_mt5_dry_run_order_and_ea_inputs():
    adapter = MT5Adapter(symbol="BTCUSD", lot=0.2, dry_run=True)
    out = adapter.send(Signal("BTCUSD", "enter_long"))
    assert out["dry_run"] is True
    assert out["order"]["action"] == "buy"
    assert out["order"]["lot"] == 0.2

    inputs = adapter.ea_inputs("trend_following", {"entry_lookback": 55, "exit_lookback": 20})
    assert inputs["InpStrategy"] == "trend_following"
    assert inputs["Inp_entry_lookback"] == 55
