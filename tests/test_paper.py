"""Tests for the paper broker and paper trader."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data.loader import synthetic_ohlcv
from src.paper.broker import PaperBroker
from src.paper.paper_trader import PaperConfig, PaperTrader
from src.risk.manager import RiskManager
from src.strategies.registry import build_strategy


def test_broker_charges_fees_and_slippage():
    b = PaperBroker(cash=10_000, fee_rate=0.001, slippage=0.0005)
    fill = b.buy(price=100.0, target_value=1_000.0, time="t0")
    assert fill is not None
    # Buy fills above quoted price due to slippage.
    assert fill.price > 100.0
    # A round trip at a flat price must lose money to costs (never create it).
    b.sell_all(price=100.0, time="t1")
    assert b.equity(100.0) < 10_000.0


def test_broker_cannot_double_enter():
    b = PaperBroker(cash=10_000)
    assert b.buy(100.0, 1000.0, "t0") is not None
    assert b.buy(100.0, 1000.0, "t1") is None  # already in position


def test_broker_realized_trades_roundtrip():
    b = PaperBroker(cash=10_000, fee_rate=0.0, slippage=0.0)
    b.buy(100.0, 1000.0, "t0")
    b.sell_all(110.0, "t1")
    pnls = b.realized_trades()
    assert len(pnls) == 1
    assert pnls[0] > 0  # bought at 100, sold at 110


def test_paper_replay_matches_shape_and_costs():
    df = synthetic_ohlcv(n=1200, timeframe="1h", seed=3)
    strat = build_strategy("trend_following", {})
    cfg = PaperConfig(initial_cash=10_000, warmup_bars=120,
                      state_file="/tmp/_ptest_state.json",
                      audit_log="/tmp/_ptest_audit.jsonl")
    trader = PaperTrader(strat, RiskManager(), cfg, symbol="TEST")
    summary = trader.run_replay(df)
    assert "final_equity" in summary
    assert summary["final_equity"] > 0
    # Health snapshot was kept current during the replay.
    assert trader.health.loops > 0
    assert round(trader.health.equity, 2) == summary["final_equity"]


def test_paper_flat_strategy_holds_initial_cash():
    df = synthetic_ohlcv(n=800, timeframe="1h", seed=5)

    class Flat:
        name = "flat"

        def generate_signals(self, d):
            import pandas as pd
            return pd.Series(0.0, index=d.index)

    cfg = PaperConfig(initial_cash=10_000, warmup_bars=50,
                      state_file="/tmp/_ptest_state2.json",
                      audit_log="/tmp/_ptest_audit2.jsonl")
    trader = PaperTrader(Flat(), RiskManager(), cfg, symbol="TEST")
    summary = trader.run_replay(df)
    assert summary["n_trades"] == 0
    assert abs(summary["final_equity"] - 10_000) < 1e-6
