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

try:
    from kalshi_edge_scanner import KALSHI_API, get_json, scan, taker_fee
except ImportError:
    sys.exit(
        "Missing kalshi_edge_scanner.py -- this bot trades on that scanner's "
        "output and needs it in the SAME folder as this script.\n"
        "Fix: clone the full repo (git clone <repo> && cd TRADEBOT && "
        "python scripts/kalshi_paper_bot.py), or download "
        "kalshi_edge_scanner.py next to this file. Also: pip install requests"
    )

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

# Sizing cut hard after a 3-day live paper run: quarter-Kelly on a miscalibrated
# model bet biggest on the trades it was most wrong about (a 781-contract, $31
# longshot that expired worthless). Tenth-Kelly plus a per-trade dollar and
# contract cap keeps any single wrong call small while the model earns trust.
START_BANKROLL = 1000.00      # simulated dollars
KELLY_FRACTION = 0.10         # tenth Kelly -- deliberately timid on a noisy model
MAX_TRADE_FRACTION = 0.02     # one directional trade <= 2% of equity
MAX_ARB_FRACTION = 0.08       # one arb basket <= 8% of equity (lower risk)
MAX_EVENT_FRACTION = 0.06     # total cost basis per event <= 6% of equity
MAX_TOTAL_EXPOSURE = 0.40     # total cost basis <= 40% of equity (keep dry powder)
MAX_CONTRACTS_PER_TRADE = 250  # absolute cap: no 700-contract lottery tickets
MAX_OPEN_POSITIONS = 20
DAILY_DRAWDOWN_HALT = 0.10    # stop opening new trades after a 10% equity DD in 24h
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


def log_trade(event_type, ticker, side, contracts, price, amount, pnl, note,
              fair=None):
    append_csv("trades.csv",
               ["ts", "type", "ticker", "side", "contracts",
                "price", "amount", "pnl", "note", "model_fair"],
               [now_iso(), event_type, ticker, side, contracts,
                f"{price:.4f}", f"{amount:.2f}",
                "" if pnl is None else f"{pnl:.2f}", note,
                "" if fair is None else f"{fair:.4f}"])


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


def _dt(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def update_drawdown_window(state):
    """Track equity-at-cost over a rolling 24h window and return True if we are
    more than DAILY_DRAWDOWN_HALT below the window's peak (halt new trades)."""
    now = datetime.now(timezone.utc)
    hist = state.setdefault("equity_window", [])
    hist.append([now_iso(), round(equity(state), 2)])
    state["equity_window"] = [h for h in hist
                              if (now - _dt(h[0])).total_seconds() <= 86400]
    peak = max(h[1] for h in state["equity_window"])
    return peak > 0 and equity(state) <= (1 - DAILY_DRAWDOWN_HALT) * peak


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
                  kind, group="", fair=None):
    total = contracts * cost_per
    state["cash"] -= total
    state["positions"][key] = {
        "ticker": ticker, "event": event, "side": side,
        "contracts": contracts, "cost_per": round(cost_per, 4),
        "cost_total": round(total, 2), "kind": kind, "group": group,
        "model_fair": fair, "opened": now_iso(),
    }
    log_trade("open", ticker, side, contracts, cost_per, -total, None, kind,
              fair=fair)
    return f"OPEN  {side.upper():3} x{contracts:<4} {ticker} @ {cost_per:.2f} (${total:.2f}) [{kind}]"


def try_single(state, alert, raw):
    side = raw["action"].replace("buy_", "")
    key = f"{raw['ticker']}:{side}"
    if key in state["positions"]:
        return None  # already holding this exact bet
    cost = raw["ask"] + taker_fee(raw["ask"])
    dollars = min(kelly_dollars(equity(state), raw["fair"], cost),
                  MAX_TRADE_FRACTION * equity(state))
    contracts = min(int(dollars // cost), int(raw["size"]),
                    MAX_CONTRACTS_PER_TRADE)
    if contracts < 1:
        return None
    if not room_for(state, contracts * cost, raw["event"], MAX_TRADE_FRACTION):
        return None
    return open_position(state, key, raw["ticker"], raw["event"], side,
                         contracts, cost, alert["kind"], fair=raw.get("fair"))


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
               int((MAX_ARB_FRACTION * equity(state)) // per_set),
               MAX_CONTRACTS_PER_TRADE)
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
                  payout, pnl, f"result={result}", fair=pos.get("model_fair"))
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

    # Settlements can change equity; evaluate the halt on the post-settlement book.
    halted = update_drawdown_window(state)

    alerts, scan_notes = scan(verbose=verbose)
    notes.extend(scan_notes)
    opened = [] if halted else execute_alerts(state, alerts)

    state["cycles"] += 1
    save_state(state)

    mv, mv_partial = mark_value(state, marks)
    log_equity(state, None if mv_partial and not marks else mv)

    for line in settled + opened:
        print("  " + line)
    if halted:
        print(f"  DRAWDOWN HALT: equity down >{DAILY_DRAWDOWN_HALT*100:.0f}% in 24h "
              f"-- no new trades ({len(alerts)} alert(s) suppressed)")
    elif not settled and not opened:
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


