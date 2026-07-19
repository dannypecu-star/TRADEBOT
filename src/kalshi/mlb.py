"""MLB moneyline paper trading: match Kalshi game markets to sportsbook odds.

The edge thesis: sharp sportsbooks are the best public probability estimate for game
winners. We devig their odds into a fair probability (src/kalshi/odds.py) and buy the
Kalshi side only when its price is cheaper than fair by more than fees + a margin.

Everything in this module is pure data processing so it can be unit-tested offline:

  * team-name matching between The Odds API (full names, "New York Yankees") and
    Kalshi markets (titles/subtitles, ticker suffixes) via nicknames;
  * a paper ledger that opens positions at the ask, charges Kalshi's fee, and settles
    against the market's official result -- no fills are assumed better than quoted.

Doubleheaders are the classic trap in MLB matching (same two teams, same day, two
markets). We disambiguate by picking the odds game whose start time is closest to the
Kalshi market's close time, and refuse the match if they disagree by more than
``MAX_MATCH_GAP_HOURS``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from .economics import fee_per_contract

# MLB nicknames are unique league-wide, which makes them a robust join key between
# data sources that disagree on city naming ("LA" vs "Los Angeles") or use ticker
# codes. Only these three nicknames span two words.
TWO_WORD_NICKNAMES = ("Red Sox", "White Sox", "Blue Jays")

MAX_MATCH_GAP_HOURS = 12.0


def nickname(full_name: str) -> str:
    name = (full_name or "").strip()
    for two in TWO_WORD_NICKNAMES:
        if name.endswith(two):
            return two
    return name.rsplit(" ", 1)[-1] if name else ""


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _contains_word(haystack: str, needle: str) -> bool:
    return re.search(r"\b" + re.escape(needle.lower()) + r"\b", haystack.lower()) is not None


@dataclass
class MarketMatch:
    game_id: str
    outcome: str          # full team name whose win the market's YES side represents
    home_team: str
    away_team: str
    commence_time: str


def match_market(market: dict, games: dict[str, dict]) -> Optional[MarketMatch]:
    """Map one Kalshi market to (odds game, YES-side team), or None if ambiguous.

    ``games`` is the output of ``fair_probabilities_from_payload``. A match requires:
    both teams' nicknames present in the market title (game identity), an unambiguous
    YES side (from yes_sub_title, or a "will the X beat/win" title pattern), and start
    time vs close time within ``MAX_MATCH_GAP_HOURS`` (doubleheader guard).
    """
    title = market.get("title") or ""
    yes_sub = market.get("yes_sub_title") or ""
    close_dt = _parse_iso(market.get("close_time") or market.get("expected_expiration_time") or "")

    best: Optional[tuple[float, MarketMatch]] = None
    for game_id, game in games.items():
        home, away = game.get("home_team") or "", game.get("away_team") or ""
        hn, an = nickname(home), nickname(away)
        if not hn or not an:
            continue
        if not (_contains_word(title, hn) and _contains_word(title, an)):
            continue

        outcome = _yes_side(title, yes_sub, home, away)
        if outcome is None:
            continue

        commence_dt = _parse_iso(game.get("commence_time") or "")
        if close_dt is not None and commence_dt is not None:
            gap_h = abs((close_dt - commence_dt).total_seconds()) / 3600.0
        else:
            gap_h = MAX_MATCH_GAP_HOURS  # unknown timing: allowed, but loses ties
        if gap_h > MAX_MATCH_GAP_HOURS:
            continue

        candidate = MarketMatch(game_id, outcome, home, away, game.get("commence_time") or "")
        if best is None or gap_h < best[0]:
            best = (gap_h, candidate)

    return best[1] if best else None


def _yes_side(title: str, yes_sub: str, home: str, away: str) -> Optional[str]:
    hn, an = nickname(home), nickname(away)
    if yes_sub:
        h_in, a_in = _contains_word(yes_sub, hn), _contains_word(yes_sub, an)
        if h_in and not a_in:
            return home
        if a_in and not h_in:
            return away
    m = re.search(r"will the (.+?) (?:beat|win|defeat)", title.lower())
    if m:
        segment = m.group(1)
        h_in, a_in = _contains_word(segment, hn), _contains_word(segment, an)
        if h_in and not a_in:
            return home
        if a_in and not h_in:
            return away
    return None


# --------------------------------------------------------------------------------
# Paper ledger
# --------------------------------------------------------------------------------

@dataclass
class Position:
    ticker: str
    event_ticker: str
    side: str            # "yes" or "no"
    price: float         # dollars per contract actually paid (the quoted ask)
    contracts: int
    fees: float          # entry fees, dollars, already deducted from bankroll
    fair_prob: float     # our estimate for the YES event at entry time
    edge: float          # net $ edge per contract at entry
    game: str
    opened_utc: str


@dataclass
class SettleResult:
    position: Position
    result: str          # market's official "yes" / "no"
    payout: float
    pnl: float           # payout - cost - fees


@dataclass
class PaperLedger:
    bankroll: float
    positions: dict[str, Position] = field(default_factory=dict)
    settled_count: int = 0
    wins: int = 0

    def can_open(self, ticker: str, event_ticker: str, max_positions: int) -> bool:
        if ticker in self.positions or len(self.positions) >= max_positions:
            return False
        # One position per event: holding YES on both teams of the same game is
        # a guaranteed loss after fees, so the whole event is locked once entered.
        return all(p.event_ticker != event_ticker for p in self.positions.values())

    def open(self, ticker: str, event_ticker: str, side: str, price: float,
             contracts: int, fair_prob: float, edge_value: float, game: str,
             now_utc: str, fee_rate: float = 0.07) -> Optional[Position]:
        fees = fee_per_contract(price, fee_rate) * contracts
        cost = price * contracts + fees
        if contracts < 1 or cost > self.bankroll:
            return None
        self.bankroll -= cost
        pos = Position(ticker, event_ticker, side, price, contracts, fees,
                       fair_prob, edge_value, game, now_utc)
        self.positions[ticker] = pos
        return pos

    def settle(self, ticker: str, result: str) -> Optional[SettleResult]:
        pos = self.positions.pop(ticker, None)
        if pos is None:
            return None
        won = (result == pos.side)
        payout = float(pos.contracts) if won else 0.0
        self.bankroll += payout
        pnl = payout - pos.price * pos.contracts - pos.fees
        self.settled_count += 1
        if pnl > 0:
            self.wins += 1
        return SettleResult(pos, result, payout, pnl)

    # -- persistence ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "bankroll": self.bankroll,
            "settled_count": self.settled_count,
            "wins": self.wins,
            "positions": {t: asdict(p) for t, p in self.positions.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PaperLedger":
        ledger = cls(bankroll=float(data.get("bankroll", 0.0)),
                     settled_count=int(data.get("settled_count", 0)),
                     wins=int(data.get("wins", 0)))
        for ticker, raw in (data.get("positions") or {}).items():
            ledger.positions[ticker] = Position(**raw)
        return ledger
