# HANO BTC AI

Telegram bot for BTC/USDT technical analysis and RSS-based fundamental headlines. It sends analysis only and never places trades.

## Railway variables
- `TELEGRAM_BOT_TOKEN` — BotFather token (secret)
- `TELEGRAM_CHAT_ID` — optional numeric Telegram chat ID for scheduled hourly alerts
- `ANALYSIS_INTERVAL_SECONDS` — optional; defaults to `3600`

## Commands
- `/start`
- `/help`
- `/signal`

The bot uses completed 1-hour Binance BTCUSDT candles and EMA20/EMA50/RSI(14) for a simple directional filter. This is an early scaffold, not a validated trading strategy. RSS headlines are informational and are not automatically converted into a fundamental trading score.
