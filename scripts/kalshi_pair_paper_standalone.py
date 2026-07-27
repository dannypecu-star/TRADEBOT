#!/usr/bin/env python3
"""BTC/ETH inverse-pair paper trader for Kalshi 15m markets -- single-file edition.

This is the zero-setup version of scripts/kalshi_pair_paper.py: same strategy, same
gates, same fees, same CSV columns, but everything in one file using only Python's
standard library. No pip install, no repo checkout, no API keys, no account.

    python kalshi_pair_paper_standalone.py

Run it from anywhere; it writes pair_paper_trades.csv (the research log) and
pair_paper_state.json (bankroll memory) next to itself. Ctrl+C stops it.
PAPER ONLY: there is no code path that sends an order.

Strategy in one paragraph: buy the two opposing legs (e.g. BTC-UP + ETH-DOWN) when
their combined ask sum is 0.80-0.93 -- cheap enough that a same-direction settle's $1
payout beats the cost. The dominant loss mode is the assets sitting on opposite sides
of their strikes (they can then co-move forever and still pay $0), so entries require
the adverse strike gap to be small, plus a SYNC reading from the co-movement
classifier. Kalshi taker fees are charged on entries and stop-exits; resolution ties
settle DOWN per "above the open" rules.
"""
from __future__ import annotations

import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# =========================
#  Strategy settings
# =========================
ENTRY_MIN = 0.80          # enter only when the pair sum is between these two values
ENTRY_MAX = 0.93
PAIR_EXIT = 0.60          # stop-loss: close when the locked pair's sum drops below this
TAKE_PROFIT = 0.0         # close early when sum >= this (0 = off, hold to resolution)
ORDER_SIZE = 5            # contracts per leg per shot
MAX_SHOTS = 10            # max entries per 15m market
COOLDOWN_SEC = 40.0       # wait after a shot before seeking the next
NO_ENTRY_MINUTES = 1.0    # no new entries in the last N minutes
INITIAL_BANKROLL = 10000.0
FEE_RATE = 0.07           # Kalshi taker fee coefficient
STRIKE_GAP_MAX_BPS = 3.0  # block entries whose losing configuration is wider than this
BEHAVIOR_LOOKBACK_S = 10.0
BEHAVIOR_EPS_BPS = 0.25
BEHAVIOR_EMA_ALPHA = 0.22
POLL_SECONDS = 1.0

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M"}
COINBASE = {"BTC": "BTC-USD", "ETH": "ETH-USD"}
UP_DOWN, DOWN_UP = "BTC_UP_ETH_DOWN", "BTC_DOWN_ETH_UP"

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(HERE, "pair_paper_trades.csv")
STATE_FILE = os.path.join(HERE, "pair_paper_state.json")
CSV_HEADER = ["utc", "event", "pair", "sum", "qty", "cost", "fees", "pnl", "outcome",
              "btcMoveBps", "ethMoveBps", "adverseGapBps", "behaviorState",
              "behaviorScore"]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now_utc()}  {msg}", flush=True)


_last_note: dict[str, float] = {}


def note(key: str, msg: str, every: float = 60.0) -> None:
    """Log a recurring condition at most once per ``every`` seconds."""
    now = time.monotonic()
    if now - _last_note.get(key, 0.0) >= every:
        _last_note[key] = now
        log(msg)


