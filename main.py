import os
import logging
from datetime import datetime, timezone

import requests
import feedparser
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("hano-btc-ai")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WATCH_INTERVAL = max(30, int(os.getenv("WATCH_INTERVAL_SECONDS", "60")))
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.federalreserve.gov/feeds/press_all.xml",
]
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "HANO-BTC-AI/2.0"})
HTTP_TIMEOUT = 15


def get_candles(interval="1h", limit=180):
    """Return completed Kraken XBT/USD candles; newest API row is still open."""
    minutes = {"15m": 15, "1h": 60, "4h": 240}.get(interval)
    if minutes is None:
        raise ValueError("Unsupported candle interval")
    response = HTTP.get(
        KRAKEN_OHLC,
        params={"pair": "XBTUSD", "interval": minutes},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError("Kraken returned an API error")
    result = payload.get("result", {})
    pair_key = next((key for key in result if key != "last"), None)
    if not pair_key:
        raise RuntimeError("Kraken returned no BTC candle data")
    rows = result[pair_key][:-1]
    candles = [
        {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
        }
        for row in rows[-limit:]
    ]
    if len(candles) < 60:
        raise RuntimeError("Not enough completed BTC candles returned")
    return candles


def ema(values, period):
    if len(values) < period:
        raise ValueError("Not enough values for EMA")
    alpha = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    for item in values[period:]:
        value = alpha * item + (1 - alpha) * value
    return value


def rsi(values, period=14):
    if len(values) <= period:
        raise ValueError("Not enough values for RSI")
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = ((period - 1) * avg_gain + gains[i]) / period
        avg_loss = ((period - 1) * avg_loss + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles, period=14):
    if len(candles) <= period:
        raise ValueError("Not enough candles for ATR")
    ranges = []
    for i in range(1, len(candles)):
        candle = candles[i]
        prev_close = candles[i - 1]["close"]
        ranges.append(max(
            candle["high"] - candle["low"],
            abs(candle["high"] - prev_close),
            abs(candle["low"] - prev_close),
        ))
    value = sum(ranges[:period]) / period
    for item in ranges[period:]:
        value = ((period - 1) * value + item) / period
    return value


def market_structure(candles, lookback=20):
    recent = candles[-(lookback + 1):-1]
    last = candles[-1]
    resistance = max(c["high"] for c in recent)
    support = min(c["low"] for c in recent)
    if last["close"] > resistance:
        return "BULLISH BOS", support, resistance
    if last["close"] < support:
        return "BEARISH BOS", support, resistance
    old = candles[-40:-20]
    new = candles[-20:]
    old_high, new_high = max(c["high"] for c in old), max(c["high"] for c in new)
    old_low, new_low = min(c["low"] for c in old), min(c["low"] for c in new)
    if new_high > old_high and new_low > old_low:
        return "BULLISH STRUCTURE", support, resistance
    if new_high < old_high and new_low < old_low:
        return "BEARISH STRUCTURE", support, resistance
    return "RANGE / NEUTRAL", support, resistance


def detect_fvg(candles):
    if len(candles) < 3:
        return "N/A"
    a, c = candles[-3], candles[-1]
    if c["low"] > a["high"]:
        return "BULLISH FVG: " + format(a["high"], ",.2f") + " - " + format(c["low"], ",.2f")
    if c["high"] < a["low"]:
        return "BEARISH FVG: " + format(c["high"], ",.2f") + " - " + format(a["low"], ",.2f")
    return "No new FVG"


def timeframe_summary(candles):
    closes = [c["close"] for c in candles]
    e20 = ema(closes[-100:], 20)
    e50 = ema(closes[-120:], 50)
    strength = rsi(closes)
    if e20 > e50 and closes[-1] > e20:
        trend = "BULLISH"
    elif e20 < e50 and closes[-1] < e20:
        trend = "BEARISH"
    else:
        trend = "MIXED"
    return {"trend": trend, "ema20": e20, "ema50": e50, "rsi": strength, "price": closes[-1]}


def translate_to_sorani(text):
    """Translate a headline to Central Kurdish (Sorani); keep source text if unavailable."""
    try:
        response = HTTP.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ckb", "dt": "t", "q": text},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        translated = "".join(part[0] for part in payload[0] if part and part[0])
        return translated.strip() or text
    except Exception as exc:
        log.warning("Sorani headline translation unavailable: %s", exc)
        return text


def headlines(limit=5):
    items = []
    for url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:4]:
                title = getattr(entry, "title", "").strip()
                if title and title not in items:
                    items.append(title)
        except Exception as exc:
            log.warning("RSS feed unavailable (%s): %s", url, exc)
    return [(title, translate_to_sorani(title)) for title in items[:limit]]


