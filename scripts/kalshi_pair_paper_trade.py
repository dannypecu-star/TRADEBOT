#!/usr/bin/env python3
"""Run the BTC/ETH inverse-pair paper-trading strategy on a synthetic replay feed.

This is the offline, runnable equivalent of the AutoHotkey bot: same strategy (inverse
pair EntryRange, SYNC behavior gate, shot scaling with cooldown, stop-loss, hold to
resolution), driven by :mod:`src.kalshi.pair_sim` instead of the live Kalshi API. It
terminates and prints a trade trace plus a P/L summary -- no network, no infinite loop.

    python scripts/kalshi_pair_paper_trade.py                 # default 8 sessions
    python scripts/kalshi_pair_paper_trade.py --sessions 20 --seed 3
    python scripts/kalshi_pair_paper_trade.py --quiet         # summary only
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.pair_sim import SimConfig, simulate
from src.kalshi.pair_strategy import PairConfig, PairTrader, fmt_money, fmt_signed_money


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sessions", type=int, default=8, help="number of 15m markets to replay")
    p.add_argument("--seed", type=int, default=7, help="RNG seed for the synthetic feed")
    p.add_argument("--balance", type=float, default=10000.0, help="starting paper balance")
    p.add_argument("--quiet", action="store_true", help="print only the final summary")
    args = p.parse_args()

    cfg = PairConfig(initial_balance=args.balance)
    trader = PairTrader(cfg, logger=(None if args.quiet else print))

    print(
        f"Paper mode | Balance {fmt_money(cfg.initial_balance)} | "
        f"EntryRange {cfg.entry_min():g}-{cfg.entry_max():g} | Exit < {cfg.effective_exit():g}"
        f"{cfg.exit_note()} | Shots {cfg.shots} x {cfg.order_size} | "
        f"Cooldown {cfg.cooldown_sec}s | Assets BTC/ETH"
    )

    sim = SimConfig(sessions=args.sessions, seed=args.seed)
    for t in simulate(sim):
        trader.on_tick(
            t.btc, t.eth, t.btc_spot, t.eth_spot, t.now_ms, t.btc_open, t.eth_open
        )

    s = trader.summary()
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Markets participated : {s['markets_participated']}")
    print(f"  Sessions completed   : {s['completed_sessions']}")
    print(f"  Wins / Losses / Even : {s['wins']} / {s['losses']} / {s['breakevens']}")
    print(f"  Stop-losses          : {s['stop_losses']}")
    print(f"  Final cash           : {fmt_money(s['cash'])}")
    print(f"  Final equity         : {fmt_money(s['equity'])}")
    print(f"  Net P/L              : {fmt_signed_money(s['pnl'])}")


if __name__ == "__main__":
    main()
