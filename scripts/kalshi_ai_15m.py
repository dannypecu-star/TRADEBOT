#!/usr/bin/env python3
"""Paper-trade Kalshi's 15-minute crypto markets with an AI direction call -- locally.

No NightShark and no Kalshi account needed: market data comes from Kalshi's public API.
Nothing is ever ordered; every fill is simulated against the live order book.

    # free test run, no API key: a momentum rule stands in for the AI
    python scripts/kalshi_ai_15m.py --model momentum

    # with Claude (set ANTHROPIC_API_KEY first)
    python scripts/kalshi_ai_15m.py --assets BTC ETH --size 300

    # summarize the CSV logs from earlier runs
    python scripts/kalshi_ai_15m.py --report

Output: a live dashboard in the terminal, a full event log in logs/ai15m.log, and one
CSV row per settled trade (logs/ai15m_trades.csv) and per AI prediction
(logs/ai15m_predictions.csv).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.ai15m import (SERIES, ClaudePredictor, KalshiPublic, MomentumPredictor,
                              PaperEngine, Settings, SpotFeed)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--assets", nargs="+", default=["BTC"], type=str.upper,
                   help=f"any of {' '.join(SERIES)} (default BTC)")
    p.add_argument("--size", type=float, default=300, help="contracts per trade (default 300)")
    p.add_argument("--time-delay", type=float, default=15,
                   help="ask the AI when this many minutes remain; 15 = session start")
    p.add_argument("--obey-model", choices=["yes", "no"], default="yes", type=str.lower,
                   help="'no' trades the opposite of the model")
    p.add_argument("--max-daily-profit", type=float, default=400, help="0 disables")
    p.add_argument("--max-daily-loss", type=float, default=-200, help="negative; 0 disables")
    p.add_argument("--max-entry-price", type=float, default=0.75,
                   help="never pay more than this per contract (default 0.75)")
    p.add_argument("--min-entry-minutes", type=float, default=2.0,
                   help="no new entries with less time left (default 2)")
    p.add_argument("--fee-rate", type=float, default=0.07, help="Kalshi taker fee coefficient")
    p.add_argument("--model", default="claude-opus-5-5",
                   help="Claude model id, or 'momentum' for the free baseline")
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                   help="Claude effort level (default medium)")
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--no-dashboard", action="store_true",
                   help="print plain log lines instead of a refreshing screen")
    p.add_argument("--report", action="store_true", help="summarize logs and exit")
    return p.parse_args()


def fmt(v, digits=2) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def render(engine: PaperEngine, events: deque, started: datetime) -> str:
    now = engine.clock()
    s = engine.s
    lines = [
        f"KALSHI AI 15m | PAPER | model {engine.predictor.name} | obey model "
        f"{'YES' if s.obey_model else 'NO'} | size {s.order_size:g} | limit {s.max_entry_price} | "
        f"{now.astimezone():%Y-%m-%d %H:%M:%S}",
        "",
        f"{'ASSET':6} {'MARKET':26} {'LEFT':>6} {'UP ask/bid':>12} {'DN ask/bid':>12} "
        f"{'AI':5} {'POSITION':22} STATUS",
    ]
    for asset in s.assets:
        st = engine.states[asset]
        m, b = st.market, st.book
        left = f"{m.minutes_left(now):.1f}m" if m else ""
        up = f"{fmt(b.best_ask('UP'))}/{fmt(b.best_bid('UP'))}" if b else ""
        dn = f"{fmt(b.best_ask('DOWN'))}/{fmt(b.best_bid('DOWN'))}" if b else ""
        ai = st.decision.direction if st.decision else ""
        pos = engine.positions.get(asset)
        pos_txt = f"{pos.side} {pos.size:g}@{pos.entry:.3f}" if pos else ""
        status = st.phase + (f" - {st.note}" if st.note else "")
        lines.append(f"{asset:6} {st.ticker[:26]:26} {left:>6} {up:>12} {dn:>12} {ai:5} "
                     f"{pos_txt:22} {status}")
    hit = f"{engine.hits}/{engine.scored} ({engine.hits / engine.scored:.0%})" if engine.scored else "0/0"
    budget = engine.loss_budget()
    lines += [
        "",
        f"Today settled PnL {engine.day_pnl:+.2f} | open MTM {engine.mark_to_market():+.2f} | "
        f"run PnL {engine.total_pnl:+.2f} | wins {engine.wins}/{engine.trades} | "
        f"model hit rate {hit} | awaiting settlement {len(engine.pending)}",
        f"Loss budget left {'uncapped' if budget is None else f'{budget:.2f}'}"
        + (" | HALTED until tomorrow" if engine.halted() else "")
        + f" | running since {started.astimezone():%H:%M}",
        "",
        "Recent events (full log in " + os.path.join(s.log_dir, "ai15m.log") + "):",
    ]
    lines += list(events) or ["  (none yet)"]
    lines += ["", "Ctrl+C to stop."]
    return "\n".join(lines)


def report(log_dir: str) -> None:
    trades_path = os.path.join(log_dir, "ai15m_trades.csv")
    preds_path = os.path.join(log_dir, "ai15m_predictions.csv")
    if os.path.exists(trades_path):
        with open(trades_path, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r["result"] != "void"]
        pnl = sum(float(r["pnl"]) for r in rows)
        fees = sum(float(r["fees"]) for r in rows)
        wins = sum(float(r["pnl"]) > 0 for r in rows)
        avg_entry = sum(float(r["entry"]) for r in rows) / len(rows) if rows else 0
        print(f"Trades: {len(rows)} | wins {wins} ({wins / len(rows):.1%})" if rows else "Trades: 0")
        if rows:
            print(f"Net PnL {pnl:+.2f} (fees paid {fees:.2f}) | average entry {avg_entry:.3f}"
                  f" -> break-even win rate about {avg_entry + fees / sum(float(r['size']) for r in rows):.1%}")
            for asset in sorted({r["asset"] for r in rows}):
                sub = [r for r in rows if r["asset"] == asset]
                print(f"  {asset}: {len(sub)} trades, PnL {sum(float(r['pnl']) for r in sub):+.2f}")
    else:
        print("No settled trades yet.")
    if os.path.exists(preds_path):
        with open(preds_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        scored = [r for r in rows if r["prediction_result"] in ("MATCH", "MISS")]
        skips = sum(r["predicted"] == "SKIP" for r in rows)
        hits = sum(r["prediction_result"] == "MATCH" for r in scored)
        print(f"Predictions: {len(rows)} | SKIP {skips} | directional {len(scored)}"
              + (f" | hit rate {hits / len(scored):.1%}" if scored else ""))
        for model in sorted({r["model"] for r in scored}):
            sub = [r for r in scored if r["model"] == model]
            h = sum(r["prediction_result"] == "MATCH" for r in sub)
            print(f"  {model}: {h}/{len(sub)} ({h / len(sub):.1%})")
    else:
        print("No scored predictions yet.")


def main() -> None:
    args = parse_args()
    if args.report:
        report(args.log_dir)
        return

    settings = Settings(
        assets=args.assets, order_size=args.size, time_delay=args.time_delay,
        obey_model=args.obey_model == "yes", max_daily_profit=args.max_daily_profit,
        max_daily_loss=args.max_daily_loss, max_entry_price=args.max_entry_price,
        min_entry_minutes=args.min_entry_minutes, fee_rate=args.fee_rate, log_dir=args.log_dir,
    )
    errors = settings.validate()
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        sys.exit(2)

    if args.model == "momentum":
        predictor = MomentumPredictor()
    else:
        predictor = ClaudePredictor(args.model, args.effort)

    os.makedirs(settings.log_dir, exist_ok=True)
    log_file = open(os.path.join(settings.log_dir, "ai15m.log"), "a", encoding="utf-8")
    events: deque = deque(maxlen=10)

    def log(msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} {msg}"
        log_file.write(f"{datetime.now():%Y-%m-%d} {line}\n")
        log_file.flush()
        events.append("  " + line)
        if args.no_dashboard:
            print(line, flush=True)

    engine = PaperEngine(settings, KalshiPublic(), SpotFeed(), predictor, log=log)
    started = datetime.now(timezone.utc)
    log(f"Started | PAPER | {' '.join(settings.assets)} | model {predictor.name} | "
        f"size {settings.order_size:g} | AI at {settings.time_delay:g}m left | "
        f"limits {settings.max_daily_profit:+g}/{settings.max_daily_loss:+g}")
    if os.name == "nt":
        os.system("")  # enables ANSI escape codes in the Windows console
    try:
        while True:
            engine.step()
            if not args.no_dashboard:
                sys.stdout.write("\033[H\033[2J" + render(engine, events, started) + "\n")
                sys.stdout.flush()
            time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        log(f"Stopped | run PnL {engine.total_pnl:+.2f} | open positions {len(engine.positions)} "
            f"| awaiting settlement {len(engine.pending)}")
        print("\nStopped. Run with --report to summarize the logs.")


if __name__ == "__main__":
    main()
