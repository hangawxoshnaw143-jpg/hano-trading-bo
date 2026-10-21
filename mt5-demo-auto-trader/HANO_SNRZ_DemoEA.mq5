#property strict
#property version   "0.10"
#property description "HANO SNRZ-inspired DEMO prototype; not an official Zindan implementation"

#include <Trade/Trade.mqh>

// SAFETY: This is a research prototype inspired by publicly described SNRZ concepts.
// It is NOT a verified reproduction of Zindan's course rules. Trading stays disabled by default.
input bool            EnableDemoTrading = false;
input ENUM_TIMEFRAMES EntryTimeframe    = PERIOD_M15;
input ENUM_TIMEFRAMES BiasTimeframe     = PERIOD_H1;
input int             StructureLookback = 20;
input int             ATRPeriod         = 14;
input double          RiskPercent       = 1.0;
input double          RewardRiskRatio   = 3.0;
input double          ZoneATRDistance  = 0.75;
input ulong           MagicNumber        = 6211027;
input int             MaxDeviationPts    = 20;

CTrade trade;
int atrHandle = INVALID_HANDLE;
datetime lastEntryBar = 0;

bool IsDemoAccount()
{
   return (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO);
}

bool IsNewEntryBar()
{
   datetime t = iTime(_Symbol, EntryTimeframe, 0);
   if(t == 0 || t == lastEntryBar) return false;
   lastEntryBar = t;
   return true;
}

bool ReadRates(ENUM_TIMEFRAMES tf, int count, MqlRates &rates[])
{
   ArraySetAsSeries(rates, true);
   return (CopyRates(_Symbol, tf, 0, count, rates) == count);
}

// Simple directional structure filter: compares recent and older swing ranges.
// This is a mechanical approximation, not a proprietary SNRZ rule.
int GetStructureBias()
{
   int n = StructureLookback;
   if(n < 10) n = 10;
   MqlRates r[];
   if(!ReadRates(BiasTimeframe, n + 3, r)) return 0;

   double recentHigh = r[2].high, recentLow = r[2].low;
   double olderHigh = r[n/2 + 2].high, olderLow = r[n/2 + 2].low;

   for(int i=2; i<2+n/2; i++)
   {
      if(r[i].high > recentHigh) recentHigh = r[i].high;
      if(r[i].low < recentLow) recentLow = r[i].low;
   }
   for(int i=2+n/2; i<2+n; i++)
   {
      if(r[i].high > olderHigh) olderHigh = r[i].high;
      if(r[i].low < olderLow) olderLow = r[i].low;
   }

   if(recentHigh > olderHigh && recentLow > olderLow) return 1;
   if(recentHigh < olderHigh && recentLow < olderLow) return -1;
   return 0;
}

bool HasOurPosition()
{
   for(int i=PositionsTotal()-1; i>=0; --i)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         return true;
   }
   return false;
}

double NormalizePrice(double p)
{
   return NormalizeDouble(p, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));
}

double CalculateRiskVolume(double entry, double stop)
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney = equity * RiskPercent / 100.0;
   double tickValue = 0.0, tickSize = 0.0;
   double minVol = 0.0, maxVol = 0.0, step = 0.0;

   if(equity <= 0 || riskMoney <= 0 ||
      !SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE, tickValue) ||
      !SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE, tickSize) ||
      !SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN, minVol) ||
      !SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX, maxVol) ||
      !SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP, step) ||
      tickValue <= 0 || tickSize <= 0 || step <= 0)
      return 0.0;

   double distance = MathAbs(entry - stop);
   if(distance <= 0) return 0.0;
   double lossPerLot = (distance / tickSize) * tickValue;
   if(lossPerLot <= 0) return 0.0;

   double lots = riskMoney / lossPerLot;
   // Never round volume upward; skip if risk-sized volume is below broker minimum.
   lots = MathFloor(lots / step) * step;
   int precision = 2;
   if(step < 0.01) precision = 3;
   if(step < 0.001) precision = 4;
   lots = NormalizeDouble(lots, precision);
   if(lots < minVol || lots > maxVol) return 0.0;
   return lots;
}

void OnInitSafetyLog()
{
   Print("HANO SNRZ-inspired prototype. Public-concept approximation only; verify all rules.");
   Print("Demo-only guard active. EnableDemoTrading defaults to false.");
}

