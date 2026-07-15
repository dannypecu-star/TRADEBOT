#!/usr/bin/env python3
"""Offline paper simulation of the weather bot -- no network, no credentials.

Purpose
-------
See a full $100 weather-bot session end to end when you can't reach Kalshi or Open-Meteo
(locked-down network, no account yet). It drives the REAL bot code -- ``WeatherEdgeFinder``,
the fee/edge math, and fractional-Kelly sizing -- over a *synthetic* forecast and a
*synthetic* market book, then Monte-Carlo settles the day to show a P&L distribution.

    python scripts/kalshi_weather_paper_sim.py                 # $100, default city
    python scripts/kalshi_weather_paper_sim.py --bankroll 250 --center 78 --sd 2.0

*** SYNTHETIC -- read this ***
The forecast and the market prices are generated, not real. The only reason the bot shows
a positive expected P&L here is the built-in premise that the *forecast is more accurate
than the market's implied view* (``--market-bias`` / ``--market-extra`` make the market's
prices worse than the forecast; ``--reality-bias`` keeps the actual outcome near the
forecast). That premise is exactly what you must prove with real calibration before
believing any live number. This script demonstrates plumbing and sizing, not alpha.
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.economics import SizingConfig
from src.kalshi.sources.openmeteo import (
    TemperatureDistribution,
    market_strike,
    probability_for_strike,
)
from src.kalshi.weather import rank_opportunities


def _price_cents(prob: float, margin: float) -> int:
    """A market ask: fair prob inflated by the book's margin, clamped to 1..99c."""
    return max(1, min(99, round(100.0 * prob * (1.0 + margin))))


def build_book(center: float, sd: float, market_bias: float, market_extra: float,
               margin: float, spread: int) -> list[dict]:
    """A synthetic book whose implied forecast is deliberately worse than ours.

    The 'market' prices every band/threshold off a biased, noisier distribution, so the
    gaps the bot finds come from the market disagreeing with the (better) forecast.
    """
    market_view = TemperatureDistribution(
        samples=[center + market_bias], bandwidth=sd + market_extra
    )
    book: list[dict] = []
    # Temperature bands, 2F wide, spanning the plausible range.
    lo = int(round(center)) - 8
    for floor in range(lo, lo + 16, 2):
        cap = floor + 1
        p = market_view.prob_between(floor, cap)
        ask = _price_cents(p, margin)
        book.append(dict(
            ticker=f"KXHIGHNY-SIM-B{floor}T{cap}", strike_type="between",
            floor_strike=floor, cap_strike=cap, yes_ask=ask, yes_bid=max(1, ask - spread),
        ))
    # A couple of "or above" threshold markets.
    for thr in (int(round(center)) + 3, int(round(center)) + 6):
        p = market_view.prob_at_least(thr)
        ask = _price_cents(p, margin)
        book.append(dict(
            ticker=f"KXHIGHNY-SIM-T{thr}", strike_type="greater",
            floor_strike=thr, cap_strike=None, yes_ask=ask, yes_bid=max(1, ask - spread),
        ))
    for m in book:
        m["_station"] = "New York City (SYNTHETIC)"
        m["_target_day"] = "SIM"
    return book


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bankroll", type=float, default=100.0)
    p.add_argument("--center", type=float, default=90.0, help="forecast mean high (F)")
    p.add_argument("--sd", type=float, default=1.6, help="forecast ensemble spread (F)")
    p.add_argument("--members", type=int, default=31, help="synthetic ensemble members")
    p.add_argument("--market-bias", type=float, default=1.2,
                   help="how far the market's implied mean is off (F)")
    p.add_argument("--market-extra", type=float, default=1.5,
                   help="extra spread in the market's (worse) implied forecast (F)")
    p.add_argument("--reality-bias", type=float, default=0.3,
                   help="how far the actual outcome sits from the forecast mean (F)")
    p.add_argument("--margin", type=float, default=0.04, help="book margin baked into asks")
    p.add_argument("--spread", type=int, default=2, help="yes bid/ask spread in cents")
    p.add_argument("--trials", type=int, default=20000, help="Monte-Carlo settlement draws")
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    rng = random.Random(args.seed)
    sizing = SizingConfig()  # shipped defaults: quarter-Kelly, 5% cap, 2c min edge

    members = [round(rng.gauss(args.center, args.sd), 1) for _ in range(args.members)]
    fc = TemperatureDistribution(samples=members)

    print("\n*** SYNTHETIC paper simulation -- inputs are fabricated, not live data ***")
    print(f"Forecast: mean {fc.mean():.1f}F  sd {fc.stdev():.1f}F  ({len(members)} members)  "
          f"|  bankroll ${args.bankroll:,.0f}\n")

    book = build_book(args.center, args.sd, args.market_bias, args.market_extra,
                      args.margin, args.spread)
    opps = rank_opportunities(book, lambda m: fc, bankroll=args.bankroll, sizing=sizing)

    print("Paper trades the bot would place (ranked by net edge):")
    print("-" * 94)
    positions, stake = [], 0.0
    for o in opps:
        cost = o.contracts * o.market_price
        stake += cost
        positions.append(o)
        print(f"  {o.ticker:<22} {o.strike:>8}  {o.side.upper():3} @ {o.market_price:4.2f}  "
              f"x{o.contracts:<3d} = ${cost:6.2f}  | fair {o.fair_prob:5.1%}  edge ${o.edge:+.3f}/ct")
    print("-" * 94)
    print(f"Positions: {len(positions)}   Deployed: ${stake:.2f} of ${args.bankroll:.0f} "
          f"({stake/args.bankroll:.0%})   Cash left: ${args.bankroll - stake:.2f}")

    if not positions:
        print("\nNo edges cleared the threshold with these settings.")
        return

    # -- Monte-Carlo settlement: reality sits near the forecast, not the market ---
    strikes = {m["ticker"]: market_strike(m) for m in book}

    def settle_once() -> float:
        true_high = round(rng.gauss(args.center + args.reality_bias, args.sd + 1.0))
        pnl = 0.0
        for o in positions:
            st, floor, cap = strikes[o.ticker]
            yes = (floor <= true_high <= cap) if st == "between" else (true_high >= floor)
            won = yes if o.side == "yes" else (not yes)
            pnl += o.contracts * ((1.0 - o.market_price) if won else (-o.market_price))
        return pnl

    results = sorted(settle_once() for _ in range(args.trials))
    n = len(results)
    mean_pnl = statistics.fmean(results)
    p_profit = sum(1 for r in results if r > 0) / n
    print(f"\nMonte-Carlo settlement over {n:,} synthetic outcomes (reality != market view):")
    print(f"  expected P&L : ${mean_pnl:+.2f}  ({mean_pnl/args.bankroll:+.1%} of bankroll)")
    print(f"  P(profit)    : {p_profit:.0%}")
    print(f"  5th-95th pct : ${results[int(0.05*n)]:+.2f} ... ${results[int(0.95*n)]:+.2f}")
    print("\n[SYNTHETIC] The edge here is assumed, not measured. Prove calibration on real "
          "resolved markets before trusting any live number.\n")


if __name__ == "__main__":
    main()
