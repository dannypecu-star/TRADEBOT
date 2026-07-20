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

### Paper trading on the demo sandbox

Once the smoke test passes, run the value strategy against live demo markets — with no
external odds API, using your own hand-entered probabilities so you can prove the whole
loop first:

```bash
# 1) write fair probabilities for a few open tickers (from smoke_test output):
echo '{"KXNBA-25JUL10-LAL": 0.62, "KXMLB-25JUL10-NYY": 0.55}' > my_probs.json

# 2) dry run — logs the orders it WOULD place, sends nothing (default):
python scripts/kalshi_paper_trade.py --probs my_probs.json

# 3) once you trust the output, place real DEMO orders (fake money):
python scripts/kalshi_paper_trade.py --probs my_probs.json --live
```

The loop (`src/kalshi/trader.py`) is deliberately boring and safe: **dry-run by
default**, a **position cap**, a **daily loss stop**, it **won't double-buy** a market
you already hold, and every thin edge is checked against fees before trading. It's fully
unit-tested offline against a fake client (`tests/test_trader.py`).

### Connecting your account (when you're ready)

1. Generate an API key in Kalshi settings; download the RSA private key.
2. `export KALSHI_KEY_ID=...` and `export KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem`.
3. Verify everything end to end (read-only, places no orders):
   ```bash
   python scripts/kalshi_smoke_test.py     # checks connectivity, market reads, and signing
   ```
4. The client defaults to the **DEMO** sandbox. Prove the strategy there first; PROD
   requires `env="prod"` *and* `confirm_prod=True`.

> Note: this dev sandbox's network blocks Kalshi's servers, so live calls run in your
> environment. The signing logic is unit-tested offline, so the client is correct;
> it just needs a network that can reach Kalshi.

### The weather edge (forecast ensemble -> fair probability), no API key

A second, fully keyless alpha source lives in `scripts/`:

```bash
python scripts/kalshi_edge_scanner.py        # one-shot scan, prints actionable edges
python scripts/kalshi_paper_bot.py           # one paper-trading cycle (cron-friendly)
python scripts/kalshi_paper_bot.py --loop 15 # automated: scan + paper-trade every 15 min
python scripts/kalshi_paper_bot.py --report  # current paper book / P&L
```

The **scanner** prices Kalshi daily-high-temperature markets against a 4-member
forecast ensemble — NWS point forecast plus ECMWF, GFS and ICON via
[Open-Meteo](https://open-meteo.com) (free, no key) — and only alerts when the edge
clears the taker fee, the real ask, a model-uncertainty haircut, and a book-depth
floor. Disagreement between ensemble members widens the distribution, so it claims
less edge exactly when the models are least sure. For same-day markets the observed
running max (the high can only go up) truncates the distribution, which is where the
sharpest mispricings show. It also flags internal arbs (event legs summing far from
100%). Fetches are parallel and per-city, so a full scan is a few seconds.

The **paper bot** turns those alerts into a simulated portfolio: fills at the real
ask + real fee, quarter-Kelly sizing capped at 5% of equity per trade (15% per arb
basket, 60% total exposure), no double-entry, settlement booked from Kalshi's public
market results, and bankroll / trade log / equity curve persisted in `data/paper_bot/`
so cron runs continue where the last one stopped. **Simulation only** — it never
places an order and fills are optimistic (full displayed ask), so treat the P&L as an
upper bound on the strategy, not proof. Fully unit-tested offline
(`tests/test_edge_scanner_bot.py`).

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

**Staying inside the free tier:** The Odds API's free plan is 500 credits/month, no card
required. A call costs `markets × regions` credits, so sticking to `h2h` + `us` is 1
credit each (~16/day). `TheOddsAPIClient` caches responses (`cache_ttl`, default 5 min)
so repeated checks don't spend credits, and exposes `credits_remaining` from the API
headers so you always know your budget. No payment needed to build or validate.

The odds math is fully unit-tested offline (`tests/test_odds.py`). The one piece that
needs live data from both sides is `ticker_map` — matching a Kalshi ticker to the right
game/outcome — which is kept explicit rather than guessed.

## Next steps we can build

- **Kalshi:** build the `ticker_map` (Kalshi market <-> sportsbook game) for a live slate.
- **Kalshi:** a demo paper-trading loop that reads live markets and places sandbox orders.
- **Kalshi:** collect resolved-market history to backtest the sports edge on real data.
- **Crypto:** more strategies (mean reversion, breakout, DCA/grid) behind the `Strategy` API.
- Telegram/Discord alerting and hard per-day loss limits shared across both tracks.
