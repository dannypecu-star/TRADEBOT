"""Riskless-basket detection for mutually exclusive Kalshi events.

In an event whose markets are mutually exclusive (Kalshi exposes this as the event's
``mutually_exclusive`` flag), at most one market settles YES. That structure alone
creates two candidate baskets:

* **NO basket** -- buy 1 NO of every market. At most one NO loses, so the payout is at
  least $(N-1) guaranteed. Profitable when sum(no_asks) + fees < N-1. This is the
  strictly safe construction: it needs nothing beyond mutual exclusivity.
* **YES basket** -- buy 1 YES of every market for a $1 payout. This additionally
  requires the buckets to be *exhaustive* (some bucket must win). Kalshi's API does
  not expose exhaustiveness, so YES-basket findings are flagged for manual
  verification of the event's rules rather than treated as riskless.

Everything here is pure computation on API payload dicts so it is unit-testable
offline. Prices are integer cents as Kalshi returns them; results are in dollars.

Fill realism: quoted asks say nothing about size. ``implied_ask_and_size`` derives the
actual best ask and its depth from the orderbook (an ask on one side is a resting bid
on the other at the complementary price), so a detected opportunity can be re-checked
against what could really fill.
"""
from __future__ import annotations

from typing import Optional

from .economics import fee_per_contract

CLOSED_STATUSES = ("closed", "settled", "finalized", "determined")


def _valid_cents(value) -> bool:
    try:
        return value is not None and 1 <= int(value) <= 99
    except (TypeError, ValueError):
        return False


def _open_markets(event: dict) -> list[dict]:
    return [m for m in (event.get("markets") or [])
            if (m.get("status") or "active") not in CLOSED_STATUSES]


def basket_cost(prices_dollars: list[float], fee_rate: float = 0.07) -> float:
    """Total cost of one contract per leg at the given prices, fees included."""
    return sum(prices_dollars) + sum(fee_per_contract(p, fee_rate) for p in prices_dollars)


def find_opportunities(
    event: dict,
    fee_rate: float = 0.07,
    min_profit: float = 0.01,
    max_legs: int = 15,
) -> list[dict]:
    """Return riskless-basket opportunities in one event (empty list if none).

    Each opportunity dict has: type ("YES_BASKET"/"NO_BASKET"), event_ticker, title,
    legs, tickers, sum_asks, fees, profit (guaranteed $ per basket), and for YES
    baskets a ``caveat`` noting the exhaustiveness requirement.
    """
    if not event.get("mutually_exclusive"):
        return []
    markets = _open_markets(event)
    n = len(markets)
    if n < 2 or n > max_legs:
        return []

    tickers = [m.get("ticker") or "" for m in markets]
    opportunities: list[dict] = []

    yes_cents = [m.get("yes_ask") for m in markets]
    if all(_valid_cents(c) for c in yes_cents):
        prices = [int(c) / 100.0 for c in yes_cents]
        cost = basket_cost(prices, fee_rate)
        profit = 1.0 - cost
        if profit >= min_profit:
            opportunities.append({
                "type": "YES_BASKET",
                "event_ticker": event.get("event_ticker") or "",
                "title": event.get("title") or "",
                "legs": n,
                "tickers": tickers,
                "sum_asks": round(sum(prices), 4),
                "fees": round(cost - sum(prices), 4),
                "profit": round(profit, 4),
                "caveat": "requires exhaustive buckets - verify event rules",
            })

    no_cents = [m.get("no_ask") for m in markets]
    if all(_valid_cents(c) for c in no_cents):
        prices = [int(c) / 100.0 for c in no_cents]
        cost = basket_cost(prices, fee_rate)
        profit = (n - 1) - cost
        if profit >= min_profit:
            opportunities.append({
                "type": "NO_BASKET",
                "event_ticker": event.get("event_ticker") or "",
                "title": event.get("title") or "",
                "legs": n,
                "tickers": tickers,
                "sum_asks": round(sum(prices), 4),
                "fees": round(cost - sum(prices), 4),
                "profit": round(profit, 4),
                "caveat": "",
            })

    return opportunities


def implied_ask_and_size(orderbook: dict, side: str) -> tuple[Optional[float], int]:
    """Best real ask (dollars) and its size for ``side``, derived from the orderbook.

    Kalshi orderbooks list resting *bids* for "yes" and "no" as ``[price_cents,
    count]`` levels in ascending price order. Buying ``side`` at the ask crosses the
    best bid on the *opposite* side at the complementary price: ask = 1 - best
    opposite bid. Returns (None, 0) when the opposite book is empty.
    """
    opposite = "no" if side == "yes" else "yes"
    levels = (orderbook or {}).get(opposite) or []
    if not levels:
        return None, 0
    best = levels[-1]  # ascending order -> last level is the best (highest) bid
    try:
        price_cents, count = int(best[0]), int(best[1])
    except (TypeError, ValueError, IndexError):
        return None, 0
    if not 1 <= price_cents <= 99:
        return None, 0
    return (100 - price_cents) / 100.0, count


def max_baskets_from_orderbooks(
    orderbooks: dict[str, dict], tickers: list[str], side: str
) -> tuple[Optional[int], dict[str, tuple[Optional[float], int]]]:
    """How many full baskets the books can actually fill (min depth across legs).

    Returns (max_baskets, {ticker: (implied_ask, size)}). ``max_baskets`` is None if
    any leg's book was unavailable -- unknown is reported as unknown, not as zero.
    """
    detail: dict[str, tuple[Optional[float], int]] = {}
    sizes: list[int] = []
    unknown = False
    for ticker in tickers:
        ob = orderbooks.get(ticker)
        if ob is None:
            detail[ticker] = (None, 0)
            unknown = True
            continue
        ask, size = implied_ask_and_size(ob, side)
        detail[ticker] = (ask, size)
        if ask is None:
            unknown = True
        else:
            sizes.append(size)
    if unknown or not sizes:
        return None, detail
    return min(sizes), detail
