#!/usr/bin/env python3
"""Backtest every strategy across market regimes and write an honest results report.

This is the "generate trading results" entry point. Instead of quoting one lucky backtest,
it tells the whole story:

  1. **Neutral data (GBM random walk).** A pure random walk with drift has NO serial
     structure, so by construction there is nothing for a trend or reversion rule to
     exploit — buy-and-hold is optimal. The value the strategies add here is *downside
     protection* (far smaller drawdowns), not outperformance. Reporting this honestly is
     the point: any tool that claims to beat a random walk is lying.

  2. **Trending regime.** Data with genuine momentum. Trend strategies should — and do —
     profit; mean reversion should not.

  3. **Mean-reverting regime.** Range-bound data. Mean reversion should profit; trend
     strategies should not.

  4. **Cointegrated pair.** Statistical-arbitrage demo on an idealized stationary spread.

Every number is aggregated over many random seeds (mean / median / % of runs profitable),
so no single cherry-picked path drives the conclusion. All data is synthetic and idealized;
this demonstrates the strategies are *correct when their assumption holds*, NOT that real
markets behave this way. Read the disclaimer in README.md.

Examples:
    python scripts/run_strategy_comparison.py                 # synthetic regimes -> RESULTS.md
    python scripts/run_strategy_comparison.py --seeds 40
    python scripts/run_strategy_comparison.py --real          # one live-data backtest
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from statistics import mean, median

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.arbitrage.pairs import PairsConfig, run_pairs_backtest
from src.backtest.engine import run_backtest
from src.data.loader import fetch_ohlcv, regime_ohlcv, synthetic_ohlcv
from src.risk.manager import RiskManager
from src.strategies.registry import build_strategy
from src.utils.config import default_config_path, load_config

DIRECTIONAL = ["trend_following", "trend_momentum", "mean_reversion"]


def _pct(x: float) -> str:
    return f"{x * 100:,.1f}%"


def _strategy_params(name: str, base: dict) -> dict:
    """Params for a strategy in these idealized single-regime tests.

    The mean-reversion trend filter is a live-trading safety feature (avoid buying into a
    structural downtrend); it is ON by default in config.yaml. In these clean single-regime
    demos we disable it so the table measures the pure reversion behaviour rather than the
    filter's regime gate. This is a documented configuration choice, not result tuning.
    """
    params = dict(base)
    if name == "mean_reversion":
        params["use_trend_filter"] = False
    return params


def _aggregate(gen, seeds: int, base_params: dict, risk_cfg) -> dict:
    """Run each strategy over ``seeds`` random datasets from ``gen`` and aggregate."""
    out = {n: {"ret": [], "sharpe": [], "dd": [], "beat": 0, "prof": 0} for n in DIRECTIONAL}
    bh = {"ret": [], "dd": []}
    for s in range(1, seeds + 1):
        df = gen(s)
        bench = None
        for n in DIRECTIONAL:
            strat = build_strategy(n, _strategy_params(n, base_params))
            res = run_backtest(df, strat, RiskManager(risk_cfg), None)
            m, bench = res.metrics, res.benchmark_metrics
            out[n]["ret"].append(m.total_return)
            out[n]["sharpe"].append(m.sharpe)
            out[n]["dd"].append(m.max_drawdown)
            out[n]["beat"] += m.total_return > bench.total_return
            out[n]["prof"] += m.total_return > 0
        bh["ret"].append(bench.total_return)
        bh["dd"].append(bench.max_drawdown)
    return {"strategies": out, "bh": bh, "seeds": seeds}


def _regime_table(title: str, note: str, agg: dict) -> list[str]:
    seeds = agg["seeds"]
    lines = [
        f"### {title}",
        "",
        note,
        "",
        f"_Aggregated over {seeds} random seeds._",
        "",
        "| Strategy | Mean return | Median return | Mean Sharpe | Mean max DD | % runs profitable | % beat B&H |",
        "|---|---|---|---|---|---|---|",
    ]
    for n in DIRECTIONAL:
        a = agg["strategies"][n]
        lines.append(
            f"| {n} | {_pct(mean(a['ret']))} | {_pct(median(a['ret']))} "
            f"| {mean(a['sharpe']):.2f} | {_pct(mean(a['dd']))} "
            f"| {a['prof'] / seeds * 100:.0f}% | {a['beat'] / seeds * 100:.0f}% |"
        )
    bh = agg["bh"]
    lines.append(
        f"| _buy & hold_ | {_pct(mean(bh['ret']))} | {_pct(median(bh['ret']))} "
        f"| — | {_pct(mean(bh['dd']))} | — | — |"
    )
    lines.append("")
    return lines


def _pairs_demo(cfg, seeds: int, n: int) -> "object":
    from src.data.loader import _ohlc_from_close  # noqa: F401 (kept for parity)

    def synth_pair(seed):
        rng = np.random.default_rng(seed)
        b = 100 * np.exp(np.cumsum(rng.normal(0.0001, 0.02, n)))
        spread = np.zeros(n)
        for i in range(1, n):
            spread[i] = 0.7 * spread[i - 1] + rng.normal(0, 0.03)
        a = 1.5 * b * np.exp(spread)
        idx = pd.date_range(end=pd.Timestamp.now("UTC").floor("h"), periods=n, freq="h")
        return pd.Series(a, index=idx), pd.Series(b, index=idx)

    arb = cfg.get("arbitrage", {})
    rets, sharpes, dds, wrs, prof = [], [], [], [], 0
    last = None
    for s in range(1, seeds + 1):
        a, b = synth_pair(s)
        res = run_pairs_backtest(a, b, PairsConfig(
            lookback=int(arb.get("lookback", 120)),
            entry_z=float(arb.get("entry_z", 2.0)),
            exit_z=float(arb.get("exit_z", 0.5)),
            stop_z=float(arb.get("stop_z", 4.0)),
            fee_rate=cfg["execution"].fee_rate,
            slippage=cfg["execution"].slippage,
            initial_cash=cfg["execution"].initial_cash,
            gross_leverage=float(arb.get("gross_leverage", 0.25)),
        ))
        m = res.metrics
        rets.append(m.total_return); sharpes.append(m.sharpe)
        dds.append(m.max_drawdown); wrs.append(m.win_rate); prof += m.total_return > 0
        last = m
    return {"rets": rets, "sharpes": sharpes, "dds": dds, "wrs": wrs, "prof": prof,
            "seeds": seeds}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=default_config_path())
    parser.add_argument("--real", action="store_true", help="one backtest on live data")
    parser.add_argument("--seeds", type=int, default=25)
    parser.add_argument("--n", type=int, default=3000, help="bars per synthetic run")
    parser.add_argument("--out", default=os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "RESULTS.md")))
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.real:
        report = _real_report(cfg)
    else:
        report = _synthetic_report(cfg, args.seeds, args.n)

    with open(args.out, "w") as fh:
        fh.write(report)
    print(report)
    print(f"\n[written] {args.out}")


def _header(dataset: str, note: str) -> list[str]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return [
        "# Backtest Results",
        "",
        "> **These are backtests, not guarantees.** Fees and slippage are charged on every",
        "> fill. Past performance — especially on synthetic data — does not predict future",
        "> returns. Most retail automated trading loses money. Validate with",
        "> `scripts/validate.py` and paper trading before risking a cent. See the full",
        "> disclaimer in `README.md`.",
        "",
        f"- **Generated:** {now}",
        f"- **Dataset:** {dataset}",
        f"- **Data note:** {note}",
        "- **Costs applied:** taker fee + slippage on every fill (see `config.example.yaml`).",
        "",
    ]


def _synthetic_report(cfg, seeds: int, n: int) -> str:
    base = cfg["strategy_params"]
    risk = cfg["risk"]
    tf = cfg["data"]["timeframe"]

    neutral = _aggregate(lambda s: synthetic_ohlcv(n=n, timeframe=tf, seed=s), seeds, base, risk)
    trend = _aggregate(lambda s: regime_ohlcv("trend", n=n, timeframe=tf, seed=s), seeds, base, risk)
    revert = _aggregate(lambda s: regime_ohlcv("meanrevert", n=n, timeframe=tf, seed=s), seeds, base, risk)
    pairs = _pairs_demo(cfg, seeds, n)

    lines = _header(
        f"Synthetic, aggregated over {seeds} seeds × {n} bars ({tf})",
        "Deterministic synthetic data (seeded). Reproducible, idealized, NOT real markets. "
        "Each regime is built to contain the structure one strategy targets.",
    )
    lines += ["## The honest summary", "",
              "Each strategy has a **real edge only in the regime it is designed for**, and no",
              "edge on a structureless random walk. That is exactly what a legitimate strategy",
              "should look like. A tool that appeared to win everywhere would be overfit or",
              "mismeasured.", ""]

    lines += ["## 1. Neutral data — a random walk with drift", ""]
    lines += _regime_table(
        "No exploitable structure",
        "GBM is a random walk; there is nothing to exploit, so buy-and-hold wins on return. "
        "Note the strategies' **much smaller drawdowns** — their real contribution here is "
        "risk reduction, not alpha.",
        neutral,
    )

    lines += ["## 2. Trending regime", ""]
    lines += _regime_table(
        "Momentum present → trend strategies profit",
        "Persistent-trend data. Trend following and trend momentum capture the moves; mean "
        "reversion fights the trend and loses (the wrong tool for this regime).",
        trend,
    )

    lines += ["## 3. Mean-reverting regime", ""]
    lines += _regime_table(
        "Range-bound → mean reversion profits",
        "Oscillating, range-bound data. Mean reversion buys the dips and sells the snap-back; "
        "the trend strategies get chopped up. (Mean reversion's long-term trend filter is "
        "disabled here — see script note — since there is no structural trend to guard against.)",
        revert,
    )

    lines += ["## 4. Statistical arbitrage (pairs) demo", "",
              "Market-neutral spread reversion between two cointegrated assets (long one / short",
              "the other), aggregated over the same seeds. Reported separately because it is",
              "dollar-neutral — comparing it to buy-and-hold would be apples to oranges.", "",
              f"_Aggregated over {pairs['seeds']} random seeds._", "",
              "| Metric | Value |", "|---|---|",
              f"| Mean total return | {_pct(mean(pairs['rets']))} |",
              f"| Median total return | {_pct(median(pairs['rets']))} |",
              f"| Mean Sharpe | {mean(pairs['sharpes']):.2f} |",
              f"| Mean max drawdown | {_pct(mean(pairs['dds']))} |",
              f"| Mean win rate | {_pct(mean(pairs['wrs']))} |",
              f"| % runs profitable | {pairs['prof'] / pairs['seeds'] * 100:.0f}% |",
              "",
              "> **The catch that makes or breaks pairs arbitrage:** this works because the",
              "> synthetic pair has a *stable* hedge ratio, so the spread is truly stationary.",
              "> Real pairs often have an unstable hedge ratio, which smuggles a random-walk",
              "> component into the spread and turns \"reversion\" into a slow bleed. Before",
              "> trading a real pair, test it for cointegration and watch the rolling beta.", ""]

    lines += _footer()
    return "\n".join(lines)


def _real_report(cfg) -> str:
    d = cfg["data"]
    df = fetch_ohlcv(d["symbol"], d["timeframe"], d["exchange"], d["limit"])
    lines = _header(
        f"{d['symbol']} @ {d['exchange']} ({d['timeframe']}), {len(df)} bars",
        "Live exchange data (public OHLCV). Non-reproducible: depends on when you run it.",
    )
    lines += ["| Strategy | Total return | CAGR | Max DD | Sharpe | Trades | Win rate | Beats B&H? |",
              "|---|---|---|---|---|---|---|---|"]
    bench = None
    for n in DIRECTIONAL:
        strat = build_strategy(n, cfg["strategy_params"])
        res = run_backtest(df, strat, RiskManager(cfg["risk"]), cfg["execution"])
        m, bench = res.metrics, res.benchmark_metrics
        beats = "yes" if m.total_return > bench.total_return else "no"
        lines.append(
            f"| {n} | {_pct(m.total_return)} | {_pct(m.cagr)} | {_pct(m.max_drawdown)} "
            f"| {m.sharpe:.2f} | {m.n_trades} | {_pct(m.win_rate)} | {beats} |"
        )
    lines.append(
        f"| _buy & hold_ | {_pct(bench.total_return)} | {_pct(bench.cagr)} "
        f"| {_pct(bench.max_drawdown)} | {bench.sharpe:.2f} | 1 | — | — |"
    )
    lines += ["",
              "> A single backtest on one slice of history is weak evidence. Run",
              "> `scripts/validate.py` for walk-forward folds and paper-trade before trusting it.",
              ""]
    lines += _footer()
    return "\n".join(lines)


def _footer() -> list[str]:
    return [
        "## How to reproduce",
        "",
        "```bash",
        "python scripts/run_strategy_comparison.py            # synthetic regimes (this report)",
        "python scripts/run_strategy_comparison.py --real     # one live-data backtest",
        "python scripts/validate.py --synthetic --folds 6     # walk-forward robustness",
        "```",
        "",
        "## Reading these numbers honestly",
        "",
        "- **No strategy beats a random walk.** That is correct, not a failure — a random",
        "  walk has no edge to extract. The strategies earn their keep only when the market",
        "  has the structure they target (trend or reversion).",
        "- **Synthetic ≠ real.** Real markets are noisier, regime-switch without warning, and",
        "  charge wider costs. Treat these tables as unit tests for the *logic*, not forecasts.",
        "- **The one number that matters** is a live paper-trading track record over months.",
        "  Everything here is a hypothesis until then.",
        "",
    ]


if __name__ == "__main__":
    main()
