"""Offline tests for the weather probability source.

No network: these exercise the pure forecast->probability math, the Kalshi market-bounds
parsing, the ticker/date helpers, and the ProbabilitySource end to end against a fake
NWS payload. This is the module's edge, so like the odds math it must be exactly right.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kalshi.sources.weather import (
    Forecast,
    WeatherProbabilitySource,
    event_date_of,
    fair_probability_for_market,
    high_from_periods,
    market_bounds,
    normal_cdf,
    probability_between,
    series_of,
    sigma_for_lead,
)


def test_normal_cdf_matches_known_values():
    assert abs(normal_cdf(0.0) - 0.5) < 1e-9
    # ~68% of mass within one standard deviation
    assert abs((normal_cdf(1.0) - normal_cdf(-1.0)) - 0.6827) < 1e-3


def test_probability_between_is_symmetric_and_bounded():
    # A window centred on the mean; open ends return the full tails.
    p = probability_between(mu=90.0, sigma=3.0, lo=87.0, hi=93.0)
    assert abs(p - 0.6827) < 1e-3
    assert probability_between(90.0, 3.0, None, None) == 1.0
    assert abs(probability_between(90.0, 3.0, 90.0, None) - 0.5) < 1e-9  # upper tail
    assert abs(probability_between(90.0, 3.0, None, 90.0) - 0.5) < 1e-9  # lower tail


def test_probabilities_across_a_full_ladder_sum_to_one():
    mu, sigma = 90.0, 3.0
    below = probability_between(mu, sigma, None, 87.5)
    mid = probability_between(mu, sigma, 87.5, 92.5)
    above = probability_between(mu, sigma, 92.5, None)
    assert abs((below + mid + above) - 1.0) < 1e-9


def test_sigma_widens_with_lead_and_never_below_base():
    assert sigma_for_lead(0) < sigma_for_lead(3)
    assert sigma_for_lead(-5) == sigma_for_lead(0)  # clamped at same-day


def test_market_bounds_from_structured_strikes():
    assert market_bounds({"floor_strike": 88, "cap_strike": 89,
                          "strike_type": "between"}) == (88.0, 89.0)
    assert market_bounds({"floor_strike": 90, "strike_type": "greater"}) == (90.0, None)
    assert market_bounds({"cap_strike": 83, "strike_type": "less"}) == (None, 83.0)


def test_market_bounds_falls_back_to_subtitle():
    assert market_bounds({"subtitle": "88° to 89°"}) == (88.0, 89.0)
    assert market_bounds({"subtitle": "90° or above"}) == (90.0, None)
    assert market_bounds({"subtitle": "83° or below"}) == (None, 83.0)
    assert market_bounds({"subtitle": "nonsense"}) is None
    assert market_bounds({}) is None


def test_fair_probability_for_market_pads_the_bucket():
    fc = Forecast(high_f=90.0, sigma_f=3.0)
    # "89° to 90°" bracket -> continuous [88.5, 90.5] after half-degree padding.
    market = {"floor_strike": 89, "cap_strike": 90, "strike_type": "between"}
    p = fair_probability_for_market(market, fc, bucket_pad=0.5)
    expected = probability_between(90.0, 3.0, 88.5, 90.5)
    assert abs(p - expected) < 1e-9
    assert 0.0 < p < 1.0


def test_fair_probability_none_when_unparseable():
    assert fair_probability_for_market({"subtitle": "???"}, Forecast(90.0, 3.0)) is None


def test_series_and_date_helpers():
    assert series_of("KXHIGHNY-25JUL15-B88") == "KXHIGHNY"
    assert event_date_of("KXHIGHNY-25JUL15-B88") == date(2025, 7, 15)
    assert event_date_of("KXHIGHNY") is None
    assert event_date_of("KXHIGHNY-BADDATE-B88") is None


def test_source_builds_per_ticker_probabilities():
    markets = [
        {"ticker": "KXHIGHNY-25JUL15-B85", "floor_strike": 85, "cap_strike": 86,
         "strike_type": "between"},
        {"ticker": "KXHIGHNY-25JUL15-B89", "floor_strike": 89, "cap_strike": 90,
         "strike_type": "between"},
        {"ticker": "KXHIGHLAX-25JUL15-B70", "floor_strike": 70, "cap_strike": 71,
         "strike_type": "between"},   # no forecast for LAX -> omitted
        {"ticker": "KXHIGHNY-25JUL15-BAD", "subtitle": "???"},  # unparseable -> omitted
    ]
    forecasts = {"KXHIGHNY": Forecast(high_f=90.0, sigma_f=3.0)}
    source = WeatherProbabilitySource.build(markets, forecasts)

    # The bracket around the forecast high is the most probable one.
    assert source.fair_probability("KXHIGHNY-25JUL15-B89") > \
        source.fair_probability("KXHIGHNY-25JUL15-B85")
    assert source.fair_probability("KXHIGHLAX-25JUL15-B70") is None  # no forecast
    assert source.fair_probability("KXHIGHNY-25JUL15-BAD") is None   # unparseable
    assert source.fair_probability("UNKNOWN") is None


def test_high_from_periods_picks_the_target_daytime_high():
    periods = [
        {"isDaytime": False, "startTime": "2025-07-15T06:00:00-04:00", "temperature": 74},
        {"isDaytime": True, "startTime": "2025-07-15T06:00:00-04:00", "temperature": 91},
        {"isDaytime": True, "startTime": "2025-07-16T06:00:00-04:00", "temperature": 88},
    ]
    assert high_from_periods(periods, date(2025, 7, 15)) == 91.0
    assert high_from_periods(periods, date(2025, 7, 16)) == 88.0
    assert high_from_periods(periods, date(2025, 7, 20)) is None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
