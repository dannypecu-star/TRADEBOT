#!/usr/bin/env python3
"""
Kalshi Edge Scanner
===================
One-command scan of live Kalshi markets for meaningful mispricings, checked
against external benchmarks (NWS + Open-Meteo forecasts for weather) and
internal consistency (mutually exclusive legs of one event should sum to ~100%).

Only prints an ACTIONABLE alert when net edge >= EDGE_THRESHOLD after:
  - Kalshi taker fee  (0.07 * P * (1-P), rounded up to the cent)
  - the actual ask price you'd pay (not the mid)
  - a model-uncertainty haircut
  - a minimum book-depth requirement

Weather fair values use an ensemble of two independent forecast sources:
  - NWS point forecast for the settlement station (api.weather.gov)
  - Open-Meteo daily forecast (api.open-meteo.com, free, no key)
When both agree the blended mean is used as-is; when they disagree the
disagreement widens sigma, so the model claims less edge exactly when the
sources are least sure.

Usage:
    pip install requests
    python scripts/kalshi_edge_scanner.py

No API key needed -- read-only public endpoints only. This script never
places orders. Nothing here is financial advice; verify everything on
kalshi.com before trading.
"""

import math
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter, Retry

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"  # serves ALL markets
NWS_API = "https://api.weather.gov"
OPEN_METEO_API = "https://api.open-meteo.com/v1/forecast"

EDGE_THRESHOLD = 0.10       # 10 percentage points, net of everything below
UNCERTAINTY_HAIRCUT = 0.03  # subtract 3pp from every raw edge (model doubt)
SINGLE_SOURCE_HAIRCUT = 0.02  # extra doubt when only one forecast source responded
MIN_BOOK_DEPTH = 25         # contracts available at the ask to count as fillable
MIN_PRICE, MAX_PRICE = 0.03, 0.97  # ignore near-settled extremes (fee/noise zone)
MAX_WORKERS = 8             # parallel HTTP fetches (forecasts, market lists)

# Weather series -> NWS station coordinates (must match Kalshi settlement station)
WEATHER_SERIES = {
    "KXHIGHNY":   {"city": "NYC (Central Park)",    "lat": 40.7789, "lon": -73.9692},
    "KXHIGHCHI":  {"city": "Chicago (Midway)",      "lat": 41.7868, "lon": -87.7522},
    "KXHIGHAUS":  {"city": "Austin (Camp Mabry)",   "lat": 30.3208, "lon": -97.7604},
    "KXHIGHMIA":  {"city": "Miami (MIA)",           "lat": 25.7906, "lon": -80.3164},
    "KXHIGHDEN":  {"city": "Denver (DEN)",          "lat": 39.8467, "lon": -104.6558},
    "KXHIGHPHIL": {"city": "Philadelphia (PHL)",    "lat": 39.8683, "lon": -75.2311},
}

# Forecast error (std dev, deg F) by hours until end of event day.
# Rough NWS verification numbers; widen them and you'll flag less.
def forecast_sigma(hours_out: float) -> float:
    if hours_out <= 12:
        return 1.7
    if hours_out <= 24:
        return 2.2
    if hours_out <= 48:
        return 3.0
    return 4.0

# Non-weather series to pull for internal-consistency checks (legs sum to 100%).
CONSISTENCY_SERIES = ["KXFEDDECISION", "KXCPI", "KXCPIYOY", "KXFED"]

TIMEOUT = 20

# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------

def make_session() -> requests.Session:
    """Pooled session with retries -- one TLS handshake per host, not per call."""
    s = requests.Session()
    s.headers["User-Agent"] = "kalshi-edge-scanner/2.0 (personal research)"
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",))
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=MAX_WORKERS)
    s.mount("https://", adapter)
    return s


SESSION = make_session()


def get_json(url, params=None):
    r = SESSION.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def kalshi_markets(series_ticker):
    """All open markets for a series, paginated."""
    out, cursor = [], None
    while True:
        params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{KALSHI_API}/markets", params)
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            return out