int OnInit()
{
   if(RiskPercent <= 0 || RiskPercent > 1.0 ||
      RewardRiskRatio < 1.0 || ATRPeriod < 2 || ZoneATRDistance <= 0)
   {
      Print("Invalid inputs. RiskPercent must be >0 and <=1.0.");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(!IsDemoAccount())
   {
      Print("SAFETY STOP: attach this prototype to a DEMO account only.");
      return INIT_FAILED;
   }

   atrHandle = iATR(_Symbol, EntryTimeframe, ATRPeriod);
   if(atrHandle == INVALID_HANDLE)
   {
      Print("Could not create ATR handle.");
      return INIT_FAILED;
   }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(MaxDeviationPts);
   trade.SetTypeFillingBySymbol(_Symbol);
   OnInitSafetyLog();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(atrHandle != INVALID_HANDLE) IndicatorRelease(atrHandle);
}

void OnTick()
{
   if(!EnableDemoTrading) return;
   if(!IsDemoAccount())
   {
      Print("SAFETY STOP: account is not DEMO.");
      return;
   }
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) ||
      !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ||
      !MQLInfoInteger(MQL_TRADE_ALLOWED))
      return;
   if(!IsNewEntryBar() || HasOurPosition()) return;

   int bias = GetStructureBias();
   if(bias == 0) return;

   MqlRates r[];
   if(!ReadRates(EntryTimeframe, 25, r)) return;

   double atr[];
   ArrayResize(atr, 3);
   ArraySetAsSeries(atr, true);
   if(CopyBuffer(atrHandle, 0, 0, 3, atr) != 3 || atr[1] <= 0) return;

   // Approximate SNRZ confluence:
   // 1) higher-timeframe structure bias;
   // 2) liquidity sweep/reclaim on completed candles;
   // 3) engulfing-style confirmation;
   // 4) three-candle fair-value gap (imbalance);
   // 5) price near a recent range edge (rough reaction/support-resistance zone).
   bool bullishSweep = (r[2].low < r[3].low && r[1].close > r[2].high);
   bool bearishSweep = (r[2].high > r[3].high && r[1].close < r[2].low);
   bool bullishEngulf = (r[1].close > r[1].open && r[2].close < r[2].open &&
                         r[1].close >= r[2].open && r[1].open <= r[2].close);
   bool bearishEngulf = (r[1].close < r[1].open && r[2].close > r[2].open &&
                         r[1].close <= r[2].open && r[1].open >= r[2].close);
   bool bullishFVG = (r[1].low > r[3].high);
   bool bearishFVG = (r[1].high < r[3].low);

   double rangeLow = r[2].low, rangeHigh = r[2].high;
   for(int i=2; i<22; i++)
   {
      if(r[i].low < rangeLow) rangeLow = r[i].low;
      if(r[i].high > rangeHigh) rangeHigh = r[i].high;
   }
   bool nearSupport = (r[1].close - rangeLow) <= atr[1] * ZoneATRDistance;
   bool nearResistance = (rangeHigh - r[1].close) <= atr[1] * ZoneATRDistance;

   bool buySignal = (bias == 1 && bullishSweep && bullishEngulf && bullishFVG && nearSupport);
   bool sellSignal = (bias == -1 && bearishSweep && bearishEngulf && bearishFVG && nearResistance);
   if(!buySignal && !sellSignal) return;

   double ask = 0.0, bid = 0.0;
   if(!SymbolInfoDouble(_Symbol, SYMBOL_ASK, ask) ||
      !SymbolInfoDouble(_Symbol, SYMBOL_BID, bid) || ask <= 0 || bid <= 0) return;

   long stopsPts = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   double minDistance = ((double)stopsPts + 1.0) * _Point;
   double entry = buySignal ? ask : bid;
   double stop = buySignal ? MathMin(r[2].low, r[3].low) : MathMax(r[2].high, r[3].high);
   if(buySignal && entry - stop < minDistance) stop = entry - minDistance;
   if(sellSignal && stop - entry < minDistance) stop = entry + minDistance;

   double riskDistance = MathAbs(entry - stop);
   if(riskDistance <= 0) return;
   double target = buySignal ? entry + riskDistance * RewardRiskRatio
                             : entry - riskDistance * RewardRiskRatio;
   stop = NormalizePrice(stop);
   target = NormalizePrice(target);

   double lots = CalculateRiskVolume(entry, stop);
   if(lots <= 0)
   {
      Print("Skipped: risk-sized volume is below broker minimum or symbol tick data is unavailable.");
      return;
   }

   bool sent = false;
   if(buySignal)
      sent = trade.Buy(lots, _Symbol, 0.0, stop, target, "HANO SNRZ-inspired DEMO BUY");
   else
      sent = trade.Sell(lots, _Symbol, 0.0, stop, target, "HANO SNRZ-inspired DEMO SELL");

   if(!sent)
      PrintFormat("Order rejected. Retcode=%u %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
   else
      PrintFormat("DEMO order submitted. Lots=%.3f SL=%.*f TP=%.*f",
                  lots, _Digits, stop, _Digits, target);
}
