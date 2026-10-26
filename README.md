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
- `WATCH_INTERVAL_SECONDS` — optional market-scan interval; defaults to 30 seconds, minimum 30. This is only the scan cadence, not a timed signal schedule.

## Telegram commands
- `/start`
- `/help`
- `/signal`

## Automatic alerts
The watcher scans market data every 30 seconds by default, but it does **not** send a signal on a timer. It sends an alert only when a newly completed M15 candle confirms a setup. The independent setup detectors are:
- Liquidity-sweep reversal
- 20-candle range breakout / BOS
- Confirmed pullback rejection
- FVG formation with directional-candle confirmation
- Order-block displacement and retest
- PO3-style sweep of the recent H1 range
- Bullish/bearish engulfing with momentum confirmation

Each detector can create its own signal with its own reason, entry, stop loss, and 2R target; a detector does not require the other strategies to agree. A shared risk sanity check and macro-news guard still apply. Signals use completed candles, so a setup is confirmed at candle close; this is not a tick-by-tick WebSocket engine. WAIT is never sent as a trade alert.

The watcher deduplicates by strategy, direction, and candle timestamp in memory. On startup it establishes a baseline and suppresses setups already present, to avoid sending stale/repeated alerts. Because Railway is not configured with persistent storage for watcher state, a restart can reset this in-memory history; historical setups are suppressed during the initial baseline scan.

## Deployment and validation
- Railway is configured as a single-replica worker using `Procfile` (`worker: python main.py`).
- Only one process may poll Telegram updates for this bot token. A `telegram.error.Conflict: terminated by other getUpdates request` means another process is polling with the same token. Check any old Railway/Replit/Codespaces/Termux instance before restarting.
- Runtime health and successful deployment do not prove that signal strategy is profitable. Backtest with historical data and verify alerts in MT5 Demo before relying on them.
- The bot does not place orders or connect to MT5.


## Signal-message safety
- A WAIT result is formatted as `WAIT — NO ACTIVE TRADE`; any associated entry/TP/SL values are explicitly labelled indicative and not confirmed. Do not trade a WAIT plan.
- Confirmed BUY/SELL compact messages continue to show the signal entry, stop loss and take profit.
- A Telegram polling conflict cannot be fixed safely by changing the formatter: stop every other process using the same bot token and keep only one polling instance active.
