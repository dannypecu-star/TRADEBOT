# TRADEBOT

An automated trading framework built **backtest-first**. The guiding rule of this
project is simple: *no real money until we are sure of everything.* Every design
decision favors honesty about performance over impressive-looking results.

Two tracks live here:

* **Kalshi (event contracts)** — the current focus. Regulated US prediction markets
  (`src/kalshi/`), with a real demo/paper sandbox. Natural home for the sports angle.
* **Crypto (price)** — a trend/momentum framework (`src/`) to move to later.

They are different games (see the Kalshi section) but share the same discipline:
backtest honestly, model costs, paper-trade first, hard risk limits.

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

## Kalshi (event contracts) — the current focus

Kalshi is **not** like crypto. You trade the *probability of an event*, not the price
of an asset:

| | Crypto | Kalshi |
|---|---|---|
| what you trade | price of an asset | probability of a Yes/No event |
| price meaning | dollars | cents = implied % (63¢ ≈ 63% chance) |
| settlement | never; you exit | resolves to $1 or $0 |
| edge comes from | price trends | **better probability estimates**, spread, or arbitrage |

So the edge is *knowing the odds better than the market*. This module supplies all the
machinery for that — **you supply the probability estimate**, which is where the real
work and the real alpha live.

```
src/kalshi/
  client.py       Signed API client (RSA-PSS). Defaults to the DEMO sandbox.
  economics.py    Fees, edge, and fractional-Kelly position sizing for binary contracts
  strategy.py     Value strategy: buy what the market underprices vs a fair probability
  backtest.py     Replay over resolved markets + a CALIBRATION report
  paper.py        Demo/paper gating; PROD needs explicit confirm_prod
```

Run it offline (synthetic markets, no account needed):

```bash
python scripts/kalshi_backtest.py --skill 0.85   # accurate model -> profits, tight calibration
python scripts/kalshi_backtest.py --skill 0.0    # useless model  -> loses, calibration exposes it
python -m pytest tests/test_kalshi.py -q
```

**Read the calibration table, not the profit.** If your model says "70%" and those
events happen 70% of the time, the edge is real. If they happen 45% of the time, the
profit is a mirage no matter how good the headline number looks — the useless-model run
above prints a fat "predicted edge" while being completely wrong.

### Connecting your account (when you're ready)

1. Generate an API key in Kalshi settings; download the RSA private key.
2. `export KALSHI_KEY_ID=...` and `export KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem`.
3. The client defaults to the **DEMO** sandbox. Prove the strategy there first; PROD
   requires `env="prod"` *and* `confirm_prod=True`.

> Note: this dev sandbox's network blocks Kalshi's servers, so live calls run in your
> environment. The signing logic is unit-tested offline, so the client is correct;
> it just needs a network that can reach Kalshi.

### The sports edge (odds -> fair probability)

The alpha source is built: `src/kalshi/odds.py` converts sportsbook odds to a devigged
fair probability, and `src/kalshi/sources/theoddsapi.py` wires that to
[The Odds API](https://the-odds-api.com) (free tier: 500 requests/month) and exposes it
as a `ProbabilitySource` the strategy consumes.

**Devigging** removes the bookmaker margin: a book quoting both sides at implied 52.4%
sums to 104.8%; dividing each by the total recovers the fair 50/50. If sharp books say
a team is 50% and Kalshi prices it at 45¢, that gap is the edge.

```python
from src.kalshi.sources.theoddsapi import (TheOddsAPIClient,
    fair_probabilities_from_payload, SportsbookProbabilitySource)

payload = TheOddsAPIClient().fetch_odds(sport="basketball_nba")   # needs THE_ODDS_API_KEY
fair = fair_probabilities_from_payload(payload)                   # devigged consensus per game
source = SportsbookProbabilitySource(fair, ticker_map={...})      # map Kalshi tickers -> (game, side)
# feed source.fair_probability(ticker) into src/kalshi/strategy.evaluate_market(...)
```

The odds math is fully unit-tested offline (`tests/test_odds.py`). The one piece that
needs live data from both sides is `ticker_map` — matching a Kalshi ticker to the right
game/outcome — which is kept explicit rather than guessed.

## Next steps we can build

- **Kalshi:** build the `ticker_map` (Kalshi market <-> sportsbook game) for a live slate.
- **Kalshi:** a demo paper-trading loop that reads live markets and places sandbox orders.
- **Kalshi:** collect resolved-market history to backtest the sports edge on real data.
- **Crypto:** more strategies (mean reversion, breakout, DCA/grid) behind the `Strategy` API.
- Telegram/Discord alerting and hard per-day loss limits shared across both tracks.
