"""Backtest a value strategy over a universe of *resolved* Kalshi markets.

Because event contracts settle to a known outcome, a backtest is a replay: for each
market we know the price we could have paid, the probability our model would have
assigned, and how it actually resolved. We size each trade off the running bankroll,
pay entry fees, and settle at $1 or $0.

Two honesty features you should read before trusting any profit number:

  * **Calibration report.** Buckets trades by the model's predicted probability and
    shows predicted vs. realized frequency. If your model says 70% but those events
    happen 50% of the time, the profit is a mirage -- fix the model, not the sizing.
  * **Realized vs. predicted edge.** If realized edge is far below predicted, your
    probabilities are overconfident.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .economics import SizingConfig, fee_per_contract
from .strategy import evaluate_market


@dataclass
class ResolvedMarket:
    ticker: str
    yes_price: float     # price (dollars, 0..1) at the moment you would have traded
    fair_prob: float     # your model's Yes-probability estimate at that moment
    outcome: int         # 1 if the market resolved Yes, 0 if No


@dataclass
class TradeRecord:
    ticker: str
    side: str
    price: float
    fair_prob: float
    contracts: int
    cost: float
    payout: float
    pnl: float
    won: bool


@dataclass
class KalshiBacktestResult:
    trades: list[TradeRecord]
    start_bankroll: float
    end_bankroll: float
    total_fees: float
    calibration: list[dict] = field(default_factory=list)

    @property
    def total_return(self) -> float:
        return self.end_bankroll / self.start_bankroll - 1.0 if self.start_bankroll else 0.0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def hit_rate(self) -> float:
        return sum(t.won for t in self.trades) / self.n_trades if self.trades else 0.0

    @property
    def predicted_edge(self) -> float:
        contracts = sum(t.contracts for t in self.trades)
        if not contracts:
            return 0.0
        # edge per contract the model *expected*, weighted by size
        return sum(
            t.contracts * (t.fair_prob if t.side == "yes" else 1 - t.fair_prob) - t.contracts * t.price
            for t in self.trades
        ) / contracts

    @property
    def realized_edge(self) -> float:
        contracts = sum(t.contracts for t in self.trades)
        return sum(t.pnl for t in self.trades) / contracts if contracts else 0.0


def _calibration_table(trades: list[TradeRecord], n_buckets: int = 5) -> list[dict]:
    """Bucket by the model's probability *for the side taken* and compare to reality."""
    rows = []
    for lo in np.linspace(0.0, 1.0, n_buckets + 1)[:-1]:
        hi = lo + 1.0 / n_buckets
        bucket = [
            t for t in trades
            if lo <= (t.fair_prob if t.side == "yes" else 1 - t.fair_prob) < hi
            or (hi == 1.0 and (t.fair_prob if t.side == "yes" else 1 - t.fair_prob) == 1.0)
        ]
        if not bucket:
            continue
        predicted = np.mean([
            t.fair_prob if t.side == "yes" else 1 - t.fair_prob for t in bucket
        ])
        realized = np.mean([t.won for t in bucket])
        rows.append({
            "bucket": f"{lo:.0%}-{hi:.0%}",
            "n": len(bucket),
            "predicted": float(predicted),
            "realized": float(realized),
        })
    return rows


def run_value_backtest(
    markets: list[ResolvedMarket],
    start_bankroll: float = 1_000.0,
    sizing: SizingConfig | None = None,
    fee_rate: float = 0.07,
) -> KalshiBacktestResult:
    """Replay the strategy over markets *in the given order* (treat it as time order).

    Simplification: trades are settled sequentially, so bankroll compounds trade by
    trade. This slightly understates how many concurrent positions you could hold, but
    it is a clean and honest measure of the edge and calibration.
    """
    sizing = sizing or SizingConfig()
    bankroll = start_bankroll
    total_fees = 0.0
    trades: list[TradeRecord] = []

    for m in markets:
        sig = evaluate_market(
            m.ticker, m.yes_price, m.fair_prob, bankroll, sizing, fee_rate
        )
        if sig is None:
            continue

        fee = fee_per_contract(sig.price, fee_rate) * sig.contracts
        cost = sig.contracts * sig.price + fee
        if cost > bankroll:  # never spend money we don't have
            continue

        won = (m.outcome == 1) if sig.side == "yes" else (m.outcome == 0)
        payout = sig.contracts * 1.0 if won else 0.0
        pnl = payout - cost

        bankroll += pnl
        total_fees += fee
        trades.append(TradeRecord(
            ticker=m.ticker, side=sig.side, price=sig.price, fair_prob=m.fair_prob,
            contracts=sig.contracts, cost=cost, payout=payout, pnl=pnl, won=won,
        ))

    return KalshiBacktestResult(
        trades=trades,
        start_bankroll=start_bankroll,
        end_bankroll=bankroll,
        total_fees=total_fees,
        calibration=_calibration_table(trades),
    )


def synthetic_resolved_markets(
    n: int = 2000,
    model_skill: float = 0.7,
    market_noise: float = 0.05,
    seed: int = 11,
) -> list[ResolvedMarket]:
    """Generate resolved markets for testing the harness offline.

    ``model_skill`` in [0, 1] blends the true probability with random noise to form the
    model's estimate: 1.0 = a perfect model, 0.0 = a useless one. ``market_noise`` is
    how far the market price wanders from the true probability (the mispricing we hope
    to exploit). This lets tests assert the obvious sanity checks: a skilled model on a
    mispriced market makes money; a useless model does not.
    """
    rng = np.random.default_rng(seed)
    out: list[ResolvedMarket] = []
    for i in range(n):
        true_p = float(rng.uniform(0.1, 0.9))
        outcome = int(rng.random() < true_p)
        price = float(np.clip(true_p + rng.normal(0, market_noise), 0.02, 0.98))
        # Model estimate blends the truth with noise: estimate = skill*truth + (1-skill)*noise
        fair = model_skill * true_p + (1 - model_skill) * float(rng.uniform(0.1, 0.9))
        fair = float(np.clip(fair, 0.02, 0.98))
        out.append(ResolvedMarket(f"MKT-{i:04d}", price, fair, outcome))
    return out
