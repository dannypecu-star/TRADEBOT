; =============================================================================
;  NightShark Fair-Value Edge Module  (AutoHotkey v1)
;  -------------------------------------------------------------------------
;  Native port of src/kalshi/crypto_fair_value.py. Turns NightShark's entries
;  from a zero-EV price-range rule into edge-driven trades: it only enters when
;  Kalshi's leg price disagrees with a live fair value computed from the
;  underlying spot, the market's open (strike), minutes-left, and a LIVE
;  volatility estimate.
;
;  Theory (identical to the Python model, validated to 2e-8):
;      A leg pays $1 if the underlying finishes above the strike, so its fair
;      Yes-probability is  P(spot_close > strike) under zero-drift GBM:
;          d2  = [ln(S/K) + (-0.5*vol^2)*tau] / (vol*sqrt(tau))
;          Pup = Phi(d2)
;      Buying BTC_UP + ETH_DOWN costs P(BTC up)+P(ETH down) in expectation, so
;      the pair is zero-EV UNLESS the market price deviates from that fair sum.
;      This module measures that deviation and gates on it.
;
;  Usage: put this file next to NightShark and add near the top of your script:
;      #Include nightshark_fair_value.ahk
;  Then follow nightshark/INTEGRATION.md to swap SelectPairEntry /
;  PairEntrySignalMet and add the two logging calls. Depends on functions that
;  already exist in NightShark: GetCachedKalshiPriceObj, PriceObjCurrent,
;  PriceObjOpen, BuildPairFromName, PairMinutesLeft, CurrentUtcNowAhk, Log.
; =============================================================================

; ---- Config -----------------------------------------------------------------
NsMinEdge := 0.03            ; require >= this net edge (dollars/pair-contract) after fees to enter
NsFeeRate := 0.035           ; Kalshi fee coefficient for 15m crypto (verify your series; general is 0.07)
NsTypicalMove15m := 0.003    ; fallback: typical 1-sigma underlying move over 15m (0.3%) -> annualised vol
NsDriftAnnual := 0.0         ; leave 0; 15m drift is unforecastable
NsVolWindowSec := 180        ; rolling window for the live volatility estimate
NsVolMinSamples := 8         ; need at least this many returns before trusting the live vol
NsAlsoRequireBehaviorSync := false  ; true = keep the old SYNC gate on top of the edge gate
NsLogPath := A_ScriptDir "\nightshark_trades.csv"  ; read by src/kalshi/trade_log.py summarize()

NsVolBuffers := {}           ; top-level -> global; functions access via `global NsVolBuffers`

; ---- Math: standard-normal CDF (Zelen & Severo, max error 7.5e-8) ----------
NsNormCdf(x) {
    if (x = "")
        return ""
    x := x + 0
    if (x < 0)
        return 1.0 - NsNormCdf(-x)
    t := 1.0 / (1.0 + 0.2316419 * x)
    d := 0.3989422804014327 * Exp(-x * x / 2.0)
    p := d * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    return 1.0 - p
}

NsMinutesToYears(minutes) {
    if (minutes = "" || minutes < 0)
        minutes := 0
    return (minutes + 0) / 525600.0    ; 365 * 24 * 60, crypto trades 24/7
}

; Fair probability the underlying finishes strictly above strike at expiry.
NsUpProbability(spot, strike, minutesLeft, volAnnual, driftAnnual := 0.0) {
    if (spot = "" || strike = "" || spot <= 0 || strike <= 0)
        return 0.5
    tau := NsMinutesToYears(minutesLeft)
    if (tau <= 0 || volAnnual = "" || volAnnual <= 0)
        return (spot > strike) ? 1.0 : ((spot < strike) ? 0.0 : 0.5)
    sigSqrtT := volAnnual * Sqrt(tau)
    d2 := (Ln((spot + 0) / (strike + 0)) + (driftAnnual - 0.5 * volAnnual * volAnnual) * tau) / sigSqrtT
    return NsNormCdf(d2)
}

; ---- Volatility -------------------------------------------------------------
NsVolFromTypicalMove(moveFraction, horizonMinutes) {
    if (horizonMinutes <= 0)
        return 0.0
    return (moveFraction + 0) * Sqrt(525600.0 / (horizonMinutes + 0))
}

NsConfiguredVol() {
    global NsTypicalMove15m
    return NsVolFromTypicalMove(NsTypicalMove15m, 15.0)
}

