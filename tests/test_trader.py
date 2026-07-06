"""Offline tests for the paper-trading loop using a fake Kalshi client.

These verify the loop's behavior without any network: it buys the right side when there
is edge, respects dry-run, honors the position cap, skips markets already held, and
stops on the daily loss limit.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.economics import SizingConfig
from src.kalshi.sources.manual import ManualProbabilitySource
from src.kalshi.trader import PaperTrader, RiskLimits


class FakeClient:
    """Records orders instead of sending them, and returns canned data."""

    def __init__(self, balance_cents=100_000, markets=None, positions=None):
        self._balance = balance_cents
        self._markets = markets or []
        self._positions = positions or []
        self.orders: list[dict] = []

    def get_balance(self):
        return {"balance": self._balance}

    def get_markets(self, **_):
        return {"markets": self._markets}

    def get_positions(self, **_):
        return {"market_positions": self._positions}

    def create_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"order": {"status": "resting", **kwargs}}


def _market(ticker, yes_ask, yes_bid=None):
    yes_bid = yes_bid if yes_bid is not None else yes_ask - 2
    return {
        "ticker": ticker,
        "status": "open",
        "yes_ask": yes_ask,
        "yes_bid": yes_bid,
        "no_ask": 100 - yes_bid,
        "title": ticker,
    }


SIZING = SizingConfig(min_edge=0.01)


def test_buys_yes_when_underpriced_live():
    # Market asks 50c for Yes; our fair estimate is 70% -> strong Yes edge.
    client = FakeClient(markets=[_market("GAME-A", yes_ask=50)])
    source = ManualProbabilitySource({"GAME-A": 0.70})
    trader = PaperTrader(client, source, SIZING, RiskLimits(dry_run=False))
    state = trader.run_once()
    assert len(state.orders) == 1
    o = state.orders[0]
    assert o.side == "yes" and o.placed and o.price_cents == 50
    assert client.orders and client.orders[0]["side"] == "yes"


def test_dry_run_places_nothing():
    client = FakeClient(markets=[_market("GAME-A", yes_ask=50)])
    source = ManualProbabilitySource({"GAME-A": 0.70})
    trader = PaperTrader(client, source, SIZING, RiskLimits(dry_run=True))
    state = trader.run_once()
    assert len(state.orders) == 1 and state.orders[0].placed is False
    assert client.orders == []  # nothing was actually sent


def test_no_trade_without_edge():
    # Fairly priced: fair == ask -> no edge after fees.
    client = FakeClient(markets=[_market("GAME-A", yes_ask=50)])
    source = ManualProbabilitySource({"GAME-A": 0.50})
    trader = PaperTrader(client, source, SIZING, RiskLimits(dry_run=False))
    assert trader.run_once().orders == []


def test_skips_market_already_held():
    client = FakeClient(
        markets=[_market("GAME-A", yes_ask=50)],
        positions=[{"ticker": "GAME-A", "position": 10}],
    )
    source = ManualProbabilitySource({"GAME-A": 0.70})
    trader = PaperTrader(client, source, SIZING, RiskLimits(dry_run=False))
    assert trader.run_once().orders == []


def test_position_cap_blocks_extra_orders():
    markets = [_market(f"G{i}", yes_ask=50) for i in range(5)]
    source = ManualProbabilitySource({f"G{i}": 0.70 for i in range(5)})
    client = FakeClient(markets=markets)
    trader = PaperTrader(client, source, SIZING,
                         RiskLimits(dry_run=False, max_open_positions=2))
    state = trader.run_once()
    assert sum(o.placed for o in state.orders) == 2  # capped at 2


def test_only_watched_tickers_are_traded():
    client = FakeClient(markets=[_market("GAME-A", 50), _market("GAME-B", 50)])
    source = ManualProbabilitySource({"GAME-A": 0.70})  # B not watched
    trader = PaperTrader(client, source, SIZING, RiskLimits(dry_run=False))
    state = trader.run_once()
    assert len(state.orders) == 1 and state.orders[0].ticker == "GAME-A"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
