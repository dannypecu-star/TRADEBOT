//+------------------------------------------------------------------+
//|                                              TrendFollowing.mq4   |
//|   Donchian-channel breakout trend follower for MetaTrader 4.     |
//|   MT4 twin of TrendFollowing.mq5 / trend_following.py.           |
//|     * ENTER long when close breaks above the highest close of the |
//|       last InpEntryLookback bars.                                 |
//|     * EXIT  when close falls below the lowest low of the last     |
//|       InpExitLookback bars.                                       |
//|   Size = fixed fraction of equity risked to an ATR stop.          |
//+------------------------------------------------------------------+
#property copyright "TRADEBOT"
#property version   "1.00"
#property strict

input int    InpEntryLookback = 55;
input int    InpExitLookback  = 20;
input int    InpAtrPeriod     = 14;
input double InpAtrStopMult   = 3.0;
input double InpRiskPerTrade  = 0.01;
input double InpMaxDrawdown   = 0.30;
input int    InpMagic         = 555010;
input int    InpSlippage      = 30;      // max slippage in points

double equityPeak = 0.0;

int OnInit()
{
   equityPeak = AccountEquity();
   return(INIT_SUCCEEDED);
}

//--- Highest CLOSE over the last `count` closed bars (shift 1..count).
double HighestClose(int count)
{
   int idx = iHighest(_Symbol, _Period, MODE_CLOSE, count, 1);
   if(idx < 0) return(0.0);
   return(iClose(_Symbol, _Period, idx));
}

//--- Lowest LOW over the last `count` closed bars.
double LowestLow(int count)
{
   int idx = iLowest(_Symbol, _Period, MODE_LOW, count, 1);
   if(idx < 0) return(0.0);
   return(iLow(_Symbol, _Period, idx));
}

//--- Is there an open long from THIS EA on THIS symbol?
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

//--- Fixed-fractional lots for a given stop distance (price units).
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

//--- New-bar detection so we act once per closed bar.
datetime lastBar = 0;

void OnTick()
{
   if(Time[0] == lastBar) return;
   lastBar = Time[0];

   double equity = AccountEquity();
   if(equity > equityPeak) equityPeak = equity;
   bool halted = (equityPeak > 0 && (1.0 - equity / equityPeak) >= InpMaxDrawdown);

   int need = MathMax(InpEntryLookback, InpExitLookback) + 2;
   if(Bars < need) return;

   double close = iClose(_Symbol, _Period, 1);
   double upper = HighestClose(InpEntryLookback);
   double lower = LowestLow(InpExitLookback);
   double atr   = iATR(_Symbol, _Period, InpAtrPeriod, 1);

   if(HasOpenPosition())
   {
      if(close <= lower) CloseLongs();
   }
   else if(!halted && atr > 0 && close >= upper)
   {
      double stopDist = InpAtrStopMult * atr;
      double lots     = LotsForRisk(stopDist);
      double sl       = Ask - stopDist;
      if(lots > 0)
         OrderSend(_Symbol, OP_BUY, lots, Ask, InpSlippage, sl, 0,
                   "trend_following", InpMagic, 0, clrGreen);
   }
}
//+------------------------------------------------------------------+
