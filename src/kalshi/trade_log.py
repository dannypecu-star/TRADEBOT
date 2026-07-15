"""Append-only CSV trade log so a paper/live run can be *measured*, not just watched.

You cannot find an edge by staring at a running balance -- a balance is dominated by a
handful of lucky tail events (see the pair strategy, where two divergence jackpots
carried an otherwise break-even night). Edge is found by logging every decision with the
inputs that produced it, then, once markets resolve, comparing the model's probability
to what actually happened.

The workflow:

  1. At decision time, call :meth:`TradeLogger.log_signal` with the model's fair
     probability, the market price, the side/size, and the live inputs (spot, strike,
     minutes-left, vol). This row is written immediately with ``status="SIGNAL"``.
  2. When the market settles, call :meth:`TradeLogger.log_settlement` with the same
     ticker and the outcome; it writes a ``status="SETTLED"`` row with realized pnl.
  3. Run :func:`summarize` over the file to get realized-vs-predicted edge, hit rate,
     and a calibration table -- the same honesty checks the backtester applies, but on
     your real fills.

Deliberately dependency-light (stdlib ``csv``) so it can run anywhere the bot runs.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from .economics import fee_per_contract

FIELDNAMES = [
    "timestamp",       # ISO-8601 UTC
    "status",          # SIGNAL | SETTLED | EXIT
    "ticker",
    "asset",
    "spot",            # live underlying at decision time
    "strike",
    "minutes_left",
    "vol_annual",
    "model_prob",      # model's Yes-probability
    "market_price",    # price paid for the side taken (dollars)
    "side",            # yes | no
    "edge_per_contract",
    "contracts",
    "cost",            # contracts * price + entry fee
    "outcome",         # 1/0 for the market (SETTLED rows only)
    "won",             # did the side taken win (SETTLED rows only)
    "payout",
    "pnl",
]


@dataclass
class LoggedTrade:
    """Parsed view of one SETTLED row, used by :func:`summarize`."""
    ticker: str
    side: str
    market_price: float
    model_prob: float
    contracts: int
    pnl: float
    won: bool

    @property
    def prob_for_side(self) -> float:
        """The model's probability for the side actually taken."""
        return self.model_prob if self.side == "yes" else 1.0 - self.model_prob