; Feed the underlying spot each poll; maintains a time-stamped ring buffer.
NsUpdateVolSample(asset, price) {
    global NsVolBuffers, NsVolWindowSec
    if (price = "" || price <= 0)
        return
    if !NsVolBuffers.HasKey(asset)
        NsVolBuffers[asset] := []
    buf := NsVolBuffers[asset]
    now := A_TickCount / 1000.0
    buf.Push({ t: now, p: price + 0 })
    while (buf.MaxIndex() != "" && (now - buf[1].t) > NsVolWindowSec)
        buf.RemoveAt(1)
}

; Annualised volatility from the buffered log-returns; falls back to configured.
NsVolAnnual(asset) {
    global NsVolBuffers, NsVolMinSamples
    buf := NsVolBuffers[asset]
    if (!IsObject(buf) || buf.MaxIndex() = "" || buf.MaxIndex() < (NsVolMinSamples + 1))
        return NsConfiguredVol()

    rets := [], sumR := 0.0, sumDt := 0.0, cnt := 0
    Loop % buf.MaxIndex() - 1 {
        i := A_Index
        p0 := buf[i].p, p1 := buf[i + 1].p
        if (p0 > 0 && p1 > 0) {
            r := Ln(p1 / p0)
            rets.Push(r)
            sumR += r
            sumDt += (buf[i + 1].t - buf[i].t)
            cnt++
        }
    }
    if (cnt < NsVolMinSamples)
        return NsConfiguredVol()
    mean := sumR / cnt
    var := 0.0
    for _, r in rets
        var += (r - mean) * (r - mean)
    var := var / (cnt - 1)                 ; sample variance
    perSampleSigma := Sqrt(var)
    avgDt := sumDt / cnt                    ; seconds per sample
    if (avgDt <= 0)
        return NsConfiguredVol()
    barsPerYear := 31536000.0 / avgDt      ; 365 * 24 * 3600
    return perSampleSigma * Sqrt(barsPerYear)
}

; ---- Economics (parity with src/kalshi/economics.py) ------------------------
NsFeePerContract(price) {
    global NsFeeRate
    p := price + 0
    raw := NsFeeRate * p * (1.0 - p)
    return Ceil(raw * 100.0) / 100.0       ; Kalshi rounds up to the next cent
}

NsLegEdge(fairProb, price) {
    return (fairProb - (price + 0)) - NsFeePerContract(price)
}

; ---- Asset spot / strike / vol from NightShark's existing price objects ------
NsRefreshAsset(asset) {
    obj := GetCachedKalshiPriceObj(asset)
    spot := PriceObjCurrent(obj)
    strike := PriceObjOpen(obj)
    if (spot != "")
        NsUpdateVolSample(asset, spot)
    return { spot: spot, strike: strike, vol: NsVolAnnual(asset) }
}

; ---- Pair evaluation: attach fair value + net edge to a pair ----------------
; Returns true if fully valued. Sets pair.fairSum, pair.marketSum, pair.edge and,
; per leg, leg.upProb / leg.fairProb / leg.spotAtEval / leg.strikeAtEval / leg.volAtEval.
NsEvaluatePair(pair, minutesLeft) {
    global NsDriftAnnual
    if (!IsObject(pair) || !IsObject(pair.legs) || pair.legs.MaxIndex() < 2)
        return false

    fairSum := 0.0, feeSum := 0.0
    for _, leg in pair.legs {
        a := NsRefreshAsset(leg.asset)
        if (a.spot = "" || a.strike = "")
            return false                    ; cannot value -> no trade
        pUp := NsUpProbability(a.spot, a.strike, minutesLeft, a.vol, NsDriftAnnual)
        leg.upProb := pUp
        leg.fairProb := (leg.side = "UP") ? pUp : (1.0 - pUp)
        leg.spotAtEval := a.spot
        leg.strikeAtEval := a.strike
        leg.volAtEval := a.vol
        leg.minsAtEval := minutesLeft
        fairSum += leg.fairProb
        feeSum += NsFeePerContract(leg.price)
    }
    pair.fairSum := fairSum
    pair.marketSum := pair.sum
    pair.edge := fairSum - (pair.sum + 0) - feeSum   ; net $/pair-contract after entry fees
    return true
}

