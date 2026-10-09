"""AI direction strategy for Kalshi's 15-minute crypto up/down markets -- PAPER ONLY.

A standalone port of the NightShark "KALSHI AI PAPER STRATEGY" script. Everything it
needs comes from public endpoints, so no Kalshi account or NightShark is required:

* market list, order books and settlement results -> Kalshi public REST API
* recent spot candles for the AI prompt            -> Coinbase / OKX public REST API
* the UP / DOWN / SKIP decision                    -> Claude (needs ANTHROPIC_API_KEY),
                                                      or a free momentum baseline

Per asset, once per 15-minute market:
    wait until ``time_delay`` minutes remain -> ask the predictor once ->
    optionally invert (obey_model = NO) -> simulate a buy against the live order book ->
    hold to settlement -> score it with Kalshi's ``result``.

Differences from the original script (each fixes something the original got wrong):

* Simulated Kalshi taker fees on entry instead of zero.
* Fills walk the order book instead of filling any size at the top ask.
* ``max_entry_price`` acts as a limit price, and no entry is made with fewer than
  ``min_entry_minutes`` left.
* The daily loss limit is a hard cap: entries are sized so settled PnL plus the
  worst case of every open position can never go below ``max_daily_loss``.
* The daily limits really are daily: they reset at local midnight, and trading resumes.
* Session boundaries come from each market's ``close_time``, not the PC clock's quarter.
* Voided markets settle at zero PnL instead of staying pending forever.
"""
from __future__ import annotations

import csv
import math
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Literal, Optional, Protocol

import requests

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {
    "BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M",
    "DOGE": "KXDOGE15M", "HYPE": "KXHYPE15M", "BNB": "KXBNB15M",
}
SIDES = ("UP", "DOWN")  # UP = buy YES, DOWN = buy NO


# ==================== Settings ====================
@dataclass
class Settings:
    assets: list[str] = field(default_factory=lambda: ["BTC"])
    order_size: float = 300           # contracts per trade (before loss-budget capping)
    time_delay: float = 15            # request the AI when this many minutes remain
    obey_model: bool = True           # False trades the opposite of the model
    max_daily_profit: float = 0       # stop for the day at this settled PnL; 0 disables
    max_daily_loss: float = 0         # hard daily loss cap; 0 disables
    max_entry_price: float = 0.75     # never pay more than this per contract
    min_entry_minutes: float = 2.0    # no new entries with less time left than this
    fee_rate: float = 0.07            # Kalshi general taker fee coefficient
    allow_skip: bool = False          # False forces UP/DOWN every market so every call is scored
    poll_seconds: float = 1.0
    log_dir: str = "logs"

    def validate(self) -> list[str]:
        errors = []
        if not self.assets:
            errors.append("assets must contain at least one asset")
        for a in self.assets:
            if a not in SERIES:
                errors.append(f"unsupported asset '{a}' (supported: {', '.join(SERIES)})")
        if self.order_size <= 0:
            errors.append("order_size must be a positive number of contracts")
        if not 0 < self.time_delay <= 15:
            errors.append("time_delay must be greater than 0 and at most 15 minutes")
        if self.max_daily_profit < 0:
            errors.append("max_daily_profit must be >= 0 (0 disables it)")
        if self.max_daily_loss > 0:
            errors.append("max_daily_loss must be <= 0 (0 disables it)")
        if not 0 < self.max_entry_price < 1:
            errors.append("max_entry_price must be between 0 and 1")
        if self.min_entry_minutes < 0 or self.min_entry_minutes >= self.time_delay:
            errors.append("min_entry_minutes must be >= 0 and less than time_delay")
        return errors


# ==================== Fees and order book ====================
def order_fee(count: float, price: float, fee_rate: float = 0.07) -> float:
    """Kalshi taker fee for one fill: ceil(rate * C * P * (1 - P)) to the next cent."""
    if count <= 0:
        return 0.0
    return math.ceil(round(fee_rate * count * price * (1.0 - price) * 100.0, 6)) / 100.0


