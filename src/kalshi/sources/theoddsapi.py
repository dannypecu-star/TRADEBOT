"""Probability source backed by The Odds API (the-odds-api.com).

Two halves:
  * ``TheOddsAPIClient.fetch_odds`` -- the live network call (runs in your environment;
    needs a free API key in THE_ODDS_API_KEY).
  * ``fair_probabilities_from_payload`` + ``SportsbookProbabilitySource`` -- pure data
    processing: devig each book, build a consensus fair probability per outcome, and
    expose it through the ``ProbabilitySource`` interface the strategy consumes. This
    half is fully unit-tested offline with a sample payload.

The genuinely fiddly real-world step is mapping a Kalshi market ticker to the right
game + outcome here (team-name normalization, matching by date). That mapping is kept
explicit in ``ticker_map`` rather than guessed, so it is auditable.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from typing import Optional

from ..odds import consensus, devig_american, weighted_consensus

_CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "cache")
)


def _cache_file(sport: str, regions: str, markets: str) -> str:
    return os.path.join(_CACHE_DIR, f"odds_{sport}_{regions}_{markets}.json")


def _read_cache(path: str, ttl_seconds: float) -> Optional[list[dict]]:
    """Return cached payload if the file exists and is younger than ``ttl_seconds``."""
    if ttl_seconds <= 0 or not os.path.exists(path):
        return None
    if time.time() - os.path.getmtime(path) > ttl_seconds:
        return None
    with open(path, "r") as fh:
        return json.load(fh)


def _write_cache(path: str, data: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh)


class TheOddsAPIClient:
    """Client for The Odds API. Keep to 1 market x 1 region to spend 1 credit per call.

    Caching (``cache_ttl`` seconds) means repeated checks within the window reuse the
    last payload instead of spending another credit -- the simplest way to stay inside
    the free 500-credit/month tier. ``credits_remaining`` is populated from the API's
    response headers after each live call so you always know your budget.
    """

    BASE = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: Optional[str] = None, cache_ttl: float = 300.0):
        self.api_key = api_key or os.environ.get("THE_ODDS_API_KEY")
        self.cache_ttl = cache_ttl
        self.credits_remaining: Optional[int] = None
        self.credits_used: Optional[int] = None

    def fetch_odds(
        self,
        sport: str = "basketball_nba",
        regions: str = "us",
        markets: str = "h2h",
        odds_format: str = "american",
    ) -> list[dict]:
        """Fetch current odds for a sport. Returns The Odds API's list-of-games JSON.

        Serves a fresh cached payload without spending a credit when possible.
        """
        cache_path = _cache_file(sport, regions, markets)
        cached = _read_cache(cache_path, self.cache_ttl)
        if cached is not None:
            return cached

        if not self.api_key:
            raise RuntimeError("Set THE_ODDS_API_KEY (free key from the-odds-api.com).")
        import requests

        url = f"{self.BASE}/sports/{sport}/odds"
        resp = requests.get(
            url,
            params={
                "apiKey": self.api_key,
                "regions": regions,
                "markets": markets,
                "oddsFormat": odds_format,
            },
            timeout=30,
        )
        resp.raise_for_status()
        # The Odds API reports quota usage in response headers.
        rem = resp.headers.get("x-requests-remaining")
        used = resp.headers.get("x-requests-used")
        self.credits_remaining = int(rem) if rem is not None else None
        self.credits_used = int(used) if used is not None else None

        data = resp.json()
        _write_cache(cache_path, data)
        return data


def fair_probabilities_from_payload(
    payload: list[dict],
    book_weights: Optional[dict[str, float]] = None,
) -> dict[str, dict]:
    """Turn The Odds API payload into a devigged consensus probability per outcome.

    Returns ``{game_id: {"home_team", "away_team", "commence_time", "probs": {name: p}}}``
    where each ``p`` is the consensus fair (devigged) probability across books.
    """
    result: dict[str, dict] = {}
    for game in payload:
        per_outcome: dict[str, dict[str, float]] = defaultdict(dict)
        for bm in game.get("bookmakers", []):
            book = bm.get("key", "unknown")
            h2h = next((m for m in bm.get("markets", []) if m.get("key") == "h2h"), None)
            if not h2h or len(h2h.get("outcomes", [])) < 2:
                continue
            names = [o["name"] for o in h2h["outcomes"]]
            probs = devig_american([o["price"] for o in h2h["outcomes"]])
            for name, p in zip(names, probs):
                per_outcome[name][book] = p

        game_probs = {}
        for name, books in per_outcome.items():
            game_probs[name] = (
                weighted_consensus(books, book_weights) if book_weights
                else consensus(list(books.values()))
            )
        result[game["id"]] = {
            "home_team": game.get("home_team"),
            "away_team": game.get("away_team"),
            "commence_time": game.get("commence_time"),
            "probs": game_probs,
        }
    return result


class SportsbookProbabilitySource:
    """A ``ProbabilitySource`` (see strategy.py) driven by devigged sportsbook odds.

    ``ticker_map`` maps each Kalshi market ticker to ``(game_id, outcome_name)`` so we
    know which game and side a contract corresponds to. Build it once per slate.
    """

    def __init__(self, fair_by_game: dict[str, dict], ticker_map: dict[str, tuple[str, str]]):
        self.fair_by_game = fair_by_game
        self.ticker_map = ticker_map

    def fair_probability(self, market_ticker: str) -> float | None:
        entry = self.ticker_map.get(market_ticker)
        if entry is None:
            return None
        game_id, outcome = entry
        game = self.fair_by_game.get(game_id)
        if game is None:
            return None
        return game["probs"].get(outcome)
