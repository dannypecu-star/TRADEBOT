"""Event-driven backtest engine (long/flat spot).

Design goals, in priority order:

1. **No lookahead.** A signal decided on bar ``i``'s close is only ever executed on
   bar ``i+1``'s open. The engine enforces the one-bar delay so strategies cannot
   accidentally trade on information they would not have had live.
2. **Realistic fills.** Trades pay a taker fee and cross a slippage spread. Protective
   stops are checked against each bar's intrabar low, and gaps through the stop fill
   at the worse of open/stop.
3. **Auditability.** The engine steps bar by bar with explicit state rather than a
   vectorized shortcut, so the logic can be read and trusted.

It is deliberately simple: one instrument, long or flat, no leverage. That covers the
first strategy and, more importantly, is small enough to be sure it is correct.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..risk.manager import RiskManager
from ..strategies.base import Strategy
from ..strategies.trend_momentum import atr as compute_atr
from .metrics import Metrics, compute_metrics


@dataclass
class ExecConfig:
    initial_cash: float = 10_000.0
    fee_rate: float = 0.001       # 0.10% taker fee, typical for major exchanges
    slippage: float = 0.0005      # 0.05% assumed slippage per fill
    atr_period: int = 14


@dataclass
class Trade:
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    units: float
    pnl: float
    reason: str  # "signal" or "stop"


@dataclass
class BacktestResult:
    equity: pd.Series
    benchmark: pd.Series
    trades: list["Trade"]
    metrics: Metrics
    benchmark_metrics: Metrics
    exec_config: ExecConfig = field(default_factory=ExecConfig)


def run_backtest(
    df: pd.DataFrame,
    strategy: Strategy,
    risk: RiskManager | None = None,
    exec_config: ExecConfig | None = None,
) -> BacktestResult:
    risk = risk or RiskManager()
    cfg = exec_config or ExecConfig()

    signal = strategy.generate_signals(df).fillna(0.0).to_numpy()
    atr = compute_atr(df, cfg.atr_period).to_numpy()

    opens = df["open"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    index = df.index
    n = len(df)

    state = {
        "cash": cfg.initial_cash,
        "units": 0.0,
        "in_pos": False,
        "entry_price": 0.0,
        "entry_time": None,
        "stop_price": 0.0,
    }
    peak_equity = cfg.initial_cash
    equity_curve = np.empty(n)
    bars_in_pos = 0
    trades: list[Trade] = []

    def close_position(exit_fill: float, exit_time, reason: str) -> None:
        gross = state["units"] * exit_fill
        fee = gross * cfg.fee_rate
        state["cash"] += gross - fee
        entry_cost = state["units"] * state["entry_price"] * (1 + cfg.fee_rate)
        pnl = (gross - fee) - entry_cost
        trades.append(
            Trade(
                entry_time=state["entry_time"],
                entry_price=state["entry_price"],
                exit_time=exit_time,
                exit_price=exit_fill,
                units=state["units"],
                pnl=pnl,
                reason=reason,
            )
        )
        state["units"] = 0.0
        state["in_pos"] = False

    for i in range(n):
        price_open = opens[i]
        equity_now = state["cash"] + state["units"] * price_open

        # --- 1. Act at this open on the decision made at the previous bar's close.
        want_long = i >= 1 and signal[i - 1] > 0
        halted = risk.drawdown_halt(equity_now, peak_equity)

        if state["in_pos"] and not want_long:
            close_position(price_open * (1 - cfg.slippage), index[i], "signal")
        elif (not state["in_pos"]) and want_long and not halted:
            atr_at_decision = atr[i - 1] if i >= 1 else atr[i]
            if np.isfinite(atr_at_decision) and atr_at_decision > 0:
                fill = price_open * (1 + cfg.slippage)
                frac = risk.position_fraction(fill, atr_at_decision)
                position_value = min(frac * equity_now,
                                     state["cash"] / (1 + cfg.fee_rate))
                if position_value > 0:
                    state["units"] = position_value / fill
                    state["cash"] -= position_value + position_value * cfg.fee_rate
                    state["in_pos"] = True
                    state["entry_price"] = fill
                    state["entry_time"] = index[i]
                    state["stop_price"] = risk.stop_price(fill, atr_at_decision)

        # --- 2. Intrabar protective-stop check.
        if state["in_pos"] and lows[i] <= state["stop_price"]:
            raw = min(price_open, state["stop_price"])  # gap-through fills at open
            close_position(raw * (1 - cfg.slippage), index[i], "stop")

        # --- 3. Mark to market at the close.
        equity_curve[i] = state["cash"] + state["units"] * closes[i]
        peak_equity = max(peak_equity, equity_curve[i])
        if state["in_pos"]:
            bars_in_pos += 1

    if state["in_pos"]:
        close_position(closes[-1] * (1 - cfg.slippage), index[-1], "signal")
        equity_curve[-1] = state["cash"]

    equity = pd.Series(equity_curve, index=index, name="equity")
    exposure = bars_in_pos / n if n else 0.0
    metrics = compute_metrics(equity, [t.pnl for t in trades], exposure)

    benchmark = cfg.initial_cash * (closes / closes[0])
    benchmark = pd.Series(benchmark, index=index, name="buy_and_hold")
    bench_metrics = compute_metrics(benchmark, [], 1.0)

    return BacktestResult(equity, benchmark, trades, metrics, bench_metrics, cfg)
