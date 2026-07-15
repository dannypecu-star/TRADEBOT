#!/usr/bin/env python3
"""Analyze a NightShark (or paper) trade log and report whether the fair-value model
actually has an edge.

NightShark writes ``nightshark_trades.csv`` in the same schema this reads (see
``nightshark/nightshark_fair_value.ahk`` and ``src/kalshi/trade_log.py``). Copy that
file off the Windows box and run:

    python scripts/kalshi_analyze_trades.py nightshark_trades.csv

The two numbers that decide everything:
  * realized_edge -- dollars/contract actually earned at resolution. Not reliably
    positive after fees? There is no edge, and no sizing fixes that.
  * calibration   -- predicted vs realized frequency per probability bucket. If the
    model says 70% but those settle 50%, it is overconfident; fix the model.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.trade_log import summarize


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", help="path to the trade log (e.g. nightshark_trades.csv)")
    p.add_argument("--buckets", type=int, default=5)
    args = p.parse_args()

    if not os.path.exists(args.csv):
        p.error(f"no such file: {args.csv}")

    s = summarize(args.csv, n_buckets=args.buckets)
    if s.get("n_trades", 0) == 0:
        print(f"\n{s.get('note', 'no data')}. "
              f"Early exits so far: {s.get('n_exits', 0)} (P&L ${s.get('exit_pnl', 0):+.2f}).\n")
        return

    print(f"\nTrade log: {args.csv}\n")
    print(f"  Resolutions (model test) : {s['n_trades']}   contracts {s['total_contracts']}")
    print(f"  Hit rate                 : {s['hit_rate']:.1%}")
    print(f"  Predicted edge           : {s['predicted_edge']:+.3f} /contract")
    print(f"  Realized edge            : {s['realized_edge']:+.3f} /contract   <-- must be > 0 after fees")
    print(f"  Settled P&L              : ${s['settled_pnl']:+,.2f}")
    print(f"  Early exits              : {s['n_exits']}   P&L ${s['exit_pnl']:+,.2f}")
    print(f"  TOTAL realized P&L       : ${s['total_pnl']:+,.2f}")
    print("\n  Calibration (predicted vs realized frequency):")
    print("    bucket     n     predicted   realized")
    for row in s["calibration"]:
        flag = "" if abs(row["predicted"] - row["realized"]) < 0.05 else "  <-- off"
        print(f"    {row['bucket']:<9} {row['n']:<5} {row['predicted']:>8.1%}   {row['realized']:>8.1%}{flag}")
    print("\n  Edge is real only if realized_edge > 0 AND predicted ~ realized in every")
    print("  bucket with enough samples. Anything else means keep validating, not funding.\n")


if __name__ == "__main__":
    main()
