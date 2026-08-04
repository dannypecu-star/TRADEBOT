# MetaTrader Expert Advisors (MT4 / MT5)

These are the **native-terminal** versions of the Python strategies. They run entirely
inside a MetaTrader terminal — no Python, no server round-trips — which makes them the
portable, broker-agnostic way to deploy on a MetaTrader VPS.

| File | Platform | Strategy | Python twin |
|------|----------|----------|-------------|
| `TrendFollowing.mq5` | MT5 | Donchian breakout trend following | `src/strategies/trend_following.py` |
| `MeanReversion.mq5`  | MT5 | Bollinger / z-score mean reversion | `src/strategies/mean_reversion.py` |
| `TrendFollowing.mq4` | MT4 | Donchian breakout trend following | `src/strategies/trend_following.py` |
| `MeanReversion.mq4`  | MT4 | Bollinger / z-score mean reversion | `src/strategies/mean_reversion.py` |

The EA inputs (`InpEntryLookback`, `InpEntryZ`, …) are deliberately named to match the
Python config so a Python backtest is a faithful preview of the EA's behaviour.

> **Arbitrage is not shipped as an MT EA.** Cross-exchange and pairs arbitrage need
> simultaneous prices/positions across venues, which a single MT terminal cannot see.
> Use the Python `src/arbitrage/` modules for those.

## Install (MT5)

1. Open MetaTrader 5 → **File ▸ Open Data Folder**.
2. Copy the `.mq5` files into `MQL5/Experts/`.
3. Back in the terminal, open **Navigator** (Ctrl+N), right-click **Expert Advisors ▸
   Refresh**, then double-click the EA to compile (or open it in MetaEditor and press F7).
4. Drag the EA onto a chart of the symbol/timeframe you want. In the dialog:
   - **Common** tab: tick *Allow Algo Trading*.
   - **Inputs** tab: set the parameters (match your Python config).
5. Ensure the global **Algo Trading** button in the toolbar is enabled (green).

## Install (MT4)

Same steps, but copy the `.mq4` files into `MQL4/Experts/`, compile in MetaEditor (F7),
and tick *Allow live trading* in the EA's Common tab.

## Test before you trade

Use the built-in **Strategy Tester** (Ctrl+R) first:

1. Select the EA, symbol, timeframe, and a date range.
2. Set modelling to *Every tick* (MT5) / *Every tick* (MT4) for the most realistic fills.
3. Run and inspect the report — max drawdown and profit factor matter more than net
   profit. Then forward-test on a **demo account** before risking real money.

## Risk controls built in

Every EA:
- sizes each position to risk only `InpRiskPerTrade` (default 1%) of equity to an
  ATR-based stop,
- attaches that stop-loss to the order at entry, and
- halts **new** entries once equity draws down more than `InpMaxDrawdown` (default 30%)
  from its peak.

These are floors, not guarantees. Leverage on forex/CFD accounts can still cause losses
faster than a stop can act during gaps. Read the root `README.md` disclaimer.
