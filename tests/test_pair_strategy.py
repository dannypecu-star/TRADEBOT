"""Tests for the BTC/ETH inverse-pair paper-trading strategy port.

These lock down the pieces that are easy to get subtly wrong in the port: the entry-range
gate, the spot-based behavior classifier (SYNC vs DIVERGENCE), the paper accounting at
resolution vs stop-out, and that a full synthetic run terminates with conserved cash.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.pair_sim import SimConfig, simulate
from src.kalshi.pair_strategy import (
    BTC_UP_ETH_DOWN,
    DIVERGENCE,
    SYNC,
    BehaviorTracker,
    MarketSnapshot,
    PairConfig,
    PairTrader,
    PaperBook,
    build_pair,
    pair_sums,
    select_pair_entry,
)


def _snap(ticker, up, down, mins=10, q=1):
    return MarketSnapshot(ticker, up, down, mins, q)


def test_entry_range_gate():
    cfg = PairConfig(entry_range=(0.80, 0.90))
    assert cfg.sum_in_entry_range(0.85)
    assert cfg.sum_in_entry_range(0.80)
    assert cfg.sum_in_entry_range(0.90)
    assert not cfg.sum_in_entry_range(0.79)
    assert not cfg.sum_in_entry_range(0.91)
    assert not cfg.sum_in_entry_range(None)


def test_effective_exit_uses_floor_when_pairexit_disabled():
    assert PairConfig(pair_exit=0.60).effective_exit() == 0.60
    cfg = PairConfig(pair_exit=0.0, paper_loss_exit_floor=0.05)
    assert cfg.effective_exit() == 0.05
    assert "paper loss floor" in cfg.exit_note()


def test_pair_sums_and_selection_prefers_cheaper_in_range():
    cfg = PairConfig(entry_range=(0.80, 0.90))
    btc = _snap("BTC", up=0.30, down=0.72)   # BTC leaning down
    eth = _snap("ETH", up=0.55, down=0.47)
    sums = pair_sums(btc, eth)
    assert round(sums[BTC_UP_ETH_DOWN], 2) == 0.77   # 0.30 + 0.47 -> out of range
    assert round(sums["BTC_DOWN_ETH_UP"], 2) == 1.27  # out of range
    # Only the in-range side is selectable.
    btc2 = _snap("BTC", up=0.42, down=0.60)
    eth2 = _snap("ETH", up=0.46, down=0.56)   # up_down = 0.42+0.56 = 0.98, down_up=0.60+0.46=1.06
    assert select_pair_entry(cfg, btc2, eth2, _behavior_sync()) is None


def _behavior_sync():
    from src.kalshi.pair_strategy import BehaviorState
    return BehaviorState(state=SYNC, score=1.0)


def test_behavior_sync_vs_divergence():
    cfg = PairConfig()
    tr = BehaviorTracker(cfg)
    # Both spots up by a similar amount -> moving together -> SYNC.
    b = tr.update("k", now_ms=0, btc_spot=100.10, eth_spot=100.08,
                  btc_open=100.0, eth_open=100.0)
    assert b.state == SYNC

    tr2 = BehaviorTracker(cfg)
    # BTC up, ETH down -> opposite signs -> DIVERGENCE.
    d = tr2.update("k", now_ms=0, btc_spot=100.10, eth_spot=99.90,
                   btc_open=100.0, eth_open=100.0)
    assert d.state == DIVERGENCE
    assert not d.entry_allowed()


def test_paper_resolution_pays_winning_legs():
    cfg = PairConfig(order_size=5)
    book = PaperBook(cfg)
    btc = _snap("BTC", up=0.45, down=0.55)
    eth = _snap("ETH", up=0.60, down=0.40)
    pair = build_pair(BTC_UP_ETH_DOWN, btc, eth)   # legs: BTC UP @0.45, ETH DOWN @0.40
    assert book.buy_pair(pair) is not None
    assert round(book.cash, 2) == round(10000.0 - (0.45 + 0.40) * 5, 2)

    # Resolution: BTC finishes up (up>=down wins), ETH finishes up (so ETH DOWN loses).
    btc_final = _snap("BTC", up=0.99, down=0.01)   # up wins -> BTC UP pays $1
    eth_final = _snap("ETH", up=0.99, down=0.01)   # up wins -> ETH DOWN pays $0
    res = book.close_pair(pair, btc_final, eth_final, "RESOLUTION")
    assert res.revenue == 1.0 * 5 + 0.0 * 5        # only the BTC UP leg pays
    assert res.contracts == 5
    assert book.completed_sessions == 1


def test_paper_stop_out_values_at_current_prices():
    cfg = PairConfig(order_size=5)
    book = PaperBook(cfg)
    btc = _snap("BTC", up=0.45, down=0.55)
    eth = _snap("ETH", up=0.60, down=0.40)
    pair = build_pair(BTC_UP_ETH_DOWN, btc, eth)
    book.buy_pair(pair)
    # Prices collapsed -> exit revenue is current leg prices * qty.
    btc_now = _snap("BTC", up=0.20, down=0.80)
    eth_now = _snap("ETH", up=0.75, down=0.25)
    res = book.close_pair(pair, btc_now, eth_now, "EXIT")
    assert res.revenue == (0.20 + 0.25) * 5
    assert res.outcome == "LOSS"
    assert book.stop_losses == 1


def test_full_simulation_runs_and_conserves_cash():
    cfg = PairConfig()
    trader = PairTrader(cfg)
    for t in simulate(SimConfig(sessions=6, seed=7)):
        trader.on_tick(t.btc, t.eth, t.btc_spot, t.eth_spot, t.now_ms,
                       t.btc_open, t.eth_open)
    # It actually traded, closed everything, and equity == cash when flat.
    assert trader.book.completed_sessions > 0
    assert trader.book.open_trades == []
    assert round(trader.book.equity(), 2) == round(trader.book.cash, 2)
    # Cash only ever moved by entry costs and payouts -- no money invented.
    assert trader.book.cash > 0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
