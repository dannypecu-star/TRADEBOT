"""Tests for the BTC/ETH pair strategy logic (fully offline).

These pin the money-deciding behavior that the AHK original could never unit-test:
the behavior classifier's states, the strike-gap gate, pair selection preferences,
fee-inclusive ledger accounting, and the DOWN-on-ties resolution rule.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.pair import (DOWN_UP, UP_DOWN, BehaviorSnapshot, BehaviorTracker,
                             PairConfig, PairLedger, adverse_strike_gap_bps,
                             entry_allowed, pair_sum, resolution_leg_value,
                             select_pair)


def _sync_behavior(btc=1.0, eth=1.0):
    return BehaviorSnapshot(state="SYNC", score=5.0, btc_move_bps=btc, eth_move_bps=eth)


# -- behavior classifier -------------------------------------------------------------

def test_behavior_sync_when_moving_together():
    tracker = BehaviorTracker(PairConfig())
    snap = tracker.update(0.0, 100000.0, 4000.0, 100000.0, 4000.0)
    assert snap.state in ("SYNC", "WAITING")
    # both up ~10bps together -> SYNC
    snap = tracker.update(1.0, 100100.0, 4004.0)
    assert snap.state == "SYNC"
    assert abs(snap.btc_move_bps - snap.eth_move_bps) < 1.0


def test_behavior_divergence_on_opposite_moves():
    tracker = BehaviorTracker(PairConfig())
    tracker.update(0.0, 100000.0, 4000.0, 100000.0, 4000.0)
    snap = tracker.update(1.0, 100200.0, 3990.0)  # BTC +20bps, ETH -25bps
    assert snap.state == "DIVERGENCE"
    assert snap.score < 0


def test_behavior_waiting_without_prices():
    tracker = BehaviorTracker(PairConfig())
    snap = tracker.update(0.0, None, 4000.0)
    assert snap.state == "WAITING" and snap.score is None


# -- strike gap and gates ------------------------------------------------------------

def test_adverse_gap_directionality():
    # BTC lagging ETH by 5bps: UP_DOWN pair carries the 5bps losing window
    assert adverse_strike_gap_bps(UP_DOWN, 0.0, 5.0) == 5.0
    assert adverse_strike_gap_bps(DOWN_UP, 0.0, 5.0) == -5.0
    assert adverse_strike_gap_bps(UP_DOWN, None, 5.0) is None


def test_entry_blocked_by_gap_and_behavior():
    cfg = PairConfig(strike_gap_max_bps=3.0)
    ok, _ = entry_allowed(cfg, UP_DOWN, 0.85, _sync_behavior(btc=0.0, eth=1.0))
    assert ok
    ok, reason = entry_allowed(cfg, UP_DOWN, 0.85, _sync_behavior(btc=0.0, eth=9.0))
    assert not ok and "strike gap" in reason
    bad = BehaviorSnapshot(state="DIVERGENCE", score=-20, btc_move_bps=1, eth_move_bps=1)
    ok, reason = entry_allowed(cfg, UP_DOWN, 0.85, bad)
    assert not ok and "DIVERGENCE" in reason
    ok, reason = entry_allowed(cfg, UP_DOWN, 0.97, _sync_behavior())
    assert not ok and "range" in reason


def test_select_pair_prefers_smaller_adverse_gap_not_cheaper_sum():
    cfg = PairConfig()
    # both sums in range; BTC lags ETH so UP_DOWN is the risky one even if cheaper
    behavior = _sync_behavior(btc=0.0, eth=2.0)
    picked = select_pair(cfg, btc_up=0.40, btc_down=0.62, eth_up=0.28, eth_down=0.42,
                         behavior=behavior)
    # sums: UP_DOWN = 0.82 (cheaper), DOWN_UP = 0.90 -- gap prefers DOWN_UP
    assert picked == DOWN_UP


def test_select_pair_falls_back_to_cheaper_without_move_data():
    cfg = PairConfig()
    behavior = BehaviorSnapshot(state="SYNC", score=5.0)
    picked = select_pair(cfg, 0.40, 0.62, 0.28, 0.42, behavior)
    assert picked == UP_DOWN  # cheaper sum wins only when gaps are unknown


# -- resolution and ledger -----------------------------------------------------------

def test_resolution_tie_settles_down():
    assert resolution_leg_value("UP", 0.5, 0.5) == 0.0
    assert resolution_leg_value("DOWN", 0.5, 0.5) == 1.0
    assert resolution_leg_value("UP", 0.9, 0.1) == 1.0
    assert resolution_leg_value("DOWN", None, 0.5) == 0.0


def _legs(btc_side, btc_price, eth_side, eth_price, qty=5):
    return [{"asset": "BTC", "side": btc_side, "price": btc_price, "qty": qty},
            {"asset": "ETH", "side": eth_side, "price": eth_price, "qty": qty}]


def test_ledger_entry_charges_cost_plus_fees():
    ledger = PairLedger(bankroll=100.0)
    trade = ledger.open_trade(UP_DOWN, _legs("UP", 0.40, "DOWN", 0.45), "t", 0.07)
    assert trade is not None
    # cost = (0.40+0.45)*5 = 4.25; fees = (0.02 + 0.02) * 5 = 0.20
    assert abs(trade.entry_cost - 4.25) < 1e-9
    assert abs(trade.entry_fees - 0.20) < 1e-9
    assert abs(ledger.bankroll - (100.0 - 4.45)) < 1e-9


def test_ledger_refuses_unaffordable_trade():
    ledger = PairLedger(bankroll=1.0)
    assert ledger.open_trade(UP_DOWN, _legs("UP", 0.40, "DOWN", 0.45), "t", 0.07) is None
    assert ledger.bankroll == 1.0


def test_resolution_win_books_correctly():
    ledger = PairLedger(bankroll=100.0)
    ledger.open_trade(UP_DOWN, _legs("UP", 0.40, "DOWN", 0.45), "t", 0.07)
    # both settle same direction: BTC up (up>down), ETH down (down>=up) -> one leg each pays
    result = ledger.close_all("RESOLUTION", 0.07,
                              final_prices={"BTC": (0.9, 0.1), "ETH": (0.2, 0.8)})
    assert result.revenue == 10.0  # ETH DOWN pays 5, BTC UP pays 5
    assert result.outcome == "WIN"
    assert abs(result.pnl - (10.0 - 4.25 - 0.20)) < 1e-9
    assert ledger.wins == 1 and ledger.sessions == 1


def test_stop_exit_charges_exit_fees_and_counts_stop():
    ledger = PairLedger(bankroll=100.0)
    ledger.open_trade(UP_DOWN, _legs("UP", 0.40, "DOWN", 0.45), "t", 0.07)
    result = ledger.close_all("EXIT", 0.07,
                              current_prices={("BTC", "UP"): 0.20, ("ETH", "DOWN"): 0.30})
    assert result.outcome == "LOSS"
    assert ledger.stop_losses == 1
    assert result.fees > 0.20  # entry fees plus exit fees
    assert result.pnl < 0


def test_ledger_round_trips_counters():
    ledger = PairLedger(bankroll=500.0, wins=3, sessions=7, stop_losses=2)
    restored = PairLedger.from_dict(ledger.to_dict())
    assert restored.bankroll == 500.0
    assert restored.wins == 3 and restored.sessions == 7 and restored.stop_losses == 2


def test_pair_sum_handles_missing_prices():
    assert abs(pair_sum(UP_DOWN, 0.4, None, None, 0.45) - 0.85) < 1e-9
    assert pair_sum(UP_DOWN, None, 0.5, 0.5, 0.45) is None
    assert abs(pair_sum(DOWN_UP, 0.4, 0.5, 0.45, None) - 0.95) < 1e-9
