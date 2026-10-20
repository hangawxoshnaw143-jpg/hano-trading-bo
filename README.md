# HANO BTC AI

Telegram bot for BTC/USD analysis and confirmed-signal alerts. It never places trades automatically.

## Market data and analysis
- Kraken public OHLC data for XBT/USD, displayed as BTC/USD.
- Uses completed candles across H4, H1 and M15.
- EMA20/EMA50 trend, RSI(14), ATR(14), recent support/resistance, market-structure/BOS checks, three-candle FVG, volume versus its 20-candle average, and weighted BUY/SELL scoring.
- Additional heuristic clues: liquidity sweeps, MSS/BOS-style swing breaks, recent opposite-candle Order Block candidates, approximate ADX, and a PO3-style range clue.
- These ICT/SMC-style detections are simplified code heuristics, not canonical or independently validated pattern recognition. PO3 is informational only and is not double-counted as a separate score.
- Entry/SL/TP are indicative calculations from the last completed M15 close and ATR/support/resistance. They are not a guarantee, and entry can differ from live price.

## News filter
- Reads RSS headlines from the Federal Reserve, BLS and CoinDesk, with caching.
- A matching recent macro headline can block BUY/SELL temporarily.
- If every RSS feed is unreachable, the bot explicitly marks analysis as technical-only; it does not silently pretend news was checked.
- RSS headline matching is not a complete economic calendar, does not measure surprise versus consensus, and can miss events. Check the official calendar before trading.

## Railway environment variables
- `TELEGRAM_BOT_TOKEN` — Telegram BotFather token (secret).
- `TELEGRAM_CHAT_ID` — optional numeric chat ID for automatic alerts.
- `WATCH_INTERVAL_SECONDS` — optional polling interval; defaults to 60 seconds, minimum 30.

## Telegram commands
- `/start`
- `/help`
- `/signal`

## Automatic alerts
The watcher checks regularly and sends BUY/SELL only when a new completed M15 candle has a qualifying signal. WAIT is not sent as a trade alert. The watcher suppresses the initial alert after process startup to avoid sending an old signal immediately. Its deduplication baseline is held in memory; after restarts, behavior can repeat or skip an alert depending on candle timing.

## Deployment and validation
- Railway is configured as a single-replica worker using `Procfile` (`worker: python main.py`).
- Only one process may poll Telegram updates for this bot token. A `telegram.error.Conflict: terminated by other getUpdates request` means another process is polling with the same token. Check any old Railway/Replit/Codespaces/Termux instance before restarting.
- Runtime health and successful deployment do not prove that signal strategy is profitable. Backtest with historical data and verify alerts in MT5 Demo before relying on them.
- The bot does not place orders or connect to MT5.


## Signal-message safety
- A WAIT result is formatted as `WAIT — NO ACTIVE TRADE`; any associated entry/TP/SL values are explicitly labelled indicative and not confirmed. Do not trade a WAIT plan.
- Confirmed BUY/SELL compact messages continue to show the signal entry, stop loss and take profit.
- A Telegram polling conflict cannot be fixed safely by changing the formatter: stop every other process using the same bot token and keep only one polling instance active.
