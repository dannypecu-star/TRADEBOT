"""Tests for the odds -> fair-probability pipeline (the sports edge foundation).

All offline. Verifies the odds math and that a sample sportsbook payload devigs into a
consensus probability the strategy can act on.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.odds import (american_to_decimal, american_to_implied, consensus,
                             devig, devig_american, weighted_consensus)
from src.kalshi.sources.theoddsapi import (SportsbookProbabilitySource,
                                           fair_probabilities_from_payload)
from src.kalshi.strategy import evaluate_market
from src.kalshi.economics import SizingConfig


def test_american_conversions():
    assert abs(american_to_decimal(100) - 2.0) < 1e-9
    assert abs(american_to_decimal(-150) - (1 + 100 / 150)) < 1e-9
    assert abs(american_to_decimal(130) - 2.30) < 1e-9
    # +100 is an even bet -> 50% implied before vig.
    assert abs(american_to_implied(100) - 0.5) < 1e-9


def test_devig_sums_to_one_and_strips_margin():
    # -110 / -110 is the classic "juice": each implies ~52.4%, summing to ~104.8%.
    raw = [american_to_implied(-110), american_to_implied(-110)]
    assert sum(raw) > 1.0  # there is a margin
    fair = devig(raw)
    assert abs(sum(fair) - 1.0) < 1e-9
    assert abs(fair[0] - 0.5) < 1e-9 and abs(fair[1] - 0.5) < 1e-9


def test_devig_asymmetric():
    fair = devig_american([-200, 170])  # favorite vs underdog
    assert abs(sum(fair) - 1.0) < 1e-9
    assert fair[0] > fair[1]  # the -200 favorite has the higher fair probability


def test_weighted_consensus_leans_on_sharp_book():
    probs = {"pinnacle": 0.60, "softbook": 0.50}
    plain = consensus(probs.values())
    sharp = weighted_consensus(probs, {"pinnacle": 3.0, "softbook": 1.0})
    assert abs(plain - 0.55) < 1e-9
    assert sharp > plain  # weighting the sharp (higher) book pulls the estimate up


SAMPLE_PAYLOAD = [
    {
        "id": "game1",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "commence_time": "2026-07-10T00:00:00Z",
        "bookmakers": [
            {"key": "pinnacle", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Lakers", "price": -150},
                    {"name": "Celtics", "price": 130},
                ]}
            ]},
            {"key": "draftkings", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Lakers", "price": -145},
                    {"name": "Celtics", "price": 125},
                ]}
            ]},
        ],
    }
]


def test_payload_devigs_to_consensus():
    fair = fair_probabilities_from_payload(SAMPLE_PAYLOAD)
    probs = fair["game1"]["probs"]
    # Two outcomes, consensus devigged, summing to ~1.
    assert set(probs) == {"Lakers", "Celtics"}
    assert abs(probs["Lakers"] + probs["Celtics"] - 1.0) < 1e-9
    assert probs["Lakers"] > 0.55  # a -150ish favorite


def test_odds_cache_read_write_and_ttl(tmp_path):
    from src.kalshi.sources.theoddsapi import _read_cache, _write_cache

    path = str(tmp_path / "odds.json")
    assert _read_cache(path, ttl_seconds=300) is None  # nothing cached yet
    _write_cache(path, SAMPLE_PAYLOAD)
    fresh = _read_cache(path, ttl_seconds=300)
    assert fresh is not None and fresh[0]["id"] == "game1"
    # A zero/expired TTL must force a miss so we don't serve stale odds.
    assert _read_cache(path, ttl_seconds=0) is None


def test_source_feeds_strategy():
    fair = fair_probabilities_from_payload(SAMPLE_PAYLOAD)
    ticker_map = {"KXNBA-LAL": ("game1", "Lakers")}
    source = SportsbookProbabilitySource(fair, ticker_map)
    p = source.fair_probability("KXNBA-LAL")
    assert p is not None
    # If Kalshi prices the Lakers well below their fair probability, we should buy Yes.
    sig = evaluate_market("KXNBA-LAL", yes_price=p - 0.10, fair_prob=p,
                          bankroll=1000, sizing=SizingConfig(min_edge=0.01))
    assert sig is not None and sig.side == "yes"
    # Unknown ticker returns None (no guess).
    assert source.fair_probability("NOPE") is None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
