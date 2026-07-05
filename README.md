# TRADEBOT

An automated crypto trading framework built **backtest-first**. The guiding rule of
this project is simple: *no real money until we are sure of everything.* Every design
decision favors honesty about performance over impressive-looking results.

## What's here

```
src/
  data/loader.py          Fetch OHLCV from any ccxt exchange (public, no API key)
                          + a synthetic generator for offline work
  strategies/
    base.py               Strategy interface (must not look into the future)
    trend_momentum.py     First strategy: EMA trend + momentum, long/flat only
  backtest/
    engine.py             Event-driven engine: no lookahead, real fees & slippage
    metrics.py            Sharpe, Sortino, drawdown, profit factor, ...
  risk/manager.py         Position sizing, ATR stops, drawdown kill switch
  live/trader.py          Live/paper skeleton -- SAFETY GATED, not yet sending orders
scripts/
  run_backtest.py         One backtest with a report vs buy & hold
  validate.py             Walk-forward validation across time folds
tests/                    Correctness tests, including a no-lookahead guard
config.example.yaml       Copy to config.yaml and edit
```

## Quick start

```bash
pip install -r requirements.txt

# Runs fully offline on synthetic data -- no account or network needed:
python scripts/run_backtest.py --synthetic
python scripts/validate.py --synthetic --folds 6
python -m pytest -q

# Once you can reach an exchange, use real candles (still no API key -- public data):
cp config.example.yaml config.yaml   # then edit symbol/exchange/timeframe
python scripts/run_backtest.py
```

> Note: some exchange APIs (e.g. Binance) are geo-restricted or blocked on certain
> networks. If `fetch_ohlcv` fails with a network error, try `exchange: kraken` or
> `coinbase` in `config.yaml`, or run with `--synthetic` while you sort out access.

## The three honesty guarantees

Most retail backtests lie. This engine is built to not.

1. **No lookahead.** A signal decided on a bar's close is executed only on the *next*
   bar's open. `tests/test_backtest.py::test_no_lookahead_leak` feeds the engine a
   deliberately cheating "sees the next bar" strategy and asserts it *cannot* profit —
   proving the one-bar delay holds.
2. **Real costs.** Every fill pays a taker fee (default 0.10%) and crosses a slippage
   spread (default 0.05%). A strategy that only works at zero cost is worthless, and
   the tests assert costs always reduce returns.
3. **Benchmarked.** Every report shows the strategy *and* buy-and-hold side by side. If
   we can't beat simply holding the coin, we don't have an edge.

## What the first strategy does

Long-only trend/momentum: go long when the fast EMA is above the slow EMA, price is
above the slow EMA, and recent momentum is positive; otherwise sit in cash. It uses no
leverage, no shorting, and no margin — matching a first live account.

Its value is *defense*. On a downtrending sample it stayed ~73% in cash and lost a
fraction of what holding would have:

```
 metric                      strategy        buy & hold
 total return                  -4.68%           -48.52%
 max drawdown                  -8.43%           -65.76%
```

(Numbers from `run_backtest.py --synthetic`; your real-data results will differ.)

## Path to live — do not skip a step

Live trading is intentionally **off by default and hard to turn on** (`live.enabled`,
`live.mode: live`, and `live.confirm_live` are all separate gates; API keys are read
only from `TRADEBOT_API_KEY` / `TRADEBOT_API_SECRET` env vars, never from a file).

1. **Backtest** a strategy until it beats buy-and-hold on risk-adjusted terms.
2. **Validate** with `validate.py` — the edge must hold across *most* time folds, not
   ride on one lucky window.
3. **Paper trade** against the exchange's testnet for a meaningful stretch and confirm
   live behavior matches the backtest.
4. **Go live small.** Only then wire real orders in `src/live/trader.py`, start with
   money you can afford to lose, and keep the drawdown kill switch on.

## Honest expectations

- No strategy here predicts the market or guarantees profit. Most retail strategies
  that backtest well fail live due to overfitting, regime change, or costs.
- A beautiful backtest is easy to produce and easy to fool yourself with. Treat every
  good result with suspicion until it survives walk-forward validation and paper
  trading.
- This is trading software, not financial advice. The capital decisions are yours.

## Next steps we can build

- More strategies (mean reversion, breakout, DCA/grid) behind the same `Strategy` API.
- A parameter-robustness sweep to check the strategy isn't perched on a fragile optimum.
- Telegram/Discord alerting and a paper-trading loop.
- The sports/event-contract idea (Kalshi-style) — same discipline, different data feed.
