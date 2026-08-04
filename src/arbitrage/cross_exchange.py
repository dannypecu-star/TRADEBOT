"""Cross-exchange arbitrage scanner (live, read-only).

Deterministic arbitrage is simple to state: the *same* asset trades at a higher price on
one venue than another at the same instant, so you buy on the cheap venue and sell on the
dear one and pocket the difference. The hard part is that the difference has to survive
**all** of your costs, and for a retail participant it very often does not.

This scanner fetches the current best bid/ask for one symbol across several ccxt
exchanges (public endpoints, no keys) and reports the spread *net of estimated costs*:

    gross_edge = best_bid_on_venue_X - best_ask_on_venue_Y
    net_edge   = gross_edge - (taker_fee_X + taker_fee_Y + slippage) * price

Only ``net_edge > 0`` is an actual opportunity. The scanner deliberately does **not**
place orders: real execution also needs pre-funded balances on both venues (you cannot
move coins between exchanges fast enough to catch a live spread), latency budgeting, and
withdrawal/transfer risk. Treat this as a monitor and a reality check, not a money
printer -- if it almost never prints a positive net edge, that is the honest and expected
result.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Quote:
    exchange: str
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class ArbOpportunity:
    symbol: str
    buy_exchange: str          # buy here (at its ask)
    sell_exchange: str         # sell here (at its bid)
    buy_price: float
    sell_price: float
    gross_edge_bps: float      # spread in basis points of mid price, before costs
    net_edge_bps: float        # after taker fees + slippage on both legs
    profitable: bool


def fetch_quotes(symbol: str, exchanges: list[str]) -> list[Quote]:
    """Fetch best bid/ask for ``symbol`` from each exchange via ccxt public tickers.

    Exchanges that error (symbol not listed, network) are skipped rather than aborting
    the whole scan.
    """
    import ccxt  # lazy import so tests/offline use don't require ccxt

    quotes: list[Quote] = []
    for name in exchanges:
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True})
            t = ex.fetch_ticker(symbol)
            bid, ask = t.get("bid"), t.get("ask")
            if bid and ask and bid > 0 and ask > 0:
                quotes.append(Quote(name, float(bid), float(ask)))
        except Exception:  # noqa: BLE001 - a single bad venue must not kill the scan
            continue
    return quotes


def find_opportunities(
    symbol: str,
    quotes: list[Quote],
    taker_fee: float = 0.001,
    slippage: float = 0.0005,
    min_net_bps: float = 0.0,
) -> list[ArbOpportunity]:
    """Compare every ordered pair of venues and return net-positive opportunities.

    ``taker_fee`` and ``slippage`` are per leg. The round trip therefore pays
    ``2*taker_fee + 2*slippage`` of the traded notional, which is what a real cross-venue
    fill costs before you even account for transfer time.
    """
    round_trip_cost = 2 * taker_fee + 2 * slippage  # as a fraction of price
    opps: list[ArbOpportunity] = []

    for buy in quotes:
        for sell in quotes:
            if buy.exchange == sell.exchange:
                continue
            # Buy at buy.ask, sell at sell.bid.
            gross = sell.bid - buy.ask
            mid = (buy.ask + sell.bid) / 2.0
            if mid <= 0:
                continue
            gross_bps = gross / mid * 1e4
            net_bps = (gross / mid - round_trip_cost) * 1e4
            profitable = net_bps > min_net_bps
            opps.append(
                ArbOpportunity(
                    symbol=symbol,
                    buy_exchange=buy.exchange,
                    sell_exchange=sell.exchange,
                    buy_price=buy.ask,
                    sell_price=sell.bid,
                    gross_edge_bps=round(gross_bps, 2),
                    net_edge_bps=round(net_bps, 2),
                    profitable=profitable,
                )
            )

    opps.sort(key=lambda o: o.net_edge_bps, reverse=True)
    return opps


def scan(
    symbol: str = "BTC/USDT",
    exchanges: list[str] | None = None,
    taker_fee: float = 0.001,
    slippage: float = 0.0005,
) -> list[ArbOpportunity]:
    """Convenience: fetch quotes and return opportunities sorted best-first."""
    exchanges = exchanges or ["binance", "kraken", "coinbase", "bitstamp"]
    quotes = fetch_quotes(symbol, exchanges)
    return find_opportunities(symbol, quotes, taker_fee, slippage)
