#!/usr/bin/env python3
"""Trade Kalshi daily high-temperature markets against a free weather forecast.

This is the weather counterpart to ``kalshi_paper_trade.py``. Instead of hand-entered
or sportsbook probabilities, it estimates each temperature bracket's fair probability
from the US National Weather Service forecast (``api.weather.gov`` — free, no key) and
feeds that into the same boring, safe :class:`PaperTrader`: dry-run by default, a
position cap, a daily-loss stop, and no double-buying.

    export KALSHI_KEY_ID=...
    export KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem

    # prove the whole loop offline, no account and no network needed:
    python scripts/kalshi_weather_bot.py --self-test --bankroll 100

    # dry run against live DEMO markets: logs the orders it WOULD place, sends nothing
    python scripts/kalshi_weather_bot.py --bankroll 100

    # once you trust the dry-run output, place real DEMO orders (fake money):
    python scripts/kalshi_weather_bot.py --bankroll 100 --live

``--bankroll`` caps the money the sizer will ever stake against, independent of the demo
account's balance, so quarter-Kelly sizes are computed as if you had exactly that much.

> This dev sandbox blocks outbound Kalshi and weather.gov, so live passes must run in
> your own environment. The probability math is fully unit-tested offline, and
> ``--self-test`` exercises the entire trading loop with a fake client.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.economics import SizingConfig
from src.kalshi.paper import LiveGate
from src.kalshi.sources.weather import (
    STATIONS,
    Forecast,
    NWSClient,
    WeatherProbabilitySource,
    event_date_of,
    series_of,
    sigma_for_lead,
)
from src.kalshi.trader import PaperTrader, RiskLimits


# --------------------------------------------------------------------------------------
# Sizing wrapper: make --bankroll the money the sizer stakes against, not the whole
# demo account. Delegates every other call straight through to the real client.
# --------------------------------------------------------------------------------------
class _BankrollView:
    def __init__(self, client, bankroll_dollars: float):
        self._client = client
        self._cap_cents = int(bankroll_dollars * 100)

    def get_balance(self) -> dict:
        real = int(self._client.get_balance().get("balance", 0))
        return {"balance": min(real, self._cap_cents)}

    def __getattr__(self, name):
        return getattr(self._client, name)


# --------------------------------------------------------------------------------------
# Offline self-test: a fake client + synthetic markets + a synthetic forecast, so the
# whole loop (parse -> probability -> edge -> size -> place) is exercised with no network.
# --------------------------------------------------------------------------------------
class _FakeClient:
    def __init__(self, balance_cents: int, markets: list[dict]):
        self._balance = balance_cents
        self._markets = markets
        self.orders: list[dict] = []

    def get_balance(self):
        return {"balance": self._balance}

    def get_markets(self, **_):
        return {"markets": self._markets}

    def get_positions(self, **_):
        return {"market_positions": []}

    def create_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"order": {"status": "resting", **kwargs}}


def _synthetic_markets(day: date) -> list[dict]:
    """A ladder of 2°F brackets for one city, priced so a couple are mispriced."""
    stamp = f"{day.strftime('%y%b%d').upper()}"
    brackets = [
        # (lo, hi, yes_ask_cents) -- forecast high is 90°F, so 89-90 is the modal bucket
        (85, 86, 6),
        (87, 88, 20),
        (89, 90, 30),   # underpriced: true prob well above 30%
        (91, 92, 22),
        (93, 94, 8),
    ]
    markets = []
    for lo, hi, ask in brackets:
        markets.append({
            "ticker": f"KXHIGHNY-{stamp}-B{lo}",
            "status": "open",
            "floor_strike": lo,
            "cap_strike": hi,
            "strike_type": "between",
            "subtitle": f"{lo}° to {hi}°",
            "yes_ask": ask,
            "yes_bid": ask - 3,
            "no_ask": 100 - (ask - 3),
        })
    return markets


def _run_self_test(args) -> None:
    day = date.today()
    markets = _synthetic_markets(day)
    forecasts = {"KXHIGHNY": Forecast(high_f=90.0, sigma_f=sigma_for_lead(0),
                                      label="New York (synthetic)")}
    source = WeatherProbabilitySource.build(markets, forecasts)

    client = _FakeClient(balance_cents=int(args.bankroll * 100), markets=markets)
    limits = RiskLimits(max_open_positions=args.max_positions,
                        max_daily_loss_fraction=args.max_daily_loss,
                        dry_run=not args.live)
    trader = PaperTrader(client, source, SizingConfig(), limits)

    print(f"\nKalshi weather bot  |  SELF-TEST (offline, fake client)  |  "
          f"bankroll ${args.bankroll:,.2f}\n" + "-" * 68)
    print(" synthetic forecast: New York high = 90°F  (sigma "
          f"{forecasts['KXHIGHNY'].sigma_f:.1f}°F)\n")
    print(f" {'bracket':<16}{'yes_ask':>9}{'fair':>9}")
    for m in markets:
        t = m["ticker"]
        fair = source.fair_probability(t)
        print(f" {m['subtitle']:<16}{m['yes_ask']:>8}¢{fair * 100:>8.1f}%")

    _run_and_report(trader, markets, args)


# --------------------------------------------------------------------------------------
# Live path: fetch open weather markets from Kalshi DEMO + NWS forecasts, then trade.
# --------------------------------------------------------------------------------------
def _fetch_open_markets(client, series: list[str], target: date) -> list[dict]:
    out: list[dict] = []
    for s in series:
        resp = client.get_markets(series_ticker=s, status="open", limit=200)
        for m in resp.get("markets", []):
            ed = event_date_of(m.get("ticker", ""))
            if ed is None or ed == target:
                out.append(m)
    return out


def _build_forecasts(series: list[str], target: date, today: date) -> dict[str, Forecast]:
    nws = NWSClient()
    lead = (target - today).days
    sigma = sigma_for_lead(lead)
    forecasts: dict[str, Forecast] = {}
    for s in series:
        label, lat, lon = STATIONS[s]
        high = nws.forecast_high(lat, lon, target)
        if high is None:
            print(f"  [skip] {s}: no NWS forecast high for {target.isoformat()}")
            continue
        forecasts[s] = Forecast(high_f=high, sigma_f=sigma, label=label)
        print(f"  {s:<12}{label:<26} high {high:>5.0f}°F   sigma {sigma:.1f}°F")
    return forecasts


def _run_live(args) -> None:
    today = date.today()
    target = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
        else today + timedelta(days=args.day_offset)
    )
    series = args.series or list(STATIONS.keys())
    bad = [s for s in series if s not in STATIONS]
    if bad:
        sys.exit(f"Unknown weather series {bad}. Known: {', '.join(STATIONS)}")

    mode = "LIVE-DEMO (placing orders)" if args.live else "DRY-RUN (no orders sent)"
    print(f"\nKalshi weather bot  |  {mode}  |  target {target.isoformat()}  |  "
          f"bankroll ${args.bankroll:,.2f}\n" + "-" * 68)

    # Demo sandbox only. This script never touches PROD.
    try:
        client = LiveGate(enabled=True, env="demo").client()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Could not build Kalshi client: {type(e).__name__}: {e}")

    print(" forecasts (NWS):")
    try:
        forecasts = _build_forecasts(series, target, today)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"\nCould not reach the NWS forecast API ({type(e).__name__}: "
                 f"{str(e)[:160]}).\nThis is expected inside the dev sandbox; run in "
                 f"your own environment. Try --self-test to exercise the loop offline.")
    if not forecasts:
        sys.exit("No forecasts available for the requested cities; nothing to trade.")

    try:
        markets = _fetch_open_markets(client, list(forecasts.keys()), target)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"\nCould not read Kalshi markets ({type(e).__name__}: "
                 f"{str(e)[:160]}).\nThis is expected inside the dev sandbox; run in "
                 f"your own environment. Try --self-test to exercise the loop offline.")

    print(f"\n {len(markets)} open bracket(s) across "
          f"{len(forecasts)} city/cities for {target.isoformat()}")

    source = WeatherProbabilitySource.build(markets, forecasts)
    client = _BankrollView(client, args.bankroll)
    limits = RiskLimits(max_open_positions=args.max_positions,
                        max_daily_loss_fraction=args.max_daily_loss,
                        dry_run=not args.live)
    trader = PaperTrader(client, source, SizingConfig(), limits)
    _run_and_report(trader, markets, args)


def _run_and_report(trader: PaperTrader, markets: list[dict], args) -> None:
    print()
    state = trader.run_once(markets)
    placed = sum(o.placed for o in state.orders)
    print("\n" + "-" * 68)
    print(f" effective bankroll: ${state.start_balance:,.2f}")
    print(f" signals: {len(state.orders)}   orders placed: {placed}")
    for o in state.orders:
        tag = "PLACED" if o.placed else "would buy"
        print(f"   {tag:<9} {o.side.upper():<3} {o.ticker:<22} "
              f"x{o.count} @ {o.price_cents}¢  (edge ${o.edge:.3f})")
    if not state.orders:
        print(" No actionable edges this pass (forecast agrees with the market).")
    print("-" * 68)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bankroll", type=float, default=100.0,
                   help="dollars the sizer stakes against (default 100)")
    p.add_argument("--live", action="store_true",
                   help="actually place demo orders (default is a dry run)")
    p.add_argument("--self-test", action="store_true",
                   help="run the whole loop offline on synthetic markets (no network)")
    p.add_argument("--series", nargs="*", default=None,
                   help=f"weather series to watch (default all): {', '.join(STATIONS)}")
    p.add_argument("--date", default=None,
                   help="target event date YYYY-MM-DD (default today + --day-offset)")
    p.add_argument("--day-offset", type=int, default=0,
                   help="trade the event this many days out (0=today, 1=tomorrow)")
    p.add_argument("--max-positions", type=int, default=10)
    p.add_argument("--max-daily-loss", type=float, default=0.10)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.self_test:
        _run_self_test(args)
    else:
        _run_live(args)


if __name__ == "__main__":
    main()
