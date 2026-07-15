"""Probability source for Kalshi weather (daily high-temperature) markets.

Kalshi lists a daily high-temperature market for several US cities (e.g. ``KXHIGHNY``
for New York / Central Park). Each event for a given day is split into brackets — "88°
to 89°", "90° or above", "83° or below" — and each bracket is a Yes/No contract that
resolves to $1 if the day's official high lands in that range.

The edge, exactly as in the sports module, is a **better probability estimate than the
market's**. Here that estimate comes from a public forecast: the US National Weather
Service API (``api.weather.gov`` — free, no key). We model the day's high as a normal
distribution centred on the forecast high with a spread equal to the forecast's typical
error, then read each bracket's probability straight off that distribution.

Two halves, mirroring ``theoddsapi``:

  * :class:`NWSClient` — the live network call (runs in *your* environment; this dev
    sandbox blocks outbound weather.gov the same way it blocks Kalshi). Cached so
    repeated passes don't re-hit the API.
  * everything else — pure, offline, unit-tested data processing: turn a forecast +
    the market's strike bounds into a fair probability, and expose it through the
    ``ProbabilitySource`` interface the strategy consumes.

The genuinely load-bearing assumption is :func:`sigma_for_lead` — how uncertain the
forecast is. It is kept explicit and conservative rather than hidden, because (as with
the sports ``ticker_map``) that is where you can fool yourself. Feed the strategy an
over-confident sigma and it will happily size into edges that aren't there.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional

# --------------------------------------------------------------------------------------
# City registry: Kalshi high-temp series ticker -> (label, latitude, longitude).
# Coordinates are the official reporting station NWS uses for each city's daily high.
# --------------------------------------------------------------------------------------
STATIONS: dict[str, tuple[str, float, float]] = {
    "KXHIGHNY": ("New York (Central Park)", 40.7789, -73.9692),
    "KXHIGHLAX": ("Los Angeles (LAX)", 33.9382, -118.3865),
    "KXHIGHCHI": ("Chicago (Midway)", 41.7842, -87.7553),
    "KXHIGHMIA": ("Miami (Intl)", 25.7932, -80.2906),
    "KXHIGHAUS": ("Austin (Camp Mabry)", 30.3210, -97.7600),
    "KXHIGHDEN": ("Denver (Intl)", 39.8467, -104.6561),
    "KXHIGHPHIL": ("Philadelphia (Intl)", 39.8722, -75.2411),
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# --------------------------------------------------------------------------------------
# Pure math — no network, fully unit-tested.
# --------------------------------------------------------------------------------------
def normal_cdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    """Standard normal CDF via the error function (no SciPy dependency)."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def probability_between(
    mu: float,
    sigma: float,
    lo: Optional[float],
    hi: Optional[float],
) -> float:
    """P(lo <= X <= hi) for X ~ Normal(mu, sigma). ``None`` means an open end."""
    upper = normal_cdf(hi, mu, sigma) if hi is not None else 1.0
    lower = normal_cdf(lo, mu, sigma) if lo is not None else 0.0
    return max(0.0, min(1.0, upper - lower))


def sigma_for_lead(lead_days: int, base: float = 3.0, per_day: float = 1.3) -> float:
    """Forecast standard deviation (°F) as a function of how far out the day is.

    Same-/next-day NWS high-temperature forecasts have a mean absolute error of roughly
    2.5–3 °F, which corresponds to a standard deviation of about 3–4 °F; skill decays
    further out. We model that as ``base + per_day * lead_days`` and never let the
    forecast look sharper than same-day. These numbers are deliberately conservative:
    an over-tight sigma manufactures phantom edges, so err wide.
    """
    return base + per_day * max(0, lead_days)


# --------------------------------------------------------------------------------------
# Market parsing — read a bracket's numeric bounds off a Kalshi market object.
# --------------------------------------------------------------------------------------
_SUBTITLE_BETWEEN = re.compile(r"(-?\d+)\D+(-?\d+)")
_SUBTITLE_ABOVE = re.compile(r"(-?\d+)\s*°?\s*(?:or above|or higher|\+|and above)", re.I)
_SUBTITLE_BELOW = re.compile(r"(-?\d+)\s*°?\s*(?:or below|or lower|and below)", re.I)


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def market_bounds(market: dict) -> Optional[tuple[Optional[float], Optional[float]]]:
    """Return ``(lo, hi)`` degrees for a market's Yes range, or ``None`` if unparseable.

    Prefers Kalshi's structured ``floor_strike`` / ``cap_strike`` fields and falls back
    to parsing the human ``subtitle`` ("88° to 89°", "90° or above"). Either bound may
    be ``None`` for an open-ended bracket (greater-than / less-than).
    """
    floor = _num(market.get("floor_strike"))
    cap = _num(market.get("cap_strike"))
    strike_type = str(market.get("strike_type") or "").lower()

    if floor is not None or cap is not None:
        if "greater" in strike_type:
            return (floor, None)
        if "less" in strike_type:
            return (None, cap)
        # "between" or unspecified with both/one bound present
        return (floor, cap)

    subtitle = str(market.get("subtitle") or market.get("yes_sub_title") or "")
    if not subtitle:
        return None
    m = _SUBTITLE_ABOVE.search(subtitle)
    if m:
        return (float(m.group(1)), None)
    m = _SUBTITLE_BELOW.search(subtitle)
    if m:
        return (None, float(m.group(1)))
    m = _SUBTITLE_BETWEEN.search(subtitle)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    return None


