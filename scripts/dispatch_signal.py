#!/usr/bin/env python3
"""Compute the latest strategy signal and dispatch it to a trading platform.

This is the glue that lets the strategies in this repo drive **3Commas**, **Cryptohopper**,
or **MetaTrader 5**. It:

  1. pulls recent candles,
  2. runs the selected strategy to get the last two bars' target positions,
  3. diffs them into a discrete action (enter_long / exit / hold), and
  4. hands that action to the chosen platform adapter.

**Dry-run by default.** Nothing is transmitted unless you pass ``--live``, and the hosted
platforms additionally require their secrets in the environment (see each adapter). Run it
on a schedule (cron / systemd timer, one run per bar close) to keep a hosted bot in sync
with the strategy.

Examples:
    python scripts/dispatch_signal.py --platform 3commas --strategy trend_following
    python scripts/dispatch_signal.py --platform cryptohopper --synthetic
    python scripts/dispatch_signal.py --platform mt5 --live       # sends for real
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data.loader import fetch_ohlcv, synthetic_ohlcv
from src.platforms.base import signal_from_position
from src.platforms.cryptohopper import CryptohopperAdapter
from src.platforms.mt5_adapter import MT5Adapter
from src.platforms.threecommas import ThreeCommasAdapter
from src.strategies.registry import available, build_strategy
from src.utils.config import default_config_path, load_config

ADAPTERS = {
    "3commas": ThreeCommasAdapter,
    "cryptohopper": CryptohopperAdapter,
    "mt5": MT5Adapter,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=sorted(ADAPTERS))
    parser.add_argument("--config", default=default_config_path())
    parser.add_argument("--strategy", default=None,
                        help=f"override config strategy: {', '.join(available())}")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--live", action="store_true",
                        help="actually transmit (default is dry-run)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    d = cfg["data"]
    if args.synthetic:
        df = synthetic_ohlcv(n=max(500, d["limit"]), timeframe=d["timeframe"])
    else:
        df = fetch_ohlcv(d["symbol"], d["timeframe"], d["exchange"], d["limit"])

    strategy = build_strategy(args.strategy or cfg["strategy_name"], cfg["strategy_params"])
    positions = strategy.generate_signals(df)
    if len(positions) < 2:
        print("not enough data to form a signal")
        return

    prev_pos = float(positions.iloc[-2])
    target_pos = float(positions.iloc[-1])
    last_price = float(df["close"].iloc[-1])
    signal = signal_from_position(
        symbol=d["symbol"], prev_position=prev_pos, target_position=target_pos,
        price=last_price, strategy=strategy.name,
    )

    adapter = ADAPTERS[args.platform](dry_run=not args.live)
    result = adapter.send(signal)

    print(json.dumps({
        "strategy": strategy.name,
        "symbol": d["symbol"],
        "prev_position": prev_pos,
        "target_position": target_pos,
        "action": signal.action,
        "price": last_price,
        "platform_result": result,
    }, indent=2, default=str))

    if not args.live and signal.action != "hold":
        print("\n[dry-run] nothing was transmitted. Re-run with --live (and the platform's "
              "secrets set in the environment) to send this for real.")


if __name__ == "__main__":
    main()
