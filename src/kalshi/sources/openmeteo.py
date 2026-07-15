"""Probability source for Kalshi weather markets, backed by Open-Meteo forecasts.

Kalshi's daily weather markets (e.g. "Highest temperature in NYC today") are binary
event contracts: each market resolves Yes if the day's official recorded high lands in a
band or past a threshold. To trade them with an edge you need a calibrated *distribution*
over that high, not a single point forecast -- the whole game is estimating P(high in
band) better than the market prices it.

Where the edge comes from
--------------------------
Open-Meteo exposes a free **ensemble** endpoint (GEFS/ICON/ECMWF ensemble members). Each
member is one plausible run of the atmosphere, so the spread across members *is* a
probabilistic forecast. We pull every member's hourly temperature for the target local
day, take each member's daily max, and treat those maxima as samples from the true-high
distribution. A handful of members (~31 for GEFS) is lumpy, so we smooth them with a
Gaussian kernel -- this avoids degenerate zero-probability bands and yields a proper CDF
we can integrate over any strike Kalshi offers.

Two halves, mirroring theoddsapi.py
------------------------------------
* ``OpenMeteoEnsembleClient.fetch_ensemble`` -- the live network call (runs in your
  environment; no API key required; cached to disk to be a good citizen).
* Everything else -- ``daily_max_per_member``, ``TemperatureDistribution``,
  ``probability_for_strike`` -- is pure data processing, fully unit-tested offline
  against a sample payload.

Resolution stations
-------------------
A temperature market resolves against ONE specific weather station, so the forecast must
use that station's coordinates. ``STATIONS`` maps Kalshi series tickers to the station we
believe each uses. **Verify these against the market's own rules before trading real
money** -- a wrong station silently poisons every probability. They are kept explicit and
overridable rather than guessed inside the fetch.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

try:  # zoneinfo is stdlib on 3.9+; degrade gracefully if tz data is missing.
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - platform without tzdata
    ZoneInfo = None  # type: ignore

_CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "cache")
)


# ---------------------------------------------------------------------------
# Resolution stations (series ticker -> where the market resolves)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Station:
    name: str
    latitude: float
    longitude: float
    timezone: str  # IANA tz, used to group hourly data into the correct local day


# Kalshi's daily high-temperature series and their (believed) resolution stations.
# Coordinates are the airport/observation site each market cites; confirm in the market
# rules before risking real money -- the station choice is load-bearing.
STATIONS: dict[str, Station] = {
    "KXHIGHNY":   Station("New York City (Central Park)", 40.7789, -73.9692, "America/New_York"),
    "KXHIGHCHI":  Station("Chicago (Midway)",             41.7860, -87.7524, "America/Chicago"),
    "KXHIGHMIA":  Station("Miami (Intl Airport)",         25.7932, -80.2906, "America/New_York"),
    "KXHIGHAUS":  Station("Austin (Camp Mabry)",          30.3210, -97.7600, "America/Chicago"),
    "KXHIGHLAX":  Station("Los Angeles (LAX)",            33.9382, -118.3866, "America/Los_Angeles"),
    "KXHIGHDEN":  Station("Denver (Intl Airport)",        39.8467, -104.6562, "America/Denver"),
    "KXHIGHPHIL": Station("Philadelphia (Intl Airport)",  39.8721, -75.2411, "America/New_York"),
}


def station_for_series(series_ticker: str) -> Optional[Station]:
    """Look up the resolution station for a series ticker (case-insensitive)."""
    if not series_ticker:
        return None
    return STATIONS.get(series_ticker.upper())


def series_from_market_ticker(market_ticker: str) -> str:
    """Kalshi market tickers look like ``KXHIGHNY-25JUL15-B72.5``; the series is the head."""
    return market_ticker.split("-", 1)[0].upper() if market_ticker else ""


# ---------------------------------------------------------------------------
# Distribution math (pure, offline-testable)
# ---------------------------------------------------------------------------
def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


@dataclass
class TemperatureDistribution:
    """A smoothed empirical distribution over a day's high temperature.

    ``samples`` are ensemble-member daily maxima (in the same unit the market uses,
    Fahrenheit for Kalshi). ``bandwidth`` is the Gaussian kernel width used to smooth the
    handful of members into a continuous CDF; it defaults to Silverman's rule of thumb,
    floored so tightly-clustered members still yield non-degenerate band probabilities.

    ``boundary`` (default 0.5) accounts for Kalshi recording the high as a whole degree:
    a band "72 to 73" resolves Yes when the *rounded* high is 72 or 73, i.e. the true
    continuous temperature falls in [71.5, 73.5). Set it to 0 to disable that adjustment.
    """

    samples: list[float]
    bandwidth: float = 0.0
    boundary: float = 0.5
    min_bandwidth: float = 0.75

    def __post_init__(self) -> None:
        self.samples = [float(s) for s in self.samples if s is not None]
        if self.bandwidth <= 0.0:
            self.bandwidth = self._silverman_bandwidth()

    def _silverman_bandwidth(self) -> float:
        n = len(self.samples)
        if n < 2:
            return max(self.min_bandwidth, 3.0)  # one sample: assume broad uncertainty
        sd = statistics.pstdev(self.samples)
        bw = 1.06 * sd * n ** (-0.2)
        return max(bw, self.min_bandwidth)

    @classmethod
    def gaussian(cls, mean: float, sigma: float, **kw) -> "TemperatureDistribution":
        """Deterministic fallback: a single-point forecast plus an assumed spread.

        Use when the ensemble is unavailable and all you have is one model's high. ``sigma``
        is your standing forecast error (~2-4 F a day or two out is typical).
        """
        return cls(samples=[mean], bandwidth=sigma, **kw)

    # -- core CDF ------------------------------------------------------------
    def cdf(self, x: float) -> float:
        if not self.samples:
            return float("nan")
        return sum(_norm_cdf(x, s, self.bandwidth) for s in self.samples) / len(self.samples)

    def mean(self) -> float:
        return statistics.fmean(self.samples) if self.samples else float("nan")

    def stdev(self) -> float:
        return statistics.pstdev(self.samples) if len(self.samples) >= 2 else 0.0

    # -- strike probabilities (band edges treated inclusively, per Kalshi labels) --
    def prob_between(self, lo: float, hi: float) -> float:
        """P(lo <= recorded high <= hi)."""
        p = self.cdf(hi + self.boundary) - self.cdf(lo - self.boundary)
        return min(1.0, max(0.0, p))

    def prob_at_least(self, threshold: float) -> float:
        """P(recorded high >= threshold) -- Kalshi's "X or above"."""
        return min(1.0, max(0.0, 1.0 - self.cdf(threshold - self.boundary)))

    def prob_at_most(self, threshold: float) -> float:
        """P(recorded high <= threshold) -- Kalshi's "X or below"."""
        return min(1.0, max(0.0, self.cdf(threshold + self.boundary)))


