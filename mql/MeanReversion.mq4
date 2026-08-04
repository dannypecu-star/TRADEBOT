//+------------------------------------------------------------------+
//|                                               MeanReversion.mq4   |
//|   Bollinger / z-score mean-reversion EA for MetaTrader 4.        |
//|   MT4 twin of MeanReversion.mq5 / mean_reversion.py:             |
//|     z = (close - SMA) / StdDev over InpLookback bars.             |
//|     * ENTER long when z <= -InpEntryZ.                            |
//|     * EXIT       when z >= -InpExitZ.                             |
//|   Optional long-term SMA regime filter avoids buying downtrends.  |
//+------------------------------------------------------------------+
#property copyright "TRADEBOT"
#property version   "1.00"
#property strict

input int    InpLookback      = 20;
input double InpEntryZ         = 2.0;
input double InpExitZ          = 0.5;
input int    InpTrendFilter    = 200;
input bool   InpUseTrendFilter = true;
input int    InpAtrPeriod      = 14;
input double InpAtrStopMult    = 3.0;
input double InpRiskPerTrade   = 0.01;
input double InpMaxDrawdown    = 0.30;
input int    InpMagic          = 555011;
input int    InpSlippage       = 30;

double equityPeak = 0.0;

int OnInit()
{
   equityPeak = AccountEquity();
   return(INIT_SUCCEEDED);
}

//--- z-score of the last closed bar. iBands gives the mean (MODE_MAIN) and, with a
//--- 1.0 deviation, the upper band = mean + 1*stddev, so stddev = upper - mean.
bool CurrentZ(double &z)
{
   double mean = iBands(_Symbol, _Period, InpLookback, 1.0, 0, PRICE_CLOSE, MODE_MAIN, 1);
   double up   = iBands(_Symbol, _Period, InpLookback, 1.0, 0, PRICE_CLOSE, MODE_UPPER, 1);
   double std  = up - mean;
   if(std <= 0) return(false);
   z = (iClose(_Symbol, _Period, 1) - mean) / std;
   return(true);
}

bool HasOpenPosition()
{
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol() == _Symbol && OrderMagicNumber() == InpMagic && OrderType() == OP_BUY)
         return(true);
   }
   return(false);
}

void CloseLongs()
{
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol() == _Symbol && OrderMagicNumber() == InpMagic && OrderType() == OP_BUY)
         OrderClose(OrderTicket(), OrderLots(), Bid, InpSlippage, clrRed);
   }
}

double LotsForRisk(double stopDistance)
{
   if(stopDistance <= 0) return(0.0);
   double riskCash  = AccountEquity() * InpRiskPerTrade;
   double tickValue = MarketInfo(_Symbol, MODE_TICKVALUE);
   double tickSize  = MarketInfo(_Symbol, MODE_TICKSIZE);
   if(tickValue <= 0 || tickSize <= 0) return(0.0);
   double lossPerLot = (stopDistance / tickSize) * tickValue;
   if(lossPerLot <= 0) return(0.0);
   double lots = riskCash / lossPerLot;
   double minLot  = MarketInfo(_Symbol, MODE_MINLOT);
   double maxLot  = MarketInfo(_Symbol, MODE_MAXLOT);
   double stepLot = MarketInfo(_Symbol, MODE_LOTSTEP);
   lots = MathFloor(lots / stepLot) * stepLot;
   return(MathMax(minLot, MathMin(maxLot, lots)));
}

datetime lastBar = 0;

void OnTick()
{
   if(Time[0] == lastBar) return;
   lastBar = Time[0];

   double equity = AccountEquity();
   if(equity > equityPeak) equityPeak = equity;
   bool halted = (equityPeak > 0 && (1.0 - equity / equityPeak) >= InpMaxDrawdown);

   double z;
   if(!CurrentZ(z)) return;
   double close = iClose(_Symbol, _Period, 1);

   if(HasOpenPosition())
   {
      if(z >= -InpExitZ) CloseLongs();
   }
   else if(!halted && z <= -InpEntryZ)
   {
      bool regimeOk = true;
      if(InpUseTrendFilter)
      {
         double sma = iMA(_Symbol, _Period, InpTrendFilter, 0, MODE_SMA, PRICE_CLOSE, 1);
         regimeOk = (close > sma);
      }
      if(!regimeOk) return;

      double atr = iATR(_Symbol, _Period, InpAtrPeriod, 1);
      if(atr <= 0) return;
      double stopDist = InpAtrStopMult * atr;
      double lots     = LotsForRisk(stopDist);
      double sl       = Ask - stopDist;
      if(lots > 0)
         OrderSend(_Symbol, OP_BUY, lots, Ask, InpSlippage, sl, 0,
                   "mean_reversion", InpMagic, 0, clrGreen);
   }
}
//+------------------------------------------------------------------+