def calibrate():
    """Bucket every SETTLED directional trade by the model's predicted
    probability and compare it to the realized hit rate. A well-calibrated
    model has realized ~= predicted in every bucket; if realized is far below
    predicted, the model is overconfident (tighten sigma / raise thresholds)."""
    path = _path("trades.csv")
    if not os.path.exists(path):
        print("No trades.csv yet -- run some cycles first.")
        return
    buckets = {}  # lo -> [n, wins, sum_fair, sum_pnl]
    edges = [0.0, 0.10, 0.20, 0.30, 0.50, 1.01]
    n_settled = wins = 0
    sum_fair = sum_pnl = 0.0
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("type") != "settle" or not row.get("model_fair"):
                continue
            fair = float(row["model_fair"])
            won = float(row["pnl"]) > 0
            n_settled += 1
            wins += int(won)
            sum_fair += fair
            sum_pnl += float(row["pnl"])
            lo = max(e for e in edges if e <= fair)
            b = buckets.setdefault(lo, [0, 0, 0.0, 0.0])
            b[0] += 1
            b[1] += int(won)
            b[2] += fair
            b[3] += float(row["pnl"])
    if not n_settled:
        print("No settled directional trades with a model_fair yet.\n"
              "(Older runs before this build did not log model_fair; let the "
              "updated bot settle some fresh trades, then re-run --calibrate.)")
        return
    print(f"Calibration over {n_settled} settled directional trades\n")
    print(f"  {'pred prob':>12} | {'n':>4} | {'predicted':>9} | {'realized':>8} "
          f"| {'net P&L':>9}")
    print("  " + "-" * 55)
    for lo in sorted(buckets):
        n, w, sf, pnl = buckets[lo]
        print(f"  {lo:>6.0%}-{min(e for e in edges if e > lo):>4.0%} | {n:>4} | "
              f"{sf / n:>9.1%} | {w / n:>8.1%} | ${pnl:>+8.2f}")
    print("  " + "-" * 55)
    overall_pred = sum_fair / n_settled
    overall_real = wins / n_settled
    print(f"  {'OVERALL':>11} | {n_settled:>4} | {overall_pred:>9.1%} | "
          f"{overall_real:>8.1%} | ${sum_pnl:>+8.2f}")
    ratio = overall_pred / overall_real if overall_real else float("inf")
    print(f"\n  Model predicted {overall_pred:.1%} on average; reality was "
          f"{overall_real:.1%} ({ratio:.1f}x overconfident)." if ratio > 1.15
          else f"\n  Model looks roughly calibrated (predicted {overall_pred:.1%} "
               f"vs realized {overall_real:.1%}).")
    print("  If overconfident, lower forecast_sigma in kalshi_edge_scanner.py "
          "and/or raise EDGE_THRESHOLD / MIN_PRICE, then keep running.")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", type=float, metavar="MINUTES",
                   help="run forever, scanning every MINUTES (default: one cycle)")
    p.add_argument("--report", action="store_true",
                   help="print the current paper book and exit (no trading)")
    p.add_argument("--calibrate", action="store_true",
                   help="compare model predicted vs realized hit rate, then exit")
    p.add_argument("--reset", action="store_true",
                   help=f"delete saved state and start over at ${START_BANKROLL:.0f}")
    args = p.parse_args()

    if args.calibrate:
        calibrate()
        return

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
          f"bankroll ${state['start_bankroll']:.2f} | {KELLY_FRACTION:g}-Kelly | "
          f"max {MAX_TRADE_FRACTION*100:.0f}%/trade "
          f"(<={MAX_CONTRACTS_PER_TRADE} contracts), "
          f"{MAX_TOTAL_EXPOSURE*100:.0f}% total exposure | "
          f"{DAILY_DRAWDOWN_HALT*100:.0f}% daily-DD halt | "
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