class TradeLogger:
    def __init__(self, path: str):
        self.path = path
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        if new_file:
            with open(path, "w", newline="") as fh:
                csv.DictWriter(fh, fieldnames=FIELDNAMES).writeheader()

    def _write(self, row: dict) -> None:
        # Only known columns; blank for anything not supplied.
        clean = {k: row.get(k, "") for k in FIELDNAMES}
        with open(self.path, "a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=FIELDNAMES).writerow(clean)

    def log_signal(
        self,
        ticker: str,
        asset: str,
        side: str,
        market_price: float,
        model_prob: float,
        edge_per_contract: float,
        contracts: int,
        spot: float | None = None,
        strike: float | None = None,
        minutes_left: float | None = None,
        vol_annual: float | None = None,
        fee_rate: float = 0.07,
        timestamp: str | None = None,
    ) -> None:
        """Record a decision the moment it is made (before the outcome is known)."""
        fee = fee_per_contract(market_price, fee_rate) * contracts
        cost = contracts * market_price + fee
        self._write({
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "SIGNAL",
            "ticker": ticker,
            "asset": asset,
            "spot": spot,
            "strike": strike,
            "minutes_left": minutes_left,
            "vol_annual": vol_annual,
            "model_prob": model_prob,
            "market_price": market_price,
            "side": side,
            "edge_per_contract": edge_per_contract,
            "contracts": contracts,
            "cost": cost,
        })

    def log_settlement(
        self,
        ticker: str,
        side: str,
        market_price: float,
        model_prob: float,
        contracts: int,
        outcome: int,
        asset: str = "",
        fee_rate: float = 0.07,
        timestamp: str | None = None,
    ) -> None:
        """Record the realized result once the market resolves.

        ``outcome`` is 1 if the market settled Yes, else 0. A "yes" position wins on
        outcome 1; a "no" position wins on outcome 0. PnL includes the entry fee;
        settlement itself is free on Kalshi.
        """
        won = (outcome == 1) if side == "yes" else (outcome == 0)
        fee = fee_per_contract(market_price, fee_rate) * contracts
        cost = contracts * market_price + fee
        payout = contracts * 1.0 if won else 0.0
        self._write({
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "SETTLED",
            "ticker": ticker,
            "asset": asset,
            "model_prob": model_prob,
            "market_price": market_price,
            "side": side,
            "contracts": contracts,
            "cost": cost,
            "outcome": outcome,
            "won": int(won),
            "payout": payout,
            "pnl": payout - cost,
        })

    def log_exit(
        self,
        ticker: str,
        side: str,
        entry_price: float,
        exit_price: float,
        contracts: int,
        model_prob: float = 0.0,
        asset: str = "",
        fee_rate: float = 0.07,
        timestamp: str | None = None,
    ) -> None:
        """Record an early exit (e.g. stop-loss) that closed before resolution.

        An exit is a risk action, not a market resolution, so it carries no
        ``outcome`` and is *excluded* from calibration and model-edge stats -- it would
        otherwise corrupt the test of whether the probabilities are accurate. It still
        counts toward total realized P&L. Both entry and exit pay a fee.
        """
        cost = contracts * entry_price + fee_per_contract(entry_price, fee_rate) * contracts
        revenue = contracts * exit_price - fee_per_contract(exit_price, fee_rate) * contracts
        self._write({
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "EXIT",
            "ticker": ticker,
            "asset": asset,
            "model_prob": model_prob,
            "market_price": entry_price,
            "side": side,
            "contracts": contracts,
            "cost": cost,
            "payout": revenue,
            "pnl": revenue - cost,
        })


def _read_settled(path: str) -> list[LoggedTrade]:
    out: list[LoggedTrade] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("status") != "SETTLED":
                continue
            try:
                out.append(LoggedTrade(
                    ticker=row["ticker"],
                    side=row["side"],
                    market_price=float(row["market_price"]),
                    model_prob=float(row["model_prob"]),
                    contracts=int(float(row["contracts"])),
                    pnl=float(row["pnl"]),
                    won=bool(int(row["won"])),
                ))
            except (ValueError, KeyError):
                continue  # skip malformed rows rather than crash a report
    return out


def _read_exit_pnl(path: str) -> tuple[int, float]:
    """Total P&L and count of early-exit rows (excluded from calibration)."""
    n, pnl = 0, 0.0
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("status") != "EXIT":
                continue
            try:
                pnl += float(row["pnl"])
                n += 1
            except (ValueError, KeyError):
                continue
    return n, pnl


def summarize(path: str, n_buckets: int = 5) -> dict:
    """Compute realized-vs-predicted edge, hit rate, and calibration from SETTLED rows.

    Returns a dict you can print or assert on. The two numbers that matter:

      * ``realized_edge`` -- actual pnl per contract. If it is not reliably positive
        after fees, there is no edge and no amount of sizing fixes that.
      * ``calibration`` -- per-probability-bucket predicted vs. realized frequency. If
        the model says 70% but those events happen 50% of the time, the model is
        overconfident and the profit is a mirage; fix the model, not the size.
    """
    trades = _read_settled(path)
    n_exits, exit_pnl = _read_exit_pnl(path)
    n = len(trades)
    if n == 0:
        return {"n_trades": 0, "n_exits": n_exits, "exit_pnl": exit_pnl,
                "note": "no SETTLED (held-to-resolution) rows yet"}

    total_contracts = sum(t.contracts for t in trades)
    total_pnl = sum(t.pnl for t in trades)
    realized_edge = total_pnl / total_contracts if total_contracts else 0.0
    predicted_edge = (
        sum(t.contracts * (t.prob_for_side - t.market_price) for t in trades) / total_contracts
        if total_contracts else 0.0
    )

    calibration = []
    step = 1.0 / n_buckets
    for b in range(n_buckets):
        lo, hi = b * step, (b + 1) * step
        bucket = [t for t in trades if lo <= t.prob_for_side < hi or (b == n_buckets - 1 and t.prob_for_side == 1.0)]
        if not bucket:
            continue
        calibration.append({
            "bucket": f"{lo:.0%}-{hi:.0%}",
            "n": len(bucket),
            "predicted": sum(t.prob_for_side for t in bucket) / len(bucket),
            "realized": sum(t.won for t in bucket) / len(bucket),
        })

    return {
        "n_trades": n,                       # held-to-resolution trades (the model test)
        "total_contracts": total_contracts,
        "settled_pnl": total_pnl,            # P&L from resolutions only
        "n_exits": n_exits,                  # early exits, excluded from calibration
        "exit_pnl": exit_pnl,
        "total_pnl": total_pnl + exit_pnl,   # honest bottom line: resolutions + exits
        "hit_rate": sum(t.won for t in trades) / n,
        "predicted_edge": predicted_edge,    # dollars/contract the model expected
        "realized_edge": realized_edge,      # dollars/contract actually earned at resolution
        "calibration": calibration,
    }
