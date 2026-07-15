"""Offline tests for the Kalshi weather bot (the Open-Meteo edge).

No network. Verifies the forecast-distribution math, the strike -> probability mapping,
and that the edge finder ranks mispriced weather markets correctly against a canned
ensemble payload.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.economics import SizingConfig
from src.kalshi.sources.openmeteo import (
    STATIONS,
    TemperatureDistribution,
    WeatherProbabilitySource,
    daily_max_per_member,
    distribution_from_ensemble,
    market_strike,
    probability_for_strike,
    series_from_market_ticker,
    station_for_series,
    strike_label,
)
from src.kalshi.weather import (
    market_ask_prices,
    rank_opportunities,
    target_day_for_market,
)


# ---------------------------------------------------------------------------
# Sample ensemble payload: three members peaking at 88 / 90 / 92 F on Jul 15,
# plus a Jul 16 hour that must be ignored when we ask for Jul 15.
# ---------------------------------------------------------------------------
def _hourly():
    times, ctrl, m1, m2 = [], [], [], []
    # Jul 15: a diurnal curve so the max is mid-afternoon.
    for h in range(24):
        times.append(f"2026-07-15T{h:02d}:00")
        bump = -abs(h - 15) * 1.5  # peak at hour 15
        ctrl.append(90.0 + bump)
        m1.append(92.0 + bump)
        m2.append(88.0 + bump)
    # A Jul 16 reading that is hotter -- must not leak into the Jul 15 max.
    times.append("2026-07-16T15:00")
    ctrl.append(120.0)
    m1.append(120.0)
    m2.append(120.0)
    return {
        "time": times,
        "temperature_2m": ctrl,
        "temperature_2m_member01": m1,
        "temperature_2m_member02": m2,
    }


SAMPLE_PAYLOAD = {"latitude": 40.78, "longitude": -73.97, "hourly": _hourly()}
JUL15 = date(2026, 7, 15)


def test_daily_max_per_member_groups_by_local_day():
    maxima = daily_max_per_member(SAMPLE_PAYLOAD["hourly"], JUL15)
    assert sorted(maxima) == [88.0, 90.0, 92.0]  # the 120 F on Jul 16 is excluded


def test_distribution_mean_and_spread():
    dist = distribution_from_ensemble(SAMPLE_PAYLOAD, JUL15)
    assert dist is not None
    assert abs(dist.mean() - 90.0) < 1e-9    # (88+90+92)/3
    assert dist.stdev() > 0.0


def test_cdf_is_monotonic_and_bounded():
    dist = TemperatureDistribution(samples=[88.0, 90.0, 92.0])
    lo, mid, hi = dist.cdf(80.0), dist.cdf(90.0), dist.cdf(100.0)
    assert 0.0 <= lo < mid < hi <= 1.0
    assert lo < 0.05 and hi > 0.95
    assert abs(mid - 0.5) < 0.05  # symmetric samples -> median near the mean


def test_prob_between_at_least_at_most_are_consistent():
    dist = TemperatureDistribution(samples=[88.0, 90.0, 92.0])
    # A band around the center should carry real mass.
    assert dist.prob_between(89.0, 91.0) > 0.15
    # at_least + at_most straddling a point exceed 1 by the boundary overlap, both valid probs.
    assert 0.0 <= dist.prob_at_least(90.0) <= 1.0
    assert 0.0 <= dist.prob_at_most(90.0) <= 1.0
    # Far tails behave.
    assert dist.prob_at_least(120.0) < 0.01
    assert dist.prob_at_most(60.0) < 0.01


def test_gaussian_fallback():
    dist = TemperatureDistribution.gaussian(mean=75.0, sigma=3.0)
    assert abs(dist.cdf(75.0) - 0.5) < 1e-9
    assert dist.prob_at_least(75.0) > 0.4


def test_probability_for_strike_greater_less_between():
    dist = TemperatureDistribution(samples=[88.0, 90.0, 92.0])
    p_ge = probability_for_strike(dist, "greater", floor=85.0, cap=None)
    p_le = probability_for_strike(dist, "less", floor=None, cap=85.0)
    p_bt = probability_for_strike(dist, "between", floor=89.0, cap=91.0)
    assert p_ge > 0.9            # 85+ is very likely given a ~90 F center
    assert p_le < 0.1            # <=85 is unlikely
    assert 0.0 < p_bt < 1.0
    # Unknown / missing strikes return None rather than guessing.
    assert probability_for_strike(dist, "structured", None, None) is None
    assert probability_for_strike(dist, "greater", floor=None, cap=None) is None


def test_market_strike_and_labels():
    m = {"strike_type": "between", "floor_strike": 72, "cap_strike": 73}
    assert market_strike(m) == ("between", 72.0, 73.0)
    assert strike_label("between", 72.0, 73.0) == "72-73F"
    assert strike_label("greater", 80.0, None) == ">= 80F"
    assert strike_label("less", None, 60.0) == "<= 60F"


def test_series_and_station_lookup():
    assert series_from_market_ticker("KXHIGHNY-25JUL15-B72.5") == "KXHIGHNY"
    assert station_for_series("kxhighny") is STATIONS["KXHIGHNY"]
    assert station_for_series("NOPE") is None


def test_market_ask_prices_derives_no_from_yes_bid():
    yes, no = market_ask_prices({"yes_ask": 40, "yes_bid": 38})
    assert abs(yes - 0.40) < 1e-9 and abs(no - 0.62) < 1e-9  # no_ask = 100 - 38


def test_target_day_uses_station_timezone():
    station = STATIONS["KXHIGHNY"]
    # 02:00 UTC on Jul 16 is still Jul 15 in New York.
    m = {"close_time": "2026-07-16T02:00:00Z"}
    assert target_day_for_market(m, station) == JUL15


# ---------------------------------------------------------------------------
# End-to-end ranking against a fixed distribution.
# ---------------------------------------------------------------------------
def _dist():
    # Center ~90 F, tight spread.
    return TemperatureDistribution(samples=[89.0, 90.0, 91.0])


def test_rank_opportunities_finds_and_orders_edges():
    dist = _dist()
    markets = [
        # Forecast says 88-90 band is very likely (~mass around 90); market prices Yes cheap.
        {"ticker": "KXHIGHNY-25JUL15-A", "strike_type": "between",
         "floor_strike": 88, "cap_strike": 92, "yes_ask": 40, "yes_bid": 38},
        # A hot tail the market overprices: 95+ is nearly impossible, but Yes asks 30c ->
        # buying No is the edge.
        {"ticker": "KXHIGHNY-25JUL15-B", "strike_type": "greater",
         "floor_strike": 95, "cap_strike": None, "yes_ask": 30, "yes_bid": 28},
        # Fairly priced: no edge either way.
        {"ticker": "KXHIGHNY-25JUL15-C", "strike_type": "greater",
         "floor_strike": 90, "cap_strike": None, "yes_ask": 50, "yes_bid": 48},
    ]

    opps = rank_opportunities(
        markets, lambda m: dist, bankroll=1000.0,
        sizing=SizingConfig(min_edge=0.01),
    )
    by_ticker = {o.ticker: o for o in opps}

    # A: Yes is underpriced -> buy Yes with a big edge.
    assert "KXHIGHNY-25JUL15-A" in by_ticker
    assert by_ticker["KXHIGHNY-25JUL15-A"].side == "yes"
    assert by_ticker["KXHIGHNY-25JUL15-A"].contracts > 0

    # B: 95+ is ~impossible so Yes @30c is overpriced -> buy No.
    assert "KXHIGHNY-25JUL15-B" in by_ticker
    assert by_ticker["KXHIGHNY-25JUL15-B"].side == "no"

    # Ranked fattest-edge first.
    assert [o.edge for o in opps] == sorted((o.edge for o in opps), reverse=True)
    # Every listed opportunity has a positive edge.
    assert all(o.edge > 0 for o in opps)


def test_rank_skips_markets_without_a_distribution():
    markets = [{"ticker": "X-1", "strike_type": "between",
                "floor_strike": 88, "cap_strike": 92, "yes_ask": 40}]
    assert rank_opportunities(markets, lambda m: None, bankroll=1000.0) == []


def test_weather_probability_source_feeds_trader_interface():
    src = WeatherProbabilitySource({"KXHIGHNY-25JUL15-A": 0.8})
    src.add("KXHIGHNY-25JUL15-B", 0.2)
    assert src.fair_probability("KXHIGHNY-25JUL15-A") == 0.8
    assert src.fair_probability("KXHIGHNY-25JUL15-B") == 0.2
    assert src.fair_probability("UNKNOWN") is None


def test_ensemble_cache_read_write_and_ttl(tmp_path):
    from src.kalshi.sources.openmeteo import _read_cache, _write_cache

    path = str(tmp_path / "ens.json")
    assert _read_cache(path, ttl_seconds=300) is None
    _write_cache(path, SAMPLE_PAYLOAD)
    fresh = _read_cache(path, ttl_seconds=300)
    assert fresh is not None and "hourly" in fresh
    assert _read_cache(path, ttl_seconds=0) is None  # expired TTL forces a miss


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