; Drop-in replacement for SelectPairEntry: picks the higher-edge inverse pair,
; but only if it clears NsMinEdge. Returns a valued pair object or false.
NsSelectPairEntry(btcSnap, ethSnap) {
    global NsMinEdge, NsAlsoRequireBehaviorSync
    minsLeft := PairMinutesLeft(btcSnap, ethSnap)
    best := false, bestEdge := ""

    for _, name in ["BTC_UP_ETH_DOWN", "BTC_DOWN_ETH_UP"] {
        pair := BuildPairFromName(name, btcSnap, ethSnap)
        if !IsObject(pair)
            continue
        if !NsEvaluatePair(pair, minsLeft)
            continue
        AttachPairBehavior(pair, UpdatePairBehaviorState(btcSnap, ethSnap))  ; keep behavior fields for logging
        if (bestEdge = "" || pair.edge > bestEdge) {
            bestEdge := pair.edge
            best := pair
        }
    }

    if (!IsObject(best) || best.edge < NsMinEdge)
        return false
    if (NsAlsoRequireBehaviorSync && best.HasKey("behaviorState") && best.behaviorState != "SYNC")
        return false
    return best
}

; Drop-in replacement for the signal check.
NsPairEntrySignalMet(pair) {
    global NsMinEdge, NsAlsoRequireBehaviorSync
    if (!IsObject(pair) || !pair.HasKey("edge"))
        return false
    if (!IsObject(pair.legs) || pair.legs.MaxIndex() < 2)
        return false
    if (pair.edge < NsMinEdge)
        return false
    if (NsAlsoRequireBehaviorSync && pair.HasKey("behaviorState") && pair.behaviorState != "SYNC")
        return false
    return true
}

; ---- CSV trade log (schema matches src/kalshi/trade_log.py exactly) ----------
NsTradeLogHeader() {
    return "timestamp,status,ticker,asset,spot,strike,minutes_left,vol_annual,model_prob,market_price,side,edge_per_contract,contracts,cost,outcome,won,payout,pnl"
}

NsEnsureLog() {
    global NsLogPath
    if !FileExist(NsLogPath) {
        header := NsTradeLogHeader() "`n"
        FileAppend, %header%, %NsLogPath%
    }
}

NsIsoUtc() {
    u := CurrentUtcNowAhk()
    if (u = "" || StrLen(u) < 14)
        return ""
    return SubStr(u, 1, 4) "-" SubStr(u, 5, 2) "-" SubStr(u, 7, 2) "T" SubStr(u, 9, 2) ":" SubStr(u, 11, 2) ":" SubStr(u, 13, 2)
}

NsN(x, dp := 6) {
    if (x = "")
        return ""
    return Round(x + 0, dp)
}

NsWriteRow(row) {
    global NsLogPath
    NsEnsureLog()
    line := row "`n"
    FileAppend, %line%, %NsLogPath%
}

; UP -> yes leg, DOWN -> no leg (matches trade_log side convention).
NsSideYesNo(side) {
    return (side = "UP") ? "yes" : "no"
}

; asset up-outcome proxy from the last snapshot (same rule NightShark settles on).
NsAssetUpOutcome(snap) {
    if (!IsObject(snap) || snap.up = "" || snap.down = "")
        return ""
    return ((snap.up + 0) >= (snap.down + 0)) ? 1 : 0
}

; Log one shot's entry (call per shot after the paper buy fills).
; `trade` legs must carry: asset, ticker, side, entry, qty, upProb, fairProb,
;  spotAtEval, strikeAtEval, volAtEval, and pair.minsAtEntry / pair.edge.
NsLogTradeEntry(trade) {
    if (!IsObject(trade) || !IsObject(trade.legs))
        return
    ts := NsIsoUtc()
    for _, leg in trade.legs {
        price := leg.entry + 0
        modelProb := leg.HasKey("upProb") ? leg.upProb : ""              ; Yes-probability
        edge := (modelProb != "") ? NsLegEdge(leg.fairProb, price) : ""
        fee := NsFeePerContract(price) * (leg.qty + 0)
        cost := (leg.qty + 0) * price + fee
        row := ts ",SIGNAL," leg.ticker "," leg.asset ","
            . NsN(leg.HasKey("spotAtEval") ? leg.spotAtEval : "", 2) ","
            . NsN(leg.HasKey("strikeAtEval") ? leg.strikeAtEval : "", 2) ","
            . NsN(leg.HasKey("minsAtEval") ? leg.minsAtEval : "", 4) ","
            . NsN(leg.HasKey("volAtEval") ? leg.volAtEval : "", 6) ","
            . NsN(modelProb, 6) "," NsN(price, 4) "," NsSideYesNo(leg.side) ","
            . NsN(edge, 6) "," (leg.qty + 0) "," NsN(cost, 4) ",,,,"
        NsWriteRow(row)
    }
}