@dataclass
class Fill:
    size: float = 0.0
    cost: float = 0.0     # sum of price * contracts, excluding fees
    fees: float = 0.0

    @property
    def avg_price(self) -> float:
        return self.cost / self.size if self.size else 0.0


@dataclass
class Book:
    """Top of Kalshi's order book. Kalshi only publishes bids; asks are implied.

    A YES ask at price p is a NO bid at 1 - p, so buying UP walks the NO bids and
    buying DOWN walks the YES bids. Levels are (price_dollars, contracts), best first.
    """
    yes_bids: list[tuple[float, float]]
    no_bids: list[tuple[float, float]]

    def bids(self, side: str) -> list[tuple[float, float]]:
        return self.yes_bids if side == "UP" else self.no_bids

    def asks(self, side: str) -> list[tuple[float, float]]:
        opposite = self.no_bids if side == "UP" else self.yes_bids
        return [(round(1.0 - p, 4), q) for p, q in opposite]

    def best_ask(self, side: str) -> Optional[float]:
        asks = self.asks(side)
        return asks[0][0] if asks else None

    def best_bid(self, side: str) -> Optional[float]:
        bids = self.bids(side)
        return bids[0][0] if bids else None

    def simulate_buy(self, side: str, size: float, limit_price: float,
                     fee_rate: float, max_spend: Optional[float] = None) -> Fill:
        """Walk the asks up to ``limit_price``. ``max_spend`` caps cost + fees."""
        fill = Fill()
        for price, qty in self.asks(side):
            if price > limit_price or fill.size >= size:
                break
            take = min(qty, size - fill.size)
            if max_spend is not None:
                per_contract = price + fee_rate * price * (1 - price)
                budget = max_spend - fill.cost - fill.fees - 0.01  # leave room for fee rounding
                take = min(take, math.floor(max(budget, 0) / per_contract))
            if take <= 0:
                break
            fill.size += take
            fill.cost += take * price
            fill.fees += order_fee(take, price, fee_rate)
        return fill

    def liquidation_value(self, side: str, size: float) -> float:
        """What selling ``size`` contracts into the bids would raise right now.

        Contracts beyond the visible depth are valued at zero, so the mark is
        conservative rather than missing.
        """
        value, left = 0.0, size
        for price, qty in self.bids(side):
            take = min(qty, left)
            value += take * price
            left -= take
            if left <= 0:
                break
        return value


