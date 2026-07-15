# Running the fair-value edge on NightShark

This turns NightShark's entries from the **zero-EV price-range rule** into **edge-driven
trades**: it enters only when Kalshi's leg price disagrees with a live fair value
computed from the underlying spot, the market open (strike), minutes-left, and a live
volatility estimate — and it logs every decision so you can *measure* whether the edge is
real instead of trusting a balance.

Two files do the work:

- `nightshark_fair_value.ahk` — the native AutoHotkey model (drop next to NightShark).
- `../src/kalshi/trade_log.py` + `../scripts/kalshi_analyze_trades.py` — the Python
  analyzer that reads the CSV NightShark writes and reports realized edge + calibration.

Everything the AHK module needs already exists in your NightShark script
(`GetCachedKalshiPriceObj`, `PriceObjCurrent`, `PriceObjOpen`, `BuildPairFromName`,
`PairMinutesLeft`, `AttachPairBehavior`, `UpdatePairBehaviorState`, `CurrentUtcNowAhk`,
`Log`).

---

## 1. Include the module

Near the top of your NightShark `.ahk` (after the config block), add:

```ahk
#Include nightshark_fair_value.ahk
NsFairValueSelfTest()   ; optional: logs PASS/FAIL that the AHK math matches the Python model
```

Tune the knobs at the top of `nightshark_fair_value.ahk` if needed:

| knob | default | meaning |
|---|---|---|
| `NsMinEdge` | `0.03` | required net edge ($/pair-contract, after fees) to enter |
| `NsFeeRate` | `0.035` | Kalshi fee coefficient for 15m crypto — **verify your series** |
| `NsTypicalMove15m` | `0.003` | fallback vol seed (0.3% typical 15m move) until live vol warms up |
| `NsAlsoRequireBehaviorSync` | `false` | `true` keeps the old SYNC gate *on top of* the edge gate |
| `NsLogPath` | `<script dir>\nightshark_trades.csv` | where trades are logged |

---

## 2. Swap the entry gate (two edits)

**A. First entry per market.** In the main loop, find:

```ahk
candidatePair := SelectPairEntry(btcSnap, ethSnap)
if !IsObject(candidatePair) {
    ...sleep/continue...
}
if !PairEntrySignalMet(candidatePair) {
    ...sleep/continue...
}
```

Replace the two function calls with the `Ns` versions:

```ahk
candidatePair := NsSelectPairEntry(btcSnap, ethSnap)     ; picks the higher-edge inverse pair, or false
if !IsObject(candidatePair) {
    ...sleep/continue...
}
if !NsPairEntrySignalMet(candidatePair) {                ; edge >= NsMinEdge
    ...sleep/continue...
}
```

**B. Add-on shots.** In `TryNextPairShot(...)`, find:

```ahk
candidatePair := BuildPairFromName(activePair.name, btcSnap, ethSnap)
AttachPairBehavior(candidatePair, UpdatePairBehaviorState(btcSnap, ethSnap))
if !PairEntrySignalMet(candidatePair)
    return false
```

Replace with:

```ahk
candidatePair := BuildPairFromName(activePair.name, btcSnap, ethSnap)
AttachPairBehavior(candidatePair, UpdatePairBehaviorState(btcSnap, ethSnap))
if !NsEvaluatePair(candidatePair, minsLeft)              ; minsLeft already computed above in this function
    return false
if !NsPairEntrySignalMet(candidatePair)
    return false
```

That's the whole strategy change. NightShark now only fires when the model sees a real
mispricing, and it re-checks edge on every add-on shot (so it stops adding the moment the
edge disappears — which also fixes the "kept averaging into the loser" problem).

> Keeping the range as a sanity bound (optional): if you still want to refuse entries
> outside `EntryRange`, leave your existing `PairSumInEntryRange(candidatePair.sum)` check
> in place *alongside* `NsPairEntrySignalMet`. The edge gate is the decision-maker; the
> range just caps how far from even-money you'll go.

---

## 3. Log every trade (three small additions)

