"""Statistical (pairs) arbitrage: trade the mean-reverting spread of two assets.

The idea
--------
Some pairs of assets move together because they share economic drivers (two exchanges'
prices for BTC; ETH vs. a correlated large-cap; a coin vs. a basket). Their *spread*

    spread_t = log(price_A_t) - beta * log(price_B_t)

tends to wander around a stable mean. When the spread is stretched far from that mean we
bet on reversion: if the spread is unusually high, A is rich relative to B, so we
**short A / long B**; if unusually low, we **long A / short B**. We close as the spread
reverts to its mean.

``beta`` is the hedge ratio -- how many units of B offset one unit of A. We estimate it
by ordinary least squares on the log prices over a rolling window, so the hedge adapts
as the relationship drifts. Everything is computed on a *trailing* window (no future
data), and the backtest applies a one-bar execution delay: a decision made from bar
``i-1``'s z-score is filled at bar ``i``.

Why a separate backtester
--------------------------
The primary engine is single-instrument, long-or-flat. A pairs trade is inherently two
legs and can be long *or* short the spread. Rather than bend the main engine, this module
carries a small, self-contained spread backtester so the logic stays obvious and
auditable. It models fees and slippage on *both* legs each time the position changes.

Honest note
-----------
Cointegration is not permanent. The single biggest risk here is a structural break -- the
two assets stop moving together and the "reverting" spread simply trends away from you
while you hold the losing side. A z-score stop (``stop_z``) caps that, but it cannot
eliminate it. Treat any backtest edge as fragile and size accordingly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..backtest.metrics import Metrics, compute_metrics


@dataclass
class PairsConfig:
    lookback: int = 60          # window for the rolling hedge ratio + spread stats
    entry_z: float = 2.0        # open when |z| >= entry_z
    exit_z: float = 0.5         # close when |z| <= exit_z (spread near its mean)
    stop_z: float = 4.0         # hard stop: close if |z| >= stop_z (relationship broke)
    fee_rate: float = 0.001     # per-leg taker fee
    slippage: float = 0.0005    # per-leg slippage
    initial_cash: float = 10_000.0
    gross_leverage: float = 1.0  # A-leg notional as a fraction of equity


@dataclass
class PairsTrade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: int                   # +1 = long spread (long A/short B), -1 = short spread
    entry_z: float
    exit_z: float
    pnl: float
    reason: str                 # "revert", "stop", or "eod"


@dataclass
class PairsResult:
    equity: pd.Series
    spread: pd.Series
    zscore: pd.Series
    trades: list[PairsTrade]
    metrics: Metrics
    config: PairsConfig = field(default_factory=PairsConfig)


def rolling_hedge_ratio(log_a: pd.Series, log_b: pd.Series, window: int) -> pd.Series:
    """Rolling OLS slope of log_a on log_b (the hedge ratio ``beta``).

    beta_t = Cov(a, b) / Var(b) over the trailing ``window`` bars, using only data up to
    and including bar ``t``.
    """
    cov = log_a.rolling(window).cov(log_b)
    var = log_b.rolling(window).var()
    return cov / var.replace(0.0, np.nan)


def build_spread(
    price_a: pd.Series, price_b: pd.Series, window: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (spread, zscore, beta), all aligned and trailing-only (no lookahead)."""
    log_a = np.log(price_a)
    log_b = np.log(price_b)
    beta = rolling_hedge_ratio(log_a, log_b, window)
    spread = log_a - beta * log_b
    mean = spread.rolling(window).mean()
    std = spread.rolling(window).std(ddof=0)
    z = (spread - mean) / std.replace(0.0, np.nan)
    return spread, z, beta


def run_pairs_backtest(
    price_a: pd.Series,
    price_b: pd.Series,
    config: PairsConfig | None = None,
) -> PairsResult:
    """Backtest mean-reversion of the A/B spread with a one-bar execution delay.

    Equity model (single source of truth):
        equity[i] = equity[i-1] + mark_to_market_pnl(i) - costs_charged(i)

    where ``mark_to_market_pnl(i)`` is the P&L of the legs *held across* bar i-1->i, and
    ``costs_charged(i)`` are per-leg fee+slippage booked whenever we open or close.

    Position is in *spread units*: side +1 = long A / short B (betting a below-mean spread
    rises); side -1 = the reverse.
    """
    cfg = config or PairsConfig()
    df = pd.DataFrame({"a": price_a, "b": price_b}).dropna()
    spread, z, beta = build_spread(df["a"], df["b"], cfg.lookback)

    a = df["a"].to_numpy()
    b = df["b"].to_numpy()
    beta_arr = beta.to_numpy()
    z_arr = z.to_numpy()
    index = df.index
    n = len(df)

    equity = np.full(n, cfg.initial_cash, dtype=float)
    in_pos = np.zeros(n, dtype=bool)

    a_units = 0.0
    b_units = 0.0
    side = 0
    entry_z = 0.0
    entry_time = None
    entry_equity = cfg.initial_cash
    trades: list[PairsTrade] = []

    def leg_cost(units: float, price: float) -> float:
        return abs(units * price) * (cfg.fee_rate + cfg.slippage)

    for i in range(n):
        if i == 0:
            equity[i] = cfg.initial_cash
            continue

        # 1. Mark legs held from i-1 to i.
        leg_pnl = a_units * (a[i] - a[i - 1]) + b_units * (b[i] - b[i - 1])
        equity[i] = equity[i - 1] + leg_pnl
        costs = 0.0

        # 2. Decide using the PREVIOUS bar's z (one-bar execution delay), fill at a[i]/b[i].
        zi = z_arr[i - 1]
        if np.isfinite(zi) and np.isfinite(beta_arr[i]):
            if side == 0:
                new_side = +1 if zi <= -cfg.entry_z else (-1 if zi >= cfg.entry_z else 0)
                if new_side != 0:
                    notional = cfg.gross_leverage * equity[i]
                    a_units = new_side * notional / a[i]
                    b_units = -new_side * (beta_arr[i] * notional) / b[i]
                    costs += leg_cost(a_units, a[i]) + leg_cost(b_units, b[i])
                    side = new_side
                    entry_z = zi
                    entry_time = index[i]
                    entry_equity = equity[i] - costs
            else:
                reverted = abs(zi) <= cfg.exit_z
                stopped = abs(zi) >= cfg.stop_z
                if reverted or stopped:
                    costs += leg_cost(a_units, a[i]) + leg_cost(b_units, b[i])
                    trades.append(
                        PairsTrade(
                            entry_time=entry_time,
                            exit_time=index[i],
                            side=side,
                            entry_z=float(entry_z),
                            exit_z=float(zi),
                            pnl=float((equity[i] - costs) - entry_equity),
                            reason="stop" if stopped else "revert",
                        )
                    )
                    a_units = b_units = 0.0
                    side = 0

        equity[i] -= costs
        in_pos[i] = side != 0

    # Force-close any open position at the final bar.
    if side != 0:
        costs = leg_cost(a_units, a[-1]) + leg_cost(b_units, b[-1])
        equity[-1] -= costs
        trades.append(
            PairsTrade(
                entry_time=entry_time,
                exit_time=index[-1],
                side=side,
                entry_z=float(entry_z),
                exit_z=float(z_arr[-1]),
                pnl=float(equity[-1] - entry_equity),
                reason="eod",
            )
        )

    equity_s = pd.Series(equity, index=index, name="equity")
    exposure = float(in_pos.mean()) if n else 0.0
    metrics = compute_metrics(equity_s, [t.pnl for t in trades], exposure)
    return PairsResult(equity_s, spread, z, trades, metrics, cfg)
