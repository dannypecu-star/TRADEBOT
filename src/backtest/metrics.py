"""Performance metrics computed from an equity curve and trade log.

Everything here is reported alongside a buy-and-hold benchmark elsewhere so that a
strategy is judged on whether it beats simply holding the asset -- not on whether it
merely made money in a bull market.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd


@dataclass
class Metrics:
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float
    volatility: float
    exposure: float
    n_trades: int
    win_rate: float
    profit_factor: float

    def as_dict(self) -> dict:
        return asdict(self)


def _periods_per_year(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 365.0
    median_delta = pd.Series(index).diff().median()
    seconds = median_delta.total_seconds()
    if seconds <= 0:
        return 365.0
    return (365 * 24 * 3600) / seconds


def compute_metrics(
    equity: pd.Series,
    trade_pnls: list[float],
    exposure: float,
) -> Metrics:
    equity = equity.dropna()
    returns = equity.pct_change().dropna()
    ppy = _periods_per_year(equity.index)

    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0) if len(equity) else 0.0
    years = len(equity) / ppy if ppy else 0.0
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) if years > 0 else 0.0

    vol = float(returns.std() * np.sqrt(ppy)) if len(returns) else 0.0
    sharpe = float(returns.mean() / returns.std() * np.sqrt(ppy)) if returns.std() else 0.0
    downside = returns[returns < 0].std()
    sortino = float(returns.mean() / downside * np.sqrt(ppy)) if downside else 0.0

    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0
    calmar = float(cagr / abs(max_dd)) if max_dd < 0 else 0.0

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    win_rate = len(wins) / len(trade_pnls) if trade_pnls else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = float(gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    return Metrics(
        total_return=total_return,
        cagr=cagr,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        calmar=calmar,
        volatility=vol,
        exposure=exposure,
        n_trades=len(trade_pnls),
        win_rate=win_rate,
        profit_factor=profit_factor,
    )