def money(value):
    return "USD " + format(value, ",.2f")


def analyze():
    h4 = get_candles("4h")
    h1 = get_candles("1h")
    m15 = get_candles("15m")
    s4, s1, s15 = timeframe_summary(h4), timeframe_summary(h1), timeframe_summary(m15)
    price = s1["price"]
    structure, support, resistance = market_structure(h1)
    volatility = atr(h1)
    volume_avg = sum(c["volume"] for c in h1[-21:-1]) / 20
    volume_ratio = h1[-1]["volume"] / volume_avg if volume_avg > 0 else 0
    fvg = detect_fvg(m15)

    buy_ok = (
        s4["trend"] == "BULLISH" and s1["trend"] == "BULLISH"
        and 52 <= s1["rsi"] <= 68 and s15["trend"] != "BEARISH"
        and s15["rsi"] >= 45
        and structure in ("BULLISH BOS", "BULLISH STRUCTURE")
    )
    sell_ok = (
        s4["trend"] == "BEARISH" and s1["trend"] == "BEARISH"
        and 32 <= s1["rsi"] <= 48 and s15["trend"] != "BULLISH"
        and s15["rsi"] <= 55
        and structure in ("BEARISH BOS", "BEARISH STRUCTURE")
    )

    direction, reason = "WAIT", "هاوتاییی تایمفریمەکان و پێکهاتەی بازاڕ تەواو نییە."
    entry = sl = tp = None
    if buy_ok:
        direction = "BUY"
        reason = "H4 و H1 بەرەو سەرەوەن، RSI پشتگیری دەکات و پێکهاتەی بازاڕ بەرەو سەرەوەیە."
        entry = price
        sl = min(price - 1.5 * volatility, support - 0.15 * volatility)
        tp = entry + 2.0 * (entry - sl)
    elif sell_ok:
        direction = "SELL"
        reason = "H4 و H1 بەرەو خوارەوەن، RSI پشتگیری دەکات و پێکهاتەی بازاڕ بەرەو خوارەوەیە."
        entry = price
        sl = max(price + 1.5 * volatility, resistance + 0.15 * volatility)
        tp = entry - 2.0 * (sl - entry)

    candle_time = datetime.fromtimestamp(h1[-1]["time"], timezone.utc).strftime("%Y-%m-%d %H:%M")
    created_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    news = headlines()
    news_text = "\n".join(
        "• " + translated + ("\n  (Original: " + original + ")" if translated == original else "")
        for original, translated in news
    ) if news else "هەواڵی RSS بەردەست نییە."
    if entry is not None:
        risk_text = (
            "ئاراستەی پلان: " + direction + "\n"
            "Entry (نرخی کەندڵی H1): " + money(entry) + "\n"
            "Stop Loss (SL): " + money(sl) + "\nTake Profit (TP): " + money(tp) + "\n"
            "Risk/Reward ئامانج: 1:2 (خەمڵاندنی پلان، نەک دڵنیایی)"
        )
    else:
        # Always show an indicative plan, without changing WAIT into a trade signal.
        if (s4["trend"] == "BEARISH" and s1["trend"] == "BEARISH") or structure.startswith("BEARISH"):
            plan_direction = "SELL"
            plan_entry = price
            plan_sl = max(price + 1.5 * volatility, resistance + 0.15 * volatility)
            plan_tp = plan_entry - 2.0 * (plan_sl - plan_entry)
        elif (s4["trend"] == "BULLISH" and s1["trend"] == "BULLISH") or structure.startswith("BULLISH"):
            plan_direction = "BUY"
            plan_entry = price
            plan_sl = min(price - 1.5 * volatility, support - 0.15 * volatility)
            plan_tp = plan_entry + 2.0 * (plan_entry - plan_sl)
        elif price >= (support + resistance) / 2:
            plan_direction = "SELL"
            plan_entry = price
            plan_sl = max(price + 1.5 * volatility, resistance + 0.15 * volatility)
            plan_tp = plan_entry - 2.0 * (plan_sl - plan_entry)
        else:
            plan_direction = "BUY"
            plan_entry = price
            plan_sl = min(price - 1.5 * volatility, support - 0.15 * volatility)
            plan_tp = plan_entry + 2.0 * (plan_entry - plan_sl)
        risk_text = (
            "⚠️ پلانی خەمڵێنراو بۆ چاودێری (سیگناڵی چالاک نییە)\n"
            "ئاراستەی پلان: " + plan_direction + "\n"
            "Entry: " + money(plan_entry) + "\n"
            "Stop Loss (SL): " + money(plan_sl) + "\n"
            "Take Profit (TP): " + money(plan_tp) + "\n"
            "Risk/Reward ئامانج: 1:2\n"
            "تێبینی: ئەنجام هەر WAIT ـە؛ تا سیگناڵ پشتڕاست نەبێتەوە مامەڵە مەکەرەوە."
        )
    return (
        "🤖 HANO BTC AI | BTC/USD • KRAKEN\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🕒 کەندڵی H1 (UTC): {candle_time}\n"
        f"🆕 کاتی دروستکردنی شیکاری (UTC): {created_time}\n"
        f"💵 نرخی دوا کەندڵی تەواوبوو: {money(price)}\n\n"
        f"🎯 ئەنجام: {direction}\n"
        f"🧠 هۆکاری سەرەکی: {reason}\n\n"
        "📊 ئاراستەی تایمفریمەکان\n"
        f"• H4: {s4['trend']} | EMA20: {money(s4['ema20'])} | EMA50: {money(s4['ema50'])}\n"
        f"• H1: {s1['trend']} | EMA20: {money(s1['ema20'])} | EMA50: {money(s1['ema50'])} | RSI: {s1['rsi']:.1f}\n"
        f"• M15: {s15['trend']} | RSI: {s15['rsi']:.1f}\n\n"
        "🔎 نیشانە تەکنیکییەکان\n"
        f"• Market Structure: {structure}\n"
        f"• Support: {money(support)}\n"
        f"• Resistance: {money(resistance)}\n"
        f"• FVG لە M15: {fvg}\n"
        f"• Volumeی H1 بەرامبەر ناوەندی 20 کەندڵ: {volume_ratio:.2f}x\n"
        f"• ATR(14) لە H1: {money(volatility)}\n\n"
        "🛡️ پلانی مامەڵە و ڕیسک\n" + risk_text + "\n\n"
        "📰 هەواڵ و فەندامێنتاڵ بە کوردیی سۆرانی (وەرگێڕانی ئۆتۆماتیکی؛ زانیارییە، نەک فلتەری سیگناڵ)\n" + news_text + "\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ تەنها شیکارییە؛ بۆتەکە مامەڵە ناکات. Entry/SL/TP خەمڵاندنی پلاندانانە و دڵنیایی نییە. پێش بەکارهێنان لە Demo تاقی بکەرەوە."
    )


