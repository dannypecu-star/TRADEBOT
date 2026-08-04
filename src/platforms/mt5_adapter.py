"""MetaTrader 5 adapter -- drive an MT5 terminal from Python (Windows), or export EA params.

Two ways this project reaches MetaTrader, and this adapter supports both:

1. **Python-driven (this class).** On Windows with MetaTrader 5 installed, the official
   ``MetaTrader5`` PyPI package lets Python place orders in the running terminal. This
   adapter wraps that: our neutral :class:`Signal` becomes an MT5 market order (buy to
   enter long, close position to exit). It is the fastest way to reuse the exact Python
   strategy on an MT5 (often forex/CFD) account.

2. **Native Expert Advisor (see ``mql/``).** MT4/MT5 can also run the strategy *inside* the
   terminal as an ``.mq5``/``.mq4`` EA, which needs no Python at all and runs on any MT
   VPS. That path is portable and broker-agnostic; this adapter helps by emitting the exact
   input parameters the EA expects (:meth:`ea_inputs`), so the Python backtest and the EA
   stay in sync.

Because ``MetaTrader5`` only exists on Windows and needs a live terminal, it is imported
lazily and every method degrades gracefully (dry-run) elsewhere -- so this file imports and
tests fine on Linux/CI.
"""
from __future__ import annotations

import os

from .base import PlatformAdapter, Signal


class MT5Adapter(PlatformAdapter):
    name = "mt5"

    def __init__(
        self,
        symbol: str = "BTCUSD",
        lot: float = 0.10,
        deviation: int = 20,          # max slippage in points the order will accept
        magic: int = 555001,          # "magic number" tags orders as ours in the terminal
        login: int | None = None,
        server: str | None = None,
        dry_run: bool = True,
    ):
        super().__init__(dry_run=dry_run)
        self.symbol = symbol
        self.lot = lot
        self.deviation = deviation
        self.magic = magic
        self.login = login or (int(os.environ["MT5_LOGIN"]) if os.environ.get("MT5_LOGIN") else None)
        self.server = server or os.environ.get("MT5_SERVER")

    # --------------------------------------------------------------- EA export
    def ea_inputs(self, strategy_name: str, params: dict) -> dict:
        """Return the input block to paste into (or feed to) the native MT5/MT4 EA.

        Keeps the terminal-side EA parameters identical to the Python backtest so results
        are comparable across the two execution paths.
        """
        return {
            "InpStrategy": strategy_name,
            "InpSymbol": self.symbol,
            "InpLot": self.lot,
            "InpDeviation": self.deviation,
            "InpMagic": self.magic,
            **{f"Inp_{k}": v for k, v in params.items()},
        }

    # ------------------------------------------------------------ live ordering
    def _mt5(self):
        """Import and initialise the MetaTrader5 terminal connection (Windows only)."""
        import MetaTrader5 as mt5  # noqa: N813 - the package's canonical alias

        kwargs = {}
        if self.login and self.server:
            kwargs = {"login": self.login, "server": self.server}
        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
        return mt5

    def send(self, signal: Signal) -> dict:
        if signal.action == "hold":
            return {"platform": self.name, "action": "hold", "sent": False}

        order = {
            "symbol": self.symbol,
            "action": "buy" if signal.action == "enter_long" else "close",
            "lot": self.lot,
            "deviation": self.deviation,
            "magic": self.magic,
        }
        if self.dry_run:
            return {"platform": self.name, "sent": False, "dry_run": True, "order": order}

        mt5 = self._mt5()
        try:
            if signal.action == "enter_long":
                return self._market_buy(mt5)
            return self._close_position(mt5)
        finally:
            mt5.shutdown()

    def _market_buy(self, mt5) -> dict:
        tick = mt5.symbol_info_tick(self.symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": self.lot,
            "type": mt5.ORDER_TYPE_BUY,
            "price": tick.ask,
            "deviation": self.deviation,
            "magic": self.magic,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        return {"platform": self.name, "sent": True, "retcode": result.retcode}

    def _close_position(self, mt5) -> dict:
        positions = mt5.positions_get(symbol=self.symbol) or []
        closed = []
        for pos in positions:
            if pos.magic != self.magic:
                continue
            tick = mt5.symbol_info_tick(self.symbol)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": pos.volume,
                "type": mt5.ORDER_TYPE_SELL,
                "position": pos.ticket,
                "price": tick.bid,
                "deviation": self.deviation,
                "magic": self.magic,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            closed.append(result.retcode)
        return {"platform": self.name, "sent": True, "closed": closed}
