"""Shared helper for turning entry/exit *events* into a held long/flat position.

Several strategies (mean reversion, Donchian trend following) are naturally expressed
as "enter when X happens, stay in until Y happens". That is a stateful process: the
position at bar ``i`` depends on whether an entry has fired since the last exit, not
just on the current bar in isolation.

This helper walks the two boolean event series once and produces the held position,
using **only** information available at each bar's close (no lookahead). The backtest
engine still applies its own one-bar execution delay on top of this, so a signal
computed here at bar ``i`` is not filled until bar ``i+1``'s open.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def hold_position(
    enter: pd.Series,
    exit_: pd.Series,
    warmup: int = 0,
) -> pd.Series:
    """Build a {0,1} held-position series from entry and exit event masks.

    Rules, evaluated bar by bar:
      * If flat and ``enter`` is true -> become long (hold 1 from this bar's close).
      * If long and ``exit_`` is true -> become flat (hold 0 from this bar's close).
      * Otherwise carry the previous state forward.

    ``warmup`` forces the position flat for the first ``warmup`` bars so indicators
    have time to populate and we never trade on half-formed windows.
    """
    enter_arr = enter.to_numpy(dtype=bool)
    exit_arr = exit_.to_numpy(dtype=bool)
    n = len(enter_arr)
    pos = np.zeros(n, dtype=float)

    in_pos = False
    for i in range(n):
        if i < warmup:
            in_pos = False
        elif in_pos:
            if exit_arr[i]:
                in_pos = False
        else:
            if enter_arr[i]:
                in_pos = True
        pos[i] = 1.0 if in_pos else 0.0

    return pd.Series(pos, index=enter.index, name="position")
