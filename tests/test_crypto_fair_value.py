"""Tests for the crypto fair-value model, its backtest bridge, and the trade log.

The important test is ``test_model_is_well_calibrated_on_its_own_process``: when the
underlying really is the GBM the model assumes, the model's probabilities must match
realized frequencies. If that fails, the formula is wrong and every downstream profit
number is meaningless.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.backtest import run_value_backtest
from src.kalshi.crypto_fair_value import (
    CryptoFairValueSource,
    CryptoMarketState,
    CryptoObservation,
    annualized_vol_from_log_returns,
    resolved_markets_from_observations,
    up_probability,
    vol_from_typical_move,
)
from src.kalshi.economics import SizingConfig
from src.kalshi.trade_log import TradeLogger, summarize


# --- The model's basic shape -------------------------------------------------------

def test_at_the_money_is_a_near_coin_flip():
    # Spot exactly at strike -> ~50/50, but *fractionally below* 0.5 because under
    # zero-drift GBM the lognormal median finishes just under the open (volatility
    # drag). Over 15 minutes the effect is tiny (<0.1c) but it is the correct sign.
    p = up_probability(100.0, 100.0, 10.0, 0.6)
    assert p < 0.5 and abs(p - 0.5) < 1e-3


def test_monotonic_in_spot():
    p_low = up_probability(99.0, 100.0, 10.0, 0.6)
    p_mid = up_probability(100.0, 100.0, 10.0, 0.6)
    p_high = up_probability(101.0, 100.0, 10.0, 0.6)
    assert p_low < p_mid < p_high


def test_more_time_pulls_toward_a_coin_flip():
    # Above strike but far from expiry -> less certain than the same edge near expiry.
    near = up_probability(101.0, 100.0, 1.0, 0.6)
    far = up_probability(101.0, 100.0, 14.0, 0.6)
    assert 0.5 < far < near


def test_degenerate_inputs_are_safe():
    # At expiry it collapses to a hard 1/0/0.5.
    assert up_probability(101.0, 100.0, 0.0, 0.6) == 1.0
    assert up_probability(99.0, 100.0, 0.0, 0.6) == 0.0
    assert up_probability(100.0, 100.0, 0.0, 0.6) == 0.5
    # Zero/negative vol behaves like expiry; bad prices give max uncertainty, no edge.
    assert up_probability(101.0, 100.0, 10.0, 0.0) == 1.0
    assert up_probability(-1.0, 100.0, 10.0, 0.6) == 0.5
    assert up_probability(100.0, 0.0, 10.0, 0.6) == 0.5


def test_vol_helpers_roundtrip():
    # A 0.3% typical move over 15 minutes, annualised, then used to price a 15m ATM
    # option, should still be ~50% (ATM) but with a sane, positive sigma.
    sigma = vol_from_typical_move(0.003, 15.0)
    assert sigma > 0
    assert abs(up_probability(100.0, 100.0, 15.0, sigma) - 0.5) < 1e-3
    # Annualising a flat return series yields zero vol; a noisy one yields positive vol.
    assert annualized_vol_from_log_returns([0.0, 0.0, 0.0], 60.0) == 0.0
    assert annualized_vol_from_log_returns([0.01, -0.008, 0.012, -0.005], 60.0) > 0


# --- Calibration: the test that actually matters -----------------------------------

def _simulate_observations(n=6000, minutes=15.0, sigma=0.6, market_noise=0.03, seed=7):
    """Simulate up/down markets whose underlying is exactly the model's GBM.

    Strike = price at open. We draw a terminal log return ~ Normal(-0.5 s^2 t, s^2 t),
    settle Yes if it ended above the open, and set the *market* price to the true model
    probability plus noise (the mispricing a skilled model exploits).
    """
    rng = np.random.default_rng(seed)
    tau = minutes / (365.0 * 24.0 * 60.0)
    drift = -0.5 * sigma ** 2 * tau
    sd = sigma * math.sqrt(tau)
    obs = []
    for i in range(n):
        # Observe at a random point during the market with some elapsed move already in.
        minutes_left = float(rng.uniform(1.0, minutes))
        elapsed_tau = (minutes - minutes_left) / (365.0 * 24.0 * 60.0)
        spot = 100.0 * math.exp(rng.normal(-0.5 * sigma ** 2 * elapsed_tau,
                                           sigma * math.sqrt(max(elapsed_tau, 1e-12))))
        strike = 100.0
        true_p = up_probability(spot, strike, minutes_left, sigma)
        # Resolve using the model's own conditional distribution from `spot`.
        rem_sd = sigma * math.sqrt(minutes_left / (365.0 * 24.0 * 60.0))
        terminal = spot * math.exp(rng.normal(-0.5 * rem_sd ** 2, rem_sd))
        outcome = int(terminal > strike)
        price = float(np.clip(true_p + rng.normal(0, market_noise), 0.02, 0.98))
        obs.append(CryptoObservation(f"BTC-{i:04d}", spot, strike, minutes_left, sigma, price, outcome))
    return obs


def test_model_is_well_calibrated_on_its_own_process():
    obs = _simulate_observations()
    resolved = resolved_markets_from_observations(obs)
    # Bucket by predicted probability and check predicted ~ realized frequency.
    preds = np.array([m.fair_prob for m in resolved])
    outs = np.array([m.outcome for m in resolved])
    for lo in (0.1, 0.3, 0.5, 0.7):
        mask = (preds >= lo) & (preds < lo + 0.2)
        if mask.sum() < 50:
            continue
        predicted = preds[mask].mean()
        realized = outs[mask].mean()
        assert abs(predicted - realized) < 0.05, f"miscalibrated in [{lo},{lo+0.2}): {predicted:.2f} vs {realized:.2f}"


def test_skilled_crypto_model_profits_through_the_backtester():
    obs = _simulate_observations()
    resolved = resolved_markets_from_observations(obs)
    result = run_value_backtest(resolved, start_bankroll=1000.0, sizing=SizingConfig(min_edge=0.02))
    assert result.n_trades > 0
    assert result.total_return > 0, "a calibrated model on noisily-mispriced markets should profit"
    # Realized edge should be within shouting distance of predicted (not wildly overconfident).
    assert result.realized_edge > 0


def test_no_edge_when_market_price_equals_the_model():
    # If the market prices exactly at the model's probability, there is nothing to trade.
    obs = _simulate_observations(market_noise=0.0)
    resolved = resolved_markets_from_observations(obs)
    # Force price == fair_prob so edge is strictly negative after fees everywhere.
    for m in resolved:
        m.yes_price = m.fair_prob
    result = run_value_backtest(resolved, start_bankroll=1000.0)
    assert result.n_trades == 0, "a market priced at fair value offers no edge"


# --- The ProbabilitySource wiring --------------------------------------------------

def test_fair_value_source_maps_ticker_to_probability():
    states = {
        "BTC-UP": CryptoMarketState("BTC", spot=101.0, strike=100.0, minutes_left=7.0, vol_annual=0.6),
        "BTC-DN": CryptoMarketState("BTC", spot=99.0, strike=100.0, minutes_left=7.0, vol_annual=0.6),
    }
    source = CryptoFairValueSource(states.get)
    assert source.fair_probability("BTC-UP") > 0.5
    assert source.fair_probability("BTC-DN") < 0.5
    assert source.fair_probability("UNKNOWN") is None  # untradeable -> no signal


# --- Trade log round trip ----------------------------------------------------------

def test_trade_log_records_and_summarizes(tmp_path):
    path = str(tmp_path / "trades.csv")
    log = TradeLogger(path)
    # Two winners and one loser, all "yes".
    log.log_signal("M1", "BTC", "yes", 0.55, 0.80, 0.20, 10, spot=101, strike=100, minutes_left=7, vol_annual=0.6)
    log.log_settlement("M1", "yes", 0.55, 0.80, 10, outcome=1, asset="BTC")
    log.log_settlement("M2", "yes", 0.60, 0.75, 10, outcome=1, asset="BTC")
    log.log_settlement("M3", "yes", 0.50, 0.70, 10, outcome=0, asset="BTC")
    s = summarize(path)
    assert s["n_trades"] == 3
    assert s["hit_rate"] == 2 / 3
    # 20 winning contracts pay $20; 10 losing contracts cost their price. Net positive.
    assert s["total_pnl"] > 0
    assert isinstance(s["calibration"], list)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
