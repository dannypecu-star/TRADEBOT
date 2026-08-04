"""Platform adapters -- translate a strategy's signal into what each venue expects.

The strategies in this project produce a single, platform-neutral decision per bar:
a target position in [0, 1] (long or flat). Getting that decision onto a real venue looks
different everywhere:

  * **MetaTrader 4/5** runs code *inside* the terminal in MQL. The Python side can either
    drive an MT5 terminal via the official ``MetaTrader5`` package (Windows) or -- the
    portable path -- hand the strategy to a native Expert Advisor (see ``mql/``). The
    :class:`~src.platforms.mt5_adapter.MT5Adapter` covers the Python-driven route and
    emits the parameters the EA needs.
  * **3Commas** and **Cryptohopper** are hosted bot platforms. You do not run your loop on
    them; instead you send them a **webhook signal** ("open/close this deal/position") and
    they execute on your connected exchange. The adapters here build exactly those signed
    webhook payloads.

Keeping this translation in one layer means the strategy code never learns about any
specific venue, and adding a venue is a new adapter, not a strategy rewrite.
"""
from .base import PlatformAdapter, Signal
from .mt5_adapter import MT5Adapter
from .threecommas import ThreeCommasAdapter
from .cryptohopper import CryptohopperAdapter

__all__ = [
    "PlatformAdapter",
    "Signal",
    "MT5Adapter",
    "ThreeCommasAdapter",
    "CryptohopperAdapter",
]
