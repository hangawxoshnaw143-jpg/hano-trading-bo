# HANO BTC AI

Telegram bot for BTC/USD analysis. It sends analysis and alerts only; it never places trades.

## Market data and analysis
- Kraken public OHLC data for XBT/USD (displayed accurately as BTC/USD).
- Uses completed candles only, across H4, H1 and M15.
- EMA20/EMA50 trend, RSI(14), ATR(14), simple market-structure / BOS checks, recent support/resistance, three-candle FVG indication, volume versus its 20-candle average, and RSS headlines from CoinDesk and the Federal Reserve.
- BUY/SELL require higher-timeframe and lower-timeframe confluence. Otherwise the bot reports WAIT.
- Entry/SL/TP are illustrative levels calculated from the last completed H1 close and ATR/support/resistance. They are not a guarantee or an execution instruction. Test in demo before relying on them.

## Railway variables
- `TELEGRAM_BOT_TOKEN` — Telegram BotFather token (secret).
- `TELEGRAM_CHAT_ID` — optional numeric chat ID for automatic alerts.
- `WATCH_INTERVAL_SECONDS` — optional polling interval; defaults to 60 seconds, minimum 30.

## Telegram commands
- `/start`
- `/help`
- `/signal`

## Automatic alert behavior
The watcher polls regularly but only considers alerts when a new completed H1 candle appears. It sends an alert only when the directional state changes into BUY or SELL; repeated polling does not itself create repeated alerts. The current alert baseline is kept in memory, so after a process restart the watcher suppresses the initial alert to reduce duplicate messages. A durable database would be needed for full deduplication across restarts.

## Important limitations
This is an experimental rule-based indicator, not a validated profitable strategy. Structure/FVG detection is intentionally simplified, RSS headlines are informational and are not scored as trade signals, and no backtest has yet established performance. Always validate the code, data feed, and strategy before using it for decisions.
