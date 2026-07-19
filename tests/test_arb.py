"""Tests for the riskless-basket arb logic (fully offline).

The fee math and the orderbook depth derivation are the places a mistake would turn
"guaranteed profit" into a guaranteed loss, so they are pinned exactly.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.arb import (basket_cost, find_opportunities, implied_ask_and_size,
                            max_baskets_from_orderbooks)


def _event(mutually_exclusive=True, markets=None):
    return {"event_ticker": "EV-TEST", "title": "Test event",
            "mutually_exclusive": mutually_exclusive, "markets": markets or []}


def _mk(ticker, yes_ask=None, no_ask=None, status="active"):
    return {"ticker": ticker, "yes_ask": yes_ask, "no_ask": no_ask, "status": status}


def test_basket_cost_includes_per_leg_fee_rounding():
    # fee at 0.30: 0.07*0.30*0.70 = 0.0147 -> rounds UP to 0.02 per contract
    cost = basket_cost([0.30, 0.30, 0.30])
    assert abs(cost - (0.90 + 0.06)) < 1e-9


def test_yes_basket_detected_and_flagged():
    event = _event(markets=[_mk("A", yes_ask=30), _mk("B", yes_ask=30),
                            _mk("C", yes_ask=30)])
    opps = find_opportunities(event, min_profit=0.01)
    yes = [o for o in opps if o["type"] == "YES_BASKET"]
    assert len(yes) == 1
    # payout 1.00 - (0.90 asks + 0.06 fees) = 0.04
    assert abs(yes[0]["profit"] - 0.04) < 1e-9
    assert "exhaustive" in yes[0]["caveat"]


def test_no_basket_detected_without_caveat():
    event = _event(markets=[_mk("A", no_ask=60), _mk("B", no_ask=60),
                            _mk("C", no_ask=60)])
    opps = find_opportunities(event, min_profit=0.01)
    no = [o for o in opps if o["type"] == "NO_BASKET"]
    assert len(no) == 1
    # payout (3-1)=2.00 - (1.80 asks + 3*0.02 fees) = 0.14
    assert abs(no[0]["profit"] - 0.14) < 1e-9
    assert no[0]["caveat"] == ""


def test_fairly_priced_event_has_no_opportunity():
    event = _event(markets=[_mk("A", yes_ask=50, no_ask=52),
                            _mk("B", yes_ask=52, no_ask=50)])
    assert find_opportunities(event, min_profit=0.01) == []


def test_non_mutually_exclusive_event_is_skipped():
    event = _event(mutually_exclusive=False,
                   markets=[_mk("A", yes_ask=30), _mk("B", yes_ask=30),
                            _mk("C", yes_ask=30)])
    assert find_opportunities(event) == []


def test_missing_ask_kills_that_basket_only():
    event = _event(markets=[_mk("A", yes_ask=None, no_ask=60),
                            _mk("B", yes_ask=30, no_ask=60),
                            _mk("C", yes_ask=30, no_ask=60)])
    opps = find_opportunities(event, min_profit=0.01)
    assert [o["type"] for o in opps] == ["NO_BASKET"]


def test_closed_markets_are_excluded_and_max_legs_respected():
    markets = [_mk("A", yes_ask=30), _mk("B", yes_ask=30),
               _mk("C", yes_ask=30), _mk("D", yes_ask=1, status="settled")]
    event = _event(markets=markets)
    opps = find_opportunities(event, min_profit=0.01)
    assert opps and opps[0]["legs"] == 3  # settled leg not counted

    assert find_opportunities(event, min_profit=0.01, max_legs=2) == []


def test_implied_ask_from_opposite_bid():
    # NO bids ascending; best NO bid 65c -> implied YES ask 35c, size 40
    orderbook = {"yes": [[10, 5]], "no": [[50, 100], [65, 40]]}
    ask, size = implied_ask_and_size(orderbook, "yes")
    assert abs(ask - 0.35) < 1e-9 and size == 40
    # best YES bid 10c -> implied NO ask 90c, size 5
    ask, size = implied_ask_and_size(orderbook, "no")
    assert abs(ask - 0.90) < 1e-9 and size == 5


def test_empty_book_reports_unknown_not_zero():
    assert implied_ask_and_size({"yes": [], "no": []}, "yes") == (None, 0)
    baskets, _ = max_baskets_from_orderbooks({"A": {"yes": [], "no": []}}, ["A"], "yes")
    assert baskets is None


def test_max_baskets_is_min_depth_across_legs():
    obs = {"A": {"no": [[70, 40]], "yes": []},
           "B": {"no": [[70, 12]], "yes": []},
           "C": {"no": [[70, 99]], "yes": []}}
    baskets, detail = max_baskets_from_orderbooks(obs, ["A", "B", "C"], "yes")
    assert baskets == 12
    assert detail["B"] == (0.30, 12)
