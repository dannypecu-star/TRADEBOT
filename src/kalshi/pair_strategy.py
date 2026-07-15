"""BTC/ETH inverse-pair paper-trading strategy.

This is a Python port of a self-contained AutoHotkey paper-trading bot that trades the
Kalshi 15-minute crypto up/down markets as a *correlation pair*. The idea:

* A "pair" is one leg on each asset that point opposite ways, e.g. ``BTC_UP + ETH_DOWN``.
  Its price is the sum of the two leg asks (``btc.up + eth.down``). When BTC and ETH are
  correlated the market prices the two legs so that sum sits near ``$1.00``. When the two
  markets transiently *diverge* in odds, the inverse pair gets cheap -- that dip into the
  ``EntryRange`` (default 0.80-0.90) is the entry signal, a bet that correlation reasserts.
* A **behavior gate** looks at the *underlying spot* moves (not the odds): only enter when
  BTC and ETH spot are moving the same direction ("SYNC"). Divergence / delayed-follow /
  crossover-instability states block new entries.
* Entries are scaled in as **shots** (up to ``shots`` per 15m market, ``order_size``
  contracts per leg, with a ``cooldown_sec`` between shots), and no new shots are allowed
  in the last ``no_entry_minutes`` of the market.
* While a pair is held, if the *current* pair sum falls below ``PairExit`` (0.60) it is
  stopped out at current prices. Otherwise it is held to resolution, where each leg pays
  ``$1`` if it won and ``$0`` if it lost.

Everything here is deterministic given its inputs -- no clock, no network. The caller
drives it tick-by-tick (see ``PairTrader.on_tick``) with market snapshots, underlying
spot prices and a millisecond timestamp, which is what makes it runnable and testable
offline against the synthetic feed in :mod:`src.kalshi.pair_sim`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------------------
# Configuration (mirrors the strategy block at the top of the AHK script)
# ---------------------------------------------------------------------------


@dataclass
class PairConfig:
    # --- strategy knobs ---
    entry_range: tuple[float, float] = (0.80, 0.90)  # enter when inverse sum is in here
    pair_exit: float = 0.60          # stop loss: exit if current pair sum drops below this
    order_size: int = 5              # contracts per leg per shot
    shots: int = 7                   # max entry shots per 15m market
    cooldown_sec: int = 120          # wait after one shot before the next
    time_delay_min: int = 15         # activation window (whole 15m market)
    no_entry_minutes: int = 1        # last N minutes: hold only, no new shots
    initial_balance: float = 10000.0

    # --- behavior gate ---
    behavior_lookback_ms: int = 10000    # velocity window
    behavior_sign_eps_bps: float = 0.25  # |move| <= this bps counts as flat/baseline
    behavior_ema_alpha: float = 0.22     # smoothing for the behavior score stream

    # --- paper safety ---
    paper_loss_exit_floor: float = 0.05  # if pair_exit <= 0, use this floor so bad
    #                                      trades still close/register

    def entry_min(self) -> float:
        return min(self.entry_range)

    def entry_max(self) -> float:
        return max(self.entry_range)

    def sum_in_entry_range(self, s: float | None) -> bool:
        if s is None:
            return False
        return self.entry_min() <= s <= self.entry_max()

    def effective_exit(self) -> float:
        """The stop-loss threshold actually used (with the paper floor fallback)."""
        if self.pair_exit <= 0 < self.paper_loss_exit_floor:
            return self.paper_loss_exit_floor
        return self.pair_exit

    def exit_note(self) -> str:
        if self.pair_exit <= 0 < self.paper_loss_exit_floor:
            return f" (PairExit 0 -> {self.paper_loss_exit_floor:g} paper loss floor)"
        return ""


# ---------------------------------------------------------------------------
# Market + spot inputs
# ---------------------------------------------------------------------------


@dataclass
class MarketSnapshot:
    """One asset's 15m up/down market at a point in time."""
    ticker: str
    up: float | None            # "yes" ask in dollars (prob price up)
    down: float | None          # "no" ask in dollars (prob price down)
    minutes_left: float
    quarter_index: int

    def resolves_up(self) -> bool:
        """Which side wins at settlement, from the final prices (up wins on a tie)."""
        up = 0.0 if self.up is None else self.up
        down = 0.0 if self.down is None else self.down
        return up >= down


# ---------------------------------------------------------------------------
# Behavior gate (underlying-spot based)
# ---------------------------------------------------------------------------

