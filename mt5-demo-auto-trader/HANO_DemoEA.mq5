#property strict
#property version   "1.00"
#property description "HANO demo-only EMA crossover EA with ATR stops"

#include <Trade/Trade.mqh>

input bool            EnableDemoTrading = false; // Must be enabled manually; DEMO account only
input ENUM_TIMEFRAMES SignalTimeframe   = PERIOD_M15;
input int             FastEMAPeriod     = 20;
input int             SlowEMAPeriod     = 50;
input int             ATRPeriod         = 14;
input double          RequestedLots     = 0.01;
input double          StopATRMultiple   = 1.5;
input double          TargetATRMultiple = 2.0;
input ulong           MagicNumber       = 6211026;
input int             MaxDeviationPts   = 20;

CTrade trade;
int fastHandle = INVALID_HANDLE;
int slowHandle = INVALID_HANDLE;
int atrHandle  = INVALID_HANDLE;
datetime lastBarTime = 0;

bool IsDemoAccount()
{
   return (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO);
}

double NormalizePrice(const double price)
{
   const int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   return NormalizeDouble(price, digits);
}

double GetAllowedVolume()
{
   double minVolume = 0.0, maxVolume = 0.0, step = 0.0;
   if(!SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN, minVolume) ||
      !SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX, maxVolume) ||
      !SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP, step) ||
      step <= 0.0)
      return 0.0;

   if(RequestedLots < minVolume || RequestedLots > maxVolume)
      return 0.0; // Never round the requested size upward.

   double volume = MathFloor((RequestedLots + 1e-10) / step) * step;
   int precision = 2;
   if(step < 0.01) precision = 3;
   if(step < 0.001) precision = 4;
   volume = NormalizeDouble(volume, precision);

   if(volume < minVolume || volume > maxVolume)
      return 0.0;
   return volume;
}

bool HasOurPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         return true;
   }
   return false;
}

bool IsNewBar()
{
   datetime currentBar = iTime(_Symbol, SignalTimeframe, 0);
   if(currentBar == 0 || currentBar == lastBarTime)
      return false;
   lastBarTime = currentBar;
   return true;
}

int OnInit()
{
   if(FastEMAPeriod < 2 || SlowEMAPeriod <= FastEMAPeriod ||
      ATRPeriod < 2 || RequestedLots <= 0.0 ||
      StopATRMultiple <= 0.0 || TargetATRMultiple <= 0.0)
   {
      Print("Invalid EA inputs.");
      return INIT_PARAMETERS_INCORRECT;
   }

   if(!IsDemoAccount())
   {
      Print("SAFETY STOP: this EA only runs on an MT5 DEMO account.");
      return INIT_FAILED;
   }

   fastHandle = iMA(_Symbol, SignalTimeframe, FastEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   slowHandle = iMA(_Symbol, SignalTimeframe, SlowEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   atrHandle  = iATR(_Symbol, SignalTimeframe, ATRPeriod);

   if(fastHandle == INVALID_HANDLE || slowHandle == INVALID_HANDLE || atrHandle == INVALID_HANDLE)
   {
      Print("Could not create indicator handles.");
      return INIT_FAILED;
   }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(MaxDeviationPts);
   Print("HANO Demo EA initialized. Confirm DEMO account and keep live trading disabled.");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(fastHandle != INVALID_HANDLE) IndicatorRelease(fastHandle);
   if(slowHandle != INVALID_HANDLE) IndicatorRelease(slowHandle);
   if(atrHandle  != INVALID_HANDLE) IndicatorRelease(atrHandle);
}

void OnTick()
{
   if(!EnableDemoTrading)
      return;

   if(!IsDemoAccount())
   {
      Print("SAFETY STOP: account is not DEMO; no new trades will be placed.");
      return;
   }

   if(!TerminalInfoInteger(TERMINAL_CONNECTED) ||
      !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ||
      !MQLInfoInteger(MQL_TRADE_ALLOWED))
      return;

   if(!IsNewBar() || HasOurPosition())
      return;

   double fast[3], slow[3], atr[3];
   ArraySetAsSeries(fast, true);
   ArraySetAsSeries(slow, true);
   ArraySetAsSeries(atr, true);

   if(CopyBuffer(fastHandle, 0, 0, 3, fast) != 3 ||
      CopyBuffer(slowHandle, 0, 0, 3, slow) != 3 ||
      CopyBuffer(atrHandle, 0, 0, 3, atr) != 3)
   {
      Print("Indicator data not ready.");
      return;
   }

   // Use completed candles only: index 1 is the latest completed bar; index 2 is the prior bar.
   bool buySignal  = (fast[2] <= slow[2] && fast[1] > slow[1]);
   bool sellSignal = (fast[2] >= slow[2] && fast[1] < slow[1]);
   if(!buySignal && !sellSignal)
      return;

   double volume = GetAllowedVolume();
   if(volume <= 0.0)
   {
      Print("Trade skipped: requested lot size is not permitted by this symbol's volume rules.");
      return;
   }

   double atrValue = atr[1];
   if(atrValue <= 0.0)
      return;

   double ask = 0.0, bid = 0.0;
   if(!SymbolInfoDouble(_Symbol, SYMBOL_ASK, ask) ||
      !SymbolInfoDouble(_Symbol, SYMBOL_BID, bid) || ask <= 0.0 || bid <= 0.0)
      return;

   long stopsLevelPoints = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   double minStopDistance = (double)stopsLevelPoints * _Point;
   double slDistance = MathMax(atrValue * StopATRMultiple, minStopDistance + _Point);
   double tpDistance = MathMax(atrValue * TargetATRMultiple, minStopDistance + _Point);

   bool sent = false;
   if(buySignal)
   {
      double sl = NormalizePrice(ask - slDistance);
      double tp = NormalizePrice(ask + tpDistance);
      sent = trade.Buy(volume, _Symbol, 0.0, sl, tp, "HANO demo EMA BUY");
   }
   else if(sellSignal)
   {
      double sl = NormalizePrice(bid + slDistance);
      double tp = NormalizePrice(bid - tpDistance);
      sent = trade.Sell(volume, _Symbol, 0.0, sl, tp, "HANO demo EMA SELL");
   }

   if(!sent)
      PrintFormat("Order not placed. Retcode=%u, %s",
                  trade.ResultRetcode(), trade.ResultRetcodeDescription());
   else
      PrintFormat("Demo order submitted. Retcode=%u, %s",
                  trade.ResultRetcode(), trade.ResultRetcodeDescription());
}
