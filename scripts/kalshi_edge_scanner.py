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

Weather fair values use an ensemble of independent forecast sources:
  - NWS point forecast for the settlement station (api.weather.gov)
  - Open-Meteo (api.open-meteo.com, free, no key): ECMWF, GFS and ICON
    global models fetched in a single call
The ensemble mean is the estimate; disagreement between members widens sigma,
so the model claims less edge exactly when the sources are least sure. For
same-day markets the observed running max temperature puts a hard floor under
the outcome (the day's high can only go up), which sharpens fair values on the
most liquid contracts.

Usage:
    pip install requests
    python scripts/kalshi_edge_scanner.py

Importable: scan() returns (alerts, notes); each alert carries a raw dict
(ticker/action/ask/fair/net/size) that scripts/kalshi_paper_bot.py trades on.

No API key needed -- read-only public endpoints only. This script never
places orders. Nothing here is financial advice; verify everything on
kalshi.com before trading.
"""

import math
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter, Retry

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"  # serves ALL markets
NWS_API = "https://api.weather.gov"
OPEN_METEO_API = "https://api.open-meteo.com/v1/forecast"

# Independent global models pulled from Open-Meteo in one call.
OPEN_METEO_MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless"]

EDGE_THRESHOLD = 0.12       # 12 percentage points, net of everything below
UNCERTAINTY_HAIRCUT = 0.05  # subtract 5pp from every raw edge (model doubt)
SINGLE_SOURCE_HAIRCUT = 0.03  # extra doubt when only one forecast source responded
MIN_BOOK_DEPTH = 25         # contracts available at the ask to count as fillable
MIN_PRICE, MAX_PRICE = 0.10, 0.90  # skip longshots/near-settled (calibration-driven)
# "Too good to be true" guard. When the model's fair probability exceeds the
# market's implied probability (the ask) by more than this multiple, treat the
# gap as model error, not edge, and skip. Live settlements showed the model
# assigning ~23% to strikes that hit ~7%; large *relative* disagreement is the
# fingerprint of an overconfident tail model, not a real mispricing.
MAX_EDGE_RATIO = 2.25
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
# Tightened after 42 live settlements showed the model over-assigning
# probability to strikes far from the forecast (implied ~23% vs realized ~7%).
# A narrower distribution puts less mass in the tails, so fewer bogus longshot
# "edges" clear the bar. Re-fit these with `kalshi_paper_bot.py --calibrate`.
def forecast_sigma(hours_out: float) -> float:
    if hours_out <= 12:
        return 1.4
    if hours_out <= 24:
        return 1.8
    if hours_out <= 48:
        return 2.5
    return 3.3

# Non-weather series to pull for internal-consistency checks (legs sum to 100%).
CONSISTENCY_SERIES = ["KXFEDDECISION", "KXCPI", "KXCPIYOY", "KXFED"]

TIMEOUT = 20

# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------

def make_session() -> requests.Session:
    """Pooled session with retries -- one TLS handshake per host, not per call."""
    s = requests.Session()
    s.headers["User-Agent"] = "kalshi-edge-scanner/3.0 (personal research)"
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
# BENCHMARK 1: FORECAST ENSEMBLE (NWS + OPEN-METEO MODELS) vs WEATHER MARKETS
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
    """{date: {model: forecast high degF}} from Open-Meteo, up to 16 days out.
    All requested models come back in a single call, already in Fahrenheit."""
    data = get_json(OPEN_METEO_API, {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": 16,
        "models": ",".join(OPEN_METEO_MODELS),
    })
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    highs = {}
    for model in OPEN_METEO_MODELS:
        for d, t in zip(dates, daily.get(f"temperature_2m_max_{model}", [])):
            if t is not None:
                highs.setdefault(d, {})[model] = t
    if not highs:  # single-model responses come back unsuffixed
        for d, t in zip(dates, daily.get("temperature_2m_max", [])):
            if t is not None:
                highs.setdefault(d, {})["open-meteo"] = t
    return highs


def openmeteo_observed_max(lat, lon):
    """(station-local date, max temp degF observed so far today) or (date, None).
    The day's high can only go up, so this floors same-day fair values."""
    data = get_json(OPEN_METEO_API, {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": 1,
    })
    offset = data.get("utc_offset_seconds", 0)
    now_local = datetime.now(timezone.utc) + timedelta(seconds=offset)
    today = f"{now_local:%Y-%m-%d}"
    cutoff = f"{now_local:%Y-%m-%dT%H:%M}"
    hourly = data.get("hourly", {})
    temps = [t for ts, t in zip(hourly.get("time", []),
                                hourly.get("temperature_2m", []))
             if t is not None and ts[:10] == today and ts <= cutoff]
    return today, (max(temps) if temps else None)


def fetch_forecasts(notes):
    """Fetch every forecast source for every city in parallel.
    Returns {series: {"nws": {date: high}, "om": {date: {model: high}},
                      "obs": (local_today, max_so_far_or_None)}}."""
    out = {s: {"nws": {}, "om": {}, "obs": (None, None)} for s in WEATHER_SERIES}
    jobs = {"nws": nws_daily_highs, "om": openmeteo_daily_highs,
            "obs": openmeteo_observed_max}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {(s, name): pool.submit(fn, info["lat"], info["lon"])
                   for s, info in WEATHER_SERIES.items()
                   for name, fn in jobs.items()}
        for (series, source), fut in futures.items():
            try:
                out[series][source] = fut.result()
            except Exception as e:
                notes.append(f"[skip] {series}/{source}: forecast fetch failed ({e})")
    return out


def blended_forecast(fc, date):
    """Combine NWS + Open-Meteo model highs for one date.
    Returns (mu, extra_var, sources) or None if no member has the date.
    extra_var is the ensemble variance -- sigma widens when members disagree."""
    members = []
    nws = fc["nws"].get(date)
    if nws is not None:
        members.append(("NWS", nws))
    for model, t in sorted(fc["om"].get(date, {}).items()):
        members.append((model.split("_")[0].upper(), t))
    if not members:
        return None
    vals = [t for _, t in members]
    mu = sum(vals) / len(vals)
    extra_var = (sum((v - mu) ** 2 for v in vals) / len(vals)
                 if len(vals) > 1 else 0.0)
    sources = [f"{name} {t:.0f}F" for name, t in members]
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


def fair_prob_for_strike(m, mu, sigma, obs_max=None):
    """P(market resolves YES) given final high H ~ Normal(mu, sigma), optionally
    truncated below at obs_max (today's running max can only go up).
    Uses 0.5 continuity correction since settlement is in whole degrees."""
    def p_above(t):
        if obs_max is not None:
            if t <= obs_max:
                return 1.0
            mass_above_obs = 1 - normal_cdf(obs_max, mu, sigma)
            if mass_above_obs < 1e-9:  # forecast fully below obs: high = obs
                return 0.0
            return (1 - normal_cdf(t, mu, sigma)) / mass_above_obs
        return 1 - normal_cdf(t, mu, sigma)

    st = m.get("strike_type")
    if st == "greater":      # yes if high > floor_strike
        return p_above(float(m["floor_strike"]) + 0.5)
    if st == "less":         # yes if high < cap_strike
        return 1 - p_above(float(m["cap_strike"]) - 0.5)
    if st == "between":
        lo = float(m["floor_strike"]) - 0.5
        hi = float(m["cap_strike"]) + 0.5
        return p_above(lo) - p_above(hi)
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
        local_today, obs_so_far = fc["obs"]

        for event, legs in group_by_event(markets).items():
            date = event_date_from_ticker(event)
            if not date:
                continue
            blend = blended_forecast(fc, date)
            if blend is None:
                notes.append(f"[skip] {event}: no forecast for {date}")
                continue
            mu, extra_var, sources = blend
            obs = obs_so_far if date == local_today else None

            close = legs[0].get("close_time", "")
            try:
                close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
                hrs = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            except Exception:
                hrs = 24
            # Base verification error, widened by ensemble disagreement.
            sigma = math.sqrt(forecast_sigma(max(hrs, 0)) ** 2 + extra_var)
            haircut = UNCERTAINTY_HAIRCUT + (
                SINGLE_SOURCE_HAIRCUT if len(sources) < 2 else 0.0)

            benchmark = (f"blend {mu:.0f}F [{', '.join(sources)}] "
                         f"sigma {sigma:.1f}")
            if obs is not None:
                benchmark += f", obs so far {obs:.0f}F"

            for m in legs:
                fair = fair_prob_for_strike(m, mu, sigma, obs_max=obs)
                if fair is None:
                    continue
                evaluate(m, fair, alerts, haircut=haircut,
                         benchmark=benchmark,
                         settlement="NWS Climatological Report (Daily) for the "
                                    f"official station -- {info['city']}. NOT app "
                                    "weather; station readings can differ 1-3F.",
                         why="Retail flow anchors on phone-app forecasts or "
                             "yesterday's temps; a multi-model ensemble at the "
                             "settlement station is the better estimator.")


# ----------------------------------------------------------------------------
# BENCHMARK 2: INTERNAL CONSISTENCY (legs of one event should sum to ~100%)
# ----------------------------------------------------------------------------

def scan_consistency(alerts, notes, series_list):
    markets_by_series = fetch_all_markets(series_list, notes)
    for series, markets in markets_by_series.items():
        for event, legs in group_by_event(markets).items():
            if len(legs) < 3:
                continue
            rows = []  # (ticker, yes_ask, no_ask, yes_size, no_size)
            for m in legs:
                ya, na, ysz, nsz = market_prices(m)
                if ya <= 0 or na <= 0:
                    rows = []
                    break
                rows.append((m["ticker"], ya, na, ysz, nsz))
            if not rows:
                continue
            n = len(rows)
            yes_cost = sum(ya + taker_fee(ya) for _, ya, _, _, _ in rows)
            no_gain = sum((1 - na) - taker_fee(na) for _, _, na, _, _ in rows)

            # Arb 1: buy YES on all legs for < $1 -> guaranteed profit
            # (exactly one leg pays $1). Needs YES depth on every leg.
            if (yes_cost < 1 - EDGE_THRESHOLD
                    and all(ysz >= MIN_BOOK_DEPTH for _, _, _, ysz, _ in rows)):
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
                    "raw": {"action": "arb_yes", "event": event,
                            "net": 1 - yes_cost,
                            "legs": [{"ticker": t, "action": "buy_yes",
                                      "ask": ya, "size": ysz}
                                     for t, ya, _, ysz, _ in rows]},
                })
            # Arb 2: buy NO on all legs -- collects $1*(n-1) if exhaustive.
            # Needs NO depth on every leg.
            if (no_gain > (n - 1) + EDGE_THRESHOLD
                    and all(nsz >= MIN_BOOK_DEPTH for _, _, _, _, nsz in rows)):
                alerts.append({
                    "ticker": event, "kind": "INTERNAL ARB (buy all NO)",
                    "price": f"net credit ${f(no_gain)} vs ${n-1} owed across {n} legs",
                    "fair": f"exactly {n-1} NO legs pay out",
                    "edge": f"{(no_gain - (n - 1)) * 100:.1f}pp guaranteed if filled",
                    "settlement": "Same event, same source across legs.",
                    "why": "Overpriced YES tails inflate NO value in aggregate.",
                    "risk": "Fill risk across many legs; capital tied to expiry.",
                    "raw": {"action": "arb_no", "event": event,
                            "net": no_gain - (n - 1),
                            "legs": [{"ticker": t, "action": "buy_no",
                                      "ask": na, "size": nsz}
                                     for t, _, na, _, nsz in rows]},
                })