; Log one shot's held-to-resolution settlement (call per trade in RESOLUTION mode).
NsLogTradeSettlement(trade, btcSnap, ethSnap) {
    if (!IsObject(trade) || !IsObject(trade.legs))
        return
    ts := NsIsoUtc()
    for _, leg in trade.legs {
        snap := (leg.asset = "BTC") ? btcSnap : ethSnap
        outcome := NsAssetUpOutcome(snap)                 ; 1 if asset finished up
        if (outcome = "")
            continue
        side := NsSideYesNo(leg.side)
        won := (side = "yes") ? (outcome = 1) : (outcome = 0)
        price := leg.entry + 0
        fee := NsFeePerContract(price) * (leg.qty + 0)
        cost := (leg.qty + 0) * price + fee
        payout := won ? (leg.qty + 0) * 1.0 : 0.0
        modelProb := leg.HasKey("upProb") ? leg.upProb : ""
        row := ts ",SETTLED," leg.ticker "," leg.asset ",,,,,"
            . NsN(modelProb, 6) "," NsN(price, 4) "," side ",,"
            . (leg.qty + 0) "," NsN(cost, 4) "," outcome "," (won ? 1 : 0) ","
            . NsN(payout, 4) "," NsN(payout - cost, 4)
        NsWriteRow(row)
    }
}

; Log one shot's early exit (stop-loss). Outcome is left blank on purpose: an
; early exit is a risk action, not a resolution, so it must NOT pollute model
; calibration. summarize() counts these only toward total realized P&L.
NsLogTradeExit(trade, btcSnap, ethSnap) {
    if (!IsObject(trade) || !IsObject(trade.legs))
        return
    ts := NsIsoUtc()
    for _, leg in trade.legs {
        snap := (leg.asset = "BTC") ? btcSnap : ethSnap
        exitPrice := (leg.side = "UP") ? (IsObject(snap) ? snap.up : "") : (IsObject(snap) ? snap.down : "")
        if (exitPrice = "")
            exitPrice := 0
        entryPrice := leg.entry + 0
        qty := leg.qty + 0
        cost := qty * entryPrice + NsFeePerContract(entryPrice) * qty
        revenue := (exitPrice + 0) * qty - NsFeePerContract(exitPrice) * qty
        modelProb := leg.HasKey("upProb") ? leg.upProb : ""
        row := ts ",EXIT," leg.ticker "," leg.asset ",,,,,"
            . NsN(modelProb, 6) "," NsN(entryPrice, 4) "," NsSideYesNo(leg.side) ",,"
            . qty "," NsN(cost, 4) ",,," NsN(revenue, 4) "," NsN(revenue - cost, 4)
        NsWriteRow(row)
    }
}

; ---- Self-test: verify the math matches the Python model on this machine ----
; Call NsFairValueSelfTest() once at startup (optional). Reference values were
; produced by the repo model; tolerance 1e-4 covers the CDF approximation.
NsFairValueSelfTest() {
    ok := true, msg := ""
    checks := [ [0, 0.5], [1.96, 0.975002], [-1.0, 0.158655] ]
    for _, c in checks {
        got := NsNormCdf(c[1])
        if (Abs(got - c[2]) > 1.0e-4) {
            ok := false
            msg .= "NormCdf(" c[1] ")=" Round(got, 6) " expected " c[2] "`n"
        }
    }
    ; spot, strike, minutes, vol, expected
    ups := [ [100,100,10,0.6, 0.499478], [99,100,7,0.6, 0.000002]
           , [105,100,5,0.6, 1.000000], [100,100,0,0.6, 0.500000]
           , [101,100,0,0.6, 1.000000], [63120,63000,7,0.5616, 0.823158] ]
    for _, c in ups {
        got := NsUpProbability(c[1], c[2], c[3], c[4])
        if (Abs(got - c[5]) > 1.0e-4) {
            ok := false
            msg .= "UpProb(" c[1] "," c[2] "," c[3] "," c[4] ")=" Round(got, 6) " expected " c[5] "`n"
        }
    }
    result := ok ? "NightShark fair-value self-test PASSED" : ("NightShark fair-value self-test FAILED:`n" msg)
    if IsFunc("Log")
        Log(result)
    return ok
}