def minimal_signal_message(message):
    """Return only the six requested signal lines."""
    import re

    def read_price(pattern):
        match = re.search(pattern, message)
        if not match:
            return None
        try:
            return float(match.group(1).replace(",", ""))
        except (TypeError, ValueError):
            return None

    outcome_line = next(
        (line for line in message.splitlines()
         if line.startswith("🎯 ئەنجام:") or line.startswith("🚦 SIGNAL:")),
        "",
    )
    outcome = outcome_line.split(":", 1)[1].strip() if ":" in outcome_line else "WAIT"
    entry = read_price(r"Entry(?: \(نرخی کەندڵی H1\))?: USD ([0-9,]+\.[0-9]{2})")
    stop = read_price(r"Stop Loss(?: \(SL\))?: USD ([0-9,]+\.[0-9]{2})")
    target = read_price(r"Take Profit(?: \(TP\))?: USD ([0-9,]+\.[0-9]{2})")

    def price(value):
        return format(value, ",.2f") if value is not None else "N/A"

    return (
        "📊 Hano Trading Bot — Bitcoin\\n"
        "⏱️ Timeframe: M15\\n"
        f"🚦 SIGNAL: {outcome}\\n"
        f"💰 ENTRY: {price(entry)}\\n"
        f"🎯 TP: {price(target)}\\n"
        f"🛑 SL: {price(stop)}"
    )


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 سیگناڵ", callback_data="menu_signal"),
            InlineKeyboardButton("🧠 هۆکاری سیگناڵ", callback_data="menu_reason"),
        ],
        [
            InlineKeyboardButton("🛡️ پلانی ڕیسک", callback_data="menu_risk"),
            InlineKeyboardButton("📰 هەواڵ و فەندامێنتاڵ", callback_data="menu_news"),
        ],
        [
            InlineKeyboardButton("⏱️ تایمفریمەکان", callback_data="menu_timeframes"),
            InlineKeyboardButton("📚 فێربوون", callback_data="menu_education"),
        ],
    ])


