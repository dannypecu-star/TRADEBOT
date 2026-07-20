#!/usr/bin/env python3
"""Kalshi 15m crypto convergence paper trader -- single-file, zero-setup edition.

    python kalshi_convergence_paper_standalone.py

Standard library only: no pip, no API keys, no account. Trades are simulated at the
quoted ask; results append to convergence_paper_trades.csv next to this file, and
bankroll/positions persist in convergence_paper_state.json.
PAPER ONLY: there is no code path that sends an order.

The thesis (certainty-lag / convergence)
----------------------------------------
As a 15-minute BTC/ETH market runs out of time, each contract's true value races
toward $0 or $1. Fair value late in the window is computable: from the distance
between spot and the strike, and how much movement remaining volatility allows,
P(close > strike) = Phi( ln(spot/strike) / sigma_remaining ). When spot sits far
from the strike with little time left, that probability is near-certain -- but on a
thin venue the quotes sometimes lag reality. This bot buys the near-certain side
only when its computed fair value beats the quoted ask by a wide margin after fees.

Honest caveats, stated up front:
  * Settlement and strikes are proxied with Coinbase candles; Kalshi settles on the
    CF Benchmarks index. The basis between them is small but nonzero.
  * Paper fills at the quoted ask are optimistic in exactly this strategy's moments:
    a stale cheap quote may be gone (or already taken) when a live order arrives.
    Expect live results below paper results; the paper run measures the signal, not
    the capture rate.
"""
from __future__ import annotations

import csv
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# =========================
#  Settings
# =========================
INITIAL_BANKROLL = 10000.0
ORDER_SIZE = 10           # contracts per entry
FAIR_MIN = 0.85           # only buy sides the model calls at least this likely
MIN_EDGE = 0.05           # required (fair - ask - fee) per contract, dollars
FEE_RATE = 0.07           # Kalshi taker fee coefficient
ACT_WINDOW_MIN = 5.0      # only act in the last N minutes of the window
MIN_MINUTES_LEFT = 0.25   # ...but not in the final seconds (data too stale to trust)
COOLDOWN_SEC = 15.0       # per asset, between entries
MAX_PRICE = 0.95          # never pay more than this per contract
POLL_SECONDS = 1.5
VOL_LOOKBACK_MIN = 30     # 1-minute candles used for the volatility estimate
SIGMA_FLOOR_BPS = 1.0     # per-sqrt-minute vol floor so certainty is never overstated

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M"}
COINBASE = {"BTC": "BTC-USD", "ETH": "ETH-USD"}

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(HERE, "convergence_paper_trades.csv")
STATE_FILE = os.path.join(HERE, "convergence_paper_state.json")
CSV_HEADER = ["utc", "event", "asset", "ticker", "side", "price", "fair", "edge",
              "spot", "strike", "dist_bps", "sigma_rem_bps", "mins_left", "qty",
              "cost", "fees", "result", "pnl", "bankroll"]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now_utc()}  {msg}", flush=True)


_last_note: dict[str, float] = {}


def note(key: str, msg: str, every: float = 60.0) -> None:
    now = time.monotonic()
    if now - _last_note.get(key, 0.0) >= every:
        _last_note[key] = now
        log(msg)


