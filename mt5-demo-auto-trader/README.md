# HANO MT5 Demo Auto-Trader (isolated draft)

This folder is an isolated starter for testing an automated MT5 Expert Advisor on a **demo account only**. It does not modify the existing Telegram analysis bot on `main`.

## Important platform limit

The Android MT5 app can monitor an account but cannot run an Expert Advisor. The `.mq5` file must be compiled and run inside MetaTrader 5 desktop on Windows, or on a Windows VPS. GitHub Codespaces/Railway Linux containers do not run the Windows MT5 terminal directly. A Windows VPS may cost money; do not buy one until you choose a provider and understand its price.

## Strategy in this starter

- EMA 20 / EMA 50 crossover on M15.
- ATR-based stop loss and take profit.
- Fixed requested volume (default 0.01 lot); trading is skipped if the broker's volume rules do not permit it.
- At most one open position per symbol and magic number.
- Only runs on a broker account that MT5 identifies as DEMO.
- `EnableDemoTrading` is false by default and must be explicitly enabled in the EA inputs.
- This is a simple engineering test strategy, **not** the user's requested final strategy and not evidence of profitability.

## How to test (requires Windows MT5)

1. Open a broker-provided MT5 demo account.
2. On Windows MT5, open MetaEditor and create/copy `HANO_DemoEA.mq5` into `MQL5/Experts/`.
3. Compile it in MetaEditor.
4. Open an EURUSD or XAUUSD chart and choose M15.
5. Attach the EA, enable Algo Trading, and set `EnableDemoTrading=true` in its inputs.
6. Verify the account is DEMO and review every trade in the Strategy Tester and demo account.
7. Do not use on a live account. The EA has an account-mode guard, but always verify the account and settings yourself.

## Files

- `HANO_DemoEA.mq5`: demo-guarded MQL5 Expert Advisor source.
