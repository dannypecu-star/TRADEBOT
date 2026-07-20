#!/usr/bin/env python3
"""
Kalshi Paper Bot
================
Automated paper-trading loop built on scripts/kalshi_edge_scanner.py.
Each cycle it:

  1. polls Kalshi for settlements on open paper positions and books P&L
  2. runs the full edge scan (forecast ensemble + internal-consistency arbs)
  3. "fills" qualifying trades on paper at the real ask plus the real taker fee
  4. sizes each trade with capped fractional Kelly against a simulated bankroll
  5. persists bankroll / positions / trade log / equity curve to disk, so
     restarts and cron runs continue exactly where the last run left off

SIMULATION ONLY. No orders are sent anywhere and no API key is used or needed
-- settlement comes from Kalshi's public market results. Fills are optimistic
(you always get the full displayed ask), so results are an upper bound on the
strategy, not proof it works. Not financial advice.

Usage:
    python scripts/kalshi_paper_bot.py                 # one cycle (cron-friendly)
    python scripts/kalshi_paper_bot.py --loop 15       # run forever, every 15 min
    python scripts/kalshi_paper_bot.py --report        # print current book, no trades
    python scripts/kalshi_paper_bot.py --reset         # wipe state, restart fresh

State lives in data/paper_bot/ (state.json, trades.csv, equity.csv).
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kalshi_edge_scanner import KALSHI_API, get_json, scan, taker_fee  # noqa: E402

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

START_BANKROLL = 1000.00      # simulated dollars
KELLY_FRACTION = 0.25         # quarter Kelly -- full Kelly overbets a noisy model
MAX_TRADE_FRACTION = 0.05     # one directional trade <= 5% of equity
MAX_ARB_FRACTION = 0.15       # one arb basket <= 15% of equity (lower risk)
MAX_EVENT_FRACTION = 0.10     # total cost basis per event <= 10% of equity
MAX_TOTAL_EXPOSURE = 0.60     # total cost basis <= 60% of equity (keep dry powder)
MAX_OPEN_POSITIONS = 20
SETTLE_BATCH = 40             # tickers per settlement-poll request

DATA_DIR = os.environ.get(
    "KALSHI_PAPER_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                 "data", "paper_bot"))

# ----------------------------------------------------------------------------
# STATE
# ----------------------------------------------------------------------------

def _path(name):
    return os.path.join(DATA_DIR, name)


def now_iso():
    return f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


def load_state():
    try:
        with open(_path("state.json")) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"cash": START_BANKROLL, "start_bankroll": START_BANKROLL,
                "realized_pnl": 0.0, "positions": {}, "cycles": 0,
                "created": now_iso()}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path("state.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, _path("state.json"))


def append_csv(name, header, row):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _path(name)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        w.writerow(row)


def log_trade(event_type, ticker, side, contracts, price, amount, pnl, note):
    append_csv("trades.csv",
               ["ts", "type", "ticker", "side", "contracts",
                "price", "amount", "pnl", "note"],
               [now_iso(), event_type, ticker, side, contracts,
                f"{price:.4f}", f"{amount:.2f}",
                "" if pnl is None else f"{pnl:.2f}", note])


def log_equity(state, mark_value):
    cost = cost_basis(state)
    append_csv("equity.csv",
               ["ts", "cash", "cost_basis", "mark_value",
                "realized_pnl", "equity_at_cost"],
               [now_iso(), f"{state['cash']:.2f}", f"{cost:.2f}",
                "" if mark_value is None else f"{mark_value:.2f}",
                f"{state['realized_pnl']:.2f}", f"{state['cash'] + cost:.2f}"])


# ----------------------------------------------------------------------------
# PORTFOLIO MATH
# ----------------------------------------------------------------------------

def cost_basis(state):
    return sum(p["cost_total"] for p in state["positions"].values())


def equity(state):
    """Cash + open positions at cost. Marks are for reporting only."""
    return state["cash"] + cost_basis(state)


def event_exposure(state, event):
    return sum(p["cost_total"] for p in state["positions"].values()
               if p["event"] == event)


def room_for(state, trade_cost, event, cap_fraction):
    """All portfolio limits for a candidate trade, in one place.
    An arb basket is inherently one event, so its per-event cap is the
    larger of the event cap and the basket cap."""
    eq = equity(state)
    event_cap = max(MAX_EVENT_FRACTION, cap_fraction)
    return (len(state["positions"]) < MAX_OPEN_POSITIONS
            and trade_cost <= state["cash"] + 1e-9
            and trade_cost <= cap_fraction * eq + 1e-9
            and cost_basis(state) + trade_cost <= MAX_TOTAL_EXPOSURE * eq + 1e-9
            and event_exposure(state, event) + trade_cost <= event_cap * eq + 1e-9)


def kelly_dollars(eq, fair, cost):
    """Fractional Kelly stake for a binary contract costing `cost`, paying $1."""
    edge = fair - cost
    if edge <= 0 or cost >= 1:
        return 0.0
    return KELLY_FRACTION * (edge / (1 - cost)) * eq


# ----------------------------------------------------------------------------
# EXECUTION (paper fills)
# ----------------------------------------------------------------------------

def open_position(state, key, ticker, event, side, contracts, cost_per,
                  kind, group=""):
    total = contracts * cost_per
    state["cash"] -= total
    state["positions"][key] = {
        "ticker": ticker, "event": event, "side": side,
        "contracts": contracts, "cost_per": round(cost_per, 4),
        "cost_total": round(total, 2), "kind": kind, "group": group,
        "opened": now_iso(),
    }
    log_trade("open", ticker, side, contracts, cost_per, -total, None, kind)
    return f"OPEN  {side.upper():3} x{contracts:<4} {ticker} @ {cost_per:.2f} (${total:.2f}) [{kind}]"


def try_single(state, alert, raw):
    side = raw["action"].replace("buy_", "")
    key = f"{raw['ticker']}:{side}"
    if key in state["positions"]:
        return None  # already holding this exact bet
    cost = raw["ask"] + taker_fee(raw["ask"])
    dollars = min(kelly_dollars(equity(state), raw["fair"], cost),
                  MAX_TRADE_FRACTION * equity(state))
    contracts = min(int(dollars // cost), int(raw["size"]))
    if contracts < 1:
        return None
    if not room_for(state, contracts * cost, raw["event"], MAX_TRADE_FRACTION):
        return None
    return open_position(state, key, raw["ticker"], raw["event"], side,
                         contracts, cost, alert["kind"])


def try_arb(state, alert, raw):
    group = f"{raw['event']}:{raw['action']}"
    keys = [f"{l['ticker']}:{l['action'].replace('buy_', '')}"
            for l in raw["legs"]]
    if any(k in state["positions"] for k in keys):
        return None  # partially in already -- don't stack
    per_set = sum(l["ask"] + taker_fee(l["ask"]) for l in raw["legs"])
    if per_set <= 0:
        return None
    sets = min(min(int(l["size"]) for l in raw["legs"]),
               int((MAX_ARB_FRACTION * equity(state)) // per_set))
    if sets < 1:
        return None
    if not room_for(state, sets * per_set, raw["event"], MAX_ARB_FRACTION):
        return None
    lines = []
    for l, key in zip(raw["legs"], keys):
        side = l["action"].replace("buy_", "")
        cost = l["ask"] + taker_fee(l["ask"])
        lines.append(open_position(state, key, l["ticker"], raw["event"],
                                   side, sets, cost, alert["kind"], group))
    return "\n".join(lines)


def execute_alerts(state, alerts):
    opened = []
    for a in alerts:
        raw = a.get("raw")
        if not raw:
            continue
        if raw["action"] in ("buy_yes", "buy_no"):
            evt = try_single(state, a, raw)
        else:
            evt = try_arb(state, a, raw)
        if evt:
            opened.append(evt)
    return opened


# ----------------------------------------------------------------------------
# SETTLEMENT (public market results, no auth)
# ----------------------------------------------------------------------------

def fetch_marks(state, notes):
    """Latest market snapshot for every ticker we hold -> {ticker: market}."""
    tickers = sorted({p["ticker"] for p in state["positions"].values()})
    marks = {}
    for i in range(0, len(tickers), SETTLE_BATCH):
        batch = tickers[i:i + SETTLE_BATCH]
        try:
            data = get_json(f"{KALSHI_API}/markets",
                            {"tickers": ",".join(batch), "limit": len(batch)})
        except Exception as e:
            notes.append(f"[settle] batch fetch failed ({e})")
            continue
        for m in data.get("markets", []):
            marks[m["ticker"]] = m
    return marks


def poll_settlements(state, marks):
    settled = []
    for key, pos in list(state["positions"].items()):
        m = marks.get(pos["ticker"])
        if not m:
            continue
        result = (m.get("result") or "").lower()
        if result not in ("yes", "no"):
            continue
        payout = pos["contracts"] * (1.0 if pos["side"] == result else 0.0)
        pnl = payout - pos["cost_total"]
        state["cash"] += payout
        state["realized_pnl"] += pnl
        del state["positions"][key]
        log_trade("settle", pos["ticker"], pos["side"], pos["contracts"],
                  payout / pos["contracts"] if pos["contracts"] else 0.0,
                  payout, pnl, f"result={result}")
        settled.append(f"SETTLE {pos['side'].upper():3} x{pos['contracts']:<4} "
                       f"{pos['ticker']} result={result.upper()} "
                       f"pnl ${pnl:+.2f}")
    return settled


def mark_value(state, marks):
    """Value open positions at the current bid (what you could sell for now)."""
    total, missing = 0.0, False
    for pos in state["positions"].values():
        m = marks.get(pos["ticker"])
        bid = None
        if m:
            try:
                bid = float(m.get(f"{pos['side']}_bid_dollars") or 0) or None
            except (TypeError, ValueError):
                bid = None
        if bid is None:
            total += pos["cost_total"]  # no quote: carry at cost
            missing = True
        else:
            total += pos["contracts"] * bid
    return total, missing


# ----------------------------------------------------------------------------
# CYCLE + REPORT
# ----------------------------------------------------------------------------

def run_cycle(state, verbose=True):
    notes = []
    marks = fetch_marks(state, notes)
    settled = poll_settlements(state, marks)

    alerts, scan_notes = scan(verbose=verbose)
    notes.extend(scan_notes)
    opened = execute_alerts(state, alerts)

    state["cycles"] += 1
    save_state(state)

    mv, mv_partial = mark_value(state, marks)
    log_equity(state, None if mv_partial and not marks else mv)

    for line in settled + opened:
        print("  " + line)
    if not settled and not opened:
        print(f"  no fills, no settlements ({len(alerts)} alert(s) seen, "
              f"{len(state['positions'])} position(s) open)")
    print_summary(state, mv)
    if notes and verbose:
        print("\n--- diagnostics ---")
        for n in notes:
            print("  " + n)
    return opened, settled


def print_summary(state, mv=None):
    cost = cost_basis(state)
    eq = state["cash"] + (mv if mv is not None else cost)
    ret = (eq / state["start_bankroll"] - 1) * 100
    print(f"\n  cash ${state['cash']:.2f} | open {len(state['positions'])} pos "
          f"(cost ${cost:.2f}{f', mark ${mv:.2f}' if mv is not None else ''}) | "
          f"realized P&L ${state['realized_pnl']:+.2f} | "
          f"equity ${eq:.2f} ({ret:+.1f}%) | cycle {state['cycles']}")


def report(state):
    print(f"Paper book as of {now_iso()} (started {state['created']}, "
          f"bankroll ${state['start_bankroll']:.2f})\n")
    notes = []
    marks = fetch_marks(state, notes) if state["positions"] else {}
    if not state["positions"]:
        print("  no open positions")
    for pos in sorted(state["positions"].values(), key=lambda p: p["opened"]):
        m = marks.get(pos["ticker"], {})
        try:
            bid = float(m.get(f"{pos['side']}_bid_dollars") or 0)
        except (TypeError, ValueError):
            bid = 0.0
        unreal = pos["contracts"] * bid - pos["cost_total"] if bid else None
        print(f"  {pos['side'].upper():3} x{pos['contracts']:<4} {pos['ticker']:24} "
              f"cost {pos['cost_per']:.2f}"
              + (f"  bid {bid:.2f}  unreal ${unreal:+.2f}" if unreal is not None
                 else "  (no quote)")
              + f"  [{pos['kind']}]")
    mv, _ = mark_value(state, marks)
    print_summary(state, mv if state["positions"] else None)
    for n in notes:
        print("  " + n)


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", type=float, metavar="MINUTES",
                   help="run forever, scanning every MINUTES (default: one cycle)")
    p.add_argument("--report", action="store_true",
                   help="print the current paper book and exit (no trading)")
    p.add_argument("--reset", action="store_true",
                   help=f"delete saved state and start over at ${START_BANKROLL:.0f}")
    args = p.parse_args()

    if args.reset:
        for name in ("state.json", "trades.csv", "equity.csv"):
            try:
                os.remove(_path(name))
            except FileNotFoundError:
                pass
        print(f"State cleared. Next run starts at ${START_BANKROLL:.2f}.")
        return

    state = load_state()

    if args.report:
        report(state)
        return

    print(f"Kalshi Paper Bot -- SIMULATION ONLY, no real orders\n"
          f"bankroll ${state['start_bankroll']:.2f} | quarter-Kelly | "
          f"max {MAX_TRADE_FRACTION*100:.0f}%/trade, "
          f"{MAX_TOTAL_EXPOSURE*100:.0f}% total exposure | "
          f"state: {os.path.abspath(DATA_DIR)}\n")

    if not args.loop:
        run_cycle(state)
        return

    print(f"Looping every {args.loop:g} min. Ctrl-C to stop.\n")
    while True:
        print(f"--- cycle at {now_iso()} ---")
        try:
            run_cycle(state, verbose=False)
        except Exception as e:
            print(f"  [cycle error] {type(e).__name__}: {e}")
        try:
            time.sleep(args.loop * 60)
        except KeyboardInterrupt:
            print("\nStopped. State saved.")
            return


if __name__ == "__main__":
    main()
