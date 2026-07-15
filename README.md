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
  weather.py      Weather edge finder: rank weather markets by net edge
  sources/
    theoddsapi.py     The sports edge: sportsbook odds -> devigged fair probability
    openmeteo.py      The weather edge: Open-Meteo ensemble -> temperature distribution
    manual.py         Hand-entered probabilities, for validating the loop with no API
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

### The weather edge (forecast -> fair probability)

Kalshi's daily weather markets (e.g. *"Highest temperature in NYC today"*) are some of
its cleanest edges: the outcome is driven by physics a public model already forecasts,
and the market is often slower to move than the forecast. The alpha source lives in
`src/kalshi/sources/openmeteo.py` and the edge hunter in `src/kalshi/weather.py`.

The key idea is to trade a **distribution**, not a point forecast.
[Open-Meteo](https://open-meteo.com)'s free **ensemble** API (no key required) returns
~31 members — each a plausible run of the atmosphere. We take each member's daily-max
temperature, smooth the handful of samples with a Gaussian kernel into a proper CDF, and
integrate it over whatever strike a market offers to get a calibrated `P(Yes)`:

```bash
# Hunt: rank the best edges across every weather city (read-only, no credentials):
python scripts/kalshi_weather_bot.py

# Narrow the cities and demand at least 3¢ of net edge after fees:
python scripts/kalshi_weather_bot.py --series KXHIGHNY KXHIGHCHI --min-edge 0.03

# Paper-trade the found edges on the demo sandbox (dry-run by default):
python scripts/kalshi_weather_bot.py --trade          # logs orders, sends nothing
python scripts/kalshi_weather_bot.py --trade --live   # places DEMO orders (fake money)
```

Sample output — each market scored, both sides considered, ranked fattest-edge first:

```
KXHIGHNY-25JUL15-B88T92  New York City (Central Park)  88-92F  fair=98.5%  YES @ 0.40  edge=$+0.565  x124  (fc 90.0+/-0.8F)
KXHIGHNY-25JUL15-T95     New York City (Central Park)  >= 95F  fair= 0.0%  NO  @ 0.72  edge=$+0.260  x69   (fc 90.0+/-0.8F)
```

The distribution math, strike→probability mapping, and edge ranking are fully unit-tested
offline against a sample ensemble payload (`tests/test_weather.py`). Two things to verify
before trusting the numbers with real money:

- **Resolution station.** Each market settles at one specific weather station; the
  forecast must use *its* coordinates. `STATIONS` in `openmeteo.py` maps each series to
  the station we believe it uses — confirm against the market rules, since a wrong station
  silently poisons every probability.
- **Calibration.** As always here, read the calibration, not the headline edge. Backtest
  the forecast against resolved weather markets before believing a 98%.

## Next steps we can build

- **Kalshi:** build the `ticker_map` (Kalshi market <-> sportsbook game) for a live slate.
- **Kalshi:** a demo paper-trading loop that reads live markets and places sandbox orders.
- **Kalshi:** collect resolved-market history to backtest the sports *and* weather edges on real data.
- **Kalshi:** extend the weather bot beyond daily highs (lows, rain, snow) and verify each resolution station.
- **Crypto:** more strategies (mean reversion, breakout, DCA/grid) behind the `Strategy` API.
- Telegram/Discord alerting and hard per-day loss limits shared across both tracks.