# States, matching the AHK classifier.
SYNC = "SYNC"
DIVERGENCE = "DIVERGENCE"
DELAYED_FOLLOW = "DELAYED FOLLOW"
CROSSOVER = "CROSSOVER INSTABILITY"
WAITING = "WAITING"


@dataclass
class BehaviorState:
    state: str = WAITING
    score: float | None = None
    btc_move_bps: float | None = None
    eth_move_bps: float | None = None

    def entry_allowed(self) -> bool:
        return self.state == SYNC


def _sign(value: float | None, eps_bps: float) -> int:
    if value is None or abs(value) <= eps_bps:
        return 0
    return 1 if value > 0 else -1


class BehaviorTracker:
    """Classifies the BTC/ETH relationship from underlying spot moves.

    Reproduces the AHK ``UpdatePairBehaviorState`` logic: log-move in bps from the session
    open, a velocity term over a lookback window, and a raw score EMA-smoothed for the
    stream. The instantaneous ``state`` drives the SYNC entry gate; ``score`` is the
    smoothed stream value shown in logs.
    """

    def __init__(self, cfg: PairConfig):
        self.cfg = cfg
        self.session_key: str | None = None
        self.btc_open: float | None = None
        self.eth_open: float | None = None
        self.ema_score: float | None = None
        self.state = WAITING
        self.score: float | None = None
        self.prev_btc_move: float | None = None
        self.prev_eth_move: float | None = None
        self.last_update_ms: int = 0
        self._history: list[tuple[int, float, float]] = []  # (ms, btc_bps, eth_bps)

    def reset(self, session_key: str | None) -> None:
        self.session_key = session_key
        self.btc_open = None
        self.eth_open = None
        self.ema_score = None
        self.state = WAITING
        self.score = None
        self.prev_btc_move = None
        self.prev_eth_move = None
        self.last_update_ms = 0
        self._history = []

    def _lookback(self, now_ms: int) -> tuple[float, float] | None:
        target = now_ms - self.cfg.behavior_lookback_ms
        found = None
        for ms, b, e in self._history:
            if ms <= target:
                found = (b, e)
            else:
                break
        return found

    def snapshot(self) -> BehaviorState:
        return BehaviorState(
            state=self.state or WAITING,
            score=self.score,
            btc_move_bps=self.prev_btc_move,
            eth_move_bps=self.prev_eth_move,
        )

    def update(
        self,
        session_key: str,
        now_ms: int,
        btc_spot: float | None,
        eth_spot: float | None,
        btc_open: float | None = None,
        eth_open: float | None = None,
    ) -> BehaviorState:
        if self.session_key != session_key:
            self.reset(session_key)

        # Throttle recompute, like the 250ms guard in the AHK version.
        if self.last_update_ms and (now_ms - self.last_update_ms) < 250:
            return self.snapshot()

        if not btc_spot or not eth_spot or btc_spot <= 0 or eth_spot <= 0:
            self.state = WAITING
            self.score = None
            return self.snapshot()

        if self.btc_open is None:
            self.btc_open = btc_open if (btc_open and btc_open > 0) else btc_spot
        if self.eth_open is None:
            self.eth_open = eth_open if (eth_open and eth_open > 0) else eth_spot
        if self.btc_open <= 0 or self.eth_open <= 0:
            self.state = WAITING
            self.score = None
            return self.snapshot()

        btc_move = math.log(btc_spot / self.btc_open) * 10000.0
        eth_move = math.log(eth_spot / self.eth_open) * 10000.0

        lb = self._lookback(now_ms)
        if lb is not None:
            btc_vel = btc_move - lb[0]
            eth_vel = eth_move - lb[1]
        else:
            btc_vel = eth_vel = 0.0

        eps = self.cfg.behavior_sign_eps_bps
        btc_sign = _sign(btc_move, eps)
        eth_sign = _sign(eth_move, eps)
        distance = abs(btc_move - eth_move)
        momentum_gap = abs(btc_vel - eth_vel)
        opposite = btc_sign != 0 and eth_sign != 0 and btc_sign != eth_sign
        delayed = (btc_sign != eth_sign) and not opposite

        if opposite:
            state_name = DIVERGENCE
            raw = -(distance + 14 + momentum_gap * 2)
        elif delayed:
            state_name = DELAYED_FOLLOW
            raw = -(distance * 0.85 + 6 + momentum_gap * 1.5)
        else:
            instability = distance * 0.42 + momentum_gap * 1.2
            raw = 10 - instability
            min_move = min(abs(btc_move), abs(eth_move))
            if raw < 0 or (min_move < 1 and momentum_gap > 0.4):
                state_name = CROSSOVER
                alt = instability - 8
                if alt < 1:
                    alt = 1
                alt = -alt
                if alt < raw:
                    raw = alt
            else:
                state_name = SYNC

        if self.ema_score is None:
            self.ema_score = raw
        else:
            self.ema_score += self.cfg.behavior_ema_alpha * (raw - self.ema_score)

        self.state = state_name
        self.score = self.ema_score
        self.prev_btc_move = btc_move
        self.prev_eth_move = eth_move
        self.last_update_ms = now_ms

        self._history.append((now_ms, btc_move, eth_move))
        cutoff = now_ms - self.cfg.behavior_lookback_ms * 3
        while self._history and self._history[0][0] < cutoff:
            self._history.pop(0)

        return self.snapshot()


