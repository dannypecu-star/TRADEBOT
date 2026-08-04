"""Paper trading bot: run any registered strategy on real prices with fake money.

Two modes, one code path:

  * **replay**  -- walk a historical OHLCV frame bar by bar, as if it were arriving live.
    This produces a full simulated track record in seconds, honouring the same one-bar
    execution delay and costs as the backtester. Use it to *demonstrate* an edge quickly.
  * **live**    -- poll a public exchange for the latest candles on a schedule and act on
    the most recent *closed* bar. Same decision logic; only the clock differs.

Both modes:
  * size positions with the shared :class:`RiskManager` (fixed-fractional, ATR stop),
  * charge fees + slippage via :class:`PaperBroker`,
  * write every decision and fill as a JSON line to an audit log, and
  * keep a :class:`HealthState` current for the monitoring endpoint.

Design choice: the strategy decides direction on bar ``i``'s close; we execute at bar
``i+1``'s price. This mirrors the backtester exactly, so a paper run and a backtest over
the same data agree -- the whole point of paper trading as *validation*.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from ..backtest.metrics import compute_metrics
from ..monitoring.health import HealthState
from ..monitoring.logging_setup import get_logger
from ..risk.manager import RiskManager
from ..strategies.base import Strategy
from ..strategies.trend_momentum import atr as compute_atr
from .broker import PaperBroker

log = get_logger("paper")


@dataclass
class PaperConfig:
    initial_cash: float = 10_000.0
    fee_rate: float = 0.001
    slippage: float = 0.0005
    atr_period: int = 14
    warmup_bars: int = 200          # min history before the bot is allowed to trade
    poll_seconds: int = 3600        # live mode: how often to check for a new bar
    state_file: str = "data/paper_state.json"
    audit_log: str = "logs/paper_trades.jsonl"


class PaperTrader:
    def __init__(
        self,
        strategy: Strategy,
        risk: RiskManager,
        config: PaperConfig,
        symbol: str = "BTC/USDT",
        health: Optional[HealthState] = None,
    ):
        self.strategy = strategy
        self.risk = risk
        self.cfg = config
        self.symbol = symbol
        self.broker = PaperBroker(
            cash=config.initial_cash,
            fee_rate=config.fee_rate,
            slippage=config.slippage,
        )
        self.equity_curve: list[tuple[pd.Timestamp, float]] = []
        self._bars = 0
        self._bars_in_pos = 0
        self.health = health or HealthState(strategy=strategy.name, symbol=symbol)
        self.health.equity = config.initial_cash

    # ------------------------------------------------------------------ audit
    def _audit(self, event: dict) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.cfg.audit_log)), exist_ok=True)
        with open(self.cfg.audit_log, "a") as fh:
            fh.write(json.dumps(event, default=str) + "\n")

    # ------------------------------------------------------------- core step
    def step(self, history: pd.DataFrame, exec_price: float, exec_time) -> None:
        """Process one bar.

        ``history`` is all candles up to and including the bar whose close drives the
        decision; ``exec_price`` is the price we can actually trade at now (the next bar's
        open in replay, or the latest price in live mode).
        """
        if len(history) < self.cfg.warmup_bars:
            return

        signal = self.strategy.generate_signals(history)
        want_long = float(signal.iloc[-1]) > 0
        atr_series = compute_atr(history, self.cfg.atr_period)
        atr_val = float(atr_series.iloc[-1])

        equity_now = self.broker.equity(exec_price)
        peak = max((e for _, e in self.equity_curve), default=equity_now)
        halted = self.risk.drawdown_halt(equity_now, peak)

        fill = None
        if self.broker.in_position() and not want_long:
            fill = self.broker.sell_all(exec_price, str(exec_time))
        elif (not self.broker.in_position()) and want_long and not halted:
            if atr_val > 0:
                frac = self.risk.position_fraction(exec_price, atr_val)
                fill = self.broker.buy(exec_price, frac * equity_now, str(exec_time))

        equity_now = self.broker.equity(exec_price)
        self.equity_curve.append((exec_time, equity_now))
        self._bars += 1
        if self.broker.in_position():
            self._bars_in_pos += 1

        # Keep the health snapshot and audit trail current.
        self.health.last_loop_at = time.time()
        self.health.loops += 1
        self.health.equity = equity_now
        self.health.open_position = self.broker.units
        self.health.trades = len(self.broker.fills)

        event = {
            "time": str(exec_time),
            "price": exec_price,
            "signal": "long" if want_long else "flat",
            "in_position": self.broker.in_position(),
            "equity": round(equity_now, 2),
            "halted": halted,
        }
        if fill is not None:
            event["fill"] = {
                "side": fill.side, "price": round(fill.price, 2),
                "units": fill.units, "fee": round(fill.fee, 4),
            }
            log.info(
                "paper %s %s @ %.2f  equity=%.2f",
                fill.side, self.symbol, fill.price, equity_now,
                extra={"extra": event},
            )
        self._audit(event)

    # --------------------------------------------------------------- replay
    def run_replay(self, df: pd.DataFrame) -> dict:
        """Replay a historical frame bar-by-bar and return a performance summary.

        Execution uses the *next* bar's open, so a decision on bar i is filled at i+1 --
        identical timing to the backtester and to how live trading actually behaves.
        """
        opens = df["open"]
        for i in range(len(df) - 1):
            history = df.iloc[: i + 1]                 # decision uses data up to bar i
            exec_price = float(opens.iloc[i + 1])      # fill at bar i+1's open
            exec_time = df.index[i + 1]
            self.step(history, exec_price, exec_time)
        # Flatten at the end so the track record is realized, not paper-open.
        if self.broker.in_position():
            last_price = float(df["close"].iloc[-1])
            self.broker.sell_all(last_price, str(df.index[-1]))
            self.equity_curve.append((df.index[-1], self.broker.equity(last_price)))
        return self.summary()

    # ----------------------------------------------------------------- live
    def run_live(
        self,
        fetch_latest: Callable[[], pd.DataFrame],
        max_iterations: Optional[int] = None,
    ) -> None:
        """Poll for new candles and trade the latest closed bar. Runs until stopped.

        ``fetch_latest`` returns a fresh OHLCV frame (newest bar last). We act only when a
        genuinely new closed bar appears, so restarts and duplicate polls are harmless.
        """
        last_seen: Optional[pd.Timestamp] = None
        iterations = 0
        self._load_state()
        while max_iterations is None or iterations < max_iterations:
            try:
                df = fetch_latest()
                if len(df) >= 2:
                    # The last row may be a still-forming candle; act on the last *closed* one.
                    closed = df.iloc[:-1]
                    newest_closed = closed.index[-1]
                    if newest_closed != last_seen:
                        exec_price = float(df["close"].iloc[-1])  # tradeable "now" price
                        self.step(closed, exec_price, df.index[-1])
                        last_seen = newest_closed
                        self._save_state()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive; log and retry
                self.health.errors += 1
                log.error("paper live loop error: %s", exc, exc_info=True)
            iterations += 1
            if max_iterations is None or iterations < max_iterations:
                time.sleep(self.cfg.poll_seconds)

    # ------------------------------------------------------------ persistence
    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.cfg.state_file)), exist_ok=True)
        with open(self.cfg.state_file, "w") as fh:
            json.dump(
                {
                    "cash": self.broker.cash,
                    "units": self.broker.units,
                    "entry_price": self.broker._entry_price,
                    "n_fills": len(self.broker.fills),
                },
                fh,
            )

    def _load_state(self) -> None:
        if os.path.exists(self.cfg.state_file):
            try:
                with open(self.cfg.state_file) as fh:
                    s = json.load(fh)
                self.broker.cash = s.get("cash", self.broker.cash)
                self.broker.units = s.get("units", self.broker.units)
                self.broker._entry_price = s.get("entry_price", 0.0)
                log.info("resumed paper state: cash=%.2f units=%.6f",
                         self.broker.cash, self.broker.units)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not load paper state: %s", exc)

    # ------------------------------------------------------------- reporting
    def summary(self) -> dict:
        if not self.equity_curve:
            return {"error": "no equity curve"}
        idx = pd.DatetimeIndex([t for t, _ in self.equity_curve])
        eq = pd.Series([e for _, e in self.equity_curve], index=idx)
        pnls = self.broker.realized_trades()
        exposure = (self._bars_in_pos / self._bars) if self._bars else 0.0
        m = compute_metrics(eq, pnls, exposure=exposure)
        return {
            "final_equity": round(float(eq.iloc[-1]), 2),
            "total_return": m.total_return,
            "max_drawdown": m.max_drawdown,
            "sharpe": m.sharpe,
            "n_trades": len(pnls),
            "win_rate": m.win_rate,
            "profit_factor": m.profit_factor,
            "metrics": m.as_dict(),
        }