def _levels(raw) -> list[tuple[float, float]]:
    out = []
    for level in raw or []:
        try:
            out.append((float(level[0]), float(level[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def parse_orderbook(payload: dict) -> Book:
    """Accept both the dollar (``*_dollars``) and legacy cent order-book formats."""
    ob = payload.get("orderbook_fp") or payload.get("orderbook") or {}
    sides = {}
    for name in ("yes", "no"):
        levels = _levels(ob.get(f"{name}_dollars"))
        if not levels:
            levels = [(p / 100.0, q) for p, q in _levels(ob.get(name))]
        # Kalshi lists levels in ascending price; sort so the best bid is first either way.
        sides[name] = sorted(levels, key=lambda lv: lv[0], reverse=True)
    return Book(yes_bids=sides["yes"], no_bids=sides["no"])


def parse_time(iso: str) -> Optional[datetime]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ==================== Data sources ====================
@dataclass
class Market:
    ticker: str
    close_time: datetime
    title: str = ""
    subtitle: str = ""
    strike: Optional[float] = None

    def minutes_left(self, now: datetime) -> float:
        return (self.close_time - now).total_seconds() / 60.0


class KalshiPublic:
    """Unauthenticated Kalshi market data."""

    def __init__(self, base: str = KALSHI_BASE, session: Optional[requests.Session] = None):
        self.base = base
        self.session = session or requests.Session()

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def current_market(self, series: str, now: datetime) -> Optional[Market]:
        """The open market in ``series`` that closes soonest (and is still open)."""
        data = self._get("/markets", {"series_ticker": series, "status": "open", "limit": 20})
        best = None
        for m in data.get("markets", []):
            close = parse_time(m.get("close_time", ""))
            if not m.get("ticker") or close is None or close <= now:
                continue
            if best is None or close < best.close_time:
                strike = m.get("floor_strike")
                best = Market(
                    ticker=m["ticker"], close_time=close,
                    title=m.get("title", ""), subtitle=m.get("yes_sub_title", ""),
                    strike=float(strike) if strike not in (None, "") else None,
                )
        return best

    def orderbook(self, ticker: str, depth: int = 25) -> Book:
        return parse_orderbook(self._get(f"/markets/{ticker}/orderbook", {"depth": depth}))

    def result(self, ticker: str) -> str:
        """'yes', 'no', 'void', or '' while the market is still unresolved."""
        m = self._get(f"/markets/{ticker}").get("market", {})
        result = str(m.get("result") or "").lower()
        if result in ("yes", "no"):
            return result
        if result or str(m.get("status", "")).lower() in ("voided", "void"):
            return "void"
        return ""


class SpotFeed:
    """Recent 1-minute candles from public exchange APIs (no keys needed)."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()

    def candles(self, asset: str, minutes: int = 60) -> list[dict]:
        """Oldest-first list of {time, open, high, low, close, volume}; [] if unavailable."""
        for fetch in (self._coinbase, self._okx):
            try:
                rows = fetch(asset, minutes)
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
                continue
            if rows:
                return rows
        return []

    def _coinbase(self, asset: str, minutes: int) -> list[dict]:
        resp = self.session.get(
            f"https://api.exchange.coinbase.com/products/{asset}-USD/candles",
            params={"granularity": 60}, timeout=10)
        resp.raise_for_status()
        rows = [{"time": int(r[0]), "low": float(r[1]), "high": float(r[2]),
                 "open": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
                for r in resp.json()]
        return sorted(rows, key=lambda r: r["time"])[-minutes:]

    def _okx(self, asset: str, minutes: int) -> list[dict]:
        resp = self.session.get(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": f"{asset}-USDT", "bar": "1m", "limit": min(minutes, 300)},
            timeout=10)
        resp.raise_for_status()
        rows = [{"time": int(r[0]) // 1000, "open": float(r[1]), "high": float(r[2]),
                 "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
                for r in resp.json()["data"]]
        return sorted(rows, key=lambda r: r["time"])[-minutes:]


# ==================== Predictors ====================
@dataclass
class Decision:
    direction: Literal["UP", "DOWN", "SKIP"]
    prob_up: Optional[float]
    reason: str
    model: str


class Predictor(Protocol):
    name: str

    def decide(self, ctx: dict) -> Optional[Decision]: ...


def build_prompt(ctx: dict) -> str:
    m: Market = ctx["market"]
    book: Book = ctx["book"]
    candles = ctx["candles"]
    lines = [
        f"Kalshi market {m.ticker}: {m.title} {m.subtitle}".strip(),
        f"Asset: {ctx['asset']}. Minutes until close: {ctx['minutes_left']:.2f}.",
        f"Strike (price to beat at close): {m.strike if m.strike is not None else 'unknown'}.",
        f"YES (UP) best ask {book.best_ask('UP')}, best bid {book.best_bid('UP')}; "
        f"NO (DOWN) best ask {book.best_ask('DOWN')}, best bid {book.best_bid('DOWN')}.",
        f"Kalshi taker fee is about {ctx['fee_rate']} * P * (1 - P) per contract.",
    ]
    if candles:
        lines.append(f"Last {len(candles)} one-minute {ctx['asset']}-USD candles "
                     "(unix_time,open,high,low,close,volume), oldest first:")
        lines += [f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},"
                  f"{c['volume']:.4g}" for c in candles]
    else:
        lines.append("Spot candles are unavailable right now.")
    question = ("Will the market resolve YES (price above the strike at close, direction UP) "
                "or NO (DOWN)? ")
    if ctx.get("allow_skip", True):
        question += ("Choose SKIP unless the probability you estimate beats the ask you "
                     "would pay by more than the fee. ")
    else:
        question += ("This is a data-collection run: you must answer UP or DOWN, never SKIP, "
                     "even when it is close to a coin flip. ")
    lines.append(question + "Give prob_up as your honest probability (0 to 1) that it "
                 "resolves YES, and a one-sentence reason.")
    return "\n".join(lines)


class ClaudePredictor:
    """Asks Claude for UP / DOWN / SKIP using structured outputs."""

    SYSTEM = ("You are a disciplined short-horizon crypto trader pricing Kalshi 15-minute "
              "binary contracts. Be calibrated: most 15-minute moves are noise, and paying "
              "the spread plus fees on a coin flip loses money.")

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium"):
        import anthropic
        from pydantic import BaseModel

        class _Out(BaseModel):
            direction: Literal["UP", "DOWN", "SKIP"]
            prob_up: float
            reason: str

        self._anthropic = anthropic
        self._schema = _Out
        self.client = anthropic.Anthropic()  # ANTHROPIC_API_KEY or an `ant auth login` profile
        self.model = model
        self.effort = effort
        self.name = model

    def decide(self, ctx: dict) -> Optional[Decision]:
        try:
            # fallbacks="default" re-runs a safety-classifier decline on Anthropic's
            # recommended fallback model instead of returning a refusal.
            resp = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=4000,
                system=self.SYSTEM,
                messages=[{"role": "user", "content": build_prompt(ctx)}],
                output_format=self._schema,
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except self._anthropic.APIStatusError as e:
            raise PredictorError(f"Claude API error {e.status_code}: {e.message}") from e
        except self._anthropic.APIConnectionError as e:
            raise PredictorError(f"cannot reach the Claude API: {e}") from e
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            return None
        out = resp.parsed_output
        prob = min(max(float(out.prob_up), 0.0), 1.0)
        return Decision(out.direction, prob, out.reason.strip(), resp.model)


class MomentumPredictor:
    """Free baseline: bet that the last few minutes' trend continues.

    Useful for testing the plumbing without an API key, and as the bar the AI must beat.
    It deliberately ignores the strike: at the start of a session the strike is the
    current price, so "distance from the strike" carries no signal yet.
    """

    name = "momentum"

    def __init__(self, lookback: int = 5, min_move: float = 0.0005):
        self.lookback = lookback
        self.min_move = min_move

    def decide(self, ctx: dict) -> Optional[Decision]:
        candles = ctx["candles"]
        if len(candles) <= self.lookback:
            return None
        last = candles[-1]["close"]
        ref = candles[-1 - self.lookback]["close"]
        move = (last - ref) / ref
        reason = f"{self.lookback}m move {move:+.3%} ({ref} -> {last})"
        if ctx.get("allow_skip", True) and abs(move) < self.min_move:
            return Decision("SKIP", None, f"{reason}, under {self.min_move:.2%}", self.name)
        return Decision("UP" if move >= 0 else "DOWN", None, reason, self.name)


class PredictorError(RuntimeError):
    pass


# ==================== Paper engine ====================
@dataclass
class Position:
    asset: str
    ticker: str
    side: str
    close_time: datetime
    size: float
    entry: float          # average fill price
    fees: float
    model_direction: str
    opened_at: datetime

    @property
    def worst_case(self) -> float:
        return self.size * self.entry + self.fees


@dataclass
class AssetState:
    ticker: str = ""
    phase: str = "WAIT_AI_WINDOW"
    decision: Optional[Decision] = None
    market: Optional[Market] = None
    book: Optional[Book] = None
    note: str = ""


class PaperEngine:
    def __init__(self, settings: Settings, kalshi: KalshiPublic, feed: SpotFeed,
                 predictor: Predictor, clock: Callable[[], datetime] = None,
                 log: Callable[[str], None] = print):
        self.s = settings
        self.kalshi = kalshi
        self.feed = feed
        self.predictor = predictor
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log = log
        self.states = {a: AssetState() for a in settings.assets}
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, Position] = {}          # ticker -> awaiting settlement
        self.audits: dict[str, dict] = {}               # ticker -> prediction record
        self.day: Optional[date] = None
        self.day_pnl = 0.0
        self.total_pnl = 0.0
        self.wins = 0
        self.trades = 0
        self.hits = 0
        self.scored = 0
        self.halted_day: Optional[date] = None
        self._last_settle_check = float("-inf")
        self.settle_interval = 3.0  # seconds between settlement polls
        self._market_cache: dict[str, tuple[float, Optional[Market]]] = {}
        os.makedirs(settings.log_dir, exist_ok=True)
        self.trades_csv = os.path.join(settings.log_dir, "ai15m_trades.csv")
        self.predictions_csv = os.path.join(settings.log_dir, "ai15m_predictions.csv")

    # ---- bookkeeping -------------------------------------------------------
    def _roll_day(self, now: datetime) -> None:
        today = now.astimezone().date()  # local calendar day
        if self.day != today:
            if self.day is not None:
                self.log(f"New day {today} | yesterday's settled PnL {self.day_pnl:+.2f} | "
                         "daily limits reset")
            self.day, self.day_pnl = today, 0.0

    def open_exposure(self) -> float:
        return sum(p.worst_case for p in list(self.positions.values()) + list(self.pending.values()))

    def halted(self) -> bool:
        return self.halted_day is not None and self.halted_day == self.day

    def loss_budget(self) -> Optional[float]:
        """Dollars that may still be put at risk today, or None when uncapped."""
        if self.s.max_daily_loss >= 0:
            return None
        return self.day_pnl - self.s.max_daily_loss - self.open_exposure()

    def _check_limits(self) -> None:
        if self.halted():
            return
        name = None
        if self.s.max_daily_profit > 0 and self.day_pnl >= self.s.max_daily_profit:
            name = f"max daily profit {self.s.max_daily_profit:+.2f}"
        # Entries are sized to stop just short of the loss limit, so treat "less than one
        # contract ($1) of budget left" as reaching it.
        elif self.s.max_daily_loss < 0 and self.day_pnl < self.s.max_daily_loss + 1:
            name = f"max daily loss {self.s.max_daily_loss:+.2f}"
        if name:
            self.halted_day = self.day
            self.log(f"Daily limit reached ({name}) | settled PnL today {self.day_pnl:+.2f} | "
                     "no new entries until tomorrow")

    @staticmethod
    def _append_csv(path: str, row: dict) -> None:
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row))
            if new:
                writer.writeheader()
            writer.writerow(row)

    # ---- main step ---------------------------------------------------------
    def step(self) -> None:
        now = self.clock()
        self._roll_day(now)
        self._close_expired(now)
        self._settle(now)
        self._check_limits()
        for asset in self.s.assets:
            try:
                self._step_asset(asset, now)
            except requests.RequestException as e:
                self.states[asset].note = f"network error: {e.__class__.__name__}"

    def _market(self, asset: str, now: datetime) -> Optional[Market]:
        cached = self._market_cache.get(asset)
        if cached and time.monotonic() - cached[0] < 5 and cached[1] and cached[1].close_time > now:
            return cached[1]
        market = self.kalshi.current_market(SERIES[asset], now)
        self._market_cache[asset] = (time.monotonic(), market)
        return market

    def _close_expired(self, now: datetime) -> None:
        for asset, pos in list(self.positions.items()):
            if now >= pos.close_time:
                self.pending[pos.ticker] = self.positions.pop(asset)
                self.log(f"{asset} PAPER {pos.side} held to resolution | settlement pending")

    def _step_asset(self, asset: str, now: datetime) -> None:
        st = self.states[asset]
        market = self._market(asset, now)
        if market is None:
            st.note = "waiting for an open market"
            return
        if market.ticker != st.ticker:
            self.states[asset] = st = AssetState(ticker=market.ticker)
            self.log(f"{asset} new session {market.ticker} (closes "
                     f"{market.close_time.astimezone():%H:%M:%S})")
        st.market = market
        st.book = self.kalshi.orderbook(market.ticker)
        mins = market.minutes_left(now)

        if asset in self.positions:
            st.phase = "IN_POSITION"
            return
        if st.phase == "WAIT_AI_WINDOW":
            if mins > self.s.time_delay:
                st.note = f"AI request at {self.s.time_delay:g}m left"
                return
            if self.halted():
                st.phase, st.note = "HALTED", "daily limit reached"
                return
            if mins < self.s.min_entry_minutes:
                st.phase, st.note = "MISSED", "joined too late to enter"
                return
            self._request_decision(asset, st, mins)
        if st.phase == "READY_TO_ENTER":
            self._try_enter(asset, st, mins, now)

    def _request_decision(self, asset: str, st: AssetState, mins: float) -> None:
        self.log(f"{asset} requesting {self.predictor.name} decision with {mins:.2f}m left")
        ctx = {"asset": asset, "market": st.market, "book": st.book, "minutes_left": mins,
               "candles": self.feed.candles(asset), "fee_rate": self.s.fee_rate,
               "allow_skip": self.s.allow_skip}
        try:
            decision = self.predictor.decide(ctx)
        except PredictorError as e:
            st.phase, st.note = "AI_FAILED", str(e)
            self.log(f"{asset} {e}; skipping this session")
            return
        if decision is None:
            st.phase, st.note = "AI_FAILED", "no usable decision"
            self.log(f"{asset} predictor returned no usable decision; skipping this session")
            return
        if decision.direction == "SKIP" and not self.s.allow_skip and decision.prob_up is not None:
            # SKIP is off for this run: fall back to the side the model's own probability favors.
            forced = "UP" if decision.prob_up >= 0.5 else "DOWN"
            decision = Decision(forced, decision.prob_up, f"[forced from SKIP] {decision.reason}",
                                decision.model)
        st.decision = decision
        side = self.trade_side(decision.direction)
        self.audits[st.ticker] = {
            "asset": asset, "ticker": st.ticker, "model": decision.model,
            "decided_at": self.clock().isoformat(timespec="seconds"),
            "minutes_left": round(mins, 2), "predicted": decision.direction,
            "prob_up": "" if decision.prob_up is None else round(decision.prob_up, 3),
            "up_ask": st.book.best_ask("UP"), "down_ask": st.book.best_ask("DOWN"),
            "trade_side": "", "reason": decision.reason,
        }
        prob = "" if decision.prob_up is None else f" | p(up)={decision.prob_up:.2f}"
        self.log(f"{asset} {decision.model} says {decision.direction}{prob} | {decision.reason}")
        if decision.direction == "SKIP":
            st.phase, st.note = "AI_SKIPPED", "model chose SKIP"
        else:
            st.phase = "READY_TO_ENTER"
            st.note = f"model {decision.direction} -> trading {side}"

    def trade_side(self, direction: str) -> str:
        if direction not in SIDES or self.s.obey_model:
            return direction
        return "DOWN" if direction == "UP" else "UP"

    def _try_enter(self, asset: str, st: AssetState, mins: float, now: datetime) -> None:
        if mins < self.s.min_entry_minutes:
            st.phase, st.note = "MISSED", f"no fill before the {self.s.min_entry_minutes:g}m cutoff"
            self.log(f"{asset} entry window closed without a fill")
            return
        if self.halted():
            st.phase, st.note = "HALTED", "daily limit reached"
            return
        side = self.trade_side(st.decision.direction)
        budget = self.loss_budget()
        if budget is not None and budget <= 0.01:
            st.note = "waiting: daily loss budget fully committed"
            return
        fill = st.book.simulate_buy(side, self.s.order_size, self.s.max_entry_price,
                                    self.s.fee_rate, max_spend=budget)
        if fill.size <= 0:
            ask = st.book.best_ask(side)
            st.note = (f"{side} ask {ask} above limit {self.s.max_entry_price}; retrying"
                       if ask is not None else f"no {side} asks; retrying")
            return
        pos = Position(asset, st.ticker, side, st.market.close_time, fill.size,
                       fill.avg_price, fill.fees, st.decision.direction, now)
        self.positions[asset] = pos
        st.phase, st.note = "IN_POSITION", ""
        if st.ticker in self.audits:
            self.audits[st.ticker]["trade_side"] = side
        partial = "" if fill.size >= self.s.order_size else f" (wanted {self.s.order_size:g})"
        self.log(f"{asset} PAPER BUY {side} {fill.size:g}{partial} @ avg {fill.avg_price:.4f} | "
                 f"fees {fill.fees:.2f} | max loss {pos.worst_case:.2f}")

    # ---- settlement --------------------------------------------------------
    def _settle(self, now: datetime) -> None:
        tickers = set(self.pending)
        tickers |= {t for t, a in self.audits.items()
                    if all(t != s.ticker for s in self.states.values())}
        if not tickers or time.monotonic() - self._last_settle_check < self.settle_interval:
            return
        self._last_settle_check = time.monotonic()
        for ticker in tickers:
            try:
                result = self.kalshi.result(ticker)
            except requests.RequestException:
                continue
            if not result:
                continue
            if ticker in self.pending:
                self._record_trade(self.pending.pop(ticker), result, now)
            if ticker in self.audits:
                self._record_audit(self.audits.pop(ticker), result)

    def _record_trade(self, pos: Position, result: str, now: datetime) -> None:
        if result == "void":
            payout, pnl = pos.size * pos.entry, 0.0  # voided markets refund the stake
        else:
            won = (result == "yes") == (pos.side == "UP")
            payout = pos.size if won else 0.0
            pnl = payout - pos.size * pos.entry - pos.fees
        self.day_pnl += pnl
        self.total_pnl += pnl
        if result != "void":
            self.trades += 1
            self.wins += pnl > 0
        self.log(f"{pos.asset} PAPER {pos.side} settled {result.upper()} | entry {pos.entry:.4f} "
                 f"x {pos.size:g} | fees {pos.fees:.2f} | PnL {pnl:+.2f} | "
                 f"today {self.day_pnl:+.2f}")
        self._append_csv(self.trades_csv, {
            "settled_at": now.isoformat(timespec="seconds"), "asset": pos.asset,
            "ticker": pos.ticker, "model_direction": pos.model_direction, "side": pos.side,
            "size": pos.size, "entry": round(pos.entry, 4), "fees": round(pos.fees, 2),
            "result": result, "pnl": round(pnl, 2),
        })

    def _record_audit(self, audit: dict, result: str) -> None:
        outcome = {"yes": "UP", "no": "DOWN"}.get(result, "VOID")
        if audit["predicted"] in SIDES and outcome in SIDES:
            self.scored += 1
            self.hits += audit["predicted"] == outcome
            audit["prediction_result"] = "MATCH" if audit["predicted"] == outcome else "MISS"
        else:
            audit["prediction_result"] = "N/A"
        audit["outcome"] = outcome
        self.log(f"MODEL AUDIT {audit['asset']} {audit['ticker']} | predicted "
                 f"{audit['predicted']} | outcome {outcome} | {audit['prediction_result']}")
        self._append_csv(self.predictions_csv, audit)

    # ---- display -----------------------------------------------------------
    def mark_to_market(self) -> float:
        total = 0.0
        for asset, pos in self.positions.items():
            book = self.states[asset].book
            value = book.liquidation_value(pos.side, pos.size) if book else 0.0
            total += value - pos.size * pos.entry - pos.fees
        return total
