"""Tests for the strategy library and registry.

These pin down the *contracts* every strategy must honour so the backtest/paper/live
layers can trust them:
  * output is a {0,1} position series aligned to the input index,
  * no position is taken during the warmup window,
  * signals depend only on past/current data (no lookahead), and
  * the registry builds each strategy and filters params safely.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data.loader import synthetic_ohlcv
from src.strategies.mean_reversion import MeanReversion
from src.strategies.registry import available, build_strategy
from src.strategies.trend_following import TrendFollowing
from src.strategies._signal_state import hold_position


def _data(n=1500, seed=11):
    return synthetic_ohlcv(n=n, timeframe="1h", seed=seed)


def test_registry_lists_three_strategies():
    names = available()
    assert {"trend_following", "trend_momentum", "mean_reversion"} <= set(names)


def test_registry_filters_unknown_params():
    # entry_z is meaningless to TrendFollowing; build_strategy must drop it, not crash.
    strat = build_strategy("trend_following", {"entry_lookback": 30, "entry_z": 99})
    assert isinstance(strat, TrendFollowing)
    assert strat.entry_lookback == 30


def test_positions_are_binary_and_aligned():
    df = _data()
    for name in ("trend_following", "trend_momentum", "mean_reversion"):
        sig = build_strategy(name, {}).generate_signals(df)
        assert sig.index.equals(df.index)
        assert set(np.unique(sig.dropna().to_numpy())) <= {0.0, 1.0}


def test_warmup_is_flat():
    df = _data()
    tf = TrendFollowing(entry_lookback=55, exit_lookback=20)
    sig = tf.generate_signals(df)
    assert (sig.iloc[:56] == 0.0).all()

    mr = MeanReversion(lookback=20, trend_filter=200)
    sig2 = mr.generate_signals(df)
    assert (sig2.iloc[:200] == 0.0).all()


def test_hold_position_hysteresis():
    # enter at index 1, exit at index 4 -> long across 1,2,3 then flat.
    idx = pd.date_range("2021-01-01", periods=6, freq="h", tz="UTC")
    enter = pd.Series([False, True, False, False, False, False], index=idx)
    exit_ = pd.Series([False, False, False, False, True, False], index=idx)
    pos = hold_position(enter, exit_, warmup=0)
    assert list(pos) == [0.0, 1.0, 1.0, 1.0, 0.0, 0.0]


def test_mean_reversion_no_lookahead():
    # Signals must not use future data: recomputing on a truncated frame must agree on the
    # overlap (a strategy that peeked ahead would change earlier values when more data is
    # appended).
    df = _data(n=800)
    mr = MeanReversion(lookback=20, trend_filter=100)
    full = mr.generate_signals(df)
    partial = mr.generate_signals(df.iloc[:600])
    # Compare the settled region (past warmup) up to the truncation point.
    a = full.iloc[300:600].to_numpy()
    b = partial.iloc[300:600].to_numpy()
    assert np.array_equal(a, b)


def test_mean_reversion_rejects_bad_params():
    try:
        MeanReversion(entry_z=1.0, exit_z=2.0)  # entry must exceed exit
    except ValueError:
        return
    raise AssertionError("expected ValueError for entry_z <= exit_z")