# ---------------------------------------------------------------------------
# Pair construction
# ---------------------------------------------------------------------------

BTC_UP_ETH_DOWN = "BTC_UP_ETH_DOWN"
BTC_DOWN_ETH_UP = "BTC_DOWN_ETH_UP"


@dataclass
class PairLeg:
    asset: str      # "BTC" or "ETH"
    ticker: str
    side: str       # "UP" or "DOWN"
    price: float


@dataclass
class Pair:
    name: str
    quarter_index: int
    btc_ticker: str
    eth_ticker: str
    sum: float
    legs: list[PairLeg]
    behavior: BehaviorState = field(default_factory=BehaviorState)


def pair_sums(btc: MarketSnapshot, eth: MarketSnapshot) -> dict[str, float | None]:
    up_down = None
    down_up = None
    if btc.up is not None and eth.down is not None:
        up_down = btc.up + eth.down
    if btc.down is not None and eth.up is not None:
        down_up = btc.down + eth.up
    return {BTC_UP_ETH_DOWN: up_down, BTC_DOWN_ETH_UP: down_up}


def build_pair(name: str, btc: MarketSnapshot, eth: MarketSnapshot) -> Pair | None:
    if name == BTC_UP_ETH_DOWN:
        if btc.up is None or eth.down is None:
            return None
        legs = [
            PairLeg("BTC", btc.ticker, "UP", btc.up),
            PairLeg("ETH", eth.ticker, "DOWN", eth.down),
        ]
        s = btc.up + eth.down
    elif name == BTC_DOWN_ETH_UP:
        if btc.down is None or eth.up is None:
            return None
        legs = [
            PairLeg("BTC", btc.ticker, "DOWN", btc.down),
            PairLeg("ETH", eth.ticker, "UP", eth.up),
        ]
        s = btc.down + eth.up
    else:
        return None
    return Pair(name, btc.quarter_index, btc.ticker, eth.ticker, s, legs)


def select_pair_entry(
    cfg: PairConfig, btc: MarketSnapshot, eth: MarketSnapshot, behavior: BehaviorState
) -> Pair | None:
    """Pick the in-range inverse pair (cheaper side wins ties), matching the AHK logic."""
    sums = pair_sums(btc, eth)
    selected = None
    if cfg.sum_in_entry_range(sums[BTC_UP_ETH_DOWN]):
        selected = BTC_UP_ETH_DOWN
    if cfg.sum_in_entry_range(sums[BTC_DOWN_ETH_UP]):
        if selected is None or sums[BTC_DOWN_ETH_UP] < sums[BTC_UP_ETH_DOWN]:
            selected = BTC_DOWN_ETH_UP
    if selected is None:
        return None
    pair = build_pair(selected, btc, eth)
    if pair is not None:
        pair.behavior = behavior
    return pair


def current_pair_sum(
    pair: Pair, btc: MarketSnapshot, eth: MarketSnapshot
) -> float | None:
    """Re-price a held pair from the current market (for the stop-loss check)."""
    if pair.name == BTC_UP_ETH_DOWN:
        if btc.up is not None and eth.down is not None:
            return btc.up + eth.down
    elif pair.name == BTC_DOWN_ETH_UP:
        if btc.down is not None and eth.up is not None:
            return btc.down + eth.up
    return None


# ---------------------------------------------------------------------------
# Paper accounting
# ---------------------------------------------------------------------------


@dataclass
class _TradeLeg:
    asset: str
    ticker: str
    side: str
    entry: float
    qty: int


@dataclass
class _Trade:
    market_key: tuple[str, str]
    pair_name: str
    legs: list[_TradeLeg]
    entry_cost: float
    qty: int


