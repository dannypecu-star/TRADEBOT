# TRADEBOT

An automated trading framework built **backtest-first**. The guiding rule of this
project is simple: *no real money until we are sure of everything.* Every design
decision favors honesty about performance over impressive-looking results.

It ships three classic strategy families, a realistic backtester, a paper-trading bot
that builds an honest track record, adapters to run on popular platforms
(**MetaTrader 4/5, 3Commas, Cryptohopper**), and everything needed to deploy with
monitoring and logging.

Two tracks live here:

* **Crypto / price** (`src/`) — the multi-strategy engine below: trend following, mean
  reversion, and arbitrage, with backtesting, paper trading, and platform adapters.
* **Kalshi (event contracts)** (`src/kalshi/`) — regulated US prediction markets with a
  real demo/paper sandbox (see the Kalshi section near the end).

They are different games but share one discipline: backtest honestly, model costs,
paper-trade first, hard risk limits.

## The strategies

| Strategy | File | Edge it targets | Wins when… |
|---|---|---|---|
| **Trend following** | `strategies/trend_following.py` | Donchian channel breakouts | markets trend |
| **Trend momentum** | `strategies/trend_momentum.py` | EMA state + momentum | markets trend |
| **Mean reversion** | `strategies/mean_reversion.py` | z-score / Bollinger reversion | markets range |
| **Statistical arbitrage** | `arbitrage/pairs.py` | cointegrated spread reversion | a pair stays cointegrated |
| **Cross-exchange arbitrage** | `arbitrage/cross_exchange.py` | same asset, price gap between venues | a net-of-cost gap exists |

Each source file opens with a comment explaining the idea, the exact rules, and — crucially
— an **honest note on when the strategy fails**. No strategy works everywhere; a tool that
claimed to would be overfit. See `RESULTS.md` for measured, per-regime performance.

## What's here

```
src/
  data/loader.py          Fetch OHLCV from any ccxt exchange (public, no API key),
                          a synthetic generator, and regime generators (trend/meanrevert)
  strategies/
    base.py               Strategy interface (must not look into the future)
    trend_following.py    Donchian breakout trend follower
    trend_momentum.py     EMA trend + momentum
    mean_reversion.py     z-score / Bollinger mean reversion
    registry.py           Look up + build any strategy by name
  arbitrage/
    pairs.py              Statistical arbitrage: cointegrated-spread backtester
    cross_exchange.py     Live cross-venue spread scanner (net of fees)
  backtest/
    engine.py             Event-driven engine: no lookahead, real fees & slippage
    metrics.py            Sharpe, Sortino, drawdown, profit factor, ...
  paper/
    broker.py             Simulated broker: fake money, real prices, real costs
    paper_trader.py       Paper-trading bot (replay + live), audit log, health state
  platforms/
    mt5_adapter.py        Drive MetaTrader 5 from Python + export EA inputs
    threecommas.py        Send signals to a 3Commas bot (webhook)
    cryptohopper.py       Send signals to a Cryptohopper hopper (webhook)
  monitoring/
    logging_setup.py      Structured JSON logging
    health.py             /healthz + Prometheus /metrics endpoint
  risk/manager.py         Position sizing, ATR stops, drawdown kill switch
  live/trader.py          Live skeleton -- SAFETY GATED, not yet sending orders
mql/                      Native MetaTrader Expert Advisors (.mq5 / .mq4)
scripts/
  run_backtest.py         One backtest with a report vs buy & hold
  run_strategy_comparison.py  All strategies across regimes -> RESULTS.md
  run_paper_trader.py     Paper trade (replay a track record, or live-poll)
  dispatch_signal.py      Push the latest signal to a platform (dry-run by default)
  scan_arbitrage.py       Live cross-exchange arbitrage scan
  validate.py             Walk-forward validation across time folds
deploy/                   Dockerfile, docker-compose (bot+Prometheus+Grafana), systemd
tests/                    Correctness tests, including a no-lookahead guard
config.example.yaml       Copy to config.yaml and edit
```

## Quick start

