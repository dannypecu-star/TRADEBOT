#!/usr/bin/env python3
"""Analyze pair-bot paper CSVs: expectancy, buckets, and settlement anomalies.

    python analyze_pair_trades.py trades1.csv [trades2.csv ...]

Accepts the shared CSV schema written by both the AHK and Python pair bots.
Sessions are reconstructed by pairing each close row (RESOLUTION / STOP_EXIT /
TAKE_PROFIT) with the ENTRY rows since the previous close, so results can be
bucketed by what the bot saw at entry time (sum paid, adverse strike gap).

The anomaly check matters: a RESOLUTION whose payout exceeds $1 per pair means the
quote-based settlement proxy scored BOTH legs as winners -- possible in a genuine
split, but also exactly what a stale final snapshot produces, so those sessions are
reported separately instead of silently flattering the total.
"""
from __future__ import annotations

import csv
import sys


def fnum(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_sessions(path: str) -> list[dict]:
    sessions = []
    pending_entries: list[dict] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            event = (row.get("event") or "").strip().upper()
            if event == "ENTRY":
                pending_entries.append(row)
                continue
            if event not in ("RESOLUTION", "STOP_EXIT", "TAKE_PROFIT"):
                continue
            pnl = fnum(row.get("pnl"))
            if pnl is None:
                continue
            qty = fnum(row.get("qty"), 0) or 0
            cost = fnum(row.get("cost"), 0) or 0
            fees = fnum(row.get("fees"), 0) or 0
            gaps = [g for g in (fnum(e.get("adverseGapBps")) for e in pending_entries)
                    if g is not None]
            sums = [s for s in (fnum(e.get("sum")) for e in pending_entries)
                    if s is not None]
            payout_per_pair = (pnl + cost + fees) / qty if qty else None
            sessions.append({
                "file": path, "utc": row.get("utc") or "", "close_type": event,
                "pair": row.get("pair") or "", "pnl": pnl, "qty": qty, "cost": cost,
                "fees": fees, "entries": len(pending_entries),
                "max_gap": max(gaps) if gaps else None,
                "avg_sum": sum(sums) / len(sums) if sums else None,
                "payout_per_pair": payout_per_pair,
                "suspicious": (event == "RESOLUTION" and payout_per_pair is not None
                               and payout_per_pair > 1.0 + 1e-6),
            })
            pending_entries = []
    return sessions


def bucket_label(value, edges, labels):
    for edge, label in zip(edges, labels):
        if value <= edge:
            return label
    return labels[-1]


def summarize(name: str, sessions: list[dict]) -> None:
    if not sessions:
        print(f"\n== {name}: no closed sessions ==")
        return
    pnls = [s["pnl"] for s in sessions]
    wins = [s for s in sessions if s["pnl"] > 0]
    losses = [s for s in sessions if s["pnl"] < 0]
    stops = [s for s in sessions if s["close_type"] == "STOP_EXIT"]
    fees = sum(s["fees"] for s in sessions)
    suspicious = [s for s in sessions if s["suspicious"]]
    susp_excess = sum((s["payout_per_pair"] - 1.0) * s["qty"] for s in suspicious)

    print(f"\n== {name} ==")
    print(f"closed sessions : {len(sessions)}  "
          f"(wins {len(wins)}, losses {len(losses)}, stops {len(stops)})")
    print(f"win rate        : {len(wins) / len(sessions):.1%}")
    print(f"total P/L       : {sum(pnls):+.2f}   fees paid: {fees:.2f}")
    print(f"expectancy      : {sum(pnls) / len(sessions):+.3f} per session")
    if wins and losses:
        avg_w = sum(s['pnl'] for s in wins) / len(wins)
        avg_l = sum(s['pnl'] for s in losses) / len(losses)
        print(f"avg win / loss  : {avg_w:+.2f} / {avg_l:+.2f}  "
              f"(one loss erases ~{abs(avg_l / avg_w):.1f} wins)")
        be = abs(avg_l) / (abs(avg_l) + avg_w)
        print(f"break-even rate : {be:.1%} needed at this win/loss shape")
    if suspicious:
        print(f"SUSPICIOUS      : {len(suspicious)} resolution(s) paid >$1/pair "
              f"(quote-proxy double-wins), inflating P/L by ~{susp_excess:+.2f}")
        print(f"                  adjusted total P/L: {sum(pnls) - susp_excess:+.2f} "
              f"({(sum(pnls) - susp_excess) / len(sessions):+.3f}/session)")

    def bucket_report(title, key, edges, labels):
        rows = {}
        for s in sessions:
            v = s[key]
            if v is None:
                continue
            label = bucket_label(v, edges, labels)
            rows.setdefault(label, []).append(s["pnl"])
        if not rows:
            return
        print(f"  {title}:")
        for label in labels:
            if label not in rows:
                continue
            vals = rows[label]
            w = sum(1 for v in vals if v > 0)
            print(f"    {label:<12} n={len(vals):<4} win {w / len(vals):>5.1%}  "
                  f"P/L {sum(vals):+8.2f}  avg {sum(vals) / len(vals):+.3f}")

    bucket_report("by max adverse gap at entry (bps)", "max_gap",
                  [0.0, 1.0, 2.0, 3.0], ["<=0", "0-1", "1-2", "2-3", ">3"])
    bucket_report("by average entry sum", "avg_sum",
                  [0.85, 0.90], ["0.80-0.85", "0.85-0.90", "0.90-0.93"])


def main() -> None:
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        sys.exit(1)
    all_sessions: list[dict] = []
    for path in paths:
        sessions = load_sessions(path)
        summarize(path, sessions)
        all_sessions.extend(sessions)
    if len(paths) > 1:
        summarize("COMBINED", all_sessions)


if __name__ == "__main__":
    main()