def fair_probability_for_market(
    market: dict,
    forecast: "Forecast",
    bucket_pad: float = 0.5,
) -> Optional[float]:
    """Probability the day's high lands in this market's bracket, given a forecast.

    ``bucket_pad`` widens a closed bracket by half a degree on each side: a "88° to
    89°" bracket resolves Yes for any actual high that *rounds* into 88 or 89, i.e. the
    continuous interval [87.5, 89.5]. Open ends are left open.
    """
    bounds = market_bounds(market)
    if bounds is None:
        return None
    lo, hi = bounds
    if lo is not None:
        lo -= bucket_pad
    if hi is not None:
        hi += bucket_pad
    return probability_between(forecast.high_f, forecast.sigma_f, lo, hi)


def series_of(ticker: str) -> str:
    """The series ticker is the first dash-separated segment (e.g. ``KXHIGHNY``)."""
    return ticker.split("-", 1)[0]


def event_date_of(ticker: str) -> Optional[date]:
    """Parse the ``25JUL15`` middle segment of a market ticker into a date, if present."""
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    m = re.fullmatch(r"(\d{2})([A-Z]{3})(\d{2})", parts[1].upper())
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2), m.group(3)
    if mon not in _MONTHS:
        return None
    try:
        return date(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None


@dataclass
class Forecast:
    """A forecast high temperature and its uncertainty for one city/day."""

    high_f: float          # forecast daily high, °F
    sigma_f: float         # forecast standard deviation, °F (from sigma_for_lead)
    label: str = ""        # human city label, for reporting


# --------------------------------------------------------------------------------------
# The ProbabilitySource the strategy consumes.
# --------------------------------------------------------------------------------------
class WeatherProbabilitySource:
    """A ``ProbabilitySource`` driven by forecast highs.

    Build it once per pass from the open weather markets plus a forecast per city series;
    it precomputes a fair probability for every bracket and then answers
    ``fair_probability(ticker)`` as a plain dict lookup (same shape as the sportsbook
    source). Markets whose series has no forecast, or whose bounds don't parse, are
    simply omitted — the trader skips any ticker it gets ``None`` for.
    """

    def __init__(self, probabilities: dict[str, float]):
        self.probabilities = {k: float(v) for k, v in probabilities.items()}

    @classmethod
    def build(
        cls,
        markets: list[dict],
        forecasts: dict[str, Forecast],
        bucket_pad: float = 0.5,
    ) -> "WeatherProbabilitySource":
        probs: dict[str, float] = {}
        for m in markets:
            ticker = m.get("ticker")
            if not ticker:
                continue
            forecast = forecasts.get(series_of(ticker))
            if forecast is None:
                continue
            p = fair_probability_for_market(m, forecast, bucket_pad)
            if p is not None:
                probs[ticker] = p
        return cls(probs)

    def fair_probability(self, market_ticker: str) -> float | None:
        return self.probabilities.get(market_ticker)


# --------------------------------------------------------------------------------------
# Live forecast fetch — the one piece that needs the network. Runs in YOUR environment.
# --------------------------------------------------------------------------------------
_CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "cache")
)


class NWSClient:
    """Fetch daily-high forecasts from the US National Weather Service (free, no key).

    weather.gov is a two-step API: ``/points/{lat},{lon}`` returns the URL of the
    gridpoint forecast, whose daytime periods carry the forecast high. Responses are
    cached (``cache_ttl`` seconds) so repeated passes within the window don't re-hit the
    API. The NWS asks that requests carry an identifying ``User-Agent``.
    """

    BASE = "https://api.weather.gov"

    def __init__(self, user_agent: str = "tradebot-weather (contact: you@example.com)",
                 cache_ttl: float = 900.0):
        self.user_agent = user_agent
        self.cache_ttl = cache_ttl

    def _cache_file(self, lat: float, lon: float) -> str:
        return os.path.join(_CACHE_DIR, f"nws_{lat:.4f}_{lon:.4f}.json")

    def _read_cache(self, path: str) -> Optional[list[dict]]:
        if self.cache_ttl <= 0 or not os.path.exists(path):
            return None
        if time.time() - os.path.getmtime(path) > self.cache_ttl:
            return None
        with open(path, "r") as fh:
            return json.load(fh)

    def _write_cache(self, path: str, periods: list[dict]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(periods, fh)

    def _get(self, url: str) -> dict:
        import requests

        resp = requests.get(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/geo+json"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_periods(self, lat: float, lon: float) -> list[dict]:
        """Return the raw forecast periods for a location (cached)."""
        cache_path = self._cache_file(lat, lon)
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached

        point = self._get(f"{self.BASE}/points/{lat:.4f},{lon:.4f}")
        forecast_url = point["properties"]["forecast"]
        periods = self._get(forecast_url)["properties"]["periods"]
        self._write_cache(cache_path, periods)
        return periods

    def forecast_high(self, lat: float, lon: float, target: date) -> Optional[float]:
        """The forecast daytime high (°F) for ``target`` at a location, or None."""
        return high_from_periods(self.fetch_periods(lat, lon), target)


def high_from_periods(periods: list[dict], target: date) -> Optional[float]:
    """Pick the daytime-high temperature for ``target`` out of NWS forecast periods.

    Pure so it can be tested against a saved payload with no network. NWS periods are
    Fahrenheit for US points; we take the ``isDaytime`` period whose ``startTime`` falls
    on the target date.
    """
    for p in periods:
        if not p.get("isDaytime", False):
            continue
        start = str(p.get("startTime", ""))[:10]  # YYYY-MM-DD
        if start == target.isoformat() and p.get("temperature") is not None:
            return float(p["temperature"])
    return None