def section_text(message, section):
    lines = message.splitlines()

    def after_heading(heading, stop_headings):
        try:
            start = next(i for i, line in enumerate(lines) if line.strip() == heading)
        except StopIteration:
            return ""
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].strip() in stop_headings:
                end = i
                break
        return "\n".join(lines[start:end]).strip()

    if section == "menu_signal":
        return minimal_signal_message(message)
    if section == "menu_risk":
        risk = after_heading(
            "🛡️ پلانی مامەڵە و ڕیسک",
            ["📰 هەواڵ و فەندامێنتاڵ بە کوردیی سۆرانی (وەرگێڕانی ئۆتۆماتیکی؛ زانیارییە، نەک فلتەری سیگناڵ)"],
        )
        return risk or "زانیاریی پلانی ڕیسک بەردەست نییە."
    if section == "menu_reason":
        reason = next((line for line in lines if line.startswith("🧠 هۆکاری سەرەکی:")), "")
        technical = after_heading("🔎 نیشانە تەکنیکییەکان", ["🛡️ پلانی مامەڵە و ڕیسک"])
        return "\n".join(part for part in [reason, technical] if part) or "وردەکاریی هۆکاری سیگناڵ بەردەست نییە."
    if section == "menu_news":
        news = after_heading(
            "📰 هەواڵ و فەندامێنتاڵ بە کوردیی سۆرانی (وەرگێڕانی ئۆتۆماتیکی؛ زانیارییە، نەک فلتەری سیگناڵ)",
            ["━━━━━━━━━━━━━━━━━━"],
        )
        return ("📰 هەواڵ و فەندامێنتاڵ\n\n" + news
                + "\n\nتێبینی: سەردێڕەکان زانیاریین و بەشێک لە فلتەری سیگناڵ نین.") if news else "ئێستا هەواڵ بەردەست نییە."
    if section == "menu_timeframes":
        return after_heading("📊 ئاراستەی تایمفریمەکان", ["🔎 نیشانە تەکنیکییەکان"]) or "زانیاریی تایمفریمەکان بەردەست نییە."
    if section == "menu_education":
        return (
            "📚 فێربوون بە کوردی سۆرانی\n\n"
            "• EMA20/EMA50: ناوەندی جوڵەی نرخ\n"
            "• RSI: پێوانەی هێزی جوڵەی نرخ\n"
            "• Market Structure: پێکهاتەی بازاڕ\n"
            "• BOS/MSS: شکاندنی پێکهاتە / گۆڕانی پێکهاتە\n"
            "• FVG: بۆشایی نرخی نێوان کەندڵەکان\n"
            "• Support/Resistance: پشتگیری و بەرگری\n"
            "• ATR: پێوانەی نوسانی نرخ\n\n"
            "ئەم وشانە لێرەدا بۆ فێرکارییە؛ یاسای سیگناڵ ناگۆڕن."
        )
    return "بەخێربێیت بۆ HANO BTC AI."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 HANO BTC AI\n"
        "سیستەمی شیکاری Bitcoin — BTC/USD\n\n"
        "بەشێک هەڵبژێرە بۆ بینینی زانیاریی تایبەت بەو بەشە.\n"
        "بۆتەکە شیکاری دەکات و هیچ مامەڵەیەک ئۆتۆماتیکی ناکات.",
        reply_markup=main_menu_keyboard(),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    section = query.data
    if section == "menu_home":
        await query.edit_message_text(
            "🤖 HANO BTC AI\nسیستەمی شیکاری Bitcoin — BTC/USD\n\nبەشێک هەڵبژێرە:",
            reply_markup=main_menu_keyboard(),
        )
        return
    try:
        message = analyze()
        body = section_text(message, section)
    except Exception:
        log.exception("menu analysis failed")
        body = "⚠️ ئێستا نەتوانرا زانیاریی نوێ بهێنرێت. تکایە دواتر دووبارە هەوڵ بدە."
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ گەڕانەوە بۆ مێنیو", callback_data="menu_home")]
    ])
    await query.edit_message_text(body, reply_markup=keyboard)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 ڕێنمایی HANO BTC AI\n\n"
        "/start — پەیامی دەستپێک\n"
        "/signal — نوێترین شیکاری BTC/USD\n"
        "/help — ئەم ڕێنماییە\n\n"
        "لە شیکارییەکەدا: ئاراستە و هۆکار، H4/H1/M15، EMA20/50، RSI، ATR، Support/Resistance، Market Structure، FVG، Volume، Entry/Stop Loss/Take Profit و سەردێڕی هەواڵ دەبینیت.\n\n"
        "🔔 ئاگادارکردنەوەی ئۆتۆماتیکی تەنها بۆ گۆڕانی ئاراستەی BUY/SELL ـی پشتڕاستکراوەیە؛ WAIT سیگناڵی مامەڵە نییە.\n"
        "⚠️ بۆتەکە تەنها شیکاری دەکات و هیچ مامەڵەیەک ئۆتۆماتیکی ناکات."
    )


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        message = analyze()
        await update.message.reply_text(minimal_signal_message(message), parse_mode="HTML")
    except Exception:
        log.exception("analysis failed")
        await update.message.reply_text(
            "⚠️ ئێستا نەتوانرا شیکاری BTC تەواو بکرێت.\n"
            "هۆکارەکە لە log ـی بۆتەکە تۆمار کراوە؛ تکایە دواتر /signal دووبارە بکەرەوە."
        )


