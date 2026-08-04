#!/usr/bin/env python3
"""Scan for cross-exchange arbitrage on one symbol across several venues.

Fetches live best bid/ask from each exchange (public endpoints, no keys) and reports the
spread *net of estimated fees and slippage*. Only a positive net edge is a real
opportunity — and for a retail participant that is rare, which this tool will show you
honestly. It never places orders; it is a monitor and a reality check.

Examples:
    python scripts/scan_arbitrage.py
    python scripts/scan_arbitrage.py --symbol ETH/USDT --exchanges binance kraken coinbase
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.arbitrage.cross_exchange import fetch_quotes, find_opportunities
from src.utils.config import default_config_path, load_config


def main() -> None:
    cfg = load_config(default_config_path())
    arb = cfg.get("arbitrage", {})
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=arb.get("cross_exchange_symbol", "BTC/USDT"))
    parser.add_argument("--exchanges", nargs="+",
                        default=arb.get("exchanges", ["binance", "kraken", "coinbase"]))
    parser.add_argument("--taker-fee", type=float, default=cfg["execution"].fee_rate)
    parser.add_argument("--slippage", type=float, default=cfg["execution"].slippage)
    args = parser.parse_args()

    print(f"Scanning {args.symbol} across {', '.join(args.exchanges)} ...")
    quotes = fetch_quotes(args.symbol, args.exchanges)
    if not quotes:
        print("No quotes returned (network blocked, or symbol not listed anywhere).")
        return

    print("\nVenue quotes:")
    for q in quotes:
        print(f"  {q.exchange:<12} bid={q.bid:,.2f}  ask={q.ask:,.2f}")

    opps = find_opportunities(args.symbol, quotes, args.taker_fee, args.slippage)
    print(f"\nTop pairs by net edge (round-trip cost "
          f"= {(2*args.taker_fee + 2*args.slippage)*1e4:.1f} bps):")
    print(f"  {'buy@':<12}{'sell@':<12}{'gross bps':>10}{'net bps':>10}   verdict")
    for o in opps[:10]:
        verdict = "PROFITABLE" if o.profitable else "no edge"
        print(f"  {o.buy_exchange:<12}{o.sell_exchange:<12}"
              f"{o.gross_edge_bps:>10.1f}{o.net_edge_bps:>10.1f}   {verdict}")

    best = opps[0] if opps else None
    if best and best.profitable:
        print(f"\n>>> Net-positive spread found: buy {args.symbol} on {best.buy_exchange}, "
              f"sell on {best.sell_exchange} ({best.net_edge_bps:.1f} bps net).")
        print("    Executing this for real also needs pre-funded balances on BOTH venues,")
        print("    latency budgeting, and transfer risk. This tool does not place orders.")
    else:
        print("\nNo net-positive opportunity right now. This is the normal, expected result")
        print("for a retail participant — venue spreads rarely exceed round-trip costs.")


if __name__ == "__main__":
    main()