def fetch_all_markets(series_list, notes):
    """Fetch market lists for many series concurrently -> {series: [markets]}."""
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {s: pool.submit(kalshi_markets, s) for s in series_list}
        for series, fut in futures.items():
            try:
                results[series] = fut.result()
            except Exception as e:
                notes.append(f"[skip] {series}: Kalshi fetch failed ({e})")
    return results


def taker_fee(price):
    """Kalshi trading fee per contract, rounded up to the next cent."""
    return math.ceil(7 * price * (1 - price)) / 100.0


def normal_cdf(x, mu, sigma):
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def f(x):
    return f"{x:.2f}"


def market_prices(m):
    """Return (yes_ask, no_ask, yes_ask_size, no_ask_size) as floats."""
    def g(key):
        try:
            return float(m.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
    return (g("yes_ask_dollars"), g("no_ask_dollars"),
            g("yes_ask_size_fp"), g("no_ask_size_fp"))


def group_by_event(markets):
    by_event = {}
    for m in markets:
        by_event.setdefault(m["event_ticker"], []).append(m)
    return by_event


# ----------------------------------------------------------------------------
# BENCHMARK 1: FORECAST ENSEMBLE (NWS + OPEN-METEO) vs WEATHER MARKETS
# ----------------------------------------------------------------------------

def nws_daily_highs(lat, lon):
    """{date: forecast high degF} for every daytime period NWS covers here.
    One metadata call + one forecast call per city (was one per event)."""
    meta = get_json(f"{NWS_API}/points/{lat},{lon}")
    fc = get_json(meta["properties"]["forecast"])
    highs = {}
    for period in fc["properties"]["periods"]:
        if period.get("isDaytime"):
            highs[period["startTime"][:10]] = float(period["temperature"])
    return highs


def openmeteo_daily_highs(lat, lon):
    """{date: forecast high degF} from Open-Meteo, up to 16 days out.
    Single call per city, already in Fahrenheit."""
    data = get_json(OPEN_METEO_API, {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": 16,
    })
    daily = data.get("daily", {})
    return {d: t for d, t in zip(daily.get("time", []),
                                 daily.get("temperature_2m_max", []))
            if t is not None}


def fetch_forecasts(notes):
    """Fetch both forecast sources for every city in parallel.
    Returns {series: {"nws": {date: high}, "om": {date: high}}}."""
    out = {s: {"nws": {}, "om": {}} for s in WEATHER_SERIES}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for series, info in WEATHER_SERIES.items():
            futures[(series, "nws")] = pool.submit(nws_daily_highs,
                                                   info["lat"], info["lon"])
            futures[(series, "om")] = pool.submit(openmeteo_daily_highs,
                                                  info["lat"], info["lon"])
        for (series, source), fut in futures.items():
            try:
                out[series][source] = fut.result()
            except Exception as e:
                notes.append(f"[skip] {series}/{source}: forecast fetch failed ({e})")
    return out


def blended_forecast(fc, date):
    """Combine NWS + Open-Meteo highs for one date.
    Returns (mu, extra_var, sources) or None if neither source has the date.
    extra_var widens sigma when the two sources disagree."""
    vals, sources = [], []
    nws = fc["nws"].get(date)
    om = fc["om"].get(date)
    if nws is not None:
        vals.append(nws)
        sources.append(f"NWS {nws:.0f}F")
    if om is not None:
        vals.append(om)
        sources.append(f"Open-Meteo {om:.0f}F")
    if not vals:
        return None
    mu = sum(vals) / len(vals)
    # Half the spread between sources, squared: treated as extra variance.
    extra_var = ((max(vals) - min(vals)) / 2) ** 2 if len(vals) > 1 else 0.0
    return mu, extra_var, sources


MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def event_date_from_ticker(event_ticker):
    """KXHIGHNY-26JUL17 -> '2026-07-17'."""
    try:
        tail = event_ticker.split("-")[1]
        month = MONTHS[tail[2:5].upper()]
        return f"20{tail[:2]}-{month:02d}-{int(tail[5:7]):02d}"
    except Exception:
        return None


def fair_prob_for_strike(m, mu, sigma):
    """P(market resolves YES) given forecast high ~ Normal(mu, sigma).
    Uses 0.5 continuity correction since NWS reports whole degrees."""
    st = m.get("strike_type")
    if st == "greater":      # yes if high > floor_strike
        return 1 - normal_cdf(float(m["floor_strike"]) + 0.5, mu, sigma)
    if st == "less":         # yes if high < cap_strike
        return normal_cdf(float(m["cap_strike"]) - 0.5, mu, sigma)
    if st == "between":
        lo = float(m["floor_strike"]) - 0.5
        hi = float(m["cap_strike"]) + 0.5
        return normal_cdf(hi, mu, sigma) - normal_cdf(lo, mu, sigma)
    return None


def scan_weather(alerts, notes):
    markets_by_series = fetch_all_markets(WEATHER_SERIES, notes)
    forecasts = fetch_forecasts(notes)

    for series, markets in markets_by_series.items():
        if not markets:
            notes.append(f"[skip] {series}: no open markets")
            continue
        info = WEATHER_SERIES[series]
        fc = forecasts[series]

        for event, legs in group_by_event(markets).items():
            date = event_date_from_ticker(event)
            if not date:
                continue
            blend = blended_forecast(fc, date)
            if blend is None:
                notes.append(f"[skip] {event}: no forecast for {date}")
                continue
            mu, extra_var, sources = blend

            close = legs[0].get("close_time", "")
            try:
                close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
                hrs = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            except Exception:
                hrs = 24
            # Base verification error, widened by source disagreement.
            sigma = math.sqrt(forecast_sigma(max(hrs, 0)) ** 2 + extra_var)
            haircut = UNCERTAINTY_HAIRCUT + (
                SINGLE_SOURCE_HAIRCUT if len(sources) < 2 else 0.0)

            for m in legs:
                fair = fair_prob_for_strike(m, mu, sigma)
                if fair is None:
                    continue
                evaluate(m, fair, alerts, haircut=haircut,
                         benchmark=f"blend {mu:.0f}F [{', '.join(sources)}] "
                                   f"sigma {sigma:.1f}",
                         settlement="NWS Climatological Report (Daily) for the "
                                    f"official station -- {info['city']}. NOT app "
                                    "weather; station readings can differ 1-3F.",
                         why="Retail flow anchors on phone-app forecasts or "
                             "yesterday's temps; an NWS + Open-Meteo ensemble at "
                             "the settlement station is the better estimator.")


# ----------------------------------------------------------------------------
# BENCHMARK 2: INTERNAL CONSISTENCY (legs of one event should sum to ~100%)
# ----------------------------------------------------------------------------

def scan_consistency(alerts, notes, series_list):
    markets_by_series = fetch_all_markets(series_list, notes)
    for series, markets in markets_by_series.items():
        for event, legs in group_by_event(markets).items():
            if len(legs) < 3:
                continue
            # Cost to buy YES on every leg (exactly one should pay $1).
            yes_cost = no_gain = 0.0
            ok = True
            for m in legs:
                ya, na, ysz, nsz = market_prices(m)
                if ya <= 0 or na <= 0 or ysz < MIN_BOOK_DEPTH:
                    ok = False
                    break
                yes_cost += ya + taker_fee(ya)
                no_gain += (1 - na) - taker_fee(na)
            if not ok:
                continue
            n = len(legs)
            # Arb 1: buy YES on all legs for < $1 -> guaranteed profit.
            if yes_cost < 1 - EDGE_THRESHOLD:
                alerts.append({
                    "ticker": event, "kind": "INTERNAL ARB (buy all YES)",
                    "price": f"total cost ${f(yes_cost)} incl. fees across {n} legs",
                    "fair": "exactly one leg pays $1.00",
                    "edge": f"{(1 - yes_cost) * 100:.1f}pp guaranteed if all legs fill",
                    "settlement": "All legs settle from the same official source; "
                                  "confirm legs are truly exhaustive & exclusive "
                                  "(watch for an uncovered catch-all range).",
                    "why": "Thin books drift apart; MM quotes lag on some legs.",
                    "risk": "Legs may move before you fill all of them; partial "
                            "fill leaves you directional.",
                })
            # Arb 2: buy NO on all legs -- collects $1*(n-1) if exhaustive.
            if no_gain > (n - 1) + EDGE_THRESHOLD:
                alerts.append({
                    "ticker": event, "kind": "INTERNAL ARB (buy all NO)",
                    "price": f"net credit ${f(no_gain)} vs ${n-1} owed across {n} legs",
                    "fair": f"exactly {n-1} NO legs pay out",
                    "edge": f"{(no_gain - (n - 1)) * 100:.1f}pp guaranteed if filled",
                    "settlement": "Same event, same source across legs.",
                    "why": "Overpriced YES tails inflate NO value in aggregate.",
                    "risk": "Fill risk across many legs; capital tied to expiry.",
                })


# ----------------------------------------------------------------------------
# EDGE EVALUATION (shared)
# ----------------------------------------------------------------------------

def evaluate(m, fair, alerts, benchmark, settlement, why,
             haircut=UNCERTAINTY_HAIRCUT):
    ya, na, ysz, nsz = market_prices(m)

    # Both sides priced identically: buy SIDE at ask, profit if fair > cost.
    for side, ask, size, fair_side in (("BUY YES", ya, ysz, fair),
                                       ("BUY NO", na, nsz, 1 - fair)):
        if not (MIN_PRICE <= ask <= MAX_PRICE and size >= MIN_BOOK_DEPTH):
            continue
        net = fair_side - ask - taker_fee(ask) - haircut
        if net >= EDGE_THRESHOLD:
            add_alert(alerts, m, side, ask, fair_side, net, size,
                      benchmark, settlement, why, haircut)


def add_alert(alerts, m, side, price, fair, net, size,
              benchmark, settlement, why, haircut):
    alerts.append({
        "ticker": m["ticker"], "kind": side,
        "title": m.get("title", ""),
        "price": f"{price*100:.0f}c ask ({size:.0f} contracts showing)",
        "fair": f"{fair*100:.0f}% est. ({benchmark})",
        "edge": f"{net*100:.1f}pp NET of fee ({taker_fee(price)*100:.0f}c), ask "
                f"spread, and {haircut*100:.0f}pp uncertainty haircut",
        "settlement": settlement, "why": why,
        "risk": "Displayed size may be one quote layer; forecast can shift; "
                "re-check the book immediately before ordering.",
    })


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main():
    alerts, notes = [], []
    print(f"Kalshi Edge Scanner -- {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"Threshold: {EDGE_THRESHOLD*100:.0f}pp net edge | haircut "
          f"{UNCERTAINTY_HAIRCUT*100:.0f}pp | min depth {MIN_BOOK_DEPTH}\n")

    print("Scanning weather markets vs NWS + Open-Meteo ensemble...")
    scan_weather(alerts, notes)

    print("Scanning internal consistency (Fed / CPI event legs)...")
    scan_consistency(alerts, notes, CONSISTENCY_SERIES)

    print()
    if not alerts:
        print("NO QUALIFYING EDGE. Nothing cleared the bar -- do nothing.")
    else:
        print(f"*** {len(alerts)} ACTIONABLE DISCREPANCY(IES) ***\n")
        for a in alerts:
            print(f"  CONTRACT : {a['ticker']}  [{a['kind']}]")
            if a.get("title"):
                print(f"  MARKET   : {a['title']}")
            print(f"  PRICE    : {a['price']}")
            print(f"  FAIR     : {a['fair']}")
            print(f"  EDGE     : {a['edge']}")
            print(f"  SETTLES  : {a['settlement']}")
            print(f"  WHY      : {a['why']}")
            print(f"  RISKS    : {a['risk']}\n")
        print("Not financial advice. Verify book depth and rules on kalshi.com.")

    if notes:
        print("\n--- diagnostics ---")
        for n in notes:
            print("  " + n)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        sys.exit(f"Network error: {e}")
