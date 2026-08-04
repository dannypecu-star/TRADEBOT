"""Arbitrage strategies.

Two distinct things live under this package, because "arbitrage" means two very
different things in practice:

* :mod:`src.arbitrage.pairs` -- *statistical* arbitrage. Two co-moving assets (or an
  asset on two venues) form a spread that mean-reverts. This is a real, backtestable
  edge with real risk (the relationship can break). It has its own two-legged backtest
  because the single-instrument engine cannot model a long/short spread.

* :mod:`src.arbitrage.cross_exchange` -- *deterministic* cross-exchange arbitrage. The
  same asset trades at slightly different prices on two venues at the same instant; you
  buy the cheap one and sell the dear one. This is a live scanner, not a backtest,
  because the opportunity exists only for milliseconds and depends on your actual fees,
  withdrawal times, and latency. The scanner is honest about how rarely a *net-of-cost*
  opportunity survives for a retail participant.
"""
