"""Tests for statistical (pairs) arbitrage and the cross-exchange scanner."""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.arbitrage.cross_exchange import Quote, find_opportunities
from src.arbitrage.pairs import PairsConfig, build_spread, run_pairs_backtest


def _cointegrated_pair(n=2000, seed=1):
    """B is a random walk; A tracks B with a mean-reverting (OU) spread."""
    rng = np.random.default_rng(seed)
    b = 100 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    spread = np.zeros(n)
    for i in range(1, n):
        spread[i] = 0.9 * spread[i - 1] + rng.normal(0, 0.01)
    a = 2.0 * b * np.exp(spread)
    idx = pd.date_range("2021-01-01", periods=n, freq="h", tz="UTC")
    return pd.Series(a, index=idx), pd.Series(b, index=idx)


def test_build_spread_is_trailing_only():
    a, b = _cointegrated_pair()
    spread, z, beta = build_spread(a, b, window=60)
    # The first `window` values must be NaN (need a full trailing window first).
    assert spread.iloc[:59].isna().all()
    assert z.index.equals(a.index)


def test_pairs_backtest_runs_and_trades_on_cointegrated_data():
    a, b = _cointegrated_pair(seed=2)
    res = run_pairs_backtest(a, b, PairsConfig(lookback=60, entry_z=1.5, exit_z=0.3))
    # On genuinely mean-reverting data the engine should find and take spread trades.
    assert len(res.trades) > 0
    assert res.equity.index.equals(a.index)
    # Equity must be finite and never NaN.
    assert res.equity.notna().all()


def test_pairs_costs_are_charged():
    a, b = _cointegrated_pair(seed=3)
    free = run_pairs_backtest(a, b, PairsConfig(lookback=60, fee_rate=0.0, slippage=0.0))
    costed = run_pairs_backtest(a, b, PairsConfig(lookback=60, fee_rate=0.002, slippage=0.001))
    # More cost per trade can only lower (or equal) the final equity, never raise it.
    assert costed.equity.iloc[-1] <= free.equity.iloc[-1]


def test_cross_exchange_finds_net_positive_only_above_costs():
    # Construct a ~40 bps gross spread (buy@100.0, sell@100.4).
    quotes = [
        Quote("cheap", bid=99.9, ask=100.0),
        Quote("dear", bid=100.4, ask=100.5),
    ]
    # round-trip cost = 2*taker + 2*slip = 2*0.0002 + 2*0.00005 = 5 bps -> net positive.
    opps = find_opportunities("X/Y", quotes, taker_fee=0.0002, slippage=0.00005)
    best = opps[0]
    assert best.buy_exchange == "cheap" and best.sell_exchange == "dear"
    assert best.profitable is True

    # round-trip cost = 2*0.0025 = 50 bps, which swamps the ~40 bps gross edge.
    opps2 = find_opportunities("X/Y", quotes, taker_fee=0.0025, slippage=0.0)
    assert all(not o.profitable for o in opps2)
