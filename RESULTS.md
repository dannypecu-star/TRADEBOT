# Backtest Results

> **These are backtests, not guarantees.** Fees and slippage are charged on every
> fill. Past performance — especially on synthetic data — does not predict future
> returns. Most retail automated trading loses money. Validate with
> `scripts/validate.py` and paper trading before risking a cent. See the full
> disclaimer in `README.md`.

- **Generated:** 2026-08-04 09:21 UTC
- **Dataset:** Synthetic, aggregated over 25 seeds × 3000 bars (1h)
- **Data note:** Deterministic synthetic data (seeded). Reproducible, idealized, NOT real markets. Each regime is built to contain the structure one strategy targets.
- **Costs applied:** taker fee + slippage on every fill (see `config.example.yaml`).

## The honest summary

Each strategy has a **real edge only in the regime it is designed for**, and no
edge on a structureless random walk. That is exactly what a legitimate strategy
should look like. A tool that appeared to win everywhere would be overfit or
mismeasured.

## 1. Neutral data — a random walk with drift

### No exploitable structure

GBM is a random walk; there is nothing to exploit, so buy-and-hold wins on return. Note the strategies' **much smaller drawdowns** — their real contribution here is risk reduction, not alpha.

_Aggregated over 25 random seeds._

| Strategy | Mean return | Median return | Mean Sharpe | Mean max DD | % runs profitable | % beat B&H |
|---|---|---|---|---|---|---|
| trend_following | -0.0% | -1.0% | -0.18 | -6.7% | 40% | 44% |
| trend_momentum | -2.5% | -3.3% | -0.99 | -6.8% | 40% | 40% |
| mean_reversion | -4.8% | -3.4% | -2.36 | -6.7% | 16% | 40% |
| _buy & hold_ | 13.3% | 7.1% | — | -43.1% | — | — |

## 2. Trending regime

### Momentum present → trend strategies profit

Persistent-trend data. Trend following and trend momentum capture the moves; mean reversion fights the trend and loses (the wrong tool for this regime).

_Aggregated over 25 random seeds._

| Strategy | Mean return | Median return | Mean Sharpe | Mean max DD | % runs profitable | % beat B&H |
|---|---|---|---|---|---|---|
| trend_following | 53.1% | 45.9% | 7.74 | -5.2% | 100% | 48% |
| trend_momentum | 54.4% | 47.5% | 7.68 | -5.6% | 100% | 52% |
| mean_reversion | -27.8% | -29.8% | -9.62 | -28.4% | 0% | 28% |
| _buy & hold_ | 84.6% | 87.1% | — | -59.3% | — | — |

## 3. Mean-reverting regime

### Range-bound → mean reversion profits

Oscillating, range-bound data. Mean reversion buys the dips and sells the snap-back; the trend strategies get chopped up. (Mean reversion's long-term trend filter is disabled here — see script note — since there is no structural trend to guard against.)

_Aggregated over 25 random seeds._

| Strategy | Mean return | Median return | Mean Sharpe | Mean max DD | % runs profitable | % beat B&H |
|---|---|---|---|---|---|---|
| trend_following | -26.6% | -26.2% | -5.58 | -27.6% | 0% | 0% |
| trend_momentum | -29.3% | -29.8% | -6.33 | -30.0% | 0% | 0% |
| mean_reversion | 19.1% | 19.8% | 4.49 | -3.0% | 100% | 96% |
| _buy & hold_ | 1.1% | 1.4% | — | -33.5% | — | — |

## 4. Statistical arbitrage (pairs) demo

Market-neutral spread reversion between two cointegrated assets (long one / short
the other), aggregated over the same seeds. Reported separately because it is
dollar-neutral — comparing it to buy-and-hold would be apples to oranges.

_Aggregated over 25 random seeds._

| Metric | Value |
|---|---|
| Mean total return | 9.5% |
| Median total return | 11.9% |
| Mean Sharpe | 0.79 |
| Mean max drawdown | -11.0% |
| Mean win rate | 60.1% |
| % runs profitable | 76% |

> **The catch that makes or breaks pairs arbitrage:** this works because the
> synthetic pair has a *stable* hedge ratio, so the spread is truly stationary.
> Real pairs often have an unstable hedge ratio, which smuggles a random-walk
> component into the spread and turns "reversion" into a slow bleed. Before
> trading a real pair, test it for cointegration and watch the rolling beta.

## How to reproduce

```bash
python scripts/run_strategy_comparison.py            # synthetic regimes (this report)
python scripts/run_strategy_comparison.py --real     # one live-data backtest
python scripts/validate.py --synthetic --folds 6     # walk-forward robustness
```

## Reading these numbers honestly

- **No strategy beats a random walk.** That is correct, not a failure — a random
  walk has no edge to extract. The strategies earn their keep only when the market
  has the structure they target (trend or reversion).
- **Synthetic ≠ real.** Real markets are noisier, regime-switch without warning, and
  charge wider costs. Treat these tables as unit tests for the *logic*, not forecasts.
- **The one number that matters** is a live paper-trading track record over months.
  Everything here is a hypothesis until then.