async def signal_watch_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID:
        return
    try:
        message = analyze()
        direction_marker = "🎯 ئەنجام: "
        direction = next(
            (line.split(direction_marker, 1)[1].strip()
             for line in message.splitlines() if direction_marker in line),
            "WAIT",
        )
        candle_marker = "🕒 کەندڵی H1 (UTC): "
        candle_time = next(
            (line.split(candle_marker, 1)[1].strip()
             for line in message.splitlines() if candle_marker in line),
            "",
        )
        current_key = f"{candle_time}:{direction}"
        previous_key = context.application.bot_data.get("last_alert_key")
        if previous_key is None:
            context.application.bot_data["last_alert_key"] = current_key
            log.info("Watcher baseline set; startup alert suppressed")
            return
        previous_direction = previous_key.rsplit(":", 1)[-1]
        previous_candle = previous_key.rsplit(":", 1)[0]
        if candle_time and candle_time != previous_candle:
            if direction in ("BUY", "SELL") and direction != previous_direction:
                await context.bot.send_message(chat_id=CHAT_ID, text=minimal_signal_message(message))
            context.application.bot_data["last_alert_key"] = current_key
    except Exception:
        log.exception("signal watch failed")


def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_"))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("signal", signal))
    if CHAT_ID:
        app.job_queue.run_repeating(signal_watch_job, interval=WATCH_INTERVAL, first=15)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
