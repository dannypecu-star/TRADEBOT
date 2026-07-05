#!/usr/bin/env python3
"""Walk-forward / out-of-sample validation.

A single backtest number is nearly meaningless -- it is trivial to find parameters
that look great on one slice of history and fall apart on the next. This script cuts
the history into sequential folds and reports each fold *independently*, so you can
see whether the edge is consistent across time or just a lucky window.

Read the per-fold table, not the average. A strategy that wins 6/6 folds modestly is
far more trustworthy than one that wins overall because a single fold went parabolic.

Examples:
    python scripts/validate.py --synthetic --folds 6
    python scripts/validate.py --folds 5
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.backtest.engine import run_backtest
from src.data.loader import fetch_ohlcv, synthetic_ohlcv
from src.risk.manager import RiskManager
from src.strategies.trend_momentum import TrendMomentum
from src.utils.config import default_config_path, load_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=default_config_path())
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--folds", type=int, default=5)
    args = parser.parse_args()

    cfg = load_config(args.config)
    d = cfg["data"]
    if args.synthetic:
        df = synthetic_ohlcv(n=d["limit"], timeframe=d["timeframe"])
        label = "SYNTHETIC"
    else:
        df = fetch_ohlcv(d["symbol"], d["timeframe"], d["exchange"], d["limit"])
        label = f"{d['symbol']} @ {d['exchange']}"

    fold_size = len(df) // args.folds
    line = "-" * 60
    print(line)
    print(f" Walk-forward validation  |  {label}  |  {args.folds} folds")
    print(line)
    print(f" {'fold':<6}{'period':<26}{'strategy':>12}{'buy&hold':>14}")
    print(line)

    wins = 0
    for k in range(args.folds):
        start = k * fold_size
        end = len(df) if k == args.folds - 1 else (k + 1) * fold_size
        chunk = df.iloc[start:end]
        if len(chunk) < 100:
            continue
        strategy = TrendMomentum(**cfg["strategy"])
        risk = RiskManager(cfg["risk"])
        res = run_backtest(chunk, strategy, risk, cfg["execution"])
        s_ret = res.metrics.total_return
        b_ret = res.benchmark_metrics.total_return
        beat = s_ret > b_ret
        wins += beat
        period = f"{chunk.index[0].date()}..{chunk.index[-1].date()}"
        flag = "  <" if beat else ""
        print(f" {k + 1:<6}{period:<26}{s_ret * 100:>11.1f}%{b_ret * 100:>13.1f}%{flag}")

    print(line)
    print(f" strategy beat buy & hold in {wins}/{args.folds} folds")
    print(line)
    print(" Reminder: consistent, modest outperformance across folds is the goal.")
    print(" One giant winning fold masking several losers is a red flag, not a win.")


if __name__ == "__main__":
    main()
