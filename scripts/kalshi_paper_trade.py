#!/usr/bin/env python3
"""Run one pass of the Kalshi paper-trading loop on the DEMO sandbox.

Start here to validate the whole loop with no external odds API. Provide your own fair
probabilities in a JSON file and the trader will value live demo markets against them.

    export KALSHI_KEY_ID=...
    export KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem

    # 1) dry run: logs the orders it WOULD place, sends nothing (default, safest)
    python scripts/kalshi_paper_trade.py --probs my_probs.json

    # 2) actually place demo orders (fake money) once you trust the dry-run output
    python scripts/kalshi_paper_trade.py --probs my_probs.json --live

my_probs.json looks like:  {"KXNBA-25JUL10-LAL": 0.62, "KXMLB-25JUL10-NYY": 0.55}
Get real open-market tickers by running scripts/kalshi_smoke_test.py first.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.economics import SizingConfig
from src.kalshi.paper import LiveGate
from src.kalshi.sources.manual import ManualProbabilitySource
from src.kalshi.trader import PaperTrader, RiskLimits


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--probs", required=True, help="JSON file of {ticker: fair_probability}")
    p.add_argument("--live", action="store_true",
                   help="actually place demo orders (default is a dry run)")
    p.add_argument("--max-positions", type=int, default=10)
    p.add_argument("--max-daily-loss", type=float, default=0.10)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Demo sandbox only. This script never touches PROD.
    gate = LiveGate(enabled=True, env="demo")
    client = gate.client()

    source = ManualProbabilitySource.from_json(args.probs)
    limits = RiskLimits(
        max_open_positions=args.max_positions,
        max_daily_loss_fraction=args.max_daily_loss,
        dry_run=not args.live,
    )
    trader = PaperTrader(client, source, SizingConfig(), limits)

    mode = "LIVE-DEMO (placing orders)" if args.live else "DRY-RUN (no orders sent)"
    print(f"\nKalshi paper trade  |  {mode}  |  {len(source.probabilities)} tickers watched\n")
    state = trader.run_once()

    placed = sum(o.placed for o in state.orders)
    print(f"\nStart balance: ${state.start_balance:,.2f}")
    print(f"Signals: {len(state.orders)}   Orders placed: {placed}")
    if not state.orders:
        print("No actionable edges this pass (or none of your tickers were open).")


if __name__ == "__main__":
    main()
