# HANO MT5 Demo Auto-Trader (isolated draft)

This folder is isolated from the existing Telegram bot and is for **demo testing only**.

## Important platform limit

The MetaTrader 5 Android app cannot run custom Expert Advisors. A `.mq5` source file must be compiled with MetaEditor and run in the desktop MT5 terminal on Windows, or on a compatible Windows VPS. Do not buy hosting until the cost and setup are understood.

## Files

- `HANO_DemoEA.mq5`: older EMA 20/50 + ATR example. It is **not** the requested SNRZ strategy.
- `HANO_SNRZ_DemoEA.mq5`: new SNRZ-inspired research prototype. It combines a simple higher-timeframe structure filter with approximate liquidity-sweep, engulfing, FVG/imbalance and range-edge checks. This is **not a verified reproduction of Zindan's full course rules**; the exact proprietary entry criteria are not available in the public summary.

## Safety and testing

- Both files require an MT5 DEMO account and default to `EnableDemoTrading=false`.
- The new prototype limits the input risk percentage to 1% or less, but actual loss can differ because of slippage, gaps, execution and symbol contract details.
- The code has been committed to GitHub but has **not yet been compiled or backtested**. Do not enable it for live trading.
- Before any demo execution: compile in MetaEditor, resolve every compiler warning/error, backtest across multiple market periods, inspect trades, then run on demo only.

## Desktop installation steps (Windows MT5 required)

1. Download `HANO_SNRZ_DemoEA.mq5` from the GitHub branch.
2. In MT5 desktop, open MetaEditor and place the file in `MQL5/Experts/`.
3. Compile with F7 and confirm there are no errors.
4. Run Strategy Tester first, then use a demo account only.
5. Keep `EnableDemoTrading=false` until compilation and testing are complete.

This README does not claim that the EA has been installed or tested.
