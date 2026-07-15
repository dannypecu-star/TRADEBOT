#!/usr/bin/env python3
"""Hunt the best edges in Kalshi's weather markets using Open-Meteo forecasts.

What it does
------------
Reads Kalshi's live daily high-temperature markets (read-only, public data -- no
credentials, no orders), builds a calibrated high-temperature distribution for each city
from Open-Meteo's free ensemble forecast, and ranks every market by the net edge between
the forecast probability and the market's price. The fattest mispricings print at the top.

    # 1) just hunt: rank the best edges across all weather cities (safe, read-only)
    python scripts/kalshi_weather_bot.py

    # narrow to a couple of cities and demand at least 3 cents of net edge
    python scripts/kalshi_weather_bot.py --series KXHIGHNY KXHIGHCHI --min-edge 0.03

    # 2) paper-trade the edges on Kalshi's DEMO sandbox (fake money)
    #    needs demo credentials; dry-run by default (logs orders, sends nothing)
    export KALSHI_KEY_ID=...  KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem
    python scripts/kalshi_weather_bot.py --trade            # dry run on demo
    python scripts/kalshi_weather_bot.py --trade --live     # actually place demo orders

Notes
-----
* Market *reading* uses the public API (default --env prod, where the weather markets
  live). This never places an order.
* Trading is always routed to the DEMO sandbox through the same LiveGate the rest of the
  bot uses -- this script never sends a real-money order.
* Verify each city's resolution station in src/kalshi/sources/openmeteo.py against the
  market rules before trusting the numbers with real money.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.client import KalshiClient
from src.kalshi.economics import SizingConfig
from src.kalshi.paper import LiveGate
from src.kalshi.sources.openmeteo import (
    OpenMeteoEnsembleClient,
    STATIONS,
    WeatherProbabilitySource,
)
from src.kalshi.trader import PaperTrader, RiskLimits
from src.kalshi.weather import WeatherEdgeFinder


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--series", nargs="*", default=None,
                   help=f"series tickers to scan (default: all known: {', '.join(STATIONS)})")
    p.add_argument("--env", choices=["demo", "prod"], default="prod",
                   help="which API to READ markets from (read-only; default prod)")
    p.add_argument("--bankroll", type=float, default=1000.0,
                   help="bankroll in dollars used for Kelly sizing (default 1000)")
    p.add_argument("--min-edge", type=float, default=0.0,
                   help="only show markets with at least this net $ edge (default 0)")
    p.add_argument("--top", type=int, default=25, help="how many ranked edges to print")
    p.add_argument("--models", default="gfs_seamless",
                   help="Open-Meteo ensemble system (default gfs_seamless / GEFS)")
    p.add_argument("--trade", action="store_true",
                   help="paper-trade the found edges on the DEMO sandbox")
    p.add_argument("--live", action="store_true",
                   help="with --trade: actually place demo orders (default is a dry run)")
    p.add_argument("--max-positions", type=int, default=10)
    p.add_argument("--max-daily-loss", type=float, default=0.10)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    sizing = SizingConfig()
    read_client = KalshiClient(env=args.env)
    meteo = OpenMeteoEnsembleClient(models=args.models)
    finder = WeatherEdgeFinder(read_client, meteo, sizing=sizing)

    print(f"\nKalshi Weather Edge Hunter  |  reading {args.env}  |  "
          f"models={args.models}  |  bankroll=${args.bankroll:,.0f}\n" + "-" * 78)

    opps = finder.find(
        bankroll=args.bankroll,
        series_tickers=args.series,
        min_edge=args.min_edge,
    )

    if not opps:
        print("No positive-edge weather markets found this pass.")
        print("(Weather markets live on prod; try --env prod, widen --series, or lower --min-edge.)")
        return

    print(f"Top {min(args.top, len(opps))} of {len(opps)} edges (sorted by net $/contract):\n")
    for o in opps[: args.top]:
        print("  " + o.describe())

    if not args.trade:
        print("\nRe-run with --trade to paper-trade these on the demo sandbox.")
        return

    # -- paper trade the found edges on DEMO -----------------------------------
    print("\n" + "-" * 78)
    source = WeatherProbabilitySource()
    for o in opps:
        source.add(o.ticker, o.fair_prob)

    gate = LiveGate(enabled=True, env="demo")   # trading is demo-only, always
    trade_client = gate.client()
    limits = RiskLimits(
        max_open_positions=args.max_positions,
        max_daily_loss_fraction=args.max_daily_loss,
        dry_run=not args.live,
    )
    trader = PaperTrader(trade_client, source, sizing, limits)

    mode = "LIVE-DEMO (placing orders)" if args.live else "DRY-RUN (no orders sent)"
    print(f"Paper trading on demo  |  {mode}  |  {len(source.probabilities)} tickers\n")

    # Only feed the trader markets it can price (the ones we found edges on).
    tickers = set(source.probabilities)
    markets = [m for s in (args.series or list(STATIONS))
               for m in trade_client.get_markets(series_ticker=s, status="open").get("markets", [])
               if m.get("ticker") in tickers]
    state = trader.run_once(markets=markets)

    placed = sum(o.placed for o in state.orders)
    print(f"\nStart balance: ${state.start_balance:,.2f}")
    print(f"Signals: {len(state.orders)}   Orders placed: {placed}")
    if not state.orders:
        print("No actionable edges cleared the sizing threshold on the demo book.")


if __name__ == "__main__":
    main()
