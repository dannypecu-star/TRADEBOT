#!/usr/bin/env python3
"""Scan Kalshi for riskless basket mispricings in mutually exclusive events.

No API key, no account, no model: this only reads public market data and logs the
moments when an event's buckets are priced so that buying every side locks in a
guaranteed profit (see src/kalshi/arb.py for the exact constructions and the one
caveat on YES baskets).

    python scripts/kalshi_arb_scan.py            # scan forever (Ctrl+C to stop)
    python scripts/kalshi_arb_scan.py --once     # one sweep, then exit

Every hit is verified against real orderbook depth (how many baskets could actually
fill) and appended to data/arb_opportunities.csv. The research questions this answers:
how often do these windows open, in which markets, how big, and for how long --
i.e. whether a real-money version could ever earn more than pocket change.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests

from src.kalshi.arb import find_opportunities, max_baskets_from_orderbooks

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
CSV_FILE = os.path.join(DATA_DIR, "arb_opportunities.csv")
CSV_HEADER = ["utc", "type", "event_ticker", "title", "legs", "sum_asks", "fees",
              "profit_per_basket", "max_baskets", "total_profit", "caveat", "tickers"]

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now_utc()}  {msg}", flush=True)


def kalshi_get(path: str, **params) -> dict:
    resp = requests.get(f"{KALSHI_BASE}{path}", params=params, timeout=30)
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


def fetch_open_events(max_pages: int) -> list[dict]:
    events: list[dict] = []
    cursor = ""
    for _ in range(max_pages):
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = kalshi_get("/events", **params)
        events.extend(payload.get("events") or [])
        cursor = payload.get("cursor") or ""
        if not cursor:
            break
    return events


def depth_check(opp: dict) -> tuple[int | None, float | None]:
    """Fetch each leg's orderbook and return (max fillable baskets, real profit).

    The quoted asks that triggered the find may be stale or sizeless; this recomputes
    the basket from implied asks in the live books. Returns (None, None) when any
    book is unavailable -- unknown, not zero.
    """
    side = "yes" if opp["type"] == "YES_BASKET" else "no"
    orderbooks: dict[str, dict] = {}
    for ticker in opp["tickers"]:
        try:
            payload = kalshi_get(f"/markets/{ticker}/orderbook", depth=1)
            orderbooks[ticker] = payload.get("orderbook") or {}
        except Exception as exc:  # noqa: BLE001
            log(f"orderbook fetch failed for {ticker}: {exc}")
    baskets, detail = max_baskets_from_orderbooks(orderbooks, opp["tickers"], side)
    if baskets is None:
        return None, None
    from src.kalshi.arb import basket_cost
    asks = [detail[t][0] for t in opp["tickers"]]
    if any(a is None for a in asks):
        return None, None
    payout = 1.0 if opp["type"] == "YES_BASKET" else float(opp["legs"] - 1)
    real_profit = payout - basket_cost(asks)
    return baskets, round(real_profit, 4)


def run_pass(args: argparse.Namespace, last_logged: dict[str, float]) -> None:
    try:
        events = fetch_open_events(args.max_pages)
    except Exception as exc:  # noqa: BLE001
        log(f"event fetch failed: {exc}")
        return

    scanned = me_events = hits = 0
    for event in events:
        scanned += 1
        if event.get("mutually_exclusive"):
            me_events += 1
        for opp in find_opportunities(event, fee_rate=args.fee_rate,
                                      min_profit=args.min_profit,
                                      max_legs=args.max_legs):
            key = f"{opp['event_ticker']}|{opp['type']}"
            previous = last_logged.get(key)
            # log each window once, and again only if it widens meaningfully
            if previous is not None and opp["profit"] < previous + 0.005:
                continue

            baskets, real_profit = depth_check(opp)
            hits += 1
            last_logged[key] = opp["profit"]
            total = (round(baskets * real_profit, 2)
                     if baskets is not None and real_profit is not None else "")
            log(f"HIT {opp['type']} {opp['event_ticker']} | {opp['legs']} legs "
                f"| sum {opp['sum_asks']:.2f} + fees {opp['fees']:.2f} "
                f"| quoted profit {opp['profit']:+.2f}/basket "
                f"| depth-checked: baskets={baskets} real_profit={real_profit} "
                f"{'| ' + opp['caveat'] if opp['caveat'] else ''}")
            append_csv({"utc": now_utc(), "type": opp["type"],
                        "event_ticker": opp["event_ticker"], "title": opp["title"],
                        "legs": opp["legs"], "sum_asks": opp["sum_asks"],
                        "fees": opp["fees"], "profit_per_basket": opp["profit"],
                        "max_baskets": "" if baskets is None else baskets,
                        "total_profit": total, "caveat": opp["caveat"],
                        "tickers": " ".join(opp["tickers"])})

    log(f"pass done | events {scanned} | mutually-exclusive {me_events} "
        f"| new/widened hits {hits}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--min-profit", type=float, default=0.01,
                   help="minimum guaranteed profit per basket, dollars (default 0.01)")
    p.add_argument("--max-legs", type=int, default=15,
                   help="skip events with more markets than this (default 15)")
    p.add_argument("--fee-rate", type=float, default=0.07,
                   help="Kalshi fee coefficient (default 0.07)")
    p.add_argument("--interval", type=float, default=300,
                   help="seconds between sweeps (default 300)")
    p.add_argument("--max-pages", type=int, default=20,
                   help="max event pages per sweep, 200 events each (default 20)")
    p.add_argument("--once", action="store_true", help="single sweep, then exit")
    args = p.parse_args()

    log(f"arb scanner started | min_profit {args.min_profit:.2f}/basket "
        f"| max_legs {args.max_legs} | interval {args.interval:.0f}s "
        f"| findings -> {CSV_FILE}")
    last_logged: dict[str, float] = {}
    while True:
        run_pass(args, last_logged)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
