#!/usr/bin/env python3
"""Run a single backtest and print a report against buy-and-hold.

Examples:
    python scripts/run_backtest.py                 # live data from config
    python scripts/run_backtest.py --synthetic     # offline, no network needed
    python scripts/run_backtest.py --config my.yaml
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.backtest.engine import run_backtest
from src.data.loader import fetch_ohlcv, synthetic_ohlcv
from src.risk.manager import RiskManager
from src.strategies.registry import available, build_strategy
from src.utils.config import default_config_path, load_config


def _fmt_pct(x: float) -> str:
    return f"{x * 100:,.2f}%"


def print_report(result, strategy_name: str, symbol: str) -> None:
    m, b = result.metrics, result.benchmark_metrics
    line = "-" * 58
    print(line)
    print(f" Backtest report  |  {strategy_name}  |  {symbol}")
    print(line)
    print(f" {'metric':<20}{'strategy':>16}{'buy & hold':>18}")
    print(f" {'total return':<20}{_fmt_pct(m.total_return):>16}{_fmt_pct(b.total_return):>18}")
    print(f" {'CAGR':<20}{_fmt_pct(m.cagr):>16}{_fmt_pct(b.cagr):>18}")
    print(f" {'max drawdown':<20}{_fmt_pct(m.max_drawdown):>16}{_fmt_pct(b.max_drawdown):>18}")
    print(f" {'sharpe':<20}{m.sharpe:>16.2f}{b.sharpe:>18.2f}")
    print(f" {'sortino':<20}{m.sortino:>16.2f}{b.sortino:>18.2f}")
    print(f" {'calmar':<20}{m.calmar:>16.2f}{b.calmar:>18.2f}")
    print(f" {'volatility (ann.)':<20}{_fmt_pct(m.volatility):>16}{_fmt_pct(b.volatility):>18}")
    print(line)
    print(f" {'exposure':<20}{_fmt_pct(m.exposure):>16}")
    print(f" {'trades':<20}{m.n_trades:>16}")
    print(f" {'win rate':<20}{_fmt_pct(m.win_rate):>16}")
    pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
    print(f" {'profit factor':<20}{pf:>16}")
    print(line)
    verdict = "BEATS" if m.total_return > b.total_return else "TRAILS"
    risk_adj = "BEATS" if m.sharpe > b.sharpe else "TRAILS"
    print(f" vs buy & hold:   return {verdict},  risk-adjusted (sharpe) {risk_adj}")
    print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=default_config_path())
    parser.add_argument("--synthetic", action="store_true",
                        help="use offline synthetic data instead of the exchange")
    parser.add_argument("--strategy", default=None,
                        help=f"override the config strategy; one of: {', '.join(available())}")
    args = parser.parse_args()

    cfg = load_config(args.config)
    d = cfg["data"]

    if args.synthetic:
        df = synthetic_ohlcv(n=d["limit"], timeframe=d["timeframe"])
        symbol = f"SYNTHETIC ({d['timeframe']})"
    else:
        df = fetch_ohlcv(d["symbol"], d["timeframe"], d["exchange"], d["limit"])
        symbol = f"{d['symbol']} @ {d['exchange']}"

    strategy_name = args.strategy or cfg["strategy_name"]
    strategy = build_strategy(strategy_name, cfg["strategy_params"])
    risk = RiskManager(cfg["risk"])
    result = run_backtest(df, strategy, risk, cfg["execution"])

    print_report(result, strategy.name, symbol)


if __name__ == "__main__":
    main()
