"""Sportsbook odds -> fair probability.

The whole point of this module is to turn quoted sportsbook odds into a devigged
probability we can compare against a Kalshi price. It has no network dependency and is
fully unit-tested, because this math is the foundation of the sports edge and must be
exactly right.

Glossary
--------
* American odds : -150 means bet 150 to win 100; +130 means bet 100 to win 130.
* Decimal odds  : total return per 1 unit staked (incl. stake). -150 -> 1.667.
* Implied prob  : 1 / decimal. Includes the book's margin ("vig"), so a game's two
                  sides sum to > 1.
* Devig         : strip the margin so the probabilities sum to 1 (a fair estimate).
"""
from __future__ import annotations

from typing import Iterable


def american_to_decimal(american: float) -> float:
    if american == 0:
        raise ValueError("American odds cannot be 0")
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def decimal_to_implied(decimal: float) -> float:
    if decimal <= 1.0:
        raise ValueError("Decimal odds must be > 1.0")
    return 1.0 / decimal


def american_to_implied(american: float) -> float:
    return decimal_to_implied(american_to_decimal(american))


def devig(raw_probs: Iterable[float]) -> list[float]:
    """Remove the book margin by proportional (multiplicative) normalization.

    This is the standard, simplest devig: divide each raw implied probability by their
    sum so they total 1. Works for two-way (moneyline) or multi-way (e.g. 3-way soccer)
    markets alike. Other methods exist (additive, power, Shin); proportional is the
    sensible default and the one to start with.
    """
    probs = [float(p) for p in raw_probs]
    total = sum(probs)
    if total <= 0:
        raise ValueError("raw probabilities must sum to a positive number")
    return [p / total for p in probs]


def devig_american(odds: Iterable[float]) -> list[float]:
    """Convenience: devig directly from a list of American odds for one market."""
    return devig([american_to_implied(o) for o in odds])


def consensus(book_probs: Iterable[float]) -> float:
    """Combine several books' devigged probabilities into one estimate (mean).

    Averaging across books reduces single-book quirks. If you have access to a sharp
    book (e.g. Pinnacle), weighting it more heavily is a reasonable refinement -- see
    ``weighted_consensus``.
    """
    vals = [float(p) for p in book_probs]
    if not vals:
        raise ValueError("need at least one book probability")
    return sum(vals) / len(vals)


def weighted_consensus(book_probs: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted average of devigged probabilities, keyed by book name.

    Books missing from ``weights`` get weight 1.0. Use this to lean on sharper books.
    """
    num = 0.0
    den = 0.0
    for book, p in book_probs.items():
        w = weights.get(book, 1.0)
        num += w * p
        den += w
    if den <= 0:
        raise ValueError("weights must sum to a positive number")
    return num / den
