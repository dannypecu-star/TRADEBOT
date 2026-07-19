"""BTC/ETH inverse-pair strategy on Kalshi 15-minute markets (paper, pure logic).

Python port of scripts/kalshi_btc_eth_pair_paper.ahk with identical strategy behavior
and CSV schema, minus the AHK version's dead code (pending-order machinery that can
never execute when paper fills are atomic). Everything here is pure computation so the
parts that decide money movement are unit-tested; network and clocks live in the
runner script.

The strategy, in one paragraph: buy the two opposing legs (e.g. BTC-UP + ETH-DOWN)
when their combined ask sum is inside ``entry_range`` -- cheap enough that if the two
assets settle in the same direction relative to their own strikes, the $1 payout beats
the cost. The known dominant loss mode is the assets sitting on *opposite* sides of
their strikes (they can then co-move forever and still pay $0), so entries additionally
require the adverse strike gap to be small. A behavior classifier (SYNC/DIVERGENCE/...)
ported verbatim from the AHK version gates on co-movement, fees follow Kalshi's
round-up schedule, and resolution ties settle DOWN per "above the open" rules.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .economics import fee_per_contract

UP_DOWN = "BTC_UP_ETH_DOWN"
DOWN_UP = "BTC_DOWN_ETH_UP"


@dataclass
class PairConfig:
    entry_min: float = 0.80
    entry_max: float = 0.93
    pair_exit: float = 0.60          # stop-loss on the locked pair's current sum
    loss_exit_floor: float = 0.05    # used instead when pair_exit is misconfigured <= 0
    take_profit: float = 0.0         # exit early when sum >= this (0 = hold to resolution)
    order_size: int = 5
    max_shots: int = 10
    cooldown_sec: float = 40.0
    no_entry_minutes: float = 1.0
    fee_rate: float = 0.07
    strike_gap_max_bps: float = 3.0
    behavior_lookback_s: float = 10.0
    behavior_eps_bps: float = 0.25
    behavior_ema_alpha: float = 0.22

    def effective_exit(self) -> float:
        return self.pair_exit if self.pair_exit > 0 else self.loss_exit_floor


# --------------------------------------------------------------------------------
# Behavior classifier (verbatim port of the AHK UpdatePairBehaviorState)
# --------------------------------------------------------------------------------

@dataclass
class BehaviorSnapshot:
    state: str = "WAITING"
    score: Optional[float] = None
    btc_move_bps: Optional[float] = None
    eth_move_bps: Optional[float] = None


class BehaviorTracker:
    """Classifies BTC/ETH co-movement from spot moves vs the 15m window open."""

    def __init__(self, cfg: PairConfig):
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self.btc_open: Optional[float] = None
        self.eth_open: Optional[float] = None
        self.ema: Optional[float] = None
        self.history: list[tuple[float, float, float]] = []  # (t, btc_bps, eth_bps)
        self.snapshot = BehaviorSnapshot()

    def _sign(self, bps: float) -> int:
        if abs(bps) <= self.cfg.behavior_eps_bps:
            return 0
        return 1 if bps > 0 else -1

    def _lookback(self, now: float) -> Optional[tuple[float, float, float]]:
        target = now - self.cfg.behavior_lookback_s
        point = None
        for entry in self.history:
            if entry[0] <= target:
                point = entry
            else:
                break
        return point

    def update(self, now: float, btc_price: Optional[float], eth_price: Optional[float],
               btc_open: Optional[float] = None,
               eth_open: Optional[float] = None) -> BehaviorSnapshot:
        if not btc_price or not eth_price or btc_price <= 0 or eth_price <= 0:
            self.snapshot = BehaviorSnapshot()
            return self.snapshot
        if self.btc_open is None:
            self.btc_open = btc_open if btc_open and btc_open > 0 else btc_price
        if self.eth_open is None:
            self.eth_open = eth_open if eth_open and eth_open > 0 else eth_price

        btc_bps = math.log(btc_price / self.btc_open) * 10000.0
        eth_bps = math.log(eth_price / self.eth_open) * 10000.0
        prev = self._lookback(now)
        btc_vel = btc_bps - prev[1] if prev else 0.0
        eth_vel = eth_bps - prev[2] if prev else 0.0

        b_sign, e_sign = self._sign(btc_bps), self._sign(eth_bps)
        distance = abs(btc_bps - eth_bps)
        momentum_gap = abs(btc_vel - eth_vel)
        opposite = b_sign != 0 and e_sign != 0 and b_sign != e_sign
        delayed = b_sign != e_sign and not opposite

        if opposite:
            state = "DIVERGENCE"
            raw = -(distance + 14 + momentum_gap * 2)
        elif delayed:
            state = "DELAYED FOLLOW"
            raw = -(distance * 0.85 + 6 + momentum_gap * 1.5)
        else:
            instability = distance * 0.42 + momentum_gap * 1.2
            raw = 10 - instability
            min_move = min(abs(btc_bps), abs(eth_bps))
            if raw < 0 or (min_move < 1 and momentum_gap > 0.4):
                state = "CROSSOVER INSTABILITY"
                raw = min(raw, -max(instability - 8, 1))
            else:
                state = "SYNC"

        alpha = self.cfg.behavior_ema_alpha
        self.ema = raw if self.ema is None else self.ema + alpha * (raw - self.ema)
        self.history.append((now, btc_bps, eth_bps))
        cutoff = now - self.cfg.behavior_lookback_s * 3
        while self.history and self.history[0][0] < cutoff:
            self.history.pop(0)

        self.snapshot = BehaviorSnapshot(state, self.ema, btc_bps, eth_bps)
        return self.snapshot


# --------------------------------------------------------------------------------
# Entry selection
# --------------------------------------------------------------------------------

def pair_sum(name: str, btc_up, btc_down, eth_up, eth_down) -> Optional[float]:
    if name == UP_DOWN:
        return None if btc_up is None or eth_down is None else btc_up + eth_down
    if name == DOWN_UP:
        return None if btc_down is None or eth_up is None else btc_down + eth_up
    return None


def adverse_strike_gap_bps(name: str, btc_move_bps: Optional[float],
                           eth_move_bps: Optional[float]) -> Optional[float]:
    """Width (bps) of the losing configuration a shared market move cannot escape.

    BTC_UP_ETH_DOWN pays $0 only when BTC settles below its strike while ETH settles
    above its own; under a common move that window exists only while BTC lags ETH,
    and its width is the lag itself. Co-movement (SYNC) does not shrink it, which is
    why this gate exists in addition to the behavior gate.
    """
    if btc_move_bps is None or eth_move_bps is None:
        return None
    if name == UP_DOWN:
        return eth_move_bps - btc_move_bps
    if name == DOWN_UP:
        return btc_move_bps - eth_move_bps
    return None


def in_entry_range(cfg: PairConfig, value: Optional[float]) -> bool:
    return value is not None and cfg.entry_min <= value <= cfg.entry_max


def select_pair(cfg: PairConfig, btc_up, btc_down, eth_up, eth_down,
                behavior: BehaviorSnapshot) -> Optional[str]:
    """Pick the pair to enter, preferring the smaller adverse strike gap.

    The cheaper sum is systematically the riskier one, so price is only the
    tiebreaker when spot-move data is unavailable.
    """
    sums = {name: pair_sum(name, btc_up, btc_down, eth_up, eth_down)
            for name in (UP_DOWN, DOWN_UP)}
    candidates = [n for n in (UP_DOWN, DOWN_UP) if in_entry_range(cfg, sums[n])]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    gaps = {n: adverse_strike_gap_bps(n, behavior.btc_move_bps, behavior.eth_move_bps)
            for n in candidates}
    if all(g is not None for g in gaps.values()):
        return min(candidates, key=lambda n: gaps[n])
    return min(candidates, key=lambda n: sums[n])


def entry_allowed(cfg: PairConfig, name: str, sum_value: Optional[float],
                  behavior: BehaviorSnapshot) -> tuple[bool, str]:
    """Full entry gate. Returns (allowed, reason-if-blocked)."""
    if not in_entry_range(cfg, sum_value):
        return False, "sum outside entry range"
    if behavior.state != "SYNC":
        return False, f"behavior {behavior.state}, waiting for SYNC"
    gap = adverse_strike_gap_bps(name, behavior.btc_move_bps, behavior.eth_move_bps)
    if gap is None:
        return False, "no spot move data for strike gate"
    if gap > cfg.strike_gap_max_bps:
        return False, (f"adverse strike gap {gap:.2f}bps > {cfg.strike_gap_max_bps}bps"
                       " (cheap sum is priced-in risk, not edge)")
    return True, ""


# --------------------------------------------------------------------------------
# Paper ledger
# --------------------------------------------------------------------------------

@dataclass
class PairTrade:
    name: str
    legs: list[dict]        # {asset, side, price, qty}
    qty: int
    entry_cost: float
    entry_fees: float
    opened_utc: str


@dataclass
class CloseResult:
    trades: int = 0
    contracts: int = 0
    entry_cost: float = 0.0
    revenue: float = 0.0
    fees: float = 0.0
    pnl: float = 0.0
    outcome: str = ""


def resolution_leg_value(side: str, up: Optional[float], down: Optional[float]) -> float:
    """Settlement proxy from final prices; a dead-even read settles DOWN
    (Kalshi pays YES only when the close is strictly above the strike)."""
    if up is None or down is None:
        return 0.0
    if side == "UP":
        return 1.0 if up > down else 0.0
    return 1.0 if down >= up else 0.0


@dataclass
class PairLedger:
    bankroll: float
    open_trades: list[PairTrade] = field(default_factory=list)
    sessions: int = 0
    wins: int = 0
    losses: int = 0
    stop_losses: int = 0
    breakevens: int = 0
    fees_paid: float = 0.0

    def entry_cost_and_fees(self, legs: list[dict], fee_rate: float) -> tuple[float, float]:
        cost = sum(leg["price"] * leg["qty"] for leg in legs)
        fees = sum(fee_per_contract(leg["price"], fee_rate) * leg["qty"] for leg in legs)
        return cost, fees

    def open_trade(self, name: str, legs: list[dict], now_utc: str,
                   fee_rate: float) -> Optional[PairTrade]:
        cost, fees = self.entry_cost_and_fees(legs, fee_rate)
        if cost + fees > self.bankroll:
            return None
        qty = min(leg["qty"] for leg in legs)
        trade = PairTrade(name, legs, qty, cost, fees, now_utc)
        self.bankroll -= cost + fees
        self.fees_paid += fees
        self.open_trades.append(trade)
        return trade

    def close_all(self, mode: str, fee_rate: float,
                  current_prices: Optional[dict] = None,
                  final_prices: Optional[dict] = None) -> CloseResult:
        """Close every open trade. EXIT sells at ``current_prices`` (with fees);
        RESOLUTION settles from ``final_prices``, each ``{asset: (up, down)}``."""
        result = CloseResult()
        for trade in self.open_trades:
            trade_revenue = 0.0
            trade_exit_fees = 0.0
            for leg in trade.legs:
                if mode == "RESOLUTION":
                    up, down = (final_prices or {}).get(leg["asset"], (None, None))
                    trade_revenue += resolution_leg_value(leg["side"], up, down) * leg["qty"]
                else:
                    price = (current_prices or {}).get((leg["asset"], leg["side"]), 0.0) or 0.0
                    trade_revenue += price * leg["qty"]
                    trade_exit_fees += fee_per_contract(price, fee_rate) * leg["qty"]
            result.trades += 1
            result.contracts += trade.qty
            result.entry_cost += trade.entry_cost
            result.revenue += trade_revenue
            result.fees += trade.entry_fees + trade_exit_fees
            self.bankroll += trade_revenue - trade_exit_fees
            self.fees_paid += trade_exit_fees
        self.open_trades = []
        result.pnl = result.revenue - result.entry_cost - result.fees

        if result.trades > 0:
            self.sessions += 1
            if result.pnl > 0:
                self.wins += 1
                result.outcome = "WIN"
            elif result.pnl < 0:
                self.losses += 1
                if mode == "EXIT":
                    self.stop_losses += 1
                result.outcome = "LOSS"
            else:
                self.breakevens += 1
                result.outcome = "BREAKEVEN"
        return result

    def locked_name(self) -> Optional[str]:
        return self.open_trades[0].name if self.open_trades else None

    def open_value(self, current_prices: dict) -> float:
        value = 0.0
        for trade in self.open_trades:
            for leg in trade.legs:
                price = current_prices.get((leg["asset"], leg["side"]))
                if price is None:
                    price = leg["price"]
                value += price * leg["qty"]
        return value

    # -- persistence (counters only; open intraday trades are not carried over) ----
    def to_dict(self) -> dict:
        return {"bankroll": self.bankroll, "sessions": self.sessions,
                "wins": self.wins, "losses": self.losses,
                "stop_losses": self.stop_losses, "breakevens": self.breakevens,
                "fees_paid": self.fees_paid}

    @classmethod
    def from_dict(cls, data: dict) -> "PairLedger":
        return cls(bankroll=float(data.get("bankroll", 0.0)),
                   sessions=int(data.get("sessions", 0)),
                   wins=int(data.get("wins", 0)),
                   losses=int(data.get("losses", 0)),
                   stop_losses=int(data.get("stop_losses", 0)),
                   breakevens=int(data.get("breakevens", 0)),
                   fees_paid=float(data.get("fees_paid", 0.0)))
