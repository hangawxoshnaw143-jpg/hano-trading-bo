# HANO BTC AI

Telegram bot for BTC/USD analysis and confirmed-signal alerts. It never places trades automatically.

## Market data and strategy
- Kraken public OHLC data for XBT/USD, displayed as BTC/USD.
- Uses completed candles across H4, H1 and M15.
- The primary M15 trigger is the supplied **Liquidity Entry Zones** workflow:
  1. Store recent confirmed swing highs/lows as liquidity levels.
  2. Detect a sweep beyond a stored level by at least 0.10 ATR.
  3. Require reclaim/close back inside the level.
  4. Require wick >= 35%, body <= 65%, and candle range >= 0.20 ATR.
  5. Keep the sweep valid for up to 2 completed candles.
  6. Require directional candle body, price on the correct side of EMA50, and a break of the sweep candle midpoint.
  7. Entry = completed M15 close; SL = 1.5 ATR; TP = 3R.
- H4/H1 provide higher-timeframe context. FVG, MSS/BOS and PO3 remain supporting context rather than automatic standalone entries.
- Signals are confirmed on completed candles only. WAIT is not a trade alert.

## News filter
- Reads RSS headlines from the Federal Reserve, BLS and CoinDesk, with caching.
- A matching recent macro headline can block BUY/SELL temporarily.
- If every RSS feed is unreachable, the bot explicitly marks analysis as technical-only.
- RSS headline matching is not a complete economic calendar and does not measure surprise versus consensus.

## Railway environment variables
- `TELEGRAM_BOT_TOKEN` — Telegram BotFather token (secret).
- `TELEGRAM_CHAT_ID` — optional numeric chat ID for automatic alerts.
- `WATCH_INTERVAL_SECONDS` — optional polling interval; defaults to 60 seconds, minimum 30.

## Telegram commands
- `/start`
- `/help`
- `/signal`

## Automatic alerts
The watcher checks regularly and sends BUY/SELL only when a new completed M15 candle has a qualifying Liquidity Entry Zones setup. WAIT is not sent as a trade alert. The first scan after startup establishes a baseline and does not send an old signal.

## Deployment and validation
- Railway is configured as a single-replica worker using `Procfile` (`worker: python main.py`).
- Only one process may poll Telegram updates for this bot token. A `telegram.error.Conflict: terminated by other getUpdates request` means another process is polling with the same token.
- Runtime health and successful deployment do not prove that the strategy is profitable. Backtest and verify alerts on MT5 Demo before relying on them.
- The bot does not place orders or connect to MT5.
