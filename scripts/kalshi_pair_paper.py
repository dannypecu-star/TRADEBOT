#!/usr/bin/env python3
"""BTC/ETH inverse-pair paper trader on Kalshi 15m markets (Python port).

Same strategy, gates, fees, and CSV schema as scripts/kalshi_btc_eth_pair_paper.ahk --
run one or the other, not both, or you are just duplicating the same experiment.

No account or API keys needed; Kalshi market data is public and fills are simulated
at the quoted ask. Spot prices for the behavior/strike gates come from Coinbase's
public 15-minute candles, which align to the same clock quarters as Kalshi's windows.

    python scripts/kalshi_pair_paper.py           # run (Ctrl+C to stop)
    python scripts/kalshi_pair_paper.py --once    # a single pass, for a smoke test

Trades append to data/pair_paper_trades.csv; bankroll and win counters persist in
data/pair_paper_state.json across restarts (open intraday positions do not -- a trade
interrupted by a restart is dropped from the record, not resumed).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests

from src.kalshi.arb import implied_ask_and_size
from src.kalshi.pair import (DOWN_UP, UP_DOWN, BehaviorTracker, PairConfig,
                             PairLedger, adverse_strike_gap_bps, entry_allowed,
                             pair_sum, select_pair)

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
STATE_FILE = os.path.join(DATA_DIR, "pair_paper_state.json")
CSV_FILE = os.path.join(DATA_DIR, "pair_paper_trades.csv")
CSV_HEADER = ["utc", "event", "pair", "sum", "qty", "cost", "fees", "pnl", "outcome",
              "btcMoveBps", "ethMoveBps", "adverseGapBps", "behaviorState",
              "behaviorScore"]

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M"}
COINBASE = {"BTC": "BTC-USD", "ETH": "ETH-USD"}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now_utc()}  {msg}", flush=True)


def kalshi_get(path: str, **params) -> dict:
    resp = requests.get(f"{KALSHI_BASE}{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def append_csv(row: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
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


class MarketFeed:
    """Kalshi snapshot per asset: current market ticker, close time, implied asks."""

    def __init__(self, refresh_s: float = 10.0):
        self.refresh_s = refresh_s
        self._market: dict[str, dict] = {}
        self._fetched: dict[str, float] = {}

    def _discover(self, asset: str) -> dict | None:
        now = time.monotonic()
        cached = self._market.get(asset)
        if cached and now - self._fetched.get(asset, 0) < self.refresh_s:
            close = parse_iso(cached.get("close_time") or "")
            if close and close > datetime.now(timezone.utc):
                return cached
        try:
            payload = kalshi_get("/markets", series_ticker=SERIES[asset],
                                 status="open", limit=1)
        except Exception as exc:  # noqa: BLE001
            log(f"{asset} market discovery failed: {exc}")
            return cached
        markets = payload.get("markets") or []
        if markets:
            self._market[asset] = markets[0]
            self._fetched[asset] = now
        return self._market.get(asset)

    def snapshot(self, asset: str) -> dict | None:
        market = self._discover(asset)
        if not market:
            return None
        ticker = market.get("ticker") or ""
        try:
            book = kalshi_get(f"/markets/{ticker}/orderbook", depth=1).get("orderbook") or {}
        except Exception as exc:  # noqa: BLE001
            log(f"{asset} orderbook failed: {exc}")
            return None
        up, _ = implied_ask_and_size(book, "yes")     # 1 - best NO bid
        down, _ = implied_ask_and_size(book, "no")    # 1 - best YES bid

        def quoted_ask(field):
            v = market.get(field)
            try:
                v = int(v)
            except (TypeError, ValueError):
                return None
            return v / 100.0 if 1 <= v <= 99 else None

        # fall back to the market's quoted asks when the book is momentarily empty
        if up is None:
            up = quoted_ask("yes_ask")
        if down is None:
            down = quoted_ask("no_ask")
        if up is None and down is None:
            return None
        return {"asset": asset, "ticker": ticker, "up": up, "down": down,
                "close_time": market.get("close_time") or ""}


class SpotFeed:
    """Coinbase 15m candles: (latest price, window open), throttled per asset."""

    def __init__(self, refresh_s: float = 2.0):
        self.refresh_s = refresh_s
        self._cache: dict[str, tuple[float, float]] = {}
        self._fetched: dict[str, float] = {}

    def prices(self, asset: str) -> tuple[float | None, float | None]:
        now = time.monotonic()
        if asset in self._cache and now - self._fetched.get(asset, 0) < self.refresh_s:
            return self._cache[asset]
        try:
            resp = requests.get(
                f"https://api.exchange.coinbase.com/products/{COINBASE[asset]}/candles",
                params={"granularity": 900}, timeout=15,
                headers={"User-Agent": "tradebot-pair-paper/1.0"})
            resp.raise_for_status()
            candles = resp.json()
            # newest first: [time, low, high, open, close, volume]
            latest = candles[0]
            price, window_open = float(latest[4]), float(latest[3])
            self._cache[asset] = (price, window_open)
            self._fetched[asset] = now
        except Exception as exc:  # noqa: BLE001
            log(f"{asset} spot fetch failed: {exc}")
        return self._cache.get(asset, (None, None))


def load_ledger(initial_bankroll: float) -> PairLedger:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return PairLedger.from_dict(json.load(fh))
    return PairLedger(bankroll=initial_bankroll)


def save_ledger(ledger: PairLedger) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(ledger.to_dict(), fh, indent=2)


def csv_row(event: str, pair_name: str, behavior, gap, *, sum_="", qty="", cost="",
            fees="", pnl="", outcome="") -> dict:
    return {"utc": now_utc(), "event": event, "pair": pair_name, "sum": sum_,
            "qty": qty, "cost": cost, "fees": fees, "pnl": pnl, "outcome": outcome,
            "btcMoveBps": "" if behavior.btc_move_bps is None else round(behavior.btc_move_bps, 3),
            "ethMoveBps": "" if behavior.eth_move_bps is None else round(behavior.eth_move_bps, 3),
            "adverseGapBps": "" if gap is None else round(gap, 3),
            "behaviorState": behavior.state,
            "behaviorScore": "" if behavior.score is None else round(behavior.score, 3)}


class PairBot:
    def __init__(self, cfg: PairConfig, ledger: PairLedger):
        self.cfg = cfg
        self.ledger = ledger
        self.markets = MarketFeed()
        self.spot = SpotFeed()
        self.behavior = BehaviorTracker(cfg)
        self.session_key = ""
        self.shots = 0
        self.last_shot_at = 0.0
        self.last_snaps: dict[str, dict] = {}
        self.logged: set[str] = set()
        self.last_stream = 0.0

    # -- helpers ----------------------------------------------------------------
    def log_once(self, key: str, msg: str) -> None:
        scoped = f"{self.session_key}|{key}"
        if scoped not in self.logged:
            self.logged.add(scoped)
            log(msg)

    def minutes_left(self, btc: dict, eth: dict) -> float | None:
        times = [parse_iso(s.get("close_time") or "") for s in (btc, eth)]
        times = [t for t in times if t]
        if not times:
            return None
        return (min(times) - datetime.now(timezone.utc)).total_seconds() / 60.0

    def current_prices(self, btc: dict, eth: dict) -> dict:
        return {("BTC", "UP"): btc.get("up"), ("BTC", "DOWN"): btc.get("down"),
                ("ETH", "UP"): eth.get("up"), ("ETH", "DOWN"): eth.get("down")}

    def settle_open_trades(self) -> None:
        if not self.ledger.open_trades:
            return
        btc, eth = self.last_snaps.get("BTC", {}), self.last_snaps.get("ETH", {})
        final = {"BTC": (btc.get("up"), btc.get("down")),
                 "ETH": (eth.get("up"), eth.get("down"))}
        name = self.ledger.locked_name() or ""
        result = self.ledger.close_all("RESOLUTION", self.cfg.fee_rate,
                                       final_prices=final)
        log(f"{name} held to resolution | {result.outcome} "
            f"| revenue {result.revenue:.2f} | fees {result.fees:.2f} "
            f"| P/L {result.pnl:+.2f} | bankroll {self.ledger.bankroll:.2f} "
            f"| record {self.ledger.wins}/{self.ledger.sessions}")
        gap = None
        append_csv(csv_row("RESOLUTION", name, self.behavior.snapshot, gap,
                           qty=result.contracts, cost=f"{result.entry_cost:.4f}",
                           fees=f"{result.fees:.4f}", pnl=f"{result.pnl:.4f}",
                           outcome=result.outcome))
        save_ledger(self.ledger)

    # -- one iteration ----------------------------------------------------------
    def step(self) -> None:
        btc = self.markets.snapshot("BTC")
        eth = self.markets.snapshot("ETH")
        if not btc or not eth:
            self.log_once("NO_DATA", "waiting for BTC/ETH market data")
            return

        key = f"{btc['ticker']}|{eth['ticker']}"
        if self.session_key and key != self.session_key:
            self.settle_open_trades()
        if key != self.session_key:
            self.session_key = key
            self.shots = 0
            self.last_shot_at = 0.0
            self.behavior.reset()
            log(f"new session | BTC {btc['ticker']} | ETH {eth['ticker']}")
        self.last_snaps = {"BTC": btc, "ETH": eth}

        btc_price, btc_open = self.spot.prices("BTC")
        eth_price, eth_open = self.spot.prices("ETH")
        behavior = self.behavior.update(time.monotonic(), btc_price, eth_price,
                                        btc_open, eth_open)

        self.stream(btc, eth, behavior)

        mins = self.minutes_left(btc, eth)
        if mins is not None and mins <= 0:
            self.settle_open_trades()
            return

        # stop-loss / take-profit on the locked pair
        locked = self.ledger.locked_name()
        if locked:
            current = pair_sum(locked, btc.get("up"), btc.get("down"),
                               eth.get("up"), eth.get("down"))
            exit_level = self.cfg.effective_exit()
            take_profit = self.cfg.take_profit
            stop = current is not None and current < exit_level
            take = take_profit > 0 and current is not None and current >= take_profit
            if stop or take:
                label = "STOP_EXIT" if stop else "TAKE_PROFIT"
                result = self.ledger.close_all(
                    "EXIT", self.cfg.fee_rate,
                    current_prices=self.current_prices(btc, eth))
                log(f"{locked} {label} @ sum {current:.2f} | {result.outcome} "
                    f"| P/L {result.pnl:+.2f} | bankroll {self.ledger.bankroll:.2f}")
                append_csv(csv_row(label, locked, behavior, None,
                                   sum_=f"{current:.4f}", qty=result.contracts,
                                   cost=f"{result.entry_cost:.4f}",
                                   fees=f"{result.fees:.4f}",
                                   pnl=f"{result.pnl:.4f}", outcome=result.outcome))
                save_ledger(self.ledger)
                return

        # entry gating
        if mins is None or not (0 < mins <= 15):
            return
        if mins <= self.cfg.no_entry_minutes:
            self.log_once("NO_ENTRY_ZONE",
                          f"last {self.cfg.no_entry_minutes:g} minute(s): no new entries")
            return
        if self.shots >= self.cfg.max_shots:
            self.log_once("SHOTS_DONE", f"max shots reached {self.shots}/{self.cfg.max_shots}")
            return
        if time.monotonic() - self.last_shot_at < self.cfg.cooldown_sec and self.last_shot_at:
            return

        name = locked or select_pair(self.cfg, btc.get("up"), btc.get("down"),
                                     eth.get("up"), eth.get("down"), behavior)
        if not name:
            return
        current = pair_sum(name, btc.get("up"), btc.get("down"),
                           eth.get("up"), eth.get("down"))
        allowed, reason = entry_allowed(self.cfg, name, current, behavior)
        if not allowed:
            if current is not None and in_range_note(self.cfg, current):
                self.log_once(f"BLOCK_{reason[:24]}", f"{name} blocked: {reason}")
            return

        legs = self.build_legs(name, btc, eth)
        trade = self.ledger.open_trade(name, legs, now_utc(), self.cfg.fee_rate)
        if trade is None:
            self.log_once("NO_BALANCE", f"{name} entry blocked: bankroll too low")
            return
        self.shots += 1
        self.last_shot_at = time.monotonic()
        gap = adverse_strike_gap_bps(name, behavior.btc_move_bps, behavior.eth_move_bps)
        log(f"{name} shot {self.shots}/{self.cfg.max_shots} @ sum {current:.4f} "
            f"| gap {gap:+.2f}bps | behavior {behavior.state} "
            f"| cost {trade.entry_cost:.2f} + fees {trade.entry_fees:.2f} "
            f"| bankroll {self.ledger.bankroll:.2f}")
        append_csv(csv_row("ENTRY", name, behavior, gap, sum_=f"{current:.4f}",
                           qty=trade.qty, cost=f"{trade.entry_cost:.4f}",
                           fees=f"{trade.entry_fees:.4f}"))
        save_ledger(self.ledger)

    def build_legs(self, name: str, btc: dict, eth: dict) -> list[dict]:
        size = self.cfg.order_size
        if name == UP_DOWN:
            return [{"asset": "BTC", "side": "UP", "price": btc["up"], "qty": size},
                    {"asset": "ETH", "side": "DOWN", "price": eth["down"], "qty": size}]
        return [{"asset": "BTC", "side": "DOWN", "price": btc["down"], "qty": size},
                {"asset": "ETH", "side": "UP", "price": eth["up"], "qty": size}]

    def stream(self, btc: dict, eth: dict, behavior) -> None:
        if time.monotonic() - self.last_stream < 5.0:
            return
        self.last_stream = time.monotonic()
        s1 = pair_sum(UP_DOWN, btc.get("up"), btc.get("down"), eth.get("up"), eth.get("down"))
        s2 = pair_sum(DOWN_UP, btc.get("up"), btc.get("down"), eth.get("up"), eth.get("down"))
        equity = self.ledger.bankroll + self.ledger.open_value(self.current_prices(btc, eth))
        log(f"bUP-eDOWN {fmt(s1)} | eUP-bDOWN {fmt(s2)} "
            f"| behavior {behavior.state} | balance {equity:.2f} "
            f"| record {self.ledger.wins}/{self.ledger.sessions} "
            f"| shots {self.shots}/{self.cfg.max_shots}")


def in_range_note(cfg: PairConfig, value: float) -> bool:
    return cfg.entry_min <= value <= cfg.entry_max


def fmt(value) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bankroll", type=float, default=10000.0)
    p.add_argument("--poll", type=float, default=1.0, help="seconds between passes")
    p.add_argument("--entry-min", type=float, default=0.80)
    p.add_argument("--entry-max", type=float, default=0.93)
    p.add_argument("--pair-exit", type=float, default=0.60)
    p.add_argument("--take-profit", type=float, default=0.0)
    p.add_argument("--order-size", type=int, default=5)
    p.add_argument("--shots", type=int, default=10)
    p.add_argument("--cooldown", type=float, default=40.0)
    p.add_argument("--strike-gap-max", type=float, default=3.0)
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    cfg = PairConfig(entry_min=args.entry_min, entry_max=args.entry_max,
                     pair_exit=args.pair_exit, take_profit=args.take_profit,
                     order_size=args.order_size, max_shots=args.shots,
                     cooldown_sec=args.cooldown,
                     strike_gap_max_bps=args.strike_gap_max)
    ledger = load_ledger(args.bankroll)
    bot = PairBot(cfg, ledger)
    log(f"pair paper trader started | bankroll {ledger.bankroll:.2f} "
        f"| entry {cfg.entry_min:.2f}-{cfg.entry_max:.2f} | exit < {cfg.effective_exit():.2f} "
        f"| strike gate <= {cfg.strike_gap_max_bps:g}bps | fees taker {cfg.fee_rate:.0%} "
        f"| PAPER ONLY - no orders are ever sent")
    while True:
        bot.step()
        if args.once:
            break
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