def http_json(url: str, params: dict | None = None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "convergence-paper-bot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fee_per_contract(price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    return math.ceil(FEE_RATE * price * (1.0 - price) * 100.0) / 100.0


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_up_probability(spot: float, strike: float, sigma_rem: float) -> float:
    """P(close > strike) with zero drift; sigma_rem is the stdev of ln(close/spot)."""
    if spot <= 0 or strike <= 0:
        return 0.5
    if sigma_rem <= 1e-9:
        return 1.0 if spot > strike else 0.0
    return normal_cdf(math.log(spot / strike) / sigma_rem)


def append_csv(row: dict) -> None:
    new_file = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def parse_iso(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


# =========================
#  Market data
# =========================
class Feeds:
    def __init__(self):
        self.minute_candles: dict[str, tuple[float, list]] = {}
        self.market: dict[str, dict] = {}
        self.market_at: dict[str, float] = {}

    def candles_1m(self, asset: str) -> list:
        cached = self.minute_candles.get(asset)
        if cached and time.monotonic() - cached[0] < 2.0:
            return cached[1]
        try:
            rows = http_json(f"https://api.exchange.coinbase.com/products/"
                             f"{COINBASE[asset]}/candles", {"granularity": 60})
            self.minute_candles[asset] = (time.monotonic(), rows)
            return rows
        except Exception as exc:  # noqa: BLE001
            note(f"{asset}_spot", f"{asset} spot fetch failed: {exc}")
            return cached[1] if cached else []

    def spot(self, asset: str) -> float | None:
        rows = self.candles_1m(asset)   # newest first: [time, low, high, open, close, vol]
        return float(rows[0][4]) if rows else None

    def sigma_per_sqrt_minute(self, asset: str) -> float | None:
        """Realized vol of 1-minute log returns (as a fraction, e.g. 0.0004)."""
        rows = self.candles_1m(asset)
        closes = [float(r[4]) for r in rows[:VOL_LOOKBACK_MIN + 1]][::-1]
        if len(closes) < 10:
            return None
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0]
        if len(rets) < 8:
            return None
        sigma = statistics.pstdev(rets)
        return max(sigma, SIGMA_FLOOR_BPS / 10000.0)

    def window_open(self, asset: str) -> float | None:
        """Open of the current 15m clock window (strike fallback)."""
        try:
            rows = http_json(f"https://api.exchange.coinbase.com/products/"
                             f"{COINBASE[asset]}/candles", {"granularity": 900})
            return float(rows[0][3]) if rows else None
        except Exception as exc:  # noqa: BLE001
            note(f"{asset}_open", f"{asset} window-open fetch failed: {exc}")
            return None

    def candle_close_for_window(self, asset: str, window_start_epoch: int) -> float | None:
        """Close of the completed 15m candle that started at the given epoch."""
        try:
            rows = http_json(f"https://api.exchange.coinbase.com/products/"
                             f"{COINBASE[asset]}/candles", {"granularity": 900})
        except Exception as exc:  # noqa: BLE001
            note(f"{asset}_settle", f"{asset} settle fetch failed: {exc}")
            return None
        for row in rows:
            if abs(int(row[0]) - window_start_epoch) <= 1:
                return float(row[4])
        return None

    def kalshi_market(self, asset: str) -> dict | None:
        now = time.monotonic()
        market = self.market.get(asset)
        if market:
            close = parse_iso(market.get("close_time") or "")
            fresh = now - self.market_at.get(asset, 0) < 10.0
            if fresh and close and close > datetime.now(timezone.utc):
                return market
        try:
            payload = http_json(f"{KALSHI_BASE}/markets",
                                {"series_ticker": SERIES[asset], "status": "open",
                                 "limit": 1})
            markets = payload.get("markets") or []
            if markets:
                self.market[asset] = markets[0]
                self.market_at[asset] = now
        except Exception as exc:  # noqa: BLE001
            note(f"{asset}_mkt", f"{asset} market discovery failed: {exc}")
        return self.market.get(asset)

    def asks(self, asset: str, market: dict) -> tuple[float | None, float | None]:
        """(yes_ask, no_ask) in dollars: live book-implied, quoted as fallback."""
        ticker = market.get("ticker") or ""
        book = {}
        try:
            book = http_json(f"{KALSHI_BASE}/markets/{ticker}/orderbook",
                             {"depth": 1}).get("orderbook") or {}
        except Exception as exc:  # noqa: BLE001
            note(f"{asset}_book", f"{asset} orderbook failed: {exc}")

        def implied(opposite):
            levels = book.get(opposite) or []
            if not levels:
                return None
            cents = int(levels[-1][0])
            return (100 - cents) / 100.0 if 1 <= cents <= 99 else None

        def quoted(field):
            v = market.get(field)
            try:
                v = int(v)
            except (TypeError, ValueError):
                return None
            return v / 100.0 if 1 <= v <= 99 else None

        yes_ask = implied("no") if implied("no") is not None else quoted("yes_ask")
        no_ask = implied("yes") if implied("yes") is not None else quoted("no_ask")
        return yes_ask, no_ask


# =========================
#  Ledger
# =========================
class Ledger:
    def __init__(self):
        self.bankroll = INITIAL_BANKROLL
        self.positions: dict[str, dict] = {}
        self.settled = self.wins = 0
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as fh:
                    data = json.load(fh)
                self.bankroll = float(data.get("bankroll", INITIAL_BANKROLL))
                self.positions = data.get("positions") or {}
                self.settled = int(data.get("settled", 0))
                self.wins = int(data.get("wins", 0))
            except (ValueError, OSError):
                pass

    def save(self):
        with open(STATE_FILE, "w") as fh:
            json.dump({"bankroll": self.bankroll, "positions": self.positions,
                       "settled": self.settled, "wins": self.wins}, fh, indent=2)

    def open(self, ticker: str, pos: dict) -> bool:
        cost = pos["price"] * pos["qty"] + pos["fees"]
        if ticker in self.positions or cost > self.bankroll:
            return False
        self.bankroll -= cost
        self.positions[ticker] = pos
        self.save()
        return True

    def settle(self, ticker: str, won: bool) -> dict | None:
        pos = self.positions.pop(ticker, None)
        if pos is None:
            return None
        payout = float(pos["qty"]) if won else 0.0
        self.bankroll += payout
        pnl = payout - pos["price"] * pos["qty"] - pos["fees"]
        self.settled += 1
        if pnl > 0:
            self.wins += 1
        self.save()
        return {"pos": pos, "pnl": pnl}


# =========================
#  Main loop
# =========================
def main() -> None:
    feeds, ledger = Feeds(), Ledger()
    last_entry: dict[str, float] = {}
    last_stream = 0.0

    log(f"convergence paper trader started | bankroll {ledger.bankroll:.2f} "
        f"| act window: last {ACT_WINDOW_MIN:g} min | fair >= {FAIR_MIN:.2f} "
        f"| min edge {MIN_EDGE:.2f} after {FEE_RATE:.0%} fees | size {ORDER_SIZE} "
        f"| PAPER ONLY - no orders are ever sent")
    log(f"trades log: {CSV_FILE}")

    while True:
        time.sleep(POLL_SECONDS)

        # 1) settle any positions whose window has closed
        for ticker in list(ledger.positions.keys()):
            pos = ledger.positions[ticker]
            close_dt = parse_iso(pos["close_time"])
            if close_dt is None or datetime.now(timezone.utc) < close_dt:
                continue
            window_start = int(close_dt.timestamp()) - 900
            close_px = feeds.candle_close_for_window(pos["asset"], window_start)
            if close_px is None:
                # candle not finalized yet; fall back to spot after a grace period
                if datetime.now(timezone.utc).timestamp() - close_dt.timestamp() < 120:
                    continue
                close_px = feeds.spot(pos["asset"])
                if close_px is None:
                    continue
            up_won = close_px > pos["strike"]           # tie settles DOWN
            won = up_won if pos["side"] == "yes" else not up_won
            outcome = ledger.settle(ticker, won)
            if outcome:
                log(f"SETTLED {ticker} {pos['side'].upper()} x{pos['qty']} "
                    f"@ {pos['price']:.2f} -> close {close_px:.2f} vs strike "
                    f"{pos['strike']:.2f} | {'WIN' if outcome['pnl'] > 0 else 'LOSS'} "
                    f"| P/L {outcome['pnl']:+.2f} | bankroll {ledger.bankroll:.2f} "
                    f"| record {ledger.wins}/{ledger.settled}")
                append_csv({"utc": now_utc(), "event": "SETTLE", "asset": pos["asset"],
                            "ticker": ticker, "side": pos["side"],
                            "price": f"{pos['price']:.2f}", "fair": f"{pos['fair']:.4f}",
                            "edge": f"{pos['edge']:.4f}", "spot": f"{close_px:.2f}",
                            "strike": f"{pos['strike']:.2f}",
                            "dist_bps": pos.get("dist_bps", ""),
                            "sigma_rem_bps": pos.get("sigma_rem_bps", ""),
                            "mins_left": "", "qty": pos["qty"],
                            "cost": f"{pos['price'] * pos['qty']:.2f}",
                            "fees": f"{pos['fees']:.2f}",
                            "result": "win" if won else "loss",
                            "pnl": f"{outcome['pnl']:.2f}",
                            "bankroll": f"{ledger.bankroll:.2f}"})

        # 2) hunt for lagging quotes in the closing minutes
        stream_bits = []
        for asset in SERIES:
            market = feeds.kalshi_market(asset)
            if not market:
                continue
            ticker = market.get("ticker") or ""
            close_dt = parse_iso(market.get("close_time") or "")
            if close_dt is None:
                continue
            mins_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 60.0
            spot = feeds.spot(asset)
            sigma_min = feeds.sigma_per_sqrt_minute(asset)
            if spot is None or sigma_min is None:
                continue
            strike = market.get("floor_strike")
            try:
                strike = float(strike)
            except (TypeError, ValueError):
                strike = feeds.window_open(asset)
            if not strike or strike <= 0:
                continue

            sigma_rem = sigma_min * math.sqrt(max(mins_left, 0.01))
            fair_up = fair_up_probability(spot, strike, sigma_rem)
            dist_bps = math.log(spot / strike) * 10000.0
            stream_bits.append(f"{asset}: {mins_left:.1f}m left | dist {dist_bps:+.1f}bps "
                               f"| fair-UP {fair_up:.3f}")

            if not (MIN_MINUTES_LEFT <= mins_left <= ACT_WINDOW_MIN):
                continue
            if ticker in ledger.positions:
                continue
            if time.monotonic() - last_entry.get(asset, 0.0) < COOLDOWN_SEC:
                continue

            yes_ask, no_ask = feeds.asks(asset, market)
            for side, ask, side_fair in (("yes", yes_ask, fair_up),
                                         ("no", no_ask, 1.0 - fair_up)):
                if side_fair < FAIR_MIN or ask is None or ask > MAX_PRICE:
                    continue
                fee = fee_per_contract(ask)
                edge = side_fair - ask - fee
                if edge < MIN_EDGE:
                    continue
                fees = fee * ORDER_SIZE
                pos = {"asset": asset, "side": side, "price": ask, "qty": ORDER_SIZE,
                       "fees": fees, "fair": side_fair, "edge": edge,
                       "strike": strike, "close_time": market.get("close_time") or "",
                       "dist_bps": round(dist_bps, 2),
                       "sigma_rem_bps": round(sigma_rem * 10000.0, 2),
                       "opened": now_utc()}
                if not ledger.open(ticker, pos):
                    continue
                last_entry[asset] = time.monotonic()
                log(f"ENTRY {ticker} {side.upper()} x{ORDER_SIZE} @ {ask:.2f} "
                    f"| fair {side_fair:.3f} | edge {edge:+.3f} "
                    f"| dist {dist_bps:+.1f}bps vs sigma_rem "
                    f"{sigma_rem * 10000.0:.1f}bps | {mins_left:.1f}m left "
                    f"| bankroll {ledger.bankroll:.2f}")
                append_csv({"utc": now_utc(), "event": "ENTRY", "asset": asset,
                            "ticker": ticker, "side": side, "price": f"{ask:.2f}",
                            "fair": f"{side_fair:.4f}", "edge": f"{edge:.4f}",
                            "spot": f"{spot:.2f}", "strike": f"{strike:.2f}",
                            "dist_bps": round(dist_bps, 2),
                            "sigma_rem_bps": round(sigma_rem * 10000.0, 2),
                            "mins_left": round(mins_left, 2), "qty": ORDER_SIZE,
                            "cost": f"{ask * ORDER_SIZE:.2f}", "fees": f"{fees:.2f}",
                            "result": "", "pnl": "",
                            "bankroll": f"{ledger.bankroll:.2f}"})
                break

        if stream_bits and time.monotonic() - last_stream >= 10.0:
            last_stream = time.monotonic()
            log(" || ".join(stream_bits) +
                f" || open {len(ledger.positions)} | bankroll {ledger.bankroll:.2f} "
                f"| record {ledger.wins}/{ledger.settled}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