@dataclass
class CloseResult:
    trades: int = 0
    contracts: int = 0
    entry_cost: float = 0.0
    revenue: float = 0.0
    resolution_value: float = 0.0
    pnl: float = 0.0
    outcome: str = ""


def _leg_current_price(
    leg: _TradeLeg, btc: MarketSnapshot | None, eth: MarketSnapshot | None
) -> float | None:
    snap = btc if leg.asset == "BTC" else eth
    if snap is None:
        return None
    return snap.up if leg.side == "UP" else snap.down


def _leg_resolution_value(leg: _TradeLeg, snap: MarketSnapshot | None) -> float:
    if snap is None:
        return 0.0
    up_wins = snap.resolves_up()
    if leg.side == "UP":
        return 1.0 if up_wins else 0.0
    return 0.0 if up_wins else 1.0


class PaperBook:
    """Cash + open positions, valued like the AHK paper accounting."""

    def __init__(self, cfg: PairConfig):
        self.cfg = cfg
        self.cash = cfg.initial_balance
        self.open_trades: list[_Trade] = []
        self.wins = 0
        self.losses = 0
        self.stop_losses = 0
        self.breakevens = 0
        self.completed_sessions = 0
        self.markets_participated = 0
        self._participated: set[tuple[str, str]] = set()
        self._completed: set[tuple[str, str]] = set()

    def min_entry_cost(self) -> float:
        return self.cfg.entry_min() * self.cfg.order_size

    def can_afford_minimum(self) -> bool:
        return self.cash + 1e-6 >= self.min_entry_cost()

    def entry_cost(self, pair: Pair) -> float:
        return sum(leg.price for leg in pair.legs) * self.cfg.order_size

    def can_afford(self, pair: Pair) -> bool:
        return self.cash + 1e-6 >= self.entry_cost(pair)

    def position_qty(self, ticker: str) -> int:
        qty = 0
        for t in self.open_trades:
            for leg in t.legs:
                if leg.ticker == ticker:
                    qty += leg.qty
        return qty

    def buy_pair(self, pair: Pair) -> _Trade | None:
        if not self.can_afford(pair):
            return None
        n = self.cfg.order_size
        legs = [_TradeLeg(l.asset, l.ticker, l.side, l.price, n) for l in pair.legs]
        cost = sum(l.entry * l.qty for l in legs)
        market_key = (pair.btc_ticker, pair.eth_ticker)
        trade = _Trade(market_key, pair.name, legs, cost, n)
        self.cash -= cost
        self.open_trades.append(trade)
        if market_key not in self._participated:
            self._participated.add(market_key)
            self.markets_participated += 1
        return trade

    def open_value(
        self, btc: MarketSnapshot | None = None, eth: MarketSnapshot | None = None
    ) -> float:
        value = 0.0
        for t in self.open_trades:
            for leg in t.legs:
                price = _leg_current_price(leg, btc, eth)
                if price is None:
                    price = leg.entry
                value += price * leg.qty
        return value

    def equity(
        self, btc: MarketSnapshot | None = None, eth: MarketSnapshot | None = None
    ) -> float:
        return self.cash + self.open_value(btc, eth)

    def live_pnl(
        self, btc: MarketSnapshot | None = None, eth: MarketSnapshot | None = None
    ) -> float:
        return self.equity(btc, eth) - self.cfg.initial_balance

    def close_pair(
        self,
        pair: Pair,
        btc: MarketSnapshot | None,
        eth: MarketSnapshot | None,
        mode: str,
    ) -> CloseResult:
        """Close every open trade for ``pair``'s market. ``mode`` is EXIT or RESOLUTION."""
        key = (pair.btc_ticker, pair.eth_ticker)
        result = CloseResult()
        kept: list[_Trade] = []
        for t in self.open_trades:
            if t.market_key != key:
                kept.append(t)
                continue
            trade_revenue = 0.0
            trade_resolution = 0.0
            for leg in t.legs:
                if mode == "RESOLUTION":
                    snap = btc if leg.asset == "BTC" else eth
                    value = _leg_resolution_value(leg, snap)
                    trade_resolution += value
                    trade_revenue += value * leg.qty
                else:
                    price = _leg_current_price(leg, btc, eth)
                    if price is None:
                        price = 0.0
                    trade_revenue += price * leg.qty
            result.trades += 1
            result.contracts += t.qty
            result.entry_cost += t.entry_cost
            result.revenue += trade_revenue
            result.resolution_value = max(result.resolution_value, trade_resolution)
            self.cash += trade_revenue
        self.open_trades = kept
        result.pnl = result.revenue - result.entry_cost

        if result.trades > 0:
            if key not in self._completed:
                self._completed.add(key)
                self.completed_sessions += 1
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


