//+------------------------------------------------------------------+
//|                                               MeanReversion.mq5   |
//|   Bollinger / z-score mean-reversion EA for MetaTrader 5.        |
//|                                                                  |
//|   Native-terminal twin of src/strategies/mean_reversion.py:      |
//|     z = (close - SMA(lookback)) / StdDev(lookback)               |
//|     * ENTER long when z <= -InpEntryZ  (price stretched below).   |
//|     * EXIT       when z >= -InpExitZ    (reverted toward mean).    |
//|   An optional long-term SMA regime filter keeps us from buying    |
//|   into a structural downtrend (the classic "falling knife" trap). |
//|                                                                  |
//|   Mean reversion earns its keep in range-bound markets and lags   |
//|   in strong trends -- run it alongside TrendFollowing.mq5, not    |
//|   instead of it.                                                  |
//+------------------------------------------------------------------+
#property copyright "TRADEBOT"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

input int    InpLookback     = 20;      // window for the mean/stddev (z-score)
input double InpEntryZ        = 2.0;    // enter when z <= -InpEntryZ
input double InpExitZ         = 0.5;    // exit  when z >= -InpExitZ
input int    InpTrendFilter   = 200;    // long-term SMA regime filter length
input bool   InpUseTrendFilter= true;   // require price above the regime SMA to enter
input int    InpAtrPeriod     = 14;     // ATR period for the protective stop
input double InpAtrStopMult   = 3.0;    // stop distance = mult * ATR
input double InpRiskPerTrade  = 0.01;   // fraction of equity risked to the stop
input double InpMaxDrawdown   = 0.30;   // halt new entries after this equity drawdown
input ulong  InpMagic         = 555011; // tags this EA's orders in the terminal

CTrade trade;
int    bbHandle;    // Bollinger Bands give us mean +/- k*stddev directly
int    smaHandle;   // regime filter
int    atrHandle;
double equityPeak = 0.0;

int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);
   // Bollinger Bands with 1 std dev let us recover mean and stddev, hence z.
   bbHandle  = iBands(_Symbol, _Period, InpLookback, 0, 1.0, PRICE_CLOSE);
   smaHandle = iMA(_Symbol, _Period, InpTrendFilter, 0, MODE_SMA, PRICE_CLOSE);
   atrHandle = iATR(_Symbol, _Period, InpAtrPeriod);
   if(bbHandle == INVALID_HANDLE || smaHandle == INVALID_HANDLE || atrHandle == INVALID_HANDLE)
      return(INIT_FAILED);
   equityPeak = AccountInfoDouble(ACCOUNT_EQUITY);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   IndicatorRelease(bbHandle);
   IndicatorRelease(smaHandle);
   IndicatorRelease(atrHandle);
}

//+------------------------------------------------------------------+
//| Recover the z-score of the last closed bar from Bollinger Bands.  |
//| BB with 1 stddev: upper = mean + std, so std = upper - mean, and  |
//| z = (close - mean) / std.                                         |
//+------------------------------------------------------------------+
bool CurrentZ(double &z)
{
   double mid[], up[];
   if(CopyBuffer(bbHandle, 0, 1, 1, mid) < 1) return(false);  // base (mean)
   if(CopyBuffer(bbHandle, 1, 1, 1, up)  < 1) return(false);  // upper band
   double mean = mid[0];
   double std  = up[0] - mean;
   if(std <= 0) return(false);
   double close = iClose(_Symbol, _Period, 1);
   z = (close - mean) / std;
   return(true);
}

double CurrentSma()
{
   double s[];
   if(CopyBuffer(smaHandle, 0, 1, 1, s) < 1) return(0.0);
   return(s[0]);
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
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   lots = MathFloor(lots / stepLot) * stepLot;
   return(MathMax(minLot, MathMin(maxLot, lots)));
}

void OnTick()
{
   static datetime lastBar = 0;
   datetime thisBar = (datetime)SeriesInfoInteger(_Symbol, _Period, SERIES_LASTBAR_DATE);
   if(thisBar == lastBar) return;
   lastBar = thisBar;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(equity > equityPeak) equityPeak = equity;
   bool halted = (equityPeak > 0 && (1.0 - equity / equityPeak) >= InpMaxDrawdown);

   double z;
   if(!CurrentZ(z)) return;
   double close = iClose(_Symbol, _Period, 1);

   if(HasOpenPosition())
   {
      if(z >= -InpExitZ)            // reverted back toward the mean -> take profit/exit
         trade.PositionClose(_Symbol);
   }
   else if(!halted && z <= -InpEntryZ)
   {
      bool regimeOk = true;
      if(InpUseTrendFilter)
         regimeOk = (close > CurrentSma());
      if(!regimeOk) return;

      double atr = CurrentAtr();
      if(atr <= 0) return;
      double stopDist = InpAtrStopMult * atr;
      double lots     = LotsForRisk(stopDist);
      double ask      = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double sl       = ask - stopDist;
      if(lots > 0)
         trade.Buy(lots, _Symbol, ask, sl, 0.0, "mean_reversion");
   }
}
//+------------------------------------------------------------------+
