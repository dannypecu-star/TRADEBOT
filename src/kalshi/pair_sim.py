"""Deterministic synthetic feed for the BTC/ETH inverse-pair strategy.

The real bot polls ``api.elections.kalshi.com`` for live 15-minute market snapshots and
underlying spot prices. There is no live feed offline, so this module *replays* a
plausible one: BTC and ETH spot follow correlated random walks, and each 15m up/down
market's odds are derived from where spot sits relative to the session's opening strike
with time decay. Everything is seeded, so a given ``seed`` always produces the same
stream -- which is what lets the strategy be exercised reproducibly and asserted on in
tests.

This is a *model*, not market truth. It exists to make the strategy runnable and to show
its mechanics (entries in-band, the SYNC gate, shot scaling, stop-outs, resolution), not
to claim any real-world edge.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .pair_strategy import MarketSnapshot


@dataclass
class Tick:
    now_ms: int
    btc: MarketSnapshot
    eth: MarketSnapshot
    btc_spot: float
    eth_spot: float
    btc_open: float
    eth_open: float


@dataclass
class SimConfig:
    sessions: int = 8
    seed: int = 7
    tick_ms: int = 1000            # one snapshot per simulated second
    session_minutes: int = 15
    btc_start: float = 60000.0
    eth_start: float = 3000.0
    sigma_15m: float = 0.006       # per-session stdev of the common log-return
    corr: float = 0.55             # BTC/ETH common-factor weight (rest is idiosyncratic)
    idio_scale: float = 1.4        # amplify idiosyncratic divergence so odds diverge
    spread: float = 0.02           # ask spread added to each side (up + down > 1)


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _odds(move: float, mins_left: float, session_minutes: int, sigma_15m: float,
          spread: float) -> tuple[float, float]:
    """Up/down asks from the current log-move vs strike and time remaining."""
    frac = max(mins_left / session_minutes, 1e-4)
    remaining_vol = max(sigma_15m * math.sqrt(frac), 1e-6)
    up_prob = _phi(move / remaining_vol)
    up = up_prob + spread / 2.0
    down = (1.0 - up_prob) + spread / 2.0
    up = min(max(up, 0.01), 0.99)
    down = min(max(down, 0.01), 0.99)
    return round(up, 2), round(down, 2)


def simulate(sim: SimConfig | None = None):
    """Yield :class:`Tick`s across ``sim.sessions`` back-to-back 15m markets."""
    sim = sim or SimConfig()
    rng = random.Random(sim.seed)

    btc_spot = sim.btc_start
    eth_spot = sim.eth_start
    now_ms = 0
    ticks_per_session = sim.session_minutes * 60 * 1000 // sim.tick_ms
    per_tick_sigma = sim.sigma_15m / math.sqrt(ticks_per_session)
    w_common = math.sqrt(sim.corr)
    w_idio = math.sqrt(max(1.0 - sim.corr, 0.0)) * sim.idio_scale

    base_quarter = 1_000_000
    for s in range(sim.sessions):
        btc_open = btc_spot
        eth_open = eth_spot
        quarter_index = base_quarter + s
        btc_ticker = f"KXBTC15M-S{s:03d}"
        eth_ticker = f"KXETH15M-S{s:03d}"

        for i in range(ticks_per_session):
            elapsed_min = (i * sim.tick_ms) / 60000.0
            mins_left = sim.session_minutes - elapsed_min

            z_common = rng.gauss(0.0, 1.0)
            btc_shock = per_tick_sigma * (w_common * z_common + w_idio * rng.gauss(0, 1))
            eth_shock = per_tick_sigma * (w_common * z_common + w_idio * rng.gauss(0, 1))
            btc_spot *= math.exp(btc_shock)
            eth_spot *= math.exp(eth_shock)

            btc_up, btc_down = _odds(
                math.log(btc_spot / btc_open), mins_left,
                sim.session_minutes, sim.sigma_15m, sim.spread,
            )
            eth_up, eth_down = _odds(
                math.log(eth_spot / eth_open), mins_left,
                sim.session_minutes, sim.sigma_15m, sim.spread,
            )

            yield Tick(
                now_ms=now_ms,
                btc=MarketSnapshot(btc_ticker, btc_up, btc_down, mins_left, quarter_index),
                eth=MarketSnapshot(eth_ticker, eth_up, eth_down, mins_left, quarter_index),
                btc_spot=btc_spot,
                eth_spot=eth_spot,
                btc_open=btc_open,
                eth_open=eth_open,
            )
            now_ms += sim.tick_ms

    # One extra tick on a fresh market so the final held pair rolls to resolution.
    quarter_index = base_quarter + sim.sessions
    yield Tick(
        now_ms=now_ms,
        btc=MarketSnapshot(f"KXBTC15M-S{sim.sessions:03d}", 0.50, 0.50,
                           sim.session_minutes, quarter_index),
        eth=MarketSnapshot(f"KXETH15M-S{sim.sessions:03d}", 0.50, 0.50,
                           sim.session_minutes, quarter_index),
        btc_spot=btc_spot,
        eth_spot=eth_spot,
        btc_open=btc_spot,
        eth_open=eth_spot,
    )
