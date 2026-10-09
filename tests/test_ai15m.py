"""Tests for the local Kalshi 15-minute AI paper strategy (fully offline)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.ai15m import (Book, Decision, Market, PaperEngine, Settings, order_fee,
                              parse_orderbook)

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeKalshi:
    """Back-to-back 15-minute markets; results are set by the test."""

    def __init__(self, clock, book):
        self.clock = clock
        self.book = book
        self.results = {}

    def current_market(self, series, now):
        start = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
        close = start + timedelta(minutes=15)
        return Market(ticker=f"{series}-{close:%H%M}", close_time=close, strike=100.0)

    def orderbook(self, ticker, depth=25):
        return self.book

    def result(self, ticker):
        return self.results.get(ticker, "")


class FakeFeed:
    def candles(self, asset, minutes=60):
        return []


class FixedPredictor:
    name = "fixed"

    def __init__(self, direction="UP"):
        self.direction = direction
        self.calls = 0

    def decide(self, ctx):
        self.calls += 1
        return Decision(self.direction, 0.6, "test", "fixed")


def make_engine(tmp_path, book=None, direction="UP", **kw):
    clock = Clock(T0 + timedelta(seconds=5))
    book = book or Book(yes_bids=[(0.48, 1000)], no_bids=[(0.49, 1000)])  # UP ask .51, DOWN ask .52
    kalshi = FakeKalshi(clock, book)
    settings = Settings(log_dir=str(tmp_path), **kw)
    engine = PaperEngine(settings, kalshi, FakeFeed(), FixedPredictor(direction),
                         clock=clock, log=lambda m: None)
    engine.settle_interval = 0
    return engine, clock, kalshi


# ---- pure helpers -----------------------------------------------------------
def test_order_fee_rounds_up_per_order():
    assert order_fee(300, 0.5) == 5.25
    assert order_fee(1, 0.5) == 0.02          # 0.0175 rounds up to 2 cents
    assert order_fee(0, 0.5) == 0.0


def test_parse_orderbook_dollars_and_cents_sorted_best_first():
    book = parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.40", "10"], ["0.45", "5"]],
                                             "no_dollars": [["0.50", "7"]]}})
    assert book.yes_bids == [(0.45, 5.0), (0.40, 10.0)]
    assert book.best_ask("UP") == 0.5 and book.best_ask("DOWN") == 0.55
    legacy = parse_orderbook({"orderbook": {"yes": [[40, 10]], "no": None}})
    assert legacy.yes_bids == [(0.40, 10.0)] and legacy.no_bids == []


def test_simulate_buy_walks_book_and_respects_limit():
    book = Book(yes_bids=[], no_bids=[(0.50, 100), (0.45, 100), (0.30, 100)])  # asks .50 .55 .70
    fill = book.simulate_buy("UP", 300, limit_price=0.60, fee_rate=0.07)
    assert fill.size == 200
    assert fill.avg_price == pytest.approx(0.525)
    assert fill.fees == order_fee(100, 0.50) + order_fee(100, 0.55)


def test_simulate_buy_caps_total_spend():
    book = Book(yes_bids=[], no_bids=[(0.50, 1000)])
    fill = book.simulate_buy("UP", 1000, limit_price=0.9, fee_rate=0.07, max_spend=50)
    assert fill.size > 0 and fill.cost + fill.fees <= 50


# ---- engine flow ------------------------------------------------------------
def test_enters_once_at_session_start_and_settles_win(tmp_path):
    engine, clock, kalshi = make_engine(tmp_path, order_size=100, max_daily_loss=0)
    engine.step()
    pos = engine.positions["BTC"]
    assert (pos.side, pos.size, pos.entry) == ("UP", 100, 0.51)
    engine.step()
    assert engine.predictor.calls == 1                       # one decision per market

    clock.t = T0 + timedelta(minutes=15, seconds=1)          # market closed
    kalshi.results[pos.ticker] = "yes"
    engine.step()
    expected = 100 * (1 - 0.51) - order_fee(100, 0.51)
    assert engine.day_pnl == pytest.approx(expected)
    assert (engine.wins, engine.trades, engine.hits, engine.scored) == (1, 1, 1, 1)
    assert os.path.exists(engine.trades_csv) and os.path.exists(engine.predictions_csv)


def test_obey_model_no_inverts_side(tmp_path):
    engine, _, _ = make_engine(tmp_path, obey_model=False, order_size=10)
    engine.step()
    assert engine.positions["BTC"].side == "DOWN"
    assert engine.positions["BTC"].entry == 0.52


def test_waits_for_time_delay_and_respects_entry_cutoff(tmp_path):
    engine, clock, _ = make_engine(tmp_path, time_delay=5)
    engine.step()
    assert engine.predictor.calls == 0 and engine.states["BTC"].phase == "WAIT_AI_WINDOW"
    clock.t = T0 + timedelta(minutes=14)                     # 1 minute left, past the 2m cutoff
    engine.step()
    assert engine.predictor.calls == 0 and engine.states["BTC"].phase == "MISSED"


def test_skips_when_ask_above_max_entry_price(tmp_path):
    book = Book(yes_bids=[(0.10, 1000)], no_bids=[(0.10, 1000)])  # both asks .90
    engine, _, _ = make_engine(tmp_path, book=book)
    engine.step()
    assert "BTC" not in engine.positions
    assert engine.states["BTC"].phase == "READY_TO_ENTER"


def test_loss_cap_limits_size_to_budget(tmp_path):
    engine, _, _ = make_engine(tmp_path, order_size=1000, max_daily_loss=-100)
    engine.step()
    pos = engine.positions["BTC"]
    assert pos.worst_case <= 100
    assert pos.size < 1000


def test_daily_loss_halts_then_resets_next_day(tmp_path):
    engine, clock, kalshi = make_engine(tmp_path, order_size=1000, max_daily_loss=-100)
    engine.step()
    ticker = engine.positions["BTC"].ticker
    clock.t = T0 + timedelta(minutes=15, seconds=1)
    kalshi.results[ticker] = "no"                             # lose the whole stake
    engine.step()
    assert engine.halted() and "BTC" not in engine.positions
    clock.t = T0 + timedelta(days=1, seconds=5)
    engine.step()
    assert not engine.halted() and "BTC" in engine.positions


def test_void_market_settles_flat(tmp_path):
    engine, clock, kalshi = make_engine(tmp_path, order_size=10)
    engine.step()
    ticker = engine.positions["BTC"].ticker
    clock.t = T0 + timedelta(minutes=15, seconds=1)
    kalshi.results[ticker] = "void"
    engine.step()
    assert engine.pending == {} and engine.day_pnl == 0 and engine.trades == 0


def test_skip_decision_places_no_order(tmp_path):
    engine, _, _ = make_engine(tmp_path, direction="SKIP")
    engine.step()
    engine.step()
    assert engine.positions == {} and engine.states["BTC"].phase == "AI_SKIPPED"
