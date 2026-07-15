"""A fair-value model for Kalshi's short-horizon crypto "up/down" markets.

This is the piece that decides whether the bot has an *edge*. Everything else --
sizing, fees, cooldowns -- is bookkeeping around this number. A Kalshi 15-minute
crypto market resolves Yes if the underlying (BTC, ETH, ...) is above a strike ``K`` at
settlement, so its fair Yes-probability is simply::

    P(spot_at_close > K  |  spot_now, minutes_left, volatility)

We model the underlying as geometric Brownian motion with ~zero drift (over 15 minutes
crypto drift is negligible and unpredictable), which gives a lognormal terminal price
and a closed-form probability. That is the entire theory:

    ln(S_T / S) ~ Normal(-0.5 sigma^2 tau, sigma^2 tau)
    P(S_T > K)  = Phi( [ln(S/K) - 0.5 sigma^2 tau] / (sigma * sqrt(tau)) )

**Why this matters for the pair strategy.** Buying ``BTC_UP + ETH_DOWN`` at a combined
price equal to ``P(BTC up) + P(ETH down)`` is zero-EV by construction -- the expected
payoff of the pair *is* that sum, because expectation is linear and correlation only
reshapes the distribution, not its mean. The only way to make money is to buy an
individual leg for less than its true probability. This module produces that true
probability so the existing ``evaluate_market`` can act on real mispricing instead of a
price range.

The hard part is not this formula -- it is feeding it an honest ``sigma``. Garbage vol
in, garbage edge out. Validate with the calibration report in ``kalshi.backtest`` before
trusting a single dollar to it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .backtest import ResolvedMarket

# Crypto trades 24/7/365, so a "year" for annualising volatility is every minute of
# every day. Using calendar time (not trading days) is the correct convention here.
MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0
SECONDS_PER_YEAR = MINUTES_PER_YEAR * 60.0


def _normal_cdf(x: float) -> float:
    """Standard-normal CDF via the error function (stdlib, no numpy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def minutes_to_years(minutes: float) -> float:
    return max(0.0, minutes) / MINUTES_PER_YEAR


def up_probability(
    spot: float,
    strike: float,
    minutes_left: float,
    vol_annual: float,
    drift_annual: float = 0.0,
) -> float:
    """Fair probability that ``spot`` finishes strictly above ``strike`` at expiry.

    ``vol_annual`` is annualised volatility (e.g. 0.6 == 60%/yr). ``drift_annual``
    defaults to 0 because over a 15-minute horizon expected drift is both tiny and
    unforecastable; leave it at 0 unless you have a real reason.

    Degenerate inputs collapse sensibly: at expiry (or zero vol) the answer is 1, 0, or
    0.5 for spot above, below, or exactly at the strike. Non-positive prices return 0.5
    (maximum uncertainty -> no edge) rather than raising, so a bad live tick can never
    manufacture a confident signal.
    """
    if spot <= 0.0 or strike <= 0.0:
        return 0.5

    tau = minutes_to_years(minutes_left)
    if tau <= 0.0 or vol_annual <= 0.0:
        if spot > strike:
            return 1.0
        if spot < strike:
            return 0.0
        return 0.5

    sigma_sqrt_t = vol_annual * math.sqrt(tau)
    d2 = (math.log(spot / strike) + (drift_annual - 0.5 * vol_annual ** 2) * tau) / sigma_sqrt_t
    return _normal_cdf(d2)


def vol_from_typical_move(move_fraction: float, horizon_minutes: float) -> float:
    """Convert an intuitive "typical move" into an annualised volatility.

    If the underlying typically moves about ``move_fraction`` (one standard deviation,
    e.g. 0.003 == 0.3%) over ``horizon_minutes``, the implied annualised sigma is
    ``move_fraction * sqrt(MINUTES_PER_YEAR / horizon_minutes)``. Handy when you have a
    feel for "BTC usually swings a few tenths of a percent in 15 minutes" but not a
    formal vol estimate.
    """
    if horizon_minutes <= 0.0:
        return 0.0
    return move_fraction * math.sqrt(MINUTES_PER_YEAR / horizon_minutes)


def annualized_vol_from_log_returns(log_returns, bar_seconds: float) -> float:
    """Annualise the volatility of a series of per-bar log returns.

    ``log_returns`` is an iterable of ``ln(p_t / p_{t-1})`` sampled every ``bar_seconds``.
    Returns 0.0 if there is not enough data to estimate a standard deviation.
    """
    vals = [float(r) for r in log_returns]
    n = len(vals)
    if n < 2 or bar_seconds <= 0.0:
        return 0.0
    mean = sum(vals) / n
    var = sum((r - mean) ** 2 for r in vals) / (n - 1)  # sample variance
    per_bar_sigma = math.sqrt(var)
    bars_per_year = SECONDS_PER_YEAR / bar_seconds
    return per_bar_sigma * math.sqrt(bars_per_year)


@dataclass
class CryptoMarketState:
    """Everything the model needs to value one crypto leg, right now."""
    asset: str            # "BTC", "ETH", ...
    spot: float           # live underlying price from the exchange feed
    strike: float         # the market's strike (for up/down markets, the open price)
    minutes_left: float   # minutes until settlement
    vol_annual: float     # annualised volatility estimate for this asset

    def up_probability(self, drift_annual: float = 0.0) -> float:
        return up_probability(self.spot, self.strike, self.minutes_left,
                              self.vol_annual, drift_annual)


class CryptoFairValueSource:
    """A ``ProbabilitySource`` (see ``kalshi.strategy``) backed by the fair-value model.

    You supply ``state_fn`` -- a callable that maps a market ticker to a live
    ``CryptoMarketState`` (spot from your exchange feed, strike + minutes-left from the
    Kalshi market, plus your vol estimate). This class turns that into a Yes-probability
    the existing ``evaluate_market`` / ``PaperTrader`` can trade on, keeping the model
    cleanly separable from live-data plumbing. Return ``None`` from ``state_fn`` for
    tickers you cannot value, and no trade is taken.
    """

    def __init__(
        self,
        state_fn: Callable[[str], CryptoMarketState | None],
        drift_annual: float = 0.0,
    ):
        self._state_fn = state_fn
        self._drift_annual = drift_annual

    def fair_probability(self, market_ticker: str) -> float | None:
        state = self._state_fn(market_ticker)
        if state is None:
            return None
        return state.up_probability(self._drift_annual)


@dataclass
class CryptoObservation:
    """A single resolved data point for backtesting the crypto model.

    Captured at the moment you would have traded: the live spot, the strike, time left,
    your vol estimate, the market's Yes ask, and how the market actually resolved.
    """
    ticker: str
    spot: float
    strike: float
    minutes_left: float
    vol_annual: float
    yes_price: float
    outcome: int          # 1 if it settled Yes (above strike), else 0


def resolved_markets_from_observations(
    observations: list[CryptoObservation],
    drift_annual: float = 0.0,
) -> list[ResolvedMarket]:
    """Turn raw crypto observations into ``ResolvedMarket`` records for the backtester.

    This is the bridge: it runs each observation through ``up_probability`` to produce
    the model's fair estimate, then hands the result to ``run_value_backtest`` -- which
    reports calibration and realized-vs-predicted edge so you can see whether the model
    is actually skilled before risking money.
    """
    return [
        ResolvedMarket(
            ticker=o.ticker,
            yes_price=o.yes_price,
            fair_prob=up_probability(o.spot, o.strike, o.minutes_left, o.vol_annual, drift_annual),
            outcome=o.outcome,
        )
        for o in observations
    ]
