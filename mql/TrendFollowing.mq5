//+------------------------------------------------------------------+
//|                                              TrendFollowing.mq5   |
//|   Donchian-channel breakout trend follower for MetaTrader 5.     |
//|                                                                  |
//|   This is the native-terminal twin of the Python strategy in     |
//|   src/strategies/trend_following.py. Same rules, same intent, so  |
//|   a Python backtest is a fair preview of this EA's behaviour:     |
//|     * ENTER long when the close breaks above the highest close    |
//|       of the last InpEntryLookback bars (a fresh breakout).       |
//|     * EXIT  when the close falls below the lowest low of the last |
//|       InpExitLookback bars (trend exhaustion).                    |
//|   A shorter exit channel than entry channel lets winners run and  |
//|   cuts losers quickly -- the asymmetry trend following lives on.  |
//|                                                                  |
//|   Risk: position size is derived from a fixed fraction of equity  |
//|   risked to an ATR-based stop, mirroring src/risk/manager.py.     |
//+------------------------------------------------------------------+
#property copyright "TRADEBOT"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

//--- Inputs (keep these identical to the Python config for comparable results)
input int    InpEntryLookback = 55;     // breakout channel length (bars)
input int    InpExitLookback  = 20;     // exit channel length (bars)
input int    InpAtrPeriod     = 14;     // ATR period for the protective stop
input double InpAtrStopMult   = 3.0;    // stop distance = mult * ATR
input double InpRiskPerTrade  = 0.01;   // fraction of equity risked to the stop
input double InpMaxDrawdown   = 0.30;   // halt new entries after this equity drawdown
input ulong  InpMagic         = 555010; // tags this EA's orders in the terminal

CTrade   trade;
int      atrHandle;
double   equityPeak = 0.0;

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);
   atrHandle = iATR(_Symbol, _Period, InpAtrPeriod);
   if(atrHandle == INVALID_HANDLE)
   {
      Print("Failed to create ATR handle");
      return(INIT_FAILED);
   }
   equityPeak = AccountInfoDouble(ACCOUNT_EQUITY);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) { IndicatorRelease(atrHandle); }

//+------------------------------------------------------------------+
//| Highest close over [1..count] (excludes the still-forming bar 0). |
//+------------------------------------------------------------------+
double HighestClose(int count)
{
   double c[];
   if(CopyClose(_Symbol, _Period, 1, count, c) < count) return(0.0);
   double hi = c[0];
   for(int i = 1; i < count; i++) if(c[i] > hi) hi = c[i];
   return(hi);
}

//+------------------------------------------------------------------+
//| Lowest low over [1..count].                                       |
//+------------------------------------------------------------------+
double LowestLow(int count)
{
   double l[];
   if(CopyLow(_Symbol, _Period, 1, count, l) < count) return(0.0);
   double lo = l[0];
   for(int i = 1; i < count; i++) if(l[i] < lo) lo = l[i];
   return(lo);
}

double CurrentAtr()
{
   double a[];
   if(CopyBuffer(atrHandle, 0, 0, 1, a) < 1) return(0.0);
   return(a[0]);
}

bool HasOpenPosition()
{
   return(PositionSelect(_Symbol) && PositionGetInteger(POSITION_MAGIC) == (long)InpMagic);
}

//+------------------------------------------------------------------+
//| Fixed-fractional position size: risk InpRiskPerTrade of equity to |
//| the ATR stop distance, converted to lots for this symbol.         |
//+------------------------------------------------------------------+
double LotsForRisk(double stopDistance)
{
   if(stopDistance <= 0) return(0.0);
   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash = equity * InpRiskPerTrade;

   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickValue <= 0 || tickSize <= 0) return(0.0);

   double lossPerLot = (stopDistance / tickSize) * tickValue;
   if(lossPerLot <= 0) return(0.0);
   double lots = riskCash / lossPerLot;

   // Clamp to the symbol's allowed volume range and step.
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   lots = MathFloor(lots / stepLot) * stepLot;
   lots = MathMax(minLot, MathMin(maxLot, lots));
   return(lots);
}

//+------------------------------------------------------------------+
//| Trade once per fully-closed bar (no intrabar churn / repainting). |
//+------------------------------------------------------------------+
void OnTick()
{
   static datetime lastBar = 0;
   datetime thisBar = (datetime)SeriesInfoInteger(_Symbol, _Period, SERIES_LASTBAR_DATE);
   if(thisBar == lastBar) return;         // act only when a new bar has closed
   lastBar = thisBar;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(equity > equityPeak) equityPeak = equity;
   bool halted = (equityPeak > 0 && (1.0 - equity / equityPeak) >= InpMaxDrawdown);

   int need = MathMax(InpEntryLookback, InpExitLookback) + 2;
   if(Bars(_Symbol, _Period) < need) return;

   double close = iClose(_Symbol, _Period, 1);   // last closed bar's close
   double upper = HighestClose(InpEntryLookback);
   double lower = LowestLow(InpExitLookback);
   double atr   = CurrentAtr();

   if(HasOpenPosition())
   {
      // Exit on a break of the lower channel (trend exhaustion).
      if(close <= lower)
         trade.PositionClose(_Symbol);
   }
   else if(!halted && atr > 0 && close >= upper)
   {
      // Enter long on the breakout, sized to the ATR stop.
      double stopDist = InpAtrStopMult * atr;
      double lots     = LotsForRisk(stopDist);
      double ask      = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double sl       = ask - stopDist;
      if(lots > 0)
         trade.Buy(lots, _Symbol, ask, sl, 0.0, "trend_following");
   }
}
//+------------------------------------------------------------------+
