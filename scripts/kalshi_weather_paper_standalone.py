#!/usr/bin/env python3
"""Kalshi daily-high-temperature paper trader -- single-file, zero-setup edition.

    python kalshi_weather_paper_standalone.py

Standard library only: no pip, no API keys, no account. Kalshi market data is public
and fair values come from the National Weather Service API (api.weather.gov, free).
Trades are simulated at the quoted ask; results append to weather_paper_trades.csv
next to this file, and bankroll/positions persist in weather_paper_state.json.
PAPER ONLY: there is no code path that sends an order.

How fair value is estimated
---------------------------
Kalshi's high-temp markets settle on a specific station's official daily high. For
each city we combine two free NWS feeds:

  * hourly forecast -> the expected max over the REMAINING hours of today, and
  * live station observations -> the high already recorded so far today.

The day's high is modeled as max(observed_so_far, Normal(remaining forecast max,
sigma)), where sigma shrinks as the day runs out of hours. That "already observed"
term is the workhorse: buckets below the recorded high get probability 0, and late
in the day the distribution collapses toward certainty while quotes sometimes lag.

Honesty notes: this is a deliberately simple model (pros use ensemble weather models);
MIN_EDGE is set high (5c) to compensate, and the point of paper trading it is to
measure whether even the simple version clears fees. Station IDs below follow each
market's settlement source per Kalshi's rules pages; verify before trusting a city.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# =========================
#  Settings
# =========================
INITIAL_BANKROLL = 10000.0
ORDER_SIZE = 10            # contracts per entry
MIN_EDGE = 0.05            # required (fair - price - fee) per contract, dollars
FEE_RATE = 0.07            # Kalshi taker fee coefficient
MAX_POSITIONS = 12         # across all cities
PRICE_MIN, PRICE_MAX = 0.05, 0.95   # ignore near-certain quotes; no juice after fees
BASE_SIGMA_F = 2.5         # forecast-high uncertainty (deg F) with a full day left
MIN_SIGMA_F = 0.6          # floor once the day is nearly over
POLL_SECONDS = 300         # market scan cadence (5 min)
FORECAST_TTL = 1800        # reuse NWS forecast for 30 min
OBS_TTL = 600              # reuse station observations for 10 min

# City config: Kalshi series -> NWS station + coordinates for the forecast gridpoint.
# Stations follow the settlement sources named in each market's rules (e.g. NYC highs
# settle on Central Park = KNYC). If you enable more cities, verify the station first.
CITIES = {
    "KXHIGHNY":   {"name": "New York (Central Park)", "station": "KNYC", "lat": 40.783, "lon": -73.967},
    "KXHIGHCHI":  {"name": "Chicago (Midway)",        "station": "KMDW", "lat": 41.786, "lon": -87.752},
    "KXHIGHMIA":  {"name": "Miami (Intl Airport)",    "station": "KMIA", "lat": 25.788, "lon": -80.317},
    "KXHIGHAUS":  {"name": "Austin (Camp Mabry)",     "station": "KATT", "lat": 30.321, "lon": -97.760},
    "KXHIGHDEN":  {"name": "Denver (Intl Airport)",   "station": "KDEN", "lat": 39.847, "lon": -104.656},
    "KXHIGHPHIL": {"name": "Philadelphia (Intl)",     "station": "KPHL", "lat": 39.868, "lon": -75.231},
    "KXHIGHLAX":  {"name": "Los Angeles (LAX)",       "station": "KLAX", "lat": 33.938, "lon": -118.389},
}

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(HERE, "weather_paper_trades.csv")
STATE_FILE = os.path.join(HERE, "weather_paper_state.json")
CSV_HEADER = ["utc", "event", "city", "ticker", "bucket", "side", "price", "fair",
              "edge", "qty", "cost", "fees", "result", "pnl", "bankroll"]

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now_utc()}  {msg}", flush=True)


def http_json(url: str, params: dict | None = None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "weather-paper-bot/1.0 (research; contact via github)",
        "Accept": "application/geo+json, application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fee_per_contract(price: float, rate: float = FEE_RATE) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    return math.ceil(rate * price * (1.0 - price) * 100.0) / 100.0


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def append_csv(row: dict) -> None:
    new_file = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


# =========================
#  Probability model
# =========================
def bucket_probability(mu: float, sigma: float, obs_max: float | None,
                       floor_strike: float | None, cap_strike: float | None) -> float:
    """P(daily high lands in this bucket), high = max(obs_max, X), X ~ N(mu, sigma).

    Buckets are integer deg-F with inclusive bounds ("between 83 and 84" means the
    high is 83 or 84), so bucket edges get the usual +/-0.5 continuity correction.
    ``greater`` means strictly above floor_strike; ``less`` strictly below cap.
    """
    sigma = max(sigma, 1e-6)
    lo = floor_strike - 0.5 if (floor_strike is not None and cap_strike is not None) \
        else (floor_strike + 0.5 if floor_strike is not None else None)
    hi = cap_strike + 0.5 if (floor_strike is not None and cap_strike is not None) \
        else (cap_strike - 0.5 if cap_strike is not None else None)

    def phi(v: float) -> float:
        return normal_cdf((v - mu) / sigma)

    if obs_max is not None:
        if hi is not None and obs_max >= hi:
            return 0.0                       # bucket already exceeded: dead
        if lo is None or obs_max > lo:       # observed high sits inside this bucket
            return 1.0 if hi is None else phi(hi)
    p_hi = 1.0 if hi is None else phi(hi)
    p_lo = 0.0 if lo is None else phi(lo)
    return max(0.0, p_hi - p_lo)


# =========================
#  NWS feeds (free, no key)
# =========================
class Weather:
    def __init__(self):
        self.hourly_url: dict[str, str] = {}
        self.forecast: dict[str, tuple[float, list]] = {}   # series -> (fetched, periods)
        self.obs: dict[str, tuple[float, float | None]] = {}  # series -> (fetched, max F)
        self.utc_offset: dict[str, timedelta] = {}

    def _periods(self, series: str) -> list:
        cached = self.forecast.get(series)
        if cached and time.monotonic() - cached[0] < FORECAST_TTL:
            return cached[1]
        city = CITIES[series]
        try:
            if series not in self.hourly_url:
                point = http_json(f"https://api.weather.gov/points/"
                                  f"{city['lat']},{city['lon']}")
                self.hourly_url[series] = point["properties"]["forecastHourly"]
            payload = http_json(self.hourly_url[series])
            periods = payload["properties"]["periods"]
            self.forecast[series] = (time.monotonic(), periods)
            first = datetime.fromisoformat(periods[0]["startTime"])
            self.utc_offset[series] = first.utcoffset() or timedelta(0)
            return periods
        except Exception as exc:  # noqa: BLE001
            log(f"{city['name']}: forecast fetch failed: {exc}")
            return cached[1] if cached else []

    def local_today(self, series: str) -> datetime.date | None:
        offset = self.utc_offset.get(series)
        if offset is None:
            self._periods(series)
            offset = self.utc_offset.get(series)
        if offset is None:
            return None
        return (datetime.now(timezone.utc) + offset).date()

    def forecast_remaining(self, series: str) -> tuple[float | None, float]:
        """(expected max temp over today's remaining hours, hours remaining)."""
        periods = self._periods(series)
        today = self.local_today(series)
        if not periods or today is None:
            return None, 0.0
        now = datetime.now(timezone.utc)
        temps, hours = [], 0
        for p in periods:
            start = datetime.fromisoformat(p["startTime"])
            if start.date() != today or start < now - timedelta(hours=1):
                continue
            if p.get("temperatureUnit") == "F" and p.get("temperature") is not None:
                temps.append(float(p["temperature"]))
                hours += 1
        return (max(temps) if temps else None), float(hours)

    def observed_max_today(self, series: str) -> float | None:
        cached = self.obs.get(series)
        if cached and time.monotonic() - cached[0] < OBS_TTL:
            return cached[1]
        city = CITIES[series]
        today = self.local_today(series)
        offset = self.utc_offset.get(series, timedelta(0))
        best: float | None = None
        try:
            payload = http_json(f"https://api.weather.gov/stations/"
                                f"{city['station']}/observations", {"limit": 60})
            for feature in payload.get("features", []):
                props = feature.get("properties") or {}
                ts = props.get("timestamp")
                val = (props.get("temperature") or {}).get("value")
                if ts is None or val is None or today is None:
                    continue
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if (when + offset).date() != today:
                    continue
                fahrenheit = val * 9.0 / 5.0 + 32.0
                best = fahrenheit if best is None else max(best, fahrenheit)
        except Exception as exc:  # noqa: BLE001
            log(f"{city['name']}: observations fetch failed: {exc}")
            return cached[1] if cached else None
        self.obs[series] = (time.monotonic(), best)
        return best

    def estimate(self, series: str) -> tuple[float, float, float | None] | None:
        """(mu, sigma, obs_max) for today's high, or None without forecast data."""
        forecast_max, hours_left = self.forecast_remaining(series)
        obs_max = self.observed_max_today(series)
        if forecast_max is None and obs_max is None:
            return None
        if forecast_max is None:
            return obs_max, MIN_SIGMA_F, obs_max
        sigma = max(MIN_SIGMA_F, BASE_SIGMA_F * min(1.0, hours_left / 12.0))
        return forecast_max, sigma, obs_max


# =========================
#  Kalshi side
# =========================
def event_local_date(event_ticker: str):
    """Parse the date out of tickers like KXHIGHNY-26JUL19 (yyMONdd)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    return datetime(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))).date()


def fetch_today_markets(series: str, today) -> list[dict]:
    try:
        payload = http_json(f"{KALSHI_BASE}/events",
                            {"series_ticker": series, "status": "open",
                             "with_nested_markets": "true", "limit": 20})
    except Exception as exc:  # noqa: BLE001
        log(f"{series}: event fetch failed: {exc}")
        return []
    for event in payload.get("events") or []:
        if event_local_date(event.get("event_ticker")) == today:
            return [m for m in (event.get("markets") or [])
                    if (m.get("status") or "active") == "active"]
    return []


def market_bucket(market: dict) -> tuple[float | None, float | None, str]:
    floor_s = market.get("floor_strike")
    cap_s = market.get("cap_strike")
    floor_s = float(floor_s) if floor_s is not None else None
    cap_s = float(cap_s) if cap_s is not None else None
    label = market.get("yes_sub_title") or market.get("subtitle") or ""
    return floor_s, cap_s, label


def ask_dollars(market: dict, field: str) -> float | None:
    v = market.get(field)
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v / 100.0 if 1 <= v <= 99 else None


# =========================
#  Ledger (positions persist across restarts; these are multi-hour holds)
# =========================
class Ledger:
    def __init__(self):
        self.bankroll = INITIAL_BANKROLL
        self.positions: dict[str, dict] = {}
        self.settled = self.wins = 0
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as fh:
                    data = json.load(fh)
                self.bankroll = float(data.get("bankroll", INITIAL_BANKROLL))
                self.positions = data.get("positions") or {}
                self.settled = int(data.get("settled", 0))
                self.wins = int(data.get("wins", 0))
            except (ValueError, OSError):
                pass

    def save(self):
        with open(STATE_FILE, "w") as fh:
            json.dump({"bankroll": self.bankroll, "positions": self.positions,
                       "settled": self.settled, "wins": self.wins}, fh, indent=2)

    def open(self, ticker: str, pos: dict) -> bool:
        cost = pos["price"] * pos["qty"] + pos["fees"]
        if ticker in self.positions or cost > self.bankroll:
            return False
        self.bankroll -= cost
        self.positions[ticker] = pos
        self.save()
        return True

    def settle(self, ticker: str, result: str) -> dict | None:
        pos = self.positions.pop(ticker, None)
        if pos is None:
            return None
        payout = float(pos["qty"]) if result == pos["side"] else 0.0
        self.bankroll += payout
        pnl = payout - pos["price"] * pos["qty"] - pos["fees"]
        self.settled += 1
        if pnl > 0:
            self.wins += 1
        self.save()
        return {"pos": pos, "payout": payout, "pnl": pnl}


# =========================
#  Main loop
# =========================
def main() -> None:
    weather, ledger = Weather(), Ledger()
    log(f"weather paper trader started | bankroll {ledger.bankroll:.2f} "
        f"| open positions {len(ledger.positions)} | min_edge {MIN_EDGE:.2f} "
        f"| size {ORDER_SIZE} | cities {len(CITIES)} "
        f"| PAPER ONLY - no orders are ever sent")
    log(f"trades log: {CSV_FILE}")

    while True:
        # 1) settle finished positions
        for ticker in list(ledger.positions.keys()):
            try:
                market = http_json(f"{KALSHI_BASE}/markets/{ticker}").get("market") or {}
            except Exception as exc:  # noqa: BLE001
                log(f"settle check failed for {ticker}: {exc}")
                continue
            result = (market.get("result") or "").lower()
            if market.get("status") in ("settled", "finalized") and result in ("yes", "no"):
                outcome = ledger.settle(ticker, result)
                if outcome:
                    pos = outcome["pos"]
                    log(f"SETTLED {ticker} {pos['side'].upper()} x{pos['qty']} "
                        f"@ {pos['price']:.2f} -> {result.upper()} "
                        f"| P/L {outcome['pnl']:+.2f} | bankroll {ledger.bankroll:.2f} "
                        f"| record {ledger.wins}/{ledger.settled}")
                    append_csv({"utc": now_utc(), "event": "SETTLE",
                                "city": pos.get("city", ""), "ticker": ticker,
                                "bucket": pos.get("bucket", ""), "side": pos["side"],
                                "price": f"{pos['price']:.2f}",
                                "fair": f"{pos.get('fair', 0):.4f}",
                                "edge": f"{pos.get('edge', 0):.4f}", "qty": pos["qty"],
                                "cost": f"{pos['price'] * pos['qty']:.2f}",
                                "fees": f"{pos['fees']:.2f}", "result": result,
                                "pnl": f"{outcome['pnl']:.2f}",
                                "bankroll": f"{ledger.bankroll:.2f}"})

        # 2) scan each city for mispriced buckets
        for series, city in CITIES.items():
            estimate = weather.estimate(series)
            if estimate is None:
                continue
            mu, sigma, obs_max = estimate
            today = weather.local_today(series)
            markets = fetch_today_markets(series, today)
            if not markets:
                continue
            summary = (f"{city['name']}: forecast-high {mu:.0f}F sigma {sigma:.1f} "
                       f"| observed-high "
                       f"{'n/a' if obs_max is None else format(obs_max, '.0f') + 'F'} "
                       f"| {len(markets)} buckets")
            log(summary)

            for market in markets:
                ticker = market.get("ticker") or ""
                if ticker in ledger.positions or len(ledger.positions) >= MAX_POSITIONS:
                    continue
                floor_s, cap_s, label = market_bucket(market)
                if floor_s is None and cap_s is None:
                    continue
                fair = bucket_probability(mu, sigma, obs_max, floor_s, cap_s)

                for side, price_field, side_fair in (
                        ("yes", "yes_ask", fair), ("no", "no_ask", 1.0 - fair)):
                    price = ask_dollars(market, price_field)
                    if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
                        continue
                    fee = fee_per_contract(price)
                    edge = side_fair - price - fee
                    if edge < MIN_EDGE:
                        continue
                    fees = fee * ORDER_SIZE
                    pos = {"side": side, "price": price, "qty": ORDER_SIZE,
                           "fees": fees, "fair": side_fair, "edge": edge,
                           "city": city["name"], "bucket": label,
                           "opened": now_utc()}
                    if not ledger.open(ticker, pos):
                        continue
                    log(f"ENTRY {ticker} {side.upper()} x{ORDER_SIZE} @ {price:.2f} "
                        f"| bucket '{label}' | fair {side_fair:.3f} "
                        f"| edge {edge:+.3f} | {city['name']} "
                        f"| bankroll {ledger.bankroll:.2f}")
                    append_csv({"utc": now_utc(), "event": "ENTRY",
                                "city": city["name"], "ticker": ticker,
                                "bucket": label, "side": side,
                                "price": f"{price:.2f}", "fair": f"{side_fair:.4f}",
                                "edge": f"{edge:.4f}", "qty": ORDER_SIZE,
                                "cost": f"{price * ORDER_SIZE:.2f}",
                                "fees": f"{fees:.2f}", "result": "", "pnl": "",
                                "bankroll": f"{ledger.bankroll:.2f}"})
                    break  # one side per bucket

        log(f"pass done | open positions {len(ledger.positions)} "
            f"| bankroll {ledger.bankroll:.2f} | record {ledger.wins}/{ledger.settled}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
