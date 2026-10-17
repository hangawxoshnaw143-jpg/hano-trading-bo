import os
import time
import logging
from datetime import datetime, timezone

import requests
import feedparser
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
# Keep HTTP client request URLs out of runtime logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("hano-btc-ai")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INTERVAL_SECONDS = int(os.getenv("ANALYSIS_INTERVAL_SECONDS", "3600"))
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"

RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.federalreserve.gov/feeds/press_all.xml",
]

def get_candles(interval="1h", limit=120):
    # Use Kraken public candles because Binance returned HTTP 451 on Railway.
    minutes = {"1h": 60, "15m": 15, "4h": 240, "1d": 1440}.get(interval)
    if minutes is None:
        raise ValueError("Unsupported candle interval")
    response = requests.get(
        KRAKEN_OHLC,
        params={"pair": "XBTUSD", "interval": minutes},
        headers={"User-Agent": "HANO-BTC-AI/1.0"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError("Kraken returned an API error")
    result = payload.get("result", {})
    pair_key = next((key for key in result if key != "last"), None)
    if not pair_key:
        raise RuntimeError("Kraken returned no BTC candle data")
    rows = result[pair_key]
    closes = [float(row[4]) for row in rows[:-1]]
    if len(closes) < 60:
        raise RuntimeError("Not enough completed BTC candles returned")
    return closes[-limit:]

def ema(values, period):
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result

def rsi(values, period=14):
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    recent = changes[-period:]
    gains = sum(max(x, 0) for x in recent) / period
    losses = sum(max(-x, 0) for x in recent) / period
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

def headlines(limit=5):
    items = []
    for url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:3]:
                title = getattr(entry, "title", "").strip()
                if title:
                    items.append(title)
        except Exception as exc:
            log.warning("RSS unavailable: %s", exc)
    return items[:limit]

def analyze():
    prices = get_candles("1h")
    price = prices[-1]
    e20, e50 = ema(prices[-80:], 20), ema(prices[-100:], 50)
    strength = rsi(prices)
    if e20 > e50 and strength >= 55:
        direction = "BUY"
        reason = "EMA20 بالای EMA50 ـە و RSI پشتگیریی بەرەو سەرەوە دەکات."
    elif e20 < e50 and strength <= 45:
        direction = "SELL"
        reason = "EMA20 لە ژێر EMA50 ـە و RSI پشتگیریی بەرەو خوارەوە دەکات."
    else:
        direction = "WAIT"
        reason = "مەرجەکانی پشتڕاستکردنەوە تەواو نین؛ سیگناڵی BUY/SELL نییە."
    news = headlines()
    news_text = "\n".join("• " + item for item in news) if news else "هەواڵی RSS بەردەست نییە."
    return (
        "🤖 HANO BTC AI — BTC/USDT\n"
        f"🕒 کاتی UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"
        f"💵 نرخی کۆتایی کەندڵی تەواوبوو: ${price:,.2f}\n"
        f"📈 EMA20: ${e20:,.2f} | EMA50: ${e50:,.2f}\n"
        f"📊 RSI(14): {strength:.1f}\n"
        f"🎯 ئاراستە: {direction}\n"
        f"🧠 هۆکار: {reason}\n\n"
        "📰 هەواڵی فەندەمێنتاڵ (سەرچاوەکان: CoinDesk و Federal Reserve):\n"
        f"{news_text}\n\n"
        "⚠️ ئەمە شیکارییە، نەک ڕاوێژی دارایی. Entry/SL/TP تەنها کاتێک دیاری دەکرێن کە یاساکانی ڕیسک بە تەواوی جێبەجێ بن. هیچ مامەڵەیەک ئۆتۆماتیکی ناکرێت."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "بەخێربێیت بۆ HANO BTC AI.\n"
        "/signal — شیکاری ئێستا\n"
        "/help — ڕێنمایی\n"
        "ئەم بۆتە تەنها شیکاری و سیگناڵ دەدات و مامەڵە ناکات."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "فەرمانەکان:\n/start — دەستپێکردن\n/signal — شیکاری BTC\n"
        "سیستەمەکە کەندڵی تەواوبووی H1 بەکاردەهێنێت و هەواڵی RSS دەخوێنێتەوە."
    )

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("شیکاری دەکرێت، تکایە چاوەڕێ بکە...")
    try:
        await update.message.reply_text(analyze())
    except Exception as exc:
        log.exception("analysis failed")
        await update.message.reply_text("ئێستا شیکاری بەردەست نییە. دواتر دووبارە هەوڵ بدە.")

async def hourly_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID:
        return
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text=analyze())
    except Exception:
        log.exception("hourly analysis failed")

def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("signal", signal))
    if CHAT_ID:
        app.job_queue.run_repeating(hourly_job, interval=INTERVAL_SECONDS, first=15)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