def daily_max_per_member(
    hourly: dict,
    target_day: date,
    variable_prefix: str = "temperature_2m",
) -> list[float]:
    """Extract each ensemble member's max temperature for ``target_day``.

    ``hourly`` is Open-Meteo's ``hourly`` object: a ``time`` list plus one array per
    member keyed ``temperature_2m``, ``temperature_2m_member01``, ... Timestamps are
    local when the request passes ``timezone=...`` (we do), so we group by their date.
    Returns one daily-max value per member that had at least one reading that day.
    """
    times = hourly.get("time") or []
    # Indices whose local calendar date matches the target day.
    idx = [i for i, t in enumerate(times) if _iso_day(t) == target_day]
    if not idx:
        return []

    member_keys = [
        k for k in hourly
        if k == variable_prefix or k.startswith(variable_prefix + "_member")
    ]
    maxima: list[float] = []
    for key in member_keys:
        series = hourly.get(key) or []
        vals = [series[i] for i in idx if i < len(series) and series[i] is not None]
        if vals:
            maxima.append(max(vals))
    return maxima


def _iso_day(ts: str) -> Optional[date]:
    """Parse an Open-Meteo timestamp ('2026-07-15T14:00') into its calendar date."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).date()
    except ValueError:
        return None


def distribution_from_ensemble(
    payload: dict,
    target_day: date,
    **dist_kwargs,
) -> Optional[TemperatureDistribution]:
    """Build a ``TemperatureDistribution`` from an Open-Meteo ensemble payload."""
    hourly = payload.get("hourly") or {}
    samples = daily_max_per_member(hourly, target_day)
    if not samples:
        return None
    return TemperatureDistribution(samples=samples, **dist_kwargs)


# ---------------------------------------------------------------------------
# Strike parsing: turn a Kalshi market's strike into a probability
# ---------------------------------------------------------------------------
def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def market_strike(market: dict) -> tuple[str, Optional[float], Optional[float]]:
    """Return ``(strike_type, floor, cap)`` from a Kalshi market object.

    Kalshi encodes weather bands in ``strike_type`` ('greater', 'less', 'between', and the
    ``*_or_equal`` variants) plus numeric ``floor_strike`` / ``cap_strike`` fields.
    """
    st = str(market.get("strike_type") or "").lower()
    return st, _num(market.get("floor_strike")), _num(market.get("cap_strike"))


def probability_for_strike(
    dist: TemperatureDistribution,
    strike_type: str,
    floor: Optional[float],
    cap: Optional[float],
) -> Optional[float]:
    """Map a strike onto the forecast distribution -> P(Yes).

    Band edges are treated inclusively to match Kalshi's "X or above / or below" labels;
    the half-degree rounding correction lives in ``TemperatureDistribution.boundary``.
    Returns None when the strike fields are missing or the type is one we don't model.
    """
    st = (strike_type or "").lower()
    if st in ("greater", "greater_or_equal", "greater_than", "greater_than_or_equal"):
        return dist.prob_at_least(floor) if floor is not None else None
    if st in ("less", "less_or_equal", "less_than", "less_than_or_equal"):
        return dist.prob_at_most(cap) if cap is not None else None
    if st == "between":
        if floor is not None and cap is not None:
            return dist.prob_between(floor, cap)
        if floor is not None:
            return dist.prob_at_least(floor)
        if cap is not None:
            return dist.prob_at_most(cap)
    return None


def strike_label(strike_type: str, floor: Optional[float], cap: Optional[float]) -> str:
    """Human-readable strike, e.g. '>= 72F', '<= 60F', '72-73F'."""
    st = (strike_type or "").lower()
    if st.startswith("greater") and floor is not None:
        return f">= {floor:g}F"
    if st.startswith("less") and cap is not None:
        return f"<= {cap:g}F"
    if st == "between" and floor is not None and cap is not None:
        return f"{floor:g}-{cap:g}F"
    if floor is not None:
        return f">= {floor:g}F"
    if cap is not None:
        return f"<= {cap:g}F"
    return st or "?"


# ---------------------------------------------------------------------------
# Live network client (with disk cache, mirroring theoddsapi.py)
# ---------------------------------------------------------------------------
def _cache_file(lat: float, lon: float, day: date, models: str) -> str:
    key = f"ensemble_{lat:.3f}_{lon:.3f}_{day.isoformat()}_{models}.json"
    return os.path.join(_CACHE_DIR, key)


def _read_cache(path: str, ttl_seconds: float) -> Optional[dict]:
    if ttl_seconds <= 0 or not os.path.exists(path):
        return None
    if time.time() - os.path.getmtime(path) > ttl_seconds:
        return None
    with open(path, "r") as fh:
        return json.load(fh)


def _write_cache(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh)


class OpenMeteoEnsembleClient:
    """Fetches ensemble temperature forecasts from Open-Meteo (free, no API key).

    ``cache_ttl`` seconds: repeated lookups for the same station/day reuse the last
    payload instead of re-hitting the API -- forecasts only refresh a few times a day, so
    a 30-minute default is plenty. ``models`` picks the ensemble system; GEFS (``gfs_seed``
    family) gives ~31 members and good CONUS coverage.
    """

    BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"

    def __init__(
        self,
        models: str = "gfs_seamless",
        cache_ttl: float = 1800.0,
        session=None,
    ):
        self.models = models
        self.cache_ttl = cache_ttl
        self._session = session

    def fetch_ensemble(
        self,
        latitude: float,
        longitude: float,
        target_day: date,
        tz: str = "auto",
    ) -> dict:
        """Return the raw ensemble payload for one station/day (cached)."""
        cache_path = _cache_file(latitude, longitude, target_day, self.models)
        cached = _read_cache(cache_path, self.cache_ttl)
        if cached is not None:
            return cached

        import requests

        session = self._session or requests
        day = target_day.isoformat()
        resp = session.get(
            self.BASE,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "temperature_2m",
                "temperature_unit": "fahrenheit",
                "models": self.models,
                "timezone": tz,
                "start_date": day,
                "end_date": day,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        _write_cache(cache_path, data)
        return data

    def distribution(
        self,
        station: Station,
        target_day: date,
        **dist_kwargs,
    ) -> Optional[TemperatureDistribution]:
        """Fetch and build the high-temperature distribution for a station/day."""
        payload = self.fetch_ensemble(
            station.latitude, station.longitude, target_day, tz=station.timezone
        )
        return distribution_from_ensemble(payload, target_day, **dist_kwargs)


# ---------------------------------------------------------------------------
# The ProbabilitySource the trader consumes
# ---------------------------------------------------------------------------
class WeatherProbabilitySource:
    """A ``ProbabilitySource`` (see strategy.py) driven by Open-Meteo forecasts.

    It holds a precomputed ``{market_ticker: fair_probability}`` map -- the weather bot
    fills this in once per pass after fetching the ensembles -- and hands it to the
    ``PaperTrader`` exactly like the sportsbook source does for sports. Building the map
    is the ``WeatherEdgeFinder``'s job (see weather.py); this class is the seam that lets
    the generic trading loop stay weather-agnostic.
    """

    def __init__(self, probabilities: Optional[dict[str, float]] = None):
        self.probabilities: dict[str, float] = {
            k: float(v) for k, v in (probabilities or {}).items()
        }

    def add(self, ticker: str, probability: float) -> None:
        self.probabilities[ticker] = float(probability)

    def fair_probability(self, market_ticker: str) -> Optional[float]:
        return self.probabilities.get(market_ticker)