```bash
pip install -r requirements.txt

# Runs fully offline on synthetic data -- no account or network needed:
python scripts/run_backtest.py --synthetic --strategy trend_following
python scripts/run_strategy_comparison.py           # writes RESULTS.md across regimes
python scripts/validate.py --synthetic --folds 6
python -m pytest -q

# Build an honest paper-trading track record (fake money, real cost model):
python scripts/run_paper_trader.py replay --regime trend --strategy trend_following
python scripts/run_paper_trader.py replay --regime meanrevert --strategy mean_reversion

# Once you can reach an exchange, use real candles (still no API key -- public data):
cp config.example.yaml config.yaml   # then edit strategy/symbol/exchange/timeframe
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

## Results — and how to read them honestly

`python scripts/run_strategy_comparison.py` writes `RESULTS.md`: every strategy measured
across three synthetic regimes, aggregated over many random seeds so no lucky path drives
the story. The headline finding is the honest one:

- **On a random walk (GBM), no strategy beats buy-and-hold.** A random walk has no serial
  structure to exploit — that is a *correct* result, not a failure. What the strategies add
  there is defense: far smaller drawdowns (~7% vs ~43%).
- **In a trending regime, the trend strategies profit** (≈ +50%, profitable in ~100% of
  runs); mean reversion loses — the wrong tool for that market.
- **In a range-bound regime, mean reversion profits** (≈ +19%, profitable in ~100% of
  runs); the trend strategies get chopped up.
- **The pairs-arbitrage demo profits on a cointegrated spread** — but only because that
  synthetic pair has a *stable* hedge ratio. Real pairs often don't, which is called out.

> These are backtests on **synthetic, idealized** data. They demonstrate the strategy
> *logic is correct when its assumption holds* — they are **not** forecasts of real-market
> profit. The paper trader exists precisely so you can test the logic against real prices
> before believing any of it.

## Running on platforms (MetaTrader, 3Commas, Cryptohopper)

The strategies produce one platform-neutral decision per bar; adapters translate it:

```bash
# Dry-run: build the exact payload that WOULD be sent (nothing transmitted):
python scripts/dispatch_signal.py --platform 3commas     --synthetic
python scripts/dispatch_signal.py --platform cryptohopper --synthetic
python scripts/dispatch_signal.py --platform mt5          --synthetic

# Send for real: pass --live AND set the platform's secrets in the environment (.env).
python scripts/dispatch_signal.py --platform 3commas --live
```

- **3Commas / Cryptohopper** run in their own cloud and hold your exchange keys; we only
  send start/close (webhook) signals. Run `dispatch_signal.py` once per bar on a timer
  (`deploy/systemd/tradebot-dispatch.timer`).
- **MetaTrader** has two paths: drive an MT5 terminal from Python (`MT5Adapter`), or — the
  portable route — run the native **Expert Advisors** in `mql/` (MT4 `.mq4` and MT5 `.mq5`,
  one per strategy) directly on a MetaTrader VPS. See `mql/README.md`.

## Paper trading — proving the edge before risking money

`run_paper_trader.py` runs any strategy against real prices with **fake money and the same
fees + slippage** the backtester charges. Two modes: `replay` (walk history fast to produce
a track record) and `live` (poll the exchange forever, exposing health + metrics). Every
decision and fill is written to `logs/paper_trades.jsonl` as an audit trail. This is the
evidence that matters — a clean multi-week paper run beats any backtest number.

## Deploy, monitor, log

See **`DEPLOYMENT.md`** for the full guide. In short:

```bash
docker compose -f deploy/docker-compose.yml up -d   # bot + Prometheus + Grafana
curl localhost:8000/healthz                          # JSON health
curl localhost:8000/metrics                          # Prometheus metrics
```

The bot serves `/healthz` (liveness, HTTP 503 if it goes silent) and `/metrics`
(`tradebot_up`, `tradebot_equity`, `tradebot_trades_total`, …). Logs are structured JSON;
set `monitoring.json_console: true` for container log shippers. `systemd` units are provided
for non-Docker VMs.

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
- **Crypto:** more strategies (breakout variants, DCA/grid) behind the `Strategy` API.
- Wire real exchange orders into `src/live/trader.py` (currently a safety-gated skeleton).
- Telegram/Discord alerting and hard per-day loss limits shared across both tracks.