# ----------------------------------------------------------------------------
# EDGE EVALUATION (shared)
# ----------------------------------------------------------------------------

def evaluate(m, fair, alerts, benchmark, settlement, why,
             haircut=UNCERTAINTY_HAIRCUT):
    ya, na, ysz, nsz = market_prices(m)

    # Both sides priced identically: buy SIDE at ask, profit if fair > cost.
    for action, ask, size, fair_side in (("buy_yes", ya, ysz, fair),
                                         ("buy_no", na, nsz, 1 - fair)):
        if not (MIN_PRICE <= ask <= MAX_PRICE and size >= MIN_BOOK_DEPTH):
            continue
        # Distrust extreme relative disagreement: if the model claims the true
        # probability is many times the market's, that's far more likely to be
        # model error than a gift. This is the single biggest loss filter.
        if fair_side > ask * MAX_EDGE_RATIO:
            continue
        net = fair_side - ask - taker_fee(ask) - haircut
        if net >= EDGE_THRESHOLD:
            add_alert(alerts, m, action, ask, fair_side, net, size,
                      benchmark, settlement, why, haircut)


def add_alert(alerts, m, action, price, fair, net, size,
              benchmark, settlement, why, haircut):
    alerts.append({
        "ticker": m["ticker"], "kind": action.replace("buy_", "BUY ").upper(),
        "title": m.get("title", ""),
        "price": f"{price*100:.0f}c ask ({size:.0f} contracts showing)",
        "fair": f"{fair*100:.0f}% est. ({benchmark})",
        "edge": f"{net*100:.1f}pp NET of fee ({taker_fee(price)*100:.0f}c), ask "
                f"spread, and {haircut*100:.0f}pp uncertainty haircut",
        "settlement": settlement, "why": why,
        "risk": "Displayed size may be one quote layer; forecast can shift; "
                "re-check the book immediately before ordering.",
        "raw": {"action": action, "ticker": m["ticker"],
                "event": m.get("event_ticker", ""),
                "ask": price, "fair": fair, "net": net, "size": size},
    })


# ----------------------------------------------------------------------------
# SCAN (importable) + MAIN
# ----------------------------------------------------------------------------

def scan(verbose=False):
    """Run the full scan. Returns (alerts, notes)."""
    alerts, notes = [], []
    if verbose:
        print("Scanning weather markets vs NWS + Open-Meteo model ensemble...")
    scan_weather(alerts, notes)
    if verbose:
        print("Scanning internal consistency (Fed / CPI event legs)...")
    scan_consistency(alerts, notes, CONSISTENCY_SERIES)
    return alerts, notes


def main():
    print(f"Kalshi Edge Scanner -- {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"Threshold: {EDGE_THRESHOLD*100:.0f}pp net edge | haircut "
          f"{UNCERTAINTY_HAIRCUT*100:.0f}pp | min depth {MIN_BOOK_DEPTH}\n")

    alerts, notes = scan(verbose=True)

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