# ---------------------------------------------------------------------------
# Formatting helpers (compact log lines, like the AHK Fmt* helpers)
# ---------------------------------------------------------------------------


def fmt_money(v: float) -> str:
    return ("-$" if v < 0 else "$") + f"{abs(v):.2f}"


def fmt_signed_money(v: float) -> str:
    return ("-$" if v < 0 else "+$") + f"{abs(v):.2f}"


def fmt_sum(v: float | None) -> str:
    return "n/a" if v is None else f"{round(v, 4):g}"


def fmt_score(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{round(v, 1 if abs(v) >= 100 else 2)}"


# ---------------------------------------------------------------------------
# The trading state machine
# ---------------------------------------------------------------------------

Logger = Callable[[str], None]


class PairTrader:
    """Drives the strategy tick-by-tick over a stream of aligned snapshots.

    Paper mode fills instantly, so the live-order pending/BUY_PENDING states from the AHK
    original collapse away: a submitted shot is an immediate fill. What remains is the
    real decision logic -- session rollover, the entry window, the SYNC gate, shot
    scaling with cooldown, the stop-loss and hold-to-resolution.
    """

    def __init__(self, cfg: PairConfig, logger: Logger | None = None):
        self.cfg = cfg
        self.book = PaperBook(cfg)
        self.behavior = BehaviorTracker(cfg)
        self.log: Logger = logger or (lambda _msg: None)

        self.phase = "WAIT_WINDOW"
        self.session_key: tuple[str, str] | None = None
        self.active_pair: Pair | None = None
        self.shots_submitted = 0
        self.last_shot_ms = 0
        self._last_btc: MarketSnapshot | None = None
        self._last_eth: MarketSnapshot | None = None

        if not self.book.can_afford_minimum():
            self.phase = "NO_BALANCE"

    # --- shot/cooldown helpers ---
    def _cooldown_remaining_ms(self, now_ms: int) -> int:
        if self.last_shot_ms == 0:
            return 0
        remaining = self.cfg.cooldown_sec * 1000 - (now_ms - self.last_shot_ms)
        return remaining if remaining > 0 else 0

    def _reset_shots(self) -> None:
        self.shots_submitted = 0
        self.last_shot_ms = 0

    def _entry_signal_met(self, pair: Pair | None) -> bool:
        if pair is None:
            return False
        if not self.cfg.sum_in_entry_range(pair.sum):
            return False
        return pair.behavior.entry_allowed()

    def _submit_shot(self, pair: Pair, now_ms: int, adding: bool) -> bool:
        if self.shots_submitted >= self.cfg.shots:
            return False
        if not self.book.can_afford(pair):
            self.log(
                f"{pair.name} PAPER entry blocked: not enough balance | required "
                f"{fmt_money(self.book.entry_cost(pair))} | cash {fmt_money(self.book.cash)}"
            )
            if self.active_pair is None and not self.book.can_afford_minimum():
                self.phase = "NO_BALANCE"
            return False

        trade = self.book.buy_pair(pair)
        if trade is None:
            return False
        self.shots_submitted += 1
        self.last_shot_ms = now_ms
        self.active_pair = pair
        self.phase = "IN_PAIR"
        b = pair.behavior
        btext = (
            f" | Behavior {b.state} score {fmt_score(b.score)}"
            if b.state != WAITING
            else ""
        )
        verb = "add shot" if adding else "shot"
        self.log(
            f"{pair.name} {verb} {self.shots_submitted}/{self.cfg.shots} @ sum "
            f"{fmt_sum(pair.sum)} in range {self.cfg.entry_min():g}-{self.cfg.entry_max():g}"
            f"{btext} | {self.cfg.order_size} contracts/leg | cost "
            f"{fmt_money(trade.entry_cost)} | cash {fmt_money(self.book.cash)}"
        )
        return True

    # --- lifecycle ---
    def _resolve_active(self) -> None:
        pair = self.active_pair
        if pair is None:
            return
        res = self.book.close_pair(pair, self._last_btc, self._last_eth, "RESOLUTION")
        self.log(
            f"{pair.name} held to resolution | {res.outcome} | payout "
            f"{fmt_sum(res.resolution_value)} | revenue {fmt_money(res.revenue)} | "
            f"close P/L {fmt_signed_money(res.pnl)} | total P/L "
            f"{fmt_signed_money(self.book.live_pnl())}"
        )
        self.active_pair = None
        self._reset_shots()

    def _try_exit(self, btc: MarketSnapshot, eth: MarketSnapshot) -> bool:
        pair = self.active_pair
        if pair is None:
            return False
        self._last_btc, self._last_eth = btc, eth
        cur = current_pair_sum(pair, btc, eth)
        eff = self.cfg.effective_exit()
        if cur is None or cur >= eff:
            return False
        res = self.book.close_pair(pair, btc, eth, "EXIT")
        self.log(
            f"{pair.name} PAPER stop-out @ locked sum {fmt_sum(cur)} < {fmt_sum(eff)}"
            f"{self.cfg.exit_note()} | {res.outcome} | revenue {fmt_money(res.revenue)} | "
            f"close P/L {fmt_signed_money(res.pnl)} | closed {res.contracts} pair contracts"
            f" | P/L {fmt_signed_money(self.book.live_pnl(btc, eth))}"
        )
        self.active_pair = None
        self._reset_shots()
        self.phase = "EXITED" if self.book.can_afford_minimum() else "NO_BALANCE"
        return True

    def on_tick(
        self,
        btc: MarketSnapshot | None,
        eth: MarketSnapshot | None,
        btc_spot: float | None,
        eth_spot: float | None,
        now_ms: int,
        btc_open: float | None = None,
        eth_open: float | None = None,
    ) -> None:
        if btc is None or eth is None:
            return

        session_key = (btc.ticker, eth.ticker)
        behavior = self.behavior.update(
            f"{btc.ticker}|{eth.ticker}", now_ms, btc_spot, eth_spot, btc_open, eth_open
        )

        # Session rollover: resolve a held pair from the prior market.
        if self.active_pair is not None and (
            self.active_pair.quarter_index != btc.quarter_index
            or self.active_pair.btc_ticker != btc.ticker
            or self.active_pair.eth_ticker != eth.ticker
        ):
            self._resolve_active()
            self.phase = (
                "WAIT_WINDOW" if self.book.can_afford_minimum() else "NO_BALANCE"
            )

        if self.session_key != session_key:
            self.session_key = session_key
            if self.active_pair is None:
                self._reset_shots()
                self.phase = (
                    "WAIT_WINDOW" if self.book.can_afford_minimum() else "NO_BALANCE"
                )
                self.log(f"PAIR New session | BTC {btc.ticker} | ETH {eth.ticker}")

        if self.phase == "NO_BALANCE":
            return

        self._last_btc, self._last_eth = btc, eth
        mins_left = min(btc.minutes_left, eth.minutes_left)

        # Holding a pair: manage exit + scale-in shots.
        if self.active_pair is not None:
            if self._try_exit(btc, eth):
                return
            in_window = 0 < mins_left <= self.cfg.time_delay_min
            can_add = (
                in_window
                and mins_left > self.cfg.no_entry_minutes
                and self.shots_submitted < self.cfg.shots
                and self._cooldown_remaining_ms(now_ms) == 0
            )
            if can_add:
                cand = build_pair(self.active_pair.name, btc, eth)
                if cand is not None:
                    cand.behavior = behavior
                    if self._entry_signal_met(cand):
                        self._submit_shot(cand, now_ms, adding=True)
            return

        # Flat: look for a fresh entry inside the window.
        in_window = 0 < mins_left <= self.cfg.time_delay_min
        entry_allowed = (
            in_window
            and mins_left > self.cfg.no_entry_minutes
            and self.shots_submitted < self.cfg.shots
            and self._cooldown_remaining_ms(now_ms) == 0
        )
        if not entry_allowed:
            self.phase = "WAIT_WINDOW"
            return

        if self.phase != "MONITORING":
            self.phase = "MONITORING"
        cand = select_pair_entry(self.cfg, btc, eth, behavior)
        if self._entry_signal_met(cand):
            self._submit_shot(cand, now_ms, adding=False)

    def summary(self) -> dict[str, float | int]:
        return {
            "cash": round(self.book.cash, 2),
            "equity": round(self.book.equity(), 2),
            "pnl": round(self.book.live_pnl(), 2),
            "wins": self.book.wins,
            "losses": self.book.losses,
            "stop_losses": self.book.stop_losses,
            "breakevens": self.book.breakevens,
            "completed_sessions": self.book.completed_sessions,
            "markets_participated": self.book.markets_participated,
        }