The logger reuses NightShark's own per-shot paper trades, so entry and settlement
reconcile exactly.

**A. Carry the model fields onto each shot's legs.** In `PaperBuyPair`, the leg-push
currently reads:

```ahk
trade.legs.Push({ asset: leg.asset, ticker: leg.ticker, side: leg.side, entry: legPrice, qty: legCount })
```

Extend it so the fair-value fields survive into the trade record:

```ahk
trade.legs.Push({ asset: leg.asset, ticker: leg.ticker, side: leg.side, entry: legPrice, qty: legCount
    , upProb: leg.HasKey("upProb") ? leg.upProb : "", fairProb: leg.HasKey("fairProb") ? leg.fairProb : ""
    , spotAtEval: leg.HasKey("spotAtEval") ? leg.spotAtEval : "", strikeAtEval: leg.HasKey("strikeAtEval") ? leg.strikeAtEval : ""
    , volAtEval: leg.HasKey("volAtEval") ? leg.volAtEval : "", minsAtEval: leg.HasKey("minsAtEval") ? leg.minsAtEval : "" })
```

Then, right after `paperOpenTrades.Push(trade)` in the same function, add:

```ahk
NsLogTradeEntry(trade)
```

**B. Log resolutions and exits.** In `PaperClosePair`, inside the
`for _, trade in paperOpenTrades` loop, after the `paperCash += ...` line (i.e. once the
trade is being closed), add:

```ahk
if (mode = "RESOLUTION")
    NsLogTradeSettlement(trade, btcSnap, ethSnap)   ; real outcome -> feeds calibration
else
    NsLogTradeExit(trade, btcSnap, ethSnap)         ; stop-loss -> counted in P&L only
```

(`btcSnap`/`ethSnap` are the arguments already passed to `PaperClosePair`; when they are
blank, `MarkPairHeldToResolution` passes `activePair.lastBtcSnap`/`lastEthSnap`, which is
correct.)

That's it. NightShark now writes `nightshark_trades.csv` with one `SIGNAL` row per leg per
shot, one `SETTLED` row per leg at resolution, and one `EXIT` row per leg on a stop-loss.

---

## 4. Measure the edge (the whole point)

Copy `nightshark_trades.csv` off the Windows box and run the analyzer:

```bash
python scripts/kalshi_analyze_trades.py nightshark_trades.csv
```

You get:

- **realized_edge** — dollars/contract actually earned at resolution. If it is not
  reliably positive after fees, there is no edge and no sizing fixes that.
- **calibration** — predicted vs realized frequency per probability bucket. If the model
  says 70% but those settle 50%, it is overconfident; fix the vol estimate, not the size.
- **settled vs exit vs total P&L** — so a couple of lucky/unlucky stop-losses can't
  disguise what the model itself is doing.

Run it after every session. Fund real money only once realized_edge is convincingly
positive and calibration holds across a few hundred resolved trades.

---

## What this does and does not fix

- **Fixes:** entries are now gated on a real, measurable disagreement between the
  orderbook and spot-implied fair value — the only place durable edge can come from for
  these markets. Add-on shots stop when the edge is gone. Every trade is logged for an
  honest post-mortem.
- **Does not magically create alpha:** the edge is only as good as the **volatility
  estimate** (`NsVolAnnual`) and the assumption that **strike = the session open**
  (`PriceObjOpen`). Two things to verify on your actual markets:
  1. Is `KXBTC15M` an *"up vs the open"* market (strike = open, the default here) or an
     *"above a fixed $ strike"* market? If the latter, feed the real strike into
     `NsRefreshAsset` instead of `PriceObjOpen`.
  2. Does `GetCachedKalshiPriceObj` return the **underlying crypto spot** (Coinbase/
     Binance), not a Kalshi-derived price? The model needs the real underlying. If it
     doesn't, wire a spot feed into `NsRefreshAsset`.

Get those two right, keep `NsMinEdge` conservative, and let the analyzer — not the
balance — tell you whether it works.
