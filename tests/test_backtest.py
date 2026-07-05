"""Tests that pin down the engine's correctness -- especially the no-lookahead rule.

Run with:  python -m pytest -q   (or)   python tests/test_backtest.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.backtest.engine import ExecConfig, run_backtest
from src.data.loader import synthetic_ohlcv
from src.risk.manager import RiskConfig, RiskManager
from src.strategies.base import Strategy


class AlwaysLong(Strategy):
    name = "always_long"

    def generate_signals(self, df):
        return pd.Series(1.0, index=df.index)


class AlwaysFlat(Strategy):
    name = "always_flat"

    def generate_signals(self, df):
        return pd.Series(0.0, index=df.index)


class PerfectForesight(Strategy):
    """Would be hugely profitable IF the engine let it act on the current bar.

    It sets the signal using the *next* bar's close -- i.e. it cheats. Because the
    engine only executes a signal on the following bar's open, this cheating signal
    is delayed by one bar and cannot actually exploit the future. The test asserts the
    result is not absurdly profitable, which would indicate a lookahead leak.
    """
    name = "perfect_foresight"

    def generate_signals(self, df):
        future_up = df["close"].shift(-1) > df["close"]
        return future_up.astype(float)


def _data():
    return synthetic_ohlcv(n=1500, timeframe="1h", seed=7)


def test_flat_strategy_never_loses_money():
    df = _data()
    res = run_backtest(df, AlwaysFlat(), RiskManager(), ExecConfig())
    assert res.metrics.n_trades == 0
    assert abs(res.equity.iloc[-1] - res.exec_config.initial_cash) < 1e-6


def test_costs_reduce_returns():
    # The same strategy must earn strictly less once fees and slippage are applied.
    # This is the cleanest statement of "costs matter" and is independent of sizing
    # and of which way the market went.
    df = _data()
    risk = RiskManager(RiskConfig(risk_per_trade=1.0, max_position_fraction=1.0,
                                  atr_stop_mult=3.0, max_drawdown_stop=1.0))
    free = run_backtest(df, AlwaysLong(), risk,
                        ExecConfig(fee_rate=0.0, slippage=0.0))
    costed = run_backtest(df, AlwaysLong(), risk,
                          ExecConfig(fee_rate=0.001, slippage=0.0005))
    assert costed.metrics.total_return < free.metrics.total_return


def test_no_lookahead_leak():
    df = _data()
    res = run_backtest(df, PerfectForesight(), RiskManager(), ExecConfig())
    # With a true one-bar delay, a next-bar signal degrades to a near-random trade and
    # cannot produce foresight-level returns. Guard against an implausible blow-up.
    assert res.metrics.total_return < 5.0, "suspiciously high return -> possible lookahead"


def test_stop_loss_caps_single_trade_loss():
    # Construct a crash: flat then a steep drop. The ATR stop must exit before the
    # full drawdown is realized on the position.
    n = 300
    idx = pd.date_range("2020-01-01", periods=n, freq="h", tz="UTC")
    price = np.concatenate([np.full(150, 100.0), np.linspace(100, 40, 150)])
    df = pd.DataFrame(
        {"open": price, "high": price, "low": price, "close": price,
         "volume": np.ones(n)},
        index=idx,
    )

    class LongAfterWarmup(Strategy):
        name = "long_after_warmup"

        def generate_signals(self, d):
            s = pd.Series(0.0, index=d.index)
            s.iloc[100:] = 1.0
            return s

    risk = RiskManager(RiskConfig(risk_per_trade=0.02, atr_stop_mult=3.0))
    res = run_backtest(df, LongAfterWarmup(), risk, ExecConfig())
    # The stop caps the loss on ANY single trade. (Cumulative loss from repeatedly
    # re-entering a downtrend can still be large -- that is exactly why the real
    # strategy has a trend filter to keep it out of falling markets.)
    assert res.trades, "expected at least one trade"
    worst = max(-t.pnl / (t.units * t.entry_price) for t in res.trades)
    assert worst < 0.05, f"a single trade lost {worst:.1%} of its notional"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
