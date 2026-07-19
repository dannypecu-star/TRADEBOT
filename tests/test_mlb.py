"""Tests for the MLB market-matching and paper-ledger logic (fully offline).

Matching is the fragile part of the sports edge -- a market mapped to the wrong game
or wrong side turns a good probability into a coin flip. These tests pin the tricky
cases: two-word nicknames, doubleheaders, and ambiguous YES sides.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.mlb import PaperLedger, MarketMatch, match_market, nickname


def _games():
    return {
        "g1": {"home_team": "Atlanta Braves", "away_team": "New York Yankees",
               "commence_time": "2026-07-19T23:10:00Z",
               "probs": {"Atlanta Braves": 0.45, "New York Yankees": 0.55}},
        "g2": {"home_team": "Boston Red Sox", "away_team": "Chicago White Sox",
               "commence_time": "2026-07-19T22:05:00Z",
               "probs": {"Boston Red Sox": 0.60, "Chicago White Sox": 0.40}},
    }


def test_nickname_handles_two_word_names():
    assert nickname("Boston Red Sox") == "Red Sox"
    assert nickname("Chicago White Sox") == "White Sox"
    assert nickname("Toronto Blue Jays") == "Blue Jays"
    assert nickname("New York Yankees") == "Yankees"


def test_match_by_yes_sub_title():
    market = {"title": "Yankees at Braves Winner?", "yes_sub_title": "New York Yankees",
              "close_time": "2026-07-20T02:30:00Z"}
    m = match_market(market, _games())
    assert isinstance(m, MarketMatch)
    assert m.game_id == "g1"
    assert m.outcome == "New York Yankees"


def test_match_by_title_pattern():
    market = {"title": "Will the Braves beat the Yankees?",
              "close_time": "2026-07-20T02:30:00Z"}
    m = match_market(market, _games())
    assert m is not None and m.outcome == "Atlanta Braves"


def test_red_sox_vs_white_sox_not_confused():
    market = {"title": "White Sox at Red Sox Winner?", "yes_sub_title": "Red Sox",
              "close_time": "2026-07-20T01:30:00Z"}
    m = match_market(market, _games())
    assert m is not None
    assert m.game_id == "g2"
    assert m.outcome == "Boston Red Sox"


def test_ambiguous_side_is_refused():
    market = {"title": "Yankees at Braves Winner?", "yes_sub_title": "",
              "close_time": "2026-07-20T02:30:00Z"}
    assert match_market(market, _games()) is None


def test_doubleheader_picks_closest_start_time():
    games = {
        "early": {"home_team": "Atlanta Braves", "away_team": "New York Yankees",
                  "commence_time": "2026-07-19T17:10:00Z",
                  "probs": {"Atlanta Braves": 0.5, "New York Yankees": 0.5}},
        "late": {"home_team": "Atlanta Braves", "away_team": "New York Yankees",
                 "commence_time": "2026-07-19T23:10:00Z",
                 "probs": {"Atlanta Braves": 0.5, "New York Yankees": 0.5}},
    }
    market = {"title": "Yankees at Braves Winner?", "yes_sub_title": "Yankees",
              "close_time": "2026-07-20T02:30:00Z"}  # ~3.3h after the late start
    m = match_market(market, games)
    assert m is not None and m.game_id == "late"


def test_unrelated_game_is_not_matched():
    market = {"title": "Dodgers at Padres Winner?", "yes_sub_title": "Dodgers",
              "close_time": "2026-07-20T05:00:00Z"}
    assert match_market(market, _games()) is None


# -- ledger -------------------------------------------------------------------------

def test_ledger_open_and_settle_win():
    ledger = PaperLedger(bankroll=1000.0)
    pos = ledger.open("T1", "E1", "yes", 0.55, 20, 0.60, 0.03,
                      "Yankees @ Braves", "2026-07-19T20:00:00Z")
    assert pos is not None
    # cost = 0.55*20 = 11.00, fee = ceil(0.07*0.55*0.45*100)/100 = 0.02/contract -> 0.40
    assert abs(ledger.bankroll - (1000.0 - 11.0 - pos.fees)) < 1e-9
    settled = ledger.settle("T1", "yes")
    assert settled is not None
    assert settled.payout == 20.0
    assert abs(settled.pnl - (20.0 - 11.0 - pos.fees)) < 1e-9
    assert ledger.wins == 1 and ledger.settled_count == 1


def test_ledger_settle_loss_and_no_double_position_per_event():
    ledger = PaperLedger(bankroll=100.0)
    assert ledger.open("T1", "E1", "no", 0.40, 10, 0.55, 0.03, "g", "t") is not None
    # second market of the same event must be refused
    assert not ledger.can_open("T2", "E1", max_positions=10)
    settled = ledger.settle("T1", "yes")  # we held NO; YES resolved -> loss
    assert settled is not None and settled.payout == 0.0 and settled.pnl < 0
    assert ledger.wins == 0 and ledger.settled_count == 1


def test_ledger_round_trips_through_dict():
    ledger = PaperLedger(bankroll=500.0)
    ledger.open("T1", "E1", "yes", 0.50, 5, 0.55, 0.02, "g", "t")
    restored = PaperLedger.from_dict(ledger.to_dict())
    assert restored.bankroll == ledger.bankroll
    assert "T1" in restored.positions
    assert restored.positions["T1"].contracts == 5
