#!/usr/bin/env python3
"""Price a Kalshi crypto up/down leg against a fair-value model, size the trade, and
(optionally) prove the model calibrates on a synthetic backtest.

This is the "does the bot actually have an edge?" tool. The pair strategy's range rule
is zero-EV by construction; the only edge is buying a leg for less than its true
probability. Here we compute that true probability from live inputs and let the existing
``evaluate_market`` decide the side and size.

Value one leg right now:

    python scripts/kalshi_crypto_fair_value.py \
        --spot 63120 --strike 63000 --minutes 7 --typical-move 0.003 \
        --yes-price 0.62 --bankroll 800

    # or give annualised vol directly instead of a typical 15m move:
    python scripts/kalshi_crypto_fair_value.py --spot 63120 --strike 63000 \
        --minutes 7 --vol 0.6 --yes-price 0.62

Prove the harness end to end (synthetic markets, calibration report):

    python scripts/kalshi_crypto_fair_value.py --demo-backtest
"""
from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.crypto_fair_value import (
    resolved_markets_from_observations,
    up_probability,
    vol_from_typical_move,
)
from src.kalshi.economics import SizingConfig, edge
from src.kalshi.strategy import evaluate_market


def price_one_leg(args) -> None:
    if args.vol is not None:
        sigma = args.vol
        vol_note = f"{sigma:.1%}/yr (given)"
    else:
        sigma = vol_from_typical_move(args.typical_move, 15.0)
        vol_note = f"{sigma:.1%}/yr (from {args.typical_move:.2%} typical 15m move)"

    fair = up_probability(args.spot, args.strike, args.minutes, sigma)
    print(f"\nAsset spot {args.spot:,.2f}  strike {args.strike:,.2f}  "
          f"minutes left {args.minutes:g}  vol {vol_note}")
    print(f"Model fair P(up / Yes): {fair:.3f}   (P(down / No): {1 - fair:.3f})")

    if args.yes_price is None:
        print("\nNo --yes-price given; nothing to compare. Add it to see the edge.\n")
        return

    no_price = args.no_price if args.no_price is not None else 1.0 - args.yes_price
    print(f"Market: Yes ask {args.yes_price:.2f}   No ask {no_price:.2f}   "
          f"fee_rate {args.fee_rate}")
    print(f"  net edge buying Yes: {edge(fair, args.yes_price, args.fee_rate):+.3f} /contract")
    print(f"  net edge buying No : {edge(1 - fair, no_price, args.fee_rate):+.3f} /contract")

    sizing = SizingConfig(min_edge=args.min_edge)
    sig = evaluate_market("LEG", args.yes_price, fair, args.bankroll, sizing,
                          args.fee_rate, no_price=no_price)
    if sig is None:
        print(f"\n=> NO TRADE. No side clears the {args.min_edge:.2f} min-edge after fees. "
              f"This is the correct answer most of the time.\n")
        return

    cost = sig.contracts * sig.price
    print(f"\n=> BUY {sig.contracts} {sig.side.upper()} @ {sig.price:.2f}  "
          f"(edge {sig.edge:+.3f}/contract, stake ${cost:,.2f} of ${args.bankroll:,.0f} "
          f"= {cost / args.bankroll:.1%} bankroll)\n")


def demo_backtest(args) -> None:
    """Simulate up/down markets that ARE the model's GBM, mispriced with noise, and show
    the model earns money *and* calibrates. Proof the pipeline is honest before real data.
    """
    import numpy as np
    from src.kalshi.backtest import run_value_backtest
    from src.kalshi.crypto_fair_value import CryptoObservation

    rng = np.random.default_rng(args.seed)
    sigma, minutes = 0.6, 15.0
    obs = []
    for i in range(args.n):
        minutes_left = float(rng.uniform(1.0, minutes))
        elapsed_tau = (minutes - minutes_left) / (365.0 * 24.0 * 60.0)
        spot = 100.0 * math.exp(rng.normal(-0.5 * sigma ** 2 * elapsed_tau,
                                           sigma * math.sqrt(max(elapsed_tau, 1e-12))))
        true_p = up_probability(spot, 100.0, minutes_left, sigma)
        rem_sd = sigma * math.sqrt(minutes_left / (365.0 * 24.0 * 60.0))
        terminal = spot * math.exp(rng.normal(-0.5 * rem_sd ** 2, rem_sd))
        outcome = int(terminal > 100.0)
        price = float(np.clip(true_p + rng.normal(0, args.market_noise), 0.02, 0.98))
        obs.append(CryptoObservation(f"BTC-{i:04d}", spot, 100.0, minutes_left, sigma, price, outcome))

    result = run_value_backtest(
        resolved_markets_from_observations(obs),
        start_bankroll=args.bankroll,
        sizing=SizingConfig(min_edge=args.min_edge),
    )
    print(f"\nDemo backtest: {args.n} synthetic markets, market noise {args.market_noise:.2%}\n")
    print(f"  Trades taken     : {result.n_trades}")
    print(f"  Hit rate         : {result.hit_rate:.1%}")
    print(f"  Predicted edge   : {result.predicted_edge:+.3f} /contract")
    print(f"  Realized edge    : {result.realized_edge:+.3f} /contract")
    print(f"  Bankroll         : ${result.start_bankroll:,.0f} -> ${result.end_bankroll:,.0f} "
          f"({result.total_return:+.1%})")
    print(f"  Fees paid        : ${result.total_fees:,.2f}")
    print("\n  Calibration (predicted vs realized frequency):")
    print("    bucket     n     predicted   realized")
    for row in result.calibration:
        print(f"    {row['bucket']:<9} {row['n']:<5} {row['predicted']:>8.1%}   {row['realized']:>8.1%}")
    print("\n  If realized edge is positive and predicted ~ realized in every bucket, the")
    print("  model is skilled and calibrated. On REAL data, expect this to be much harder.\n")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spot", type=float, help="live underlying price")
    p.add_argument("--strike", type=float, help="market strike (open price for up/down)")
    p.add_argument("--minutes", type=float, help="minutes until settlement")
    p.add_argument("--vol", type=float, default=None, help="annualised vol, e.g. 0.6")
    p.add_argument("--typical-move", type=float, default=0.003,
                   help="typical 1-sigma move over 15m if --vol not given (default 0.3%%)")
    p.add_argument("--yes-price", type=float, default=None, help="market Yes ask (dollars)")
    p.add_argument("--no-price", type=float, default=None, help="market No ask (defaults 1-yes)")
    p.add_argument("--bankroll", type=float, default=800.0)
    p.add_argument("--fee-rate", type=float, default=0.07,
                   help="Kalshi fee coefficient; crypto series are often 0.035")
    p.add_argument("--min-edge", type=float, default=0.02, help="min net edge to trade")
    p.add_argument("--demo-backtest", action="store_true", help="run the synthetic proof instead")
    p.add_argument("--n", type=int, default=6000, help="demo-backtest market count")
    p.add_argument("--market-noise", type=float, default=0.03, help="demo-backtest mispricing sd")
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    if args.demo_backtest:
        demo_backtest(args)
        return
    if None in (args.spot, args.strike, args.minutes):
        p.error("provide --spot, --strike and --minutes (or use --demo-backtest)")
    price_one_leg(args)


if __name__ == "__main__":
    main()