def http_json(url: str, params: dict | None = None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "pair-paper-bot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fee_per_contract(price: float, rate: float = FEE_RATE) -> float:
    """Kalshi's fee: rate * P * (1-P), rounded UP to the next cent."""
    if price <= 0 or price >= 1:
        return 0.0
    return math.ceil(rate * price * (1.0 - price) * 100.0) / 100.0


def parse_iso(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


# =========================
#  Behavior classifier (SYNC / DIVERGENCE / ...)
# =========================
class Behavior:
    def __init__(self):
        self.reset()

    def reset(self):
        self.btc_open = self.eth_open = self.ema = None
        self.history = []           # (t, btc_bps, eth_bps)
        self.state, self.score = "WAITING", None
        self.btc_bps = self.eth_bps = None

    def update(self, now, btc_price, eth_price, btc_open, eth_open):
        if not btc_price or not eth_price:
            self.state, self.score = "WAITING", None
            return
        if self.btc_open is None:
            self.btc_open = btc_open if btc_open and btc_open > 0 else btc_price
        if self.eth_open is None:
            self.eth_open = eth_open if eth_open and eth_open > 0 else eth_price

        b = math.log(btc_price / self.btc_open) * 10000.0
        e = math.log(eth_price / self.eth_open) * 10000.0
        prev = None
        for entry in self.history:
            if entry[0] <= now - BEHAVIOR_LOOKBACK_S:
                prev = entry
            else:
                break
        b_vel = b - prev[1] if prev else 0.0
        e_vel = e - prev[2] if prev else 0.0

        def sign(v):
            return 0 if abs(v) <= BEHAVIOR_EPS_BPS else (1 if v > 0 else -1)

        bs, es = sign(b), sign(e)
        distance, gap = abs(b - e), abs(b_vel - e_vel)
        opposite = bs != 0 and es != 0 and bs != es
        delayed = bs != es and not opposite

        if opposite:
            state, raw = "DIVERGENCE", -(distance + 14 + gap * 2)
        elif delayed:
            state, raw = "DELAYED FOLLOW", -(distance * 0.85 + 6 + gap * 1.5)
        else:
            instability = distance * 0.42 + gap * 1.2
            raw = 10 - instability
            if raw < 0 or (min(abs(b), abs(e)) < 1 and gap > 0.4):
                state = "CROSSOVER INSTABILITY"
                raw = min(raw, -max(instability - 8, 1))
            else:
                state = "SYNC"

        self.ema = raw if self.ema is None else self.ema + BEHAVIOR_EMA_ALPHA * (raw - self.ema)
        self.history.append((now, b, e))
        while self.history and self.history[0][0] < now - BEHAVIOR_LOOKBACK_S * 3:
            self.history.pop(0)
        self.state, self.score, self.btc_bps, self.eth_bps = state, self.ema, b, e


def adverse_gap_bps(name, btc_bps, eth_bps):
    """Width of the losing strike configuration a shared move cannot escape."""
    if btc_bps is None or eth_bps is None:
        return None
    return (eth_bps - btc_bps) if name == UP_DOWN else (btc_bps - eth_bps)


def pair_sum(name, btc_up, btc_down, eth_up, eth_down):
    if name == UP_DOWN:
        return None if btc_up is None or eth_down is None else btc_up + eth_down
    return None if btc_down is None or eth_up is None else btc_down + eth_up


# =========================
#  Market data feeds
# =========================
class Feeds:
    def __init__(self):
        self.market_cache, self.market_at = {}, {}
        self.spot_cache, self.spot_at = {}, {}

    def kalshi_snapshot(self, asset):
        now = time.monotonic()
        market = self.market_cache.get(asset)
        stale = now - self.market_at.get(asset, 0) > 10.0
        if market:
            close = parse_iso(market.get("close_time") or "")
            if close is None or close <= datetime.now(timezone.utc):
                stale = True
        if market is None or stale:
            try:
                payload = http_json(f"{KALSHI_BASE}/markets",
                                    {"series_ticker": SERIES[asset], "status": "open",
                                     "limit": 1})
                markets = payload.get("markets") or []
                if markets:
                    self.market_cache[asset] = market = markets[0]
                    self.market_at[asset] = now
            except Exception as exc:  # noqa: BLE001
                note(f"{asset}_disc", f"{asset} market discovery failed: {exc}")
        if not market:
            note(f"{asset}_nomkt",
                 f"{asset}: no open market returned for {SERIES[asset]}")
            return None
        ticker = market.get("ticker") or ""
        try:
            book = http_json(f"{KALSHI_BASE}/markets/{ticker}/orderbook",
                             {"depth": 1}).get("orderbook") or {}
        except Exception as exc:  # noqa: BLE001
            note(f"{asset}_book", f"{asset} orderbook failed: {exc}")
            book = {}
        # an ask on one side is the best resting bid on the other, complemented
        def implied_ask(opposite_side):
            levels = book.get(opposite_side) or []
            if not levels:
                return None
            price_cents = int(levels[-1][0])
            return (100 - price_cents) / 100.0 if 1 <= price_cents <= 99 else None

        def quoted_ask(field):
            v = market.get(field)
            try:
                v = int(v)
            except (TypeError, ValueError):
                return None
            return v / 100.0 if 1 <= v <= 99 else None

        # prefer live book-implied asks; fall back to the market's quoted asks
        # (mirrors the AHK version, which returns quoted asks when the book is thin)
        up = implied_ask("no")
        down = implied_ask("yes")
        if up is None:
            up = quoted_ask("yes_ask")
        if down is None:
            down = quoted_ask("no_ask")
        if up is None and down is None:
            note(f"{asset}_empty",
                 f"{asset} {ticker}: empty orderbook and no quoted asks")
            return None
        return {"ticker": ticker, "up": up, "down": down,
                "close_time": market.get("close_time") or ""}

    def spot(self, asset):
        now = time.monotonic()
        if asset in self.spot_cache and now - self.spot_at.get(asset, 0) < 2.0:
            return self.spot_cache[asset]
        try:
            candles = http_json(
                f"https://api.exchange.coinbase.com/products/{COINBASE[asset]}/candles",
                {"granularity": 900})
            latest = candles[0]  # newest first: [time, low, high, open, close, volume]
            self.spot_cache[asset] = (float(latest[4]), float(latest[3]))
            self.spot_at[asset] = now
        except Exception as exc:  # noqa: BLE001
            log(f"{asset} spot fetch failed: {exc}")
        return self.spot_cache.get(asset, (None, None))

    def window_result(self, asset, close_time_iso):
        """UP/DOWN result of the completed 15m window ending at close_time.

        Settles from the completed Coinbase candle (close vs open; a dead-even
        close settles DOWN). Returns None while the candle is not yet available.
        Candle-based settlement replaces the old final-quote proxy, which could
        score BOTH legs of a pair as winners off a stale last snapshot.
        """
        close_dt = parse_iso(close_time_iso)
        if close_dt is None:
            return None
        window_start = int(close_dt.timestamp()) - 900
        try:
            candles = http_json(
                f"https://api.exchange.coinbase.com/products/{COINBASE[asset]}/candles",
                {"granularity": 900})
        except Exception as exc:  # noqa: BLE001
            note(f"{asset}_settle", f"{asset} settle fetch failed: {exc}")
            return None
        for row in candles:
            if abs(int(row[0]) - window_start) <= 1:
                open_px, close_px = float(row[3]), float(row[4])
                return "UP" if close_px > open_px else "DOWN"
        return None


# =========================
#  Paper ledger
# =========================
class Ledger:
    def __init__(self):
        self.bankroll = INITIAL_BANKROLL
        self.open_trades = []
        self.sessions = self.wins = self.losses = self.stops = self.breakevens = 0
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as fh:
                    data = json.load(fh)
                self.bankroll = float(data.get("bankroll", INITIAL_BANKROLL))
                for key in ("sessions", "wins", "losses", "stops", "breakevens"):
                    setattr(self, key, int(data.get(key, 0)))
            except (ValueError, OSError):
                pass

    def save(self):
        with open(STATE_FILE, "w") as fh:
            json.dump({"bankroll": self.bankroll, "sessions": self.sessions,
                       "wins": self.wins, "losses": self.losses, "stops": self.stops,
                       "breakevens": self.breakevens}, fh, indent=2)

    def open_trade(self, name, legs):
        cost = sum(l["price"] * l["qty"] for l in legs)
        fees = sum(fee_per_contract(l["price"]) * l["qty"] for l in legs)
        if cost + fees > self.bankroll:
            return None
        self.bankroll -= cost + fees
        trade = {"name": name, "legs": legs, "qty": min(l["qty"] for l in legs),
                 "cost": cost, "fees": fees}
        self.open_trades.append(trade)
        return trade

    def close_all(self, mode, current=None, final=None, results=None):
        """EXIT sells at ``current`` {(asset, side): price}. RESOLUTION settles from
        ``results`` {asset: "UP"/"DOWN"} (candle-based) when available, else from
        ``final`` {asset: (up, down)} quotes -- ties settle DOWN, no settlement fee."""
        res = {"trades": 0, "contracts": 0, "cost": 0.0, "revenue": 0.0,
               "fees": 0.0, "pnl": 0.0, "outcome": ""}
        for trade in self.open_trades:
            revenue = exit_fees = 0.0
            for leg in trade["legs"]:
                if mode == "RESOLUTION":
                    result = (results or {}).get(leg["asset"])
                    if result in ("UP", "DOWN"):
                        revenue += (1.0 if result == leg["side"] else 0.0) * leg["qty"]
                        continue
                    up, down = (final or {}).get(leg["asset"], (None, None))
                    if up is not None and down is not None:
                        won = (up > down) if leg["side"] == "UP" else (down >= up)
                        revenue += (1.0 if won else 0.0) * leg["qty"]
                else:
                    price = (current or {}).get((leg["asset"], leg["side"])) or 0.0
                    revenue += price * leg["qty"]
                    exit_fees += fee_per_contract(price) * leg["qty"]
            res["trades"] += 1
            res["contracts"] += trade["qty"]
            res["cost"] += trade["cost"]
            res["revenue"] += revenue
            res["fees"] += trade["fees"] + exit_fees
            self.bankroll += revenue - exit_fees
        self.open_trades = []
        res["pnl"] = res["revenue"] - res["cost"] - res["fees"]
        if res["trades"]:
            self.sessions += 1
            if res["pnl"] > 0:
                self.wins += 1
                res["outcome"] = "WIN"
            elif res["pnl"] < 0:
                self.losses += 1
                if mode == "EXIT":
                    self.stops += 1
                res["outcome"] = "LOSS"
            else:
                self.breakevens += 1
                res["outcome"] = "BREAKEVEN"
        self.save()
        return res


def append_csv(row: dict) -> None:
    new_file = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def csv_row(event, name, behavior, gap, **kw):
    row = {"utc": now_utc(), "event": event, "pair": name,
           "sum": "", "qty": "", "cost": "", "fees": "", "pnl": "", "outcome": "",
           "btcMoveBps": "" if behavior.btc_bps is None else round(behavior.btc_bps, 3),
           "ethMoveBps": "" if behavior.eth_bps is None else round(behavior.eth_bps, 3),
           "adverseGapBps": "" if gap is None else round(gap, 3),
           "behaviorState": behavior.state,
           "behaviorScore": "" if behavior.score is None else round(behavior.score, 3)}
    row.update(kw)
    return row


# =========================
#  Main loop
# =========================
def main() -> None:
    feeds, behavior, ledger = Feeds(), Behavior(), Ledger()
    session_key, shots, last_shot, last_stream = "", 0, 0.0, 0.0
    last_snaps, logged = {}, set()

    def log_once(key, msg):
        scoped = f"{session_key}|{key}"
        if scoped not in logged:
            logged.add(scoped)
            log(msg)

    def settle():
        nonlocal shots
        if not ledger.open_trades:
            return
        name = ledger.open_trades[0]["name"]
        btc, eth = last_snaps.get("BTC", {}), last_snaps.get("ETH", {})
        results = {a: feeds.window_result(a, last_snaps.get(a, {}).get("close_time") or "")
                   for a in ("BTC", "ETH")}
        res = ledger.close_all("RESOLUTION",
                               final={"BTC": (btc.get("up"), btc.get("down")),
                                      "ETH": (eth.get("up"), eth.get("down"))},
                               results=results)
        log(f"{name} held to resolution | {res['outcome']} "
            f"| revenue {res['revenue']:.2f} | fees {res['fees']:.2f} "
            f"| P/L {res['pnl']:+.2f} | bankroll {ledger.bankroll:.2f} "
            f"| record {ledger.wins}/{ledger.sessions}")
        append_csv(csv_row("RESOLUTION", name, behavior, None, qty=res["contracts"],
                           cost=f"{res['cost']:.4f}", fees=f"{res['fees']:.4f}",
                           pnl=f"{res['pnl']:.4f}", outcome=res["outcome"]))

    log(f"pair paper trader started (standalone) | bankroll {ledger.bankroll:.2f} "
        f"| entry {ENTRY_MIN:.2f}-{ENTRY_MAX:.2f} | exit < {PAIR_EXIT:.2f} "
        f"| strike gate <= {STRIKE_GAP_MAX_BPS:g}bps | fees taker {FEE_RATE:.0%} "
        f"| record {ledger.wins}/{ledger.sessions} | PAPER ONLY - no orders ever sent")
    log(f"trades log: {CSV_FILE}")

    while True:
        time.sleep(POLL_SECONDS)
        btc = feeds.kalshi_snapshot("BTC")
        eth = feeds.kalshi_snapshot("ETH")
        if not btc or not eth:
            note("no_data", "waiting for BTC/ETH market data...")
            continue

        key = f"{btc['ticker']}|{eth['ticker']}"
        if session_key and key != session_key:
            settle()
        if key != session_key:
            session_key, shots, last_shot = key, 0, 0.0
            behavior.reset()
            log(f"new session | BTC {btc['ticker']} | ETH {eth['ticker']}")
        last_snaps = {"BTC": btc, "ETH": eth}

        btc_price, btc_open = feeds.spot("BTC")
        eth_price, eth_open = feeds.spot("ETH")
        behavior.update(time.monotonic(), btc_price, eth_price, btc_open, eth_open)

        current = {("BTC", "UP"): btc.get("up"), ("BTC", "DOWN"): btc.get("down"),
                   ("ETH", "UP"): eth.get("up"), ("ETH", "DOWN"): eth.get("down")}

        if time.monotonic() - last_stream >= 5.0:
            last_stream = time.monotonic()
            s1 = pair_sum(UP_DOWN, btc.get("up"), btc.get("down"), eth.get("up"), eth.get("down"))
            s2 = pair_sum(DOWN_UP, btc.get("up"), btc.get("down"), eth.get("up"), eth.get("down"))
            equity = ledger.bankroll + sum(
                (current.get((l["asset"], l["side"])) or l["price"]) * l["qty"]
                for t in ledger.open_trades for l in t["legs"])
            fmt = lambda v: "n/a" if v is None else f"{v:.4f}"  # noqa: E731
            log(f"bUP-eDOWN {fmt(s1)} | eUP-bDOWN {fmt(s2)} | behavior {behavior.state} "
                f"| balance {equity:.2f} | record {ledger.wins}/{ledger.sessions} "
                f"| shots {shots}/{MAX_SHOTS}")

        closes = [parse_iso(s.get("close_time") or "") for s in (btc, eth)]
        closes = [c for c in closes if c]
        mins_left = (min(closes) - datetime.now(timezone.utc)).total_seconds() / 60.0 if closes else None
        if mins_left is not None and mins_left <= 0:
            settle()
            continue

        # stop-loss / take-profit on the locked pair
        if ledger.open_trades:
            name = ledger.open_trades[0]["name"]
            locked_sum = pair_sum(name, btc.get("up"), btc.get("down"),
                                  eth.get("up"), eth.get("down"))
            exit_level = PAIR_EXIT if PAIR_EXIT > 0 else 0.05
            stop = locked_sum is not None and locked_sum < exit_level
            take = TAKE_PROFIT > 0 and locked_sum is not None and locked_sum >= TAKE_PROFIT
            if stop or take:
                label = "STOP_EXIT" if stop else "TAKE_PROFIT"
                res = ledger.close_all("EXIT", current=current)
                log(f"{name} {label} @ sum {locked_sum:.2f} | {res['outcome']} "
                    f"| P/L {res['pnl']:+.2f} | bankroll {ledger.bankroll:.2f}")
                append_csv(csv_row(label, name, behavior, None,
                                   **{"sum": f"{locked_sum:.4f}"},
                                   qty=res["contracts"], cost=f"{res['cost']:.4f}",
                                   fees=f"{res['fees']:.4f}", pnl=f"{res['pnl']:.4f}",
                                   outcome=res["outcome"]))
                continue

        # entry gating
        if mins_left is None or not (0 < mins_left <= 15):
            continue
        if mins_left <= NO_ENTRY_MINUTES:
            log_once("NO_ENTRY", f"last {NO_ENTRY_MINUTES:g} minute(s): no new entries")
            continue
        if shots >= MAX_SHOTS:
            log_once("SHOTS_DONE", f"max shots reached {shots}/{MAX_SHOTS}")
            continue
        if last_shot and time.monotonic() - last_shot < COOLDOWN_SEC:
            continue

        if ledger.open_trades:
            name = ledger.open_trades[0]["name"]  # add-on shots stay on the locked pair
        else:
            in_range = []
            for cand in (UP_DOWN, DOWN_UP):
                s = pair_sum(cand, btc.get("up"), btc.get("down"),
                             eth.get("up"), eth.get("down"))
                if s is not None and ENTRY_MIN <= s <= ENTRY_MAX:
                    in_range.append(cand)
            if not in_range:
                continue
            if len(in_range) == 2:
                gaps = {c: adverse_gap_bps(c, behavior.btc_bps, behavior.eth_bps)
                        for c in in_range}
                name = (min(in_range, key=lambda c: gaps[c])
                        if all(g is not None for g in gaps.values()) else in_range[0])
            else:
                name = in_range[0]

        s = pair_sum(name, btc.get("up"), btc.get("down"), eth.get("up"), eth.get("down"))
        if s is None or not (ENTRY_MIN <= s <= ENTRY_MAX):
            continue
        if behavior.state != "SYNC":
            log_once(f"WAIT_{behavior.state}",
                     f"{name} sum {s:.2f} in range but behavior {behavior.state}; "
                     "waiting for SYNC")
            continue
        gap = adverse_gap_bps(name, behavior.btc_bps, behavior.eth_bps)
        if gap is None:
            log_once("NO_SPOT", "no spot move data for strike gate")
            continue
        if gap > STRIKE_GAP_MAX_BPS:
            log_once(f"GATE_{round(gap)}",
                     f"{name} blocked: adverse strike gap {gap:.2f}bps > "
                     f"{STRIKE_GAP_MAX_BPS}bps (cheap sum is priced-in risk, not edge)")
            continue

        if name == UP_DOWN:
            legs = [{"asset": "BTC", "side": "UP", "price": btc["up"], "qty": ORDER_SIZE},
                    {"asset": "ETH", "side": "DOWN", "price": eth["down"], "qty": ORDER_SIZE}]
        else:
            legs = [{"asset": "BTC", "side": "DOWN", "price": btc["down"], "qty": ORDER_SIZE},
                    {"asset": "ETH", "side": "UP", "price": eth["up"], "qty": ORDER_SIZE}]
        if any(l["price"] is None for l in legs):
            continue
        trade = ledger.open_trade(name, legs)
        if trade is None:
            log_once("NO_BALANCE", f"{name} entry blocked: bankroll too low")
            continue
        shots += 1
        last_shot = time.monotonic()
        ledger.save()
        log(f"{name} shot {shots}/{MAX_SHOTS} @ sum {s:.4f} | gap {gap:+.2f}bps "
            f"| behavior {behavior.state} | cost {trade['cost']:.2f} "
            f"+ fees {trade['fees']:.2f} | bankroll {ledger.bankroll:.2f}")
        append_csv(csv_row("ENTRY", name, behavior, gap, **{"sum": f"{s:.4f}"},
                           qty=trade["qty"], cost=f"{trade['cost']:.4f}",
                           fees=f"{trade['fees']:.4f}"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
