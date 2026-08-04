"""Load YAML config into the strongly-typed dataclasses the engine expects.

Backward compatible with the original format where ``strategy:`` was a dict of
trend-momentum parameters. The current format uses ``strategy:`` as a *name* selector and
``strategy_params:`` as a shared parameter block for all strategies.
"""
from __future__ import annotations

import os

import yaml

from ..backtest.engine import ExecConfig
from ..risk.manager import RiskConfig


def load_config(path: str) -> dict:
    with open(path, "r") as fh:
        raw = yaml.safe_load(fh) or {}

    data = raw.get("data", {})
    risk = raw.get("risk", {})
    execu = raw.get("execution", {})

    # Strategy selection: support both the new (name + strategy_params) and the legacy
    # (strategy: {dict of trend_momentum params}) shapes.
    strat_field = raw.get("strategy", "trend_following")
    if isinstance(strat_field, dict):
        strategy_name = "trend_momentum"
        strategy_params = dict(strat_field)
    else:
        strategy_name = str(strat_field)
        strategy_params = dict(raw.get("strategy_params", {}))

    return {
        "data": {
            "symbol": data.get("symbol", "BTC/USDT"),
            "timeframe": data.get("timeframe", "1h"),
            "exchange": data.get("exchange", "binance"),
            "limit": int(data.get("limit", 3000)),
        },
        "strategy_name": strategy_name,
        "strategy_params": strategy_params,
        # Legacy alias: older code reads cfg["strategy"] as a trend_momentum kwargs dict.
        "strategy": {
            "fast": int(strategy_params.get("fast", 20)),
            "slow": int(strategy_params.get("slow", 50)),
            "mom_lookback": int(strategy_params.get("mom_lookback", 24)),
            "atr_period": int(strategy_params.get("atr_period", 14)),
        },
        "risk": RiskConfig(
            risk_per_trade=float(risk.get("risk_per_trade", 0.01)),
            max_position_fraction=float(risk.get("max_position_fraction", 1.0)),
            atr_stop_mult=float(risk.get("atr_stop_mult", 3.0)),
            max_drawdown_stop=float(risk.get("max_drawdown_stop", 0.30)),
        ),
        "execution": ExecConfig(
            initial_cash=float(execu.get("initial_cash", 10_000.0)),
            fee_rate=float(execu.get("fee_rate", 0.001)),
            slippage=float(execu.get("slippage", 0.0005)),
            atr_period=int(execu.get("atr_period", 14)),
        ),
        "arbitrage": raw.get("arbitrage", {}),
        "monitoring": raw.get("monitoring", {}),
        "live": raw.get("live", {"enabled": False}),
    }


def default_config_path() -> str:
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "config.example.yaml")
    )
