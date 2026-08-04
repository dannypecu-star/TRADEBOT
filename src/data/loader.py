"""Market data loading.

Two sources:
  * ``fetch_ohlcv``   -- real candles from any ccxt-supported exchange. Uses only
                         PUBLIC endpoints, so no API key or account is required.
  * ``synthetic_ohlcv`` -- geometric-Brownian-motion candles for offline testing
                         and for sanity-checking the backtester without a network.

All data is returned as a pandas DataFrame indexed by UTC timestamp with the
columns [open, high, low, close, volume].
"""
from __future__ import annotations

import os
import time
from typing import Optional

import numpy as np
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "cache")

_TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def _cache_path(exchange: str, symbol: str, timeframe: str) -> str:
    safe = f"{exchange}_{symbol.replace('/', '-')}_{timeframe}.parquet"
    return os.path.abspath(os.path.join(CACHE_DIR, safe))


def fetch_ohlcv(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    exchange: str = "binance",
    limit: int = 3000,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch recent OHLCV candles from a public ccxt endpoint.

    Pages backwards until ``limit`` candles are collected. Results are cached to
    parquet so repeated backtests don't re-hit the exchange.
    """
    import ccxt  # imported lazily so offline/synthetic use needs no ccxt

    cache = _cache_path(exchange, symbol, timeframe)
    if use_cache and os.path.exists(cache):
        cached = pd.read_parquet(cache)
        if len(cached) >= limit:
            return cached.tail(limit)

    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    tf_ms = _TIMEFRAME_MS[timeframe]
    now = ex.milliseconds()
    since = now - tf_ms * limit

    rows: list[list] = []
    while since < now and len(rows) < limit:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + tf_ms
        time.sleep(ex.rateLimit / 1000)

    df = _to_frame(rows)
    if use_cache and not df.empty:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_parquet(cache)
    return df.tail(limit)


def synthetic_ohlcv(
    n: int = 3000,
    timeframe: str = "1h",
    start_price: float = 30_000.0,
    annual_vol: float = 0.8,
    annual_drift: float = 0.3,
    seed: Optional[int] = 42,
) -> pd.DataFrame:
    """Generate deterministic synthetic candles via geometric Brownian motion.

    Useful for offline development and unit tests. The drift/vol defaults roughly
    resemble a trending crypto asset so trend strategies have something to bite on.
    """
    rng = np.random.default_rng(seed)
    tf_ms = _TIMEFRAME_MS[timeframe]
    bars_per_year = (365 * 24 * 3600 * 1000) / tf_ms
    dt = 1.0 / bars_per_year

    mu, sigma = annual_drift, annual_vol
    shocks = rng.standard_normal(n)
    log_returns = (mu - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * shocks
    close = start_price * np.exp(np.cumsum(log_returns))

    open_ = np.empty(n)
    open_[0] = start_price
    open_[1:] = close[:-1]
    # Build plausible intrabar highs/lows around the open->close move.
    wick = np.abs(rng.standard_normal(n)) * sigma * np.sqrt(dt) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    volume = rng.uniform(10, 100, n)

    end = pd.Timestamp.now("UTC").floor("h")
    index = pd.date_range(end=end, periods=n, freq=pd.Timedelta(milliseconds=tf_ms))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def _ohlc_from_close(
    close: np.ndarray, rng: np.random.Generator, timeframe: str, wick_frac: float = 0.003
) -> pd.DataFrame:
    """Wrap a close-price path in plausible open/high/low/volume candles."""
    n = len(close)
    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1]
    wick = np.abs(rng.standard_normal(n)) * wick_frac * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    volume = rng.uniform(10, 100, n)
    tf_ms = _TIMEFRAME_MS[timeframe]
    end = pd.Timestamp.now("UTC").floor("h")
    index = pd.date_range(end=end, periods=n, freq=pd.Timedelta(milliseconds=tf_ms))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def regime_ohlcv(
    kind: str,
    n: int = 3000,
    timeframe: str = "1h",
    start_price: float = 30_000.0,
    seed: int | None = 42,
) -> pd.DataFrame:
    """Synthetic candles that deliberately CONTAIN the structure a strategy targets.

    Pure geometric Brownian motion is a random walk with drift: it has no serial structure,
    so by construction there is nothing for a trend or reversion rule to exploit and
    buy-and-hold is optimal. That is the honest reason strategies do not beat B&H on GBM.

    Real markets are not pure random walks -- they exhibit momentum (trends persist) and,
    at other times, mean reversion (ranges). This generator produces those regimes so a
    strategy can be shown to work *when its assumption holds*. It is idealized, not a claim
    that real markets are this clean.

    * ``kind="trend"``  -- a persistent-momentum path (AR(1) drift). Trend strategies should
      profit here; mean reversion should not.
    * ``kind="meanrevert"`` -- an Ornstein-Uhlenbeck path in log-price that oscillates around
      a slowly drifting level. Mean reversion should profit; trend strategies should not.
    """
    rng = np.random.default_rng(seed)
    if kind == "trend":
        # AR(1) drift: today's drift is mostly yesterday's -> trends persist for a while,
        # then reverse, giving trend followers rides to catch and chop to avoid.
        drift = np.zeros(n)
        for i in range(1, n):
            drift[i] = 0.95 * drift[i - 1] + rng.normal(0, 0.0008)
        noise = rng.normal(0, 0.008, n)
        log_close = np.cumsum(drift + noise)
        close = start_price * np.exp(log_close)
    elif kind == "meanrevert":
        # OU in log-price around a slowly wandering mean: price overshoots and snaps back.
        log_mu = np.cumsum(rng.normal(0, 0.0008, n))   # slow drift of the "fair value"
        logp = np.zeros(n)
        logp[0] = 0.0
        kappa = 0.05                                   # reversion speed toward log_mu
        for i in range(1, n):
            logp[i] = logp[i - 1] + kappa * (log_mu[i] - logp[i - 1]) + rng.normal(0, 0.02)
        close = start_price * np.exp(logp)
    else:
        raise ValueError("kind must be 'trend' or 'meanrevert'")
    return _ohlc_from_close(np.asarray(close, dtype=float), rng, timeframe)


def _to_frame(rows: list[list]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="ts").sort_values("ts")
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.index.name = "timestamp"
    return df[["open", "high", "low", "close", "volume"]]
