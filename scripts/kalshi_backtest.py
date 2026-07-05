#!/usr/bin/env python3
"""Backtest the Kalshi value strategy and print a report with a calibration table.

Runs fully offline on synthetic resolved markets so you can see the machinery work
before wiring a real probability source (sportsbook odds, a model, a forecast feed).

    python scripts/kalshi_backtest.py                 # skilled model (should profit)
    python scripts/kalshi_backtest.py --skill 0.0     # useless model (should NOT profit)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.backtest import run_value_backtest, synthetic_resolved_markets
from src.kalshi.economics import SizingConfig


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skill", type=float, default=0.7,
                   help="model skill 0..1 (1=perfect, 0=useless). Default 0.7")
    p.add_argument("--n", type=int, default=2000, help="number of markets")
    p.add_argument("--bankroll", type=float, default=1000.0)
    args = p.parse_args()

    markets = synthetic_resolved_markets(n=args.n, model_skill=args.skill)
    res = run_value_backtest(markets, start_bankroll=args.bankroll, sizing=SizingConfig())

    line = "-" * 56
    print(line)
    print(f" Kalshi value backtest  |  model skill = {args.skill:.2f}")
    print(line)
    print(f" {'start bankroll':<22}${res.start_bankroll:>12,.2f}")
    print(f" {'end bankroll':<22}${res.end_bankroll:>12,.2f}")
    print(f" {'total return':<22}{res.total_return * 100:>12.1f}%")
    print(f" {'trades taken':<22}{res.n_trades:>13}")
    print(f" {'hit rate':<22}{res.hit_rate * 100:>12.1f}%")
    print(f" {'fees paid':<22}${res.total_fees:>12,.2f}")
    print(f" {'predicted edge/contract':<22}${res.predicted_edge:>12,.3f}")
    print(f" {'realized edge/contract':<22}${res.realized_edge:>12,.3f}")
    print(line)
    print(" Calibration (does 'X%' predicted actually happen X% of the time?)")
    print(f" {'bucket':<12}{'n':>6}{'predicted':>12}{'realized':>12}")
    for row in res.calibration:
        print(f" {row['bucket']:<12}{row['n']:>6}{row['predicted'] * 100:>11.1f}%{row['realized'] * 100:>11.1f}%")
    print(line)
    if res.calibration:
        drift = max(abs(r["predicted"] - r["realized"]) for r in res.calibration)
        print(f" worst calibration gap: {drift * 100:.1f} percentage points")
        print(" (big gaps => the model is miscalibrated and profits are not trustworthy)")
    print(line)


if __name__ == "__main__":
    main()
