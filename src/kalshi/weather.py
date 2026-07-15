"""Hunt the best edges in Kalshi's weather markets using Open-Meteo forecasts.

This is the piece that turns a forecast distribution into a *ranked shopping list*. For
every open weather market it:

  1. finds the resolution station and target day,
  2. builds the day's high-temperature distribution from the Open-Meteo ensemble,
  3. reads the market's strike and computes a calibrated P(Yes),
  4. compares that to the market's Yes/No asks to get the net edge after fees,
  5. sizes the trade with fractional Kelly.

It then sorts every market by net edge so the fattest mispricings float to the top -- the
"best edges" the bot exists to find. The scoring core (``rank_opportunities``) is a pure
function that takes a distribution-lookup callable, so it is fully unit-testable offline;
``WeatherEdgeFinder`` wires it to the real Kalshi + Open-Meteo clients.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Optional

from .economics import SizingConfig, contracts_to_buy, edge
from .sources.openmeteo import (
    STATIONS,
    OpenMeteoEnsembleClient,
    Station,
    TemperatureDistribution,
    market_strike,
    probability_for_strike,
    series_from_market_ticker,
    strike_label,
)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


@dataclass
class WeatherOpportunity:
    """One ranked, actionable (or near-actionable) weather market."""

    ticker: str
    station: str
    target_day: date
    strike: str
    fair_prob: float
    market_price: float   # the ask you'd pay on the chosen side (dollars)
    side: str             # "yes" or "no"
    edge: float           # net $ edge per contract after entry fee
    contracts: int        # fractional-Kelly size (0 if below the min-edge threshold)
    forecast_mean: float
    forecast_std: float

    def describe(self) -> str:
        return (
            f"{self.ticker:<26} {self.station:<28} {self.strike:>10}  "
            f"fair={self.fair_prob:5.1%}  {self.side.upper():3} @ {self.market_price:4.2f}  "
            f"edge=${self.edge:+.3f}  x{self.contracts}  "
            f"(fc {self.forecast_mean:.1f}+/-{self.forecast_std:.1f}F)"
        )


def _cents(x) -> Optional[int]:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def market_ask_prices(market: dict) -> tuple[Optional[float], Optional[float]]:
    """Return ``(yes_ask, no_ask)`` in dollars. Mirrors PaperTrader's price reading.

    You buy Yes at the yes ask and No at the no ask. When the no ask is missing it is
    derived from the yes bid (no_ask = 100 - yes_bid), so the bid/ask spread is honored.
    """
    yes_ask = _cents(market.get("yes_ask"))
    no_ask = _cents(market.get("no_ask"))
    if no_ask is None:
        yes_bid = _cents(market.get("yes_bid"))
        no_ask = (100 - yes_bid) if yes_bid is not None else None
    yes_price = yes_ask / 100.0 if yes_ask else None
    no_price = no_ask / 100.0 if no_ask else None
    return yes_price, no_price


def rank_opportunities(
    markets: list[dict],
    dist_for: Callable[[dict], Optional[TemperatureDistribution]],
    bankroll: float,
    sizing: Optional[SizingConfig] = None,
    fee_rate: float = 0.07,
    min_edge: float = 0.0,
) -> list[WeatherOpportunity]:
    """Score and rank markets by net edge. Pure: ``dist_for`` supplies the forecast.

    Considers both sides of each market and keeps the better-edged one. Only markets whose
    best net edge exceeds ``min_edge`` (default 0 -> any positive edge) are returned, sorted
    fattest-edge first. ``contracts`` uses the sizing config's own ``min_edge`` gate, so a
    listed opportunity can still size to 0 if it is too thin to actually trade.
    """
    sizing = sizing or SizingConfig()
    out: list[WeatherOpportunity] = []

    for m in markets:
        ticker = m.get("ticker")
        if not ticker:
            continue
        dist = dist_for(m)
        if dist is None:
            continue

        strike_type, floor, cap = market_strike(m)
        fair = probability_for_strike(dist, strike_type, floor, cap)
        if fair is None:
            continue

        yes_price, no_price = market_ask_prices(m)
        if yes_price is None or no_price is None:
            continue

        yes_edge = edge(fair, yes_price, fee_rate)
        no_edge = edge(1.0 - fair, no_price, fee_rate)
        if yes_edge >= no_edge:
            side, price, best_edge, side_prob = "yes", yes_price, yes_edge, fair
        else:
            side, price, best_edge, side_prob = "no", no_price, no_edge, 1.0 - fair

        if best_edge <= min_edge:
            continue

        contracts = contracts_to_buy(bankroll, side_prob, price, sizing, fee_rate)
        out.append(WeatherOpportunity(
            ticker=ticker,
            station=str(m.get("_station") or ""),
            target_day=m.get("_target_day"),
            strike=strike_label(strike_type, floor, cap),
            fair_prob=fair,
            market_price=price,
            side=side,
            edge=best_edge,
            contracts=contracts,
            forecast_mean=dist.mean(),
            forecast_std=dist.stdev(),
        ))

    out.sort(key=lambda o: o.edge, reverse=True)
    return out


def target_day_for_market(market: dict, station: Station) -> Optional[date]:
    """The local calendar day a market resolves on, from its close/expiration time."""
    raw = market.get("close_time") or market.get("expiration_time") or market.get("expected_expiration_time")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if ZoneInfo is not None:
        try:
            dt = dt.astimezone(ZoneInfo(station.timezone))
        except Exception:  # pragma: no cover - missing tzdata
            pass
    return dt.date()


class WeatherEdgeFinder:
    """Wires the pure scorer to live Kalshi + Open-Meteo clients."""

    def __init__(
        self,
        kalshi_client,
        ensemble_client: Optional[OpenMeteoEnsembleClient] = None,
        stations: Optional[dict[str, Station]] = None,
        sizing: Optional[SizingConfig] = None,
        fee_rate: float = 0.07,
        dist_kwargs: Optional[dict] = None,
    ):
        self.kalshi = kalshi_client
        self.meteo = ensemble_client or OpenMeteoEnsembleClient()
        self.stations = stations or STATIONS
        self.sizing = sizing or SizingConfig()
        self.fee_rate = fee_rate
        self.dist_kwargs = dist_kwargs or {}
        self._dist_cache: dict[tuple[str, date], Optional[TemperatureDistribution]] = {}

    # -- distribution lookup, memoized per (series, day) ----------------------
    def _distribution_for(self, market: dict) -> Optional[TemperatureDistribution]:
        ticker = market.get("ticker", "")
        series = market.get("series_ticker") or series_from_market_ticker(ticker)
        station = self.stations.get(str(series).upper())
        if station is None:
            return None
        day = target_day_for_market(market, station)
        if day is None:
            return None

        # Annotate for the ranker's human-readable output.
        market["_station"] = station.name
        market["_target_day"] = day

        cache_key = (str(series).upper(), day)
        if cache_key not in self._dist_cache:
            try:
                self._dist_cache[cache_key] = self.meteo.distribution(
                    station, day, **self.dist_kwargs
                )
            except Exception:  # noqa: BLE001 - one bad city must not sink the whole scan
                self._dist_cache[cache_key] = None
        return self._dist_cache[cache_key]

    def _collect_markets(self, series_tickers: list[str], status: str, limit: int) -> list[dict]:
        markets: list[dict] = []
        for series in series_tickers:
            resp = self.kalshi.get_markets(
                series_ticker=series, status=status, limit=limit
            )
            for m in resp.get("markets", []):
                m.setdefault("series_ticker", series)
                markets.append(m)
        return markets

    def find(
        self,
        bankroll: float,
        series_tickers: Optional[list[str]] = None,
        status: str = "open",
        limit: int = 1000,
        min_edge: float = 0.0,
    ) -> list[WeatherOpportunity]:
        """Fetch weather markets for the given series and rank them by net edge."""
        series_tickers = series_tickers or list(self.stations.keys())
        self._dist_cache.clear()
        markets = self._collect_markets(series_tickers, status, limit)
        return rank_opportunities(
            markets, self._distribution_for, bankroll,
            self.sizing, self.fee_rate, min_edge=min_edge,
        )
