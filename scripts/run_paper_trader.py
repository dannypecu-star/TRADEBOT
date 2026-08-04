#!/usr/bin/env python3
"""Run the paper-trading bot: real prices, fake money, honest track record.

Two modes:

  replay  -- walk historical data bar-by-bar in seconds to produce a simulated track
             record (great for demonstrating an edge; identical timing/costs to the
             backtester). Uses synthetic data by default, ``--real`` for exchange data.

  live    -- poll a public exchange on a schedule and paper-trade the latest closed bar,
             forever. Exposes /healthz and /metrics for monitoring. This is what you leave
             running for weeks before considering real capital.

Examples:
    python scripts/run_paper_trader.py replay --synthetic
    python scripts/run_paper_trader.py replay --real --strategy mean_reversion
    python scripts/run_paper_trader.py live   --strategy trend_following
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data.loader import fetch_ohlcv, regime_ohlcv, synthetic_ohlcv
from src.monitoring.health import HealthServer, HealthState
from src.monitoring.logging_setup import setup_logging
from src.paper.paper_trader import PaperConfig, PaperTrader
from src.risk.manager import RiskManager
from src.strategies.registry import available, build_strategy
from src.utils.config import default_config_path, load_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["replay", "live"])
    parser.add_argument("--config", default=default_config_path())
    parser.add_argument("--strategy", default=None,
                        help=f"override config strategy: {', '.join(available())}")
    parser.add_argument("--synthetic", action="store_true", help="replay: use synthetic GBM data")
    parser.add_argument("--real", action="store_true", help="replay: use exchange data")
    parser.add_argument("--regime", choices=["trend", "meanrevert"], default=None,
                        help="replay: use an idealized regime that contains a real edge")
    parser.add_argument("--iterations", type=int, default=None,
                        help="live: stop after N polls (default: run forever)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    d = cfg["data"]
    mon = cfg.get("monitoring", {})
    setup_logging(
        level=mon.get("log_level", "INFO"),
        log_file="logs/paper.log",
        json_console=bool(mon.get("json_console", False)),
    )

    strategy_name = args.strategy or cfg["strategy_name"]
    strategy = build_strategy(strategy_name, cfg["strategy_params"])
    risk = RiskManager(cfg["risk"])
    ex = cfg["execution"]
    paper_cfg = PaperConfig(
        initial_cash=ex.initial_cash,
        fee_rate=ex.fee_rate,
        slippage=ex.slippage,
        atr_period=ex.atr_period,
        poll_seconds={"1m": 60, "5m": 300, "15m": 900,
                      "1h": 3600, "4h": 14400, "1d": 86400}.get(d["timeframe"], 3600),
    )

    health = HealthState(strategy=strategy.name, symbol=d["symbol"])
    trader = PaperTrader(strategy, risk, paper_cfg, symbol=d["symbol"], health=health)

    if args.mode == "replay":
        if args.real:
            df = fetch_ohlcv(d["symbol"], d["timeframe"], d["exchange"], d["limit"])
        elif args.regime:
            df = regime_ohlcv(args.regime, n=d["limit"], timeframe=d["timeframe"])
        else:
            df = synthetic_ohlcv(n=d["limit"], timeframe=d["timeframe"])
        summary = trader.run_replay(df)
        print(json.dumps(summary, indent=2, default=str))
        return

    # live mode: expose monitoring endpoints, then poll forever.
    server = HealthServer(health,
                          host=mon.get("health_host", "0.0.0.0"),
                          port=int(mon.get("health_port", 8000)))
    server.start()
    print(f"[monitoring] health on http://{mon.get('health_host', '0.0.0.0')}:"
          f"{mon.get('health_port', 8000)}/healthz and /metrics")

    def fetch_latest():
        # Pull a rolling window big enough for the slowest indicator warmup.
        return fetch_ohlcv(d["symbol"], d["timeframe"], d["exchange"],
                           limit=max(500, cfg["strategy_params"].get("trend_filter", 200) + 50),
                           use_cache=False)

    try:
        trader.run_live(fetch_latest, max_iterations=args.iterations)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
