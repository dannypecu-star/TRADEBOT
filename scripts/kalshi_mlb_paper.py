#!/usr/bin/env python3
"""MLB moneyline paper trader: Kalshi prices vs devigged sportsbook consensus.

No Kalshi account or credentials needed -- market data is public and fills are
simulated locally at the quoted ask. The only key required is a free one from
the-odds-api.com (500 credits/month):

    # Windows PowerShell                          # macOS / Linux
    $env:THE_ODDS_API_KEY = "yourkey"             export THE_ODDS_API_KEY=yourkey

    python scripts/kalshi_mlb_paper.py            # run the loop (Ctrl+C to stop)
    python scripts/kalshi_mlb_paper.py --once     # single pass, then exit
    python scripts/kalshi_mlb_paper.py --discover # just list Kalshi MLB markets

Odds are refreshed every --odds-refresh seconds (default 2 hours; each refresh costs
1 API credit, so the default budget is ~12/day against the 500/month free tier) while
Kalshi prices and settlements are polled every --interval seconds for free.

State (bankroll + open positions) persists in data/mlb_paper_state.json across
restarts, and every entry/settlement appends to data/mlb_paper_trades.csv -- that CSV
is the research output that eventually answers whether the edge is real.
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

from src.kalshi.economics import SizingConfig
from src.kalshi.mlb import PaperLedger, match_market, _parse_iso
from src.kalshi.sources.theoddsapi import (TheOddsAPIClient,
                                           fair_probabilities_from_payload)
from src.kalshi.strategy import evaluate_market

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
STATE_FILE = os.path.join(DATA_DIR, "mlb_paper_state.json")
CSV_FILE = os.path.join(DATA_DIR, "mlb_paper_trades.csv")

CSV_HEADER = ["utc", "event", "ticker", "event_ticker", "game", "side", "price",
              "fair_prob", "edge", "contracts", "cost", "fees", "result", "pnl",
              "bankroll"]

CANDIDATE_SERIES = ("KXMLBGAME", "KXMLB", "MLBGAME")

# Only public, unauthenticated Kalshi endpoints are used -- no account, no keys, and
# deliberately no import of the signing client so a broken cryptography install can
# never stop a paper run.
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def kalshi_get(path: str, **params) -> dict:
    resp = requests.get(f"{KALSHI_BASE}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now_utc()}  {msg}", flush=True)


def append_csv(row: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    new_file = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def load_ledger(initial_bankroll: float) -> PaperLedger:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return PaperLedger.from_dict(json.load(fh))
    return PaperLedger(bankroll=initial_bankroll)


def save_ledger(ledger: PaperLedger) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(ledger.to_dict(), fh, indent=2)


def fetch_mlb_markets(series: str | None) -> tuple[str, list[dict]]:
    """Return (series_ticker_used, open MLB game markets). Tries candidates in order."""
    candidates = (series,) if series else CANDIDATE_SERIES
    for cand in candidates:
        try:
            resp = kalshi_get("/markets", series_ticker=cand, status="open", limit=200)
        except Exception as exc:  # noqa: BLE001 - network errors just mean "try next"
            log(f"series {cand}: request failed ({exc})")
            continue
        markets = resp.get("markets") or []
        if markets:
            return cand, markets
    return "", []


def cents(market: dict, field: str) -> float | None:
    v = market.get(field)
    if v is None or not (1 <= int(v) <= 99):
        return None
    return int(v) / 100.0


def run_pass(odds_client: TheOddsAPIClient, ledger: PaperLedger,
             args: argparse.Namespace) -> None:
    # 1) settle any open positions whose market has resolved
    for ticker in list(ledger.positions.keys()):
        try:
            market = kalshi_get(f"/markets/{ticker}").get("market") or {}
        except Exception as exc:  # noqa: BLE001
            log(f"settle check failed for {ticker}: {exc}")
            continue
        result = (market.get("result") or "").lower()
        if market.get("status") in ("settled", "finalized") and result in ("yes", "no"):
            settled = ledger.settle(ticker, result)
            if settled:
                p = settled.position
                log(f"SETTLED {ticker} {p.side.upper()} x{p.contracts} @ {p.price:.2f} "
                    f"-> result {result.upper()} | P/L {settled.pnl:+.2f} "
                    f"| bankroll {ledger.bankroll:.2f} "
                    f"| record {ledger.wins}/{ledger.settled_count}")
                append_csv({"utc": now_utc(), "event": "SETTLE", "ticker": ticker,
                            "event_ticker": p.event_ticker, "game": p.game,
                            "side": p.side, "price": f"{p.price:.2f}",
                            "fair_prob": f"{p.fair_prob:.4f}", "edge": f"{p.edge:.4f}",
                            "contracts": p.contracts,
                            "cost": f"{p.price * p.contracts:.2f}",
                            "fees": f"{p.fees:.2f}", "result": result,
                            "pnl": f"{settled.pnl:.2f}",
                            "bankroll": f"{ledger.bankroll:.2f}"})
                save_ledger(ledger)

    # 2) fair probabilities from sportsbook consensus (cached; 1 credit per refresh)
    try:
        payload = odds_client.fetch_odds(sport="baseball_mlb", regions=args.regions,
                                         markets="h2h")
    except Exception as exc:  # noqa: BLE001
        log(f"odds fetch failed: {exc}")
        return
    games = fair_probabilities_from_payload(payload)
    if odds_client.credits_remaining is not None:
        log(f"odds: {len(games)} games | API credits remaining: "
            f"{odds_client.credits_remaining}")

    # 3) Kalshi MLB markets
    series_used, markets = fetch_mlb_markets(args.series or None)
    if not markets:
        log("no open Kalshi MLB markets found (try --discover, or --series TICKER)")
        return

    # 4) evaluate every matchable market
    sizing = SizingConfig(kelly_fraction=args.kelly, min_edge=args.min_edge,
                          max_bankroll_fraction=args.max_fraction)
    now = datetime.now(timezone.utc)
    evaluated = entered = 0
    for market in markets:
        ticker = market.get("ticker") or ""
        event_ticker = market.get("event_ticker") or ""
        matched = match_market(market, games)
        if matched is None:
            continue
        commence = _parse_iso(matched.commence_time)
        if commence is not None and commence <= now:
            continue  # pregame only: in-play odds vs delayed consensus is a mismatch
        fair = games[matched.game_id]["probs"].get(matched.outcome)
        yes_ask = cents(market, "yes_ask")
        no_ask = cents(market, "no_ask")
        if fair is None or yes_ask is None:
            continue
        evaluated += 1
        signal = evaluate_market(ticker, yes_ask, fair, ledger.bankroll, sizing,
                                 fee_rate=args.fee_rate, no_price=no_ask)
        if signal is None:
            continue
        if not ledger.can_open(ticker, event_ticker, args.max_positions):
            continue
        game_desc = f"{matched.away_team} @ {matched.home_team} (YES={matched.outcome})"
        pos = ledger.open(ticker, event_ticker, signal.side, signal.price,
                          signal.contracts, signal.fair_prob, signal.edge,
                          game_desc, now_utc(), fee_rate=args.fee_rate)
        if pos is None:
            continue
        entered += 1
        log(f"ENTRY {ticker} {signal.side.upper()} x{signal.contracts} "
            f"@ {signal.price:.2f} | fair {signal.fair_prob:.3f} "
            f"| edge {signal.edge:+.3f}/contract | {game_desc} "
            f"| bankroll {ledger.bankroll:.2f}")
        append_csv({"utc": now_utc(), "event": "ENTRY", "ticker": ticker,
                    "event_ticker": event_ticker, "game": game_desc,
                    "side": signal.side, "price": f"{signal.price:.2f}",
                    "fair_prob": f"{signal.fair_prob:.4f}",
                    "edge": f"{signal.edge:.4f}", "contracts": signal.contracts,
                    "cost": f"{signal.price * signal.contracts:.2f}",
                    "fees": f"{pos.fees:.2f}", "result": "", "pnl": "",
                    "bankroll": f"{ledger.bankroll:.2f}"})
        save_ledger(ledger)

    log(f"pass done | series {series_used} | markets {len(markets)} "
        f"| priced+matched {evaluated} | new entries {entered} "
        f"| open positions {len(ledger.positions)} "
        f"| bankroll {ledger.bankroll:.2f} "
        f"| record {ledger.wins}/{ledger.settled_count}")


def discover() -> None:
    for cand in CANDIDATE_SERIES:
        try:
            resp = kalshi_get("/markets", series_ticker=cand, status="open", limit=5)
            markets = resp.get("markets") or []
            print(f"series {cand}: {len(markets)} open market(s)")
            for m in markets:
                print(f"  {m.get('ticker')}  yes_ask={m.get('yes_ask')}  "
                      f"title={m.get('title')!r}  yes_sub={m.get('yes_sub_title')!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"series {cand}: failed ({exc})")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bankroll", type=float, default=1000.0,
                   help="starting paper bankroll on first run (default 1000)")
    p.add_argument("--min-edge", type=float, default=0.03,
                   help="required net edge per contract after fees (default 0.03)")
    p.add_argument("--kelly", type=float, default=0.25,
                   help="Kelly fraction (default 0.25 = quarter Kelly)")
    p.add_argument("--max-fraction", type=float, default=0.05,
                   help="max bankroll fraction per market (default 0.05)")
    p.add_argument("--max-positions", type=int, default=10)
    p.add_argument("--fee-rate", type=float, default=0.07,
                   help="Kalshi fee coefficient (default 0.07)")
    p.add_argument("--interval", type=float, default=900,
                   help="seconds between passes in loop mode (default 900)")
    p.add_argument("--odds-refresh", type=float, default=7200,
                   help="seconds to cache sportsbook odds; each refresh costs 1 API "
                        "credit (default 7200 = ~12 credits/day)")
    p.add_argument("--regions", default="us",
                   help="The Odds API regions; more regions = more credits (default us)")
    p.add_argument("--series", default="",
                   help="Kalshi series ticker override (default: auto-try candidates)")
    p.add_argument("--once", action="store_true", help="single pass, then exit")
    p.add_argument("--discover", action="store_true",
                   help="list open Kalshi MLB markets and exit")
    args = p.parse_args()

    if args.discover:
        discover()
        return

    odds_client = TheOddsAPIClient(cache_ttl=args.odds_refresh)
    ledger = load_ledger(args.bankroll)
    log(f"MLB paper trader started | bankroll {ledger.bankroll:.2f} "
        f"| open positions {len(ledger.positions)} "
        f"| min_edge {args.min_edge:.2f} | quarter-Kelly cap {args.max_fraction:.0%} "
        f"| PAPER ONLY - no orders are ever sent")

    while True:
        run_pass(odds_client, ledger, args)
        save_ledger(ledger)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
