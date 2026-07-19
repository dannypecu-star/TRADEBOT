#!/usr/bin/env python3
"""Kalshi riskless-basket scanner -- single-file, zero-setup edition.

    python kalshi_arb_scan_standalone.py

Standard library only: no pip, no API keys, no account. Findings append to
arb_opportunities.csv next to this file. This bot only ever LOGS -- it holds no
positions and there is nothing to settle; the research output is how often riskless
mispricings appear, in which markets, how big, and how deep the books really are.

What it looks for, in an event whose markets are mutually exclusive (at most one can
settle YES -- Kalshi marks these with the event's ``mutually_exclusive`` flag):

  * NO basket  -- buy 1 NO of every market: at most one NO loses, so the payout is at
    least $(N-1) guaranteed. Profit when sum(no_asks) + fees < N-1. Strictly riskless
    under mutual exclusivity alone.
  * YES basket -- buy 1 YES of every market for a $1 payout. Additionally requires the
    buckets to be exhaustive (some bucket must win), which the API does not expose,
    so these findings carry a "verify event rules" caveat.

Every quoted-price hit is re-checked against live orderbook depth: the real ask is
derived from the best opposite-side bid, and the fillable basket count is the minimum
size across legs -- stale-quote mirages show up as baskets=0.
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
#  Settings
# =========================
MIN_PROFIT = 0.01        # minimum guaranteed profit per basket, dollars
MAX_LEGS = 15            # skip events with more markets than this (fees pile up)
FEE_RATE = 0.07          # Kalshi fee coefficient
SCAN_SECONDS = 300       # seconds between full sweeps
MAX_PAGES = 20           # event pages per sweep, 200 events each

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(HERE, "arb_opportunities.csv")
CSV_HEADER = ["utc", "type", "event_ticker", "title", "legs", "sum_asks", "fees",
              "profit_per_basket", "max_baskets", "total_profit", "caveat", "tickers"]

CLOSED_STATUSES = ("closed", "settled", "finalized", "determined")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now_utc()}  {msg}", flush=True)


def http_json(url: str, params: dict | None = None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "arb-scan-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fee_per_contract(price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    return math.ceil(FEE_RATE * price * (1.0 - price) * 100.0) / 100.0


def append_csv(row: dict) -> None:
    new_file = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def valid_cents(value) -> bool:
    try:
        return value is not None and 1 <= int(value) <= 99
    except (TypeError, ValueError):
        return False


def find_opportunities(event: dict) -> list[dict]:
    if not event.get("mutually_exclusive"):
        return []
    markets = [m for m in (event.get("markets") or [])
               if (m.get("status") or "active") not in CLOSED_STATUSES]
    n = len(markets)
    if n < 2 or n > MAX_LEGS:
        return []
    tickers = [m.get("ticker") or "" for m in markets]
    found = []

    for side_field, payout, opp_type, caveat in (
            ("yes_ask", 1.0, "YES_BASKET", "requires exhaustive buckets - verify event rules"),
            ("no_ask", float(n - 1), "NO_BASKET", "")):
        cents = [m.get(side_field) for m in markets]
        if not all(valid_cents(c) for c in cents):
            continue
        prices = [int(c) / 100.0 for c in cents]
        fees = sum(fee_per_contract(p) for p in prices)
        profit = payout - (sum(prices) + fees)
        if profit >= MIN_PROFIT:
            found.append({"type": opp_type, "event_ticker": event.get("event_ticker") or "",
                          "title": event.get("title") or "", "legs": n,
                          "tickers": tickers, "sum_asks": round(sum(prices), 4),
                          "fees": round(fees, 4), "profit": round(profit, 4),
                          "caveat": caveat})
    return found


def implied_ask_and_size(orderbook: dict, side: str):
    """Real best ask and size for ``side``: complement of the best opposite bid."""
    opposite = "no" if side == "yes" else "yes"
    levels = (orderbook or {}).get(opposite) or []
    if not levels:
        return None, 0
    try:
        price_cents, count = int(levels[-1][0]), int(levels[-1][1])
    except (TypeError, ValueError, IndexError):
        return None, 0
    if not 1 <= price_cents <= 99:
        return None, 0
    return (100 - price_cents) / 100.0, count


def depth_check(opp: dict):
    """(max fillable baskets, realistic profit) from live books; (None, None) if any
    book is unavailable -- unknown reported as unknown, not zero."""
    side = "yes" if opp["type"] == "YES_BASKET" else "no"
    asks, sizes = [], []
    for ticker in opp["tickers"]:
        try:
            book = http_json(f"{KALSHI_BASE}/markets/{ticker}/orderbook",
                             {"depth": 1}).get("orderbook") or {}
        except Exception as exc:  # noqa: BLE001
            log(f"orderbook fetch failed for {ticker}: {exc}")
            return None, None
        ask, size = implied_ask_and_size(book, side)
        if ask is None:
            return None, None
        asks.append(ask)
        sizes.append(size)
    payout = 1.0 if opp["type"] == "YES_BASKET" else float(opp["legs"] - 1)
    fees = sum(fee_per_contract(p) for p in asks)
    return min(sizes), round(payout - (sum(asks) + fees), 4)


def fetch_open_events() -> list[dict]:
    events, cursor = [], ""
    for _ in range(MAX_PAGES):
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = http_json(f"{KALSHI_BASE}/events", params)
        events.extend(payload.get("events") or [])
        cursor = payload.get("cursor") or ""
        if not cursor:
            break
    return events


def main() -> None:
    log(f"arb scanner started (standalone) | min_profit {MIN_PROFIT:.2f}/basket "
        f"| max_legs {MAX_LEGS} | sweep every {SCAN_SECONDS}s | LOG ONLY - no orders")
    log(f"findings log: {CSV_FILE}")
    last_logged: dict[str, float] = {}

    while True:
        try:
            events = fetch_open_events()
        except Exception as exc:  # noqa: BLE001
            log(f"event fetch failed: {exc}")
            time.sleep(SCAN_SECONDS)
            continue

        me_events = hits = 0
        for event in events:
            if event.get("mutually_exclusive"):
                me_events += 1
            for opp in find_opportunities(event):
                key = f"{opp['event_ticker']}|{opp['type']}"
                previous = last_logged.get(key)
                # log each window once, again only if it widens meaningfully
                if previous is not None and opp["profit"] < previous + 0.005:
                    continue
                baskets, real_profit = depth_check(opp)
                hits += 1
                last_logged[key] = opp["profit"]
                total = ("" if baskets is None or real_profit is None
                         else round(baskets * real_profit, 2))
                caveat = f" | {opp['caveat']}" if opp["caveat"] else ""
                log(f"HIT {opp['type']} {opp['event_ticker']} | {opp['legs']} legs "
                    f"| sum {opp['sum_asks']:.2f} + fees {opp['fees']:.2f} "
                    f"| quoted profit {opp['profit']:+.2f}/basket "
                    f"| depth-checked: baskets={baskets} real_profit={real_profit}"
                    f"{caveat}")
                append_csv({"utc": now_utc(), "type": opp["type"],
                            "event_ticker": opp["event_ticker"],
                            "title": opp["title"], "legs": opp["legs"],
                            "sum_asks": opp["sum_asks"], "fees": opp["fees"],
                            "profit_per_basket": opp["profit"],
                            "max_baskets": "" if baskets is None else baskets,
                            "total_profit": total, "caveat": opp["caveat"],
                            "tickers": " ".join(opp["tickers"])})

        log(f"sweep done | events {len(events)} | mutually-exclusive {me_events} "
            f"| new/widened hits {hits}")
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
