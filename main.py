import os
import asyncio
import logging
import time
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
HTTP_TIMEOUT = 15
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "HANO-BTC-AI/3.0"})

NEWS_FEEDS = [
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.bls.gov/feed/cpi.rss",
    "https://www.bls.gov/feed/empsit.rss",
    "https://www.bls.gov/feed/bls_latest.rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]
NEWS_TERMS = (
    "fomc", "federal reserve", "fed rate", "interest rate decision", "rate decision",
    "consumer price index", "cpi", "inflation", "nonfarm payroll", "payrolls",
    "employment situation", "unemployment rate", "jobs report", "pce", "gdp",
    "powell", "monetary policy"
)
NEWS_CACHE = {"checked": 0.0, "allowed": True, "status": "News not checked", "items": []}
HEADLINES_CACHE = {"checked": 0.0, "items": []}


def get_candles(interval="15m", limit=240):
    minutes = {"15m": 15, "1h": 60, "4h": 240}.get(interval)
    if minutes is None:
        raise ValueError("Unsupported interval")
    response = HTTP.get(KRAKEN_OHLC, params={"pair": "XBTUSD", "interval": minutes}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError("Kraken API error: " + str(payload["error"]))
    result = payload.get("result", {})
    pair = next((k for k in result if k != "last"), None)
    if not pair:
        raise RuntimeError("Kraken returned no BTC candles")
    rows = result[pair][:-1]  # completed candles only
    candles = [{
        "time": int(r[0]), "open": float(r[1]), "high": float(r[2]),
        "low": float(r[3]), "close": float(r[4]), "volume": float(r[6])
    } for r in rows[-limit:]]
    if len(candles) < 80:
        raise RuntimeError("Not enough completed BTC candles")
    return candles


def ema(values, period):
    if len(values) < period:
        raise ValueError("Not enough values for EMA")
    a = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = a * x + (1 - a) * value
    return value


def rsi(values, period=14):
    if len(values) <= period:
        return 50.0
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        ag = ((period - 1) * ag + gains[i]) / period
        al = ((period - 1) * al + losses[i]) / period
    if al == 0:
        return 100.0 if ag else 50.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def atr(candles, period=14):
    if len(candles) <= period:
        return 0.0
    tr = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        tr.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    value = sum(tr[:period]) / period
    for x in tr[period:]:
        value = ((period - 1) * value + x) / period
    return value


def money(x):
    return "USD " + format(x, ",.2f")


def swing_points(candles, left=5, right=5):
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        c = candles[i]
        window = candles[i-left:i+right+1]
        if c["high"] == max(x["high"] for x in window) and sum(x["high"] == c["high"] for x in window) == 1:
            highs.append((i, c["high"]))
        if c["low"] == min(x["low"] for x in window) and sum(x["low"] == c["low"] for x in window) == 1:
            lows.append((i, c["low"]))
    return highs, lows


def timeframe_summary(candles):
    closes = [c["close"] for c in candles]
    e20, e50 = ema(closes[-120:], 20), ema(closes[-160:], 50)
    rr = rsi(closes)
    trend = "BULLISH" if closes[-1] > e20 and e20 > e50 else "BEARISH" if closes[-1] < e20 and e20 < e50 else "MIXED"
    return {"trend": trend, "ema20": e20, "ema50": e50, "rsi": rr, "price": closes[-1]}


# ---------------------------------------------------------------------------
# Liquidity Entry Zones strategy
# Ported from the supplied TradingView Pine Script.
# ---------------------------------------------------------------------------
PIVOT_LENGTH = 5
STORED_LEVELS = 20
MIN_SWEEP_ATR = 0.10
MIN_WICK_PCT = 0.35
MAX_BODY_PCT = 0.65
MIN_RANGE_ATR = 0.20
CONFIRMATION_WINDOW = 2
COOLDOWN_BARS = 10
EMA_LENGTH = 50
SL_ATR = 1.50
TP_RR = 3.0


def detect_liquidity_entry(candles):
    """Bar-by-bar port of the supplied Pine Script's confirmed M15 logic.

    Replays completed candles in chronological order so pivot confirmation,
    pending sweeps, signal cooldown, and the optional one-active-trade rule
    behave like the TradingView indicator rather than checking only the latest
    few candles.
    """
    if len(candles) < 80:
        return None

    levels_high, levels_low = [], []
    pending_bull = None
    pending_bear = None
    last_signal_i = None
    position = 0
    trade_entry = trade_sl = trade_tp = None
    trade_entry_i = None
    latest_signal = None

    closes = [c["close"] for c in candles]
    # Pine ta.ema/ta.atr are evaluated as a series; use trailing series values
    # at each bar, only from candles available at that point.
    for i in range(len(candles)):
        if i < 20:
            continue
        history = candles[:i + 1]
        atr_now = atr(history, 14)
        if atr_now <= 0:
            continue
        ema_now = ema(closes[:i + 1], EMA_LENGTH)
        c = candles[i]
        rng = c["high"] - c["low"]
        if rng <= 0:
            upper_pct = lower_pct = body_pct = 0.0
        else:
            upper_pct = (c["high"] - max(c["open"], c["close"])) / rng
            lower_pct = (min(c["open"], c["close"]) - c["low"]) / rng
            body_pct = abs(c["close"] - c["open"]) / rng
        range_atr = rng / atr_now
        sweep_distance = MIN_SWEEP_ATR * atr_now

        # ta.pivothigh/low becomes known only after right-side candles close.
        center = i - PIVOT_LENGTH
        if center >= PIVOT_LENGTH:
            window = candles[center - PIVOT_LENGTH:center + PIVOT_LENGTH + 1]
            center_candle = candles[center]
            highs = [x["high"] for x in window]
            lows = [x["low"] for x in window]
            if (center_candle["high"] == max(highs)
                    and highs.count(center_candle["high"]) == 1):
                levels_high.append((center, center_candle["high"]))
                if len(levels_high) > STORED_LEVELS:
                    levels_high.pop(0)
            if (center_candle["low"] == min(lows)
                    and lows.count(center_candle["low"]) == 1):
                levels_low.append((center, center_candle["low"]))
                if len(levels_low) > STORED_LEVELS:
                    levels_low.pop(0)

        # Find the newest eligible swept high/low, matching Pine's reverse scan.
        swept_high = None
        for pivot_i, level in reversed(levels_high):
            if pivot_i < i and c["high"] > level and c["high"] - level >= sweep_distance:
                swept_high = level
                break
        swept_low = None
        for pivot_i, level in reversed(levels_low):
            if pivot_i < i and c["low"] < level and level - c["low"] >= sweep_distance:
                swept_low = level
                break

        bull_reclaim = swept_low is not None and c["close"] > swept_low
        bear_reclaim = swept_high is not None and c["close"] < swept_high
        quality_ok = body_pct <= MAX_BODY_PCT and range_atr >= MIN_RANGE_ATR

        valid_buy_sweep = (
            bull_reclaim and lower_pct >= MIN_WICK_PCT and quality_ok
        )
        valid_sell_sweep = (
            bear_reclaim and upper_pct >= MIN_WICK_PCT and quality_ok
        )

        if valid_buy_sweep:
            pending_bull = {
                "bar": i, "high": c["high"], "low": c["low"],
                "mid": (c["high"] + c["low"]) / 2,
                "level": swept_low, "wick_pct": lower_pct,
                "body_pct": body_pct, "range_atr": range_atr,
                "ema_ok": c["close"] > ema_now,
                "mid_ok": c["close"] > (c["high"] + c["low"]) / 2,
            }
        if valid_sell_sweep:
            pending_bear = {
                "bar": i, "high": c["high"], "low": c["low"],
                "mid": (c["high"] + c["low"]) / 2,
                "level": swept_high, "wick_pct": upper_pct,
                "body_pct": body_pct, "range_atr": range_atr,
                "ema_ok": c["close"] < ema_now,
                "mid_ok": c["close"] < (c["high"] + c["low"]) / 2,
            }

        bull_open = pending_bull is not None and i - pending_bull["bar"] <= CONFIRMATION_WINDOW
        bear_open = pending_bear is not None and i - pending_bear["bar"] <= CONFIRMATION_WINDOW
        buy_confirmed = (
            bull_open and c["close"] > c["open"]
            and c["close"] > ema_now and c["close"] > pending_bull["mid"]
        )
        sell_confirmed = (
            bear_open and c["close"] < c["open"]
            and c["close"] < ema_now and c["close"] < pending_bear["mid"]
        )

        cooldown_passed = last_signal_i is None or i - last_signal_i > COOLDOWN_BARS
        buy_signal = buy_confirmed and cooldown_passed
        sell_signal = sell_confirmed and cooldown_passed and not buy_signal

        # Pine's internal simulation prioritizes SL if SL and TP are touched
        # in the same candle, and ignores exits on the entry candle itself.
        if position != 0 and trade_entry_i is not None and i > trade_entry_i:
            if position == 1:
                sl_hit, tp_hit = c["low"] <= trade_sl, c["high"] >= trade_tp
            else:
                sl_hit, tp_hit = c["high"] >= trade_sl, c["low"] <= trade_tp
            if sl_hit or tp_hit:
                position = 0
                trade_entry = trade_sl = trade_tp = None
                trade_entry_i = None

        # When simulation is enabled and a projected trade is active, Pine blocks
        # fresh signals. This script keeps that default behavior.
        if position != 0:
            buy_signal = sell_signal = False

        if buy_signal or sell_signal:
            direction = "BUY" if buy_signal else "SELL"
            sweep = pending_bull if buy_signal else pending_bear
            score_wick = sweep["wick_pct"]
            score_body = sweep["body_pct"]
            score_range = sweep["range_atr"]
            wick_score = min(32.0, score_wick / MIN_WICK_PCT * 32.0)
            body_score = min(24.0, max(0.0, (MAX_BODY_PCT - score_body) / MAX_BODY_PCT) * 24.0)
            range_score = min(18.0, score_range / MIN_RANGE_ATR * 18.0)
            score = min(100.0, wick_score + body_score + range_score
                        + (10.0 if sweep["ema_ok"] else 0.0)
                        + 10.0
                        + (6.0 if sweep["mid_ok"] else 0.0))
            entry = c["close"]
            stop_distance = atr_now * SL_ATR
            sl = entry - stop_distance if direction == "BUY" else entry + stop_distance
            tp = entry + stop_distance * TP_RR if direction == "BUY" else entry - stop_distance * TP_RR
            signal_data = {
                "direction": direction, "entry": entry, "sl": sl, "tp": tp,
                "rr": TP_RR, "score": score, "sweep_level": sweep["level"],
                "sweep_bar": sweep["bar"], "bars_after": i - sweep["bar"],
                "ema": ema_now, "atr": atr_now,
                "reason": (
                    f"Liquidity sweep + reclaim + candle quality + EMA50 + "
                    f"midpoint confirmation; score {score:.0f}/100"
                ),
                "signal_bar": i,
            }
            last_signal_i = i
            latest_signal = signal_data
            position = 1 if direction == "BUY" else -1
            trade_entry, trade_sl, trade_tp = entry, sl, tp
            trade_entry_i = i

        if pending_bull is not None and (buy_signal or i - pending_bull["bar"] > CONFIRMATION_WINDOW):
            pending_bull = None
        if pending_bear is not None and (sell_signal or i - pending_bear["bar"] > CONFIRMATION_WINDOW):
            pending_bear = None

    # Return only a signal confirmed on the newest completed candle; don't replay
    # old historical entries as if they were fresh signals.
    if latest_signal is None or latest_signal["signal_bar"] != len(candles) - 1:
        return None
    latest_signal.pop("signal_bar", None)
    return latest_signal

def detect_fvg(candles):
    if len(candles) < 3:
        return "N/A"
    a, c = candles[-3], candles[-1]
    if c["low"] > a["high"]:
        return f"BULLISH FVG: {a['high']:.2f} - {c['low']:.2f}"
    if c["high"] < a["low"]:
        return f"BEARISH FVG: {c['high']:.2f} - {a['low']:.2f}"
    return "No new FVG"


def detect_mss(candles, lookback=40):
    if len(candles) < lookback + 2:
        return "NONE"
    prior = candles[-lookback-1:-1]
    last = candles[-1]
    hi = max(x["high"] for x in prior)
    lo = min(x["low"] for x in prior)
    if last["close"] > hi:
        return "BULLISH MSS/BOS"
    if last["close"] < lo:
        return "BEARISH MSS/BOS"
    return "NONE"


def market_structure(candles, lookback=20):
    prior = candles[-lookback-1:-1]
    last = candles[-1]
    support = min(x["low"] for x in prior)
    resistance = max(x["high"] for x in prior)
    highs, lows = swing_points(candles[-120:])
    if last["close"] > resistance:
        return "BULLISH BOS", support, resistance
    if last["close"] < support:
        return "BEARISH BOS", support, resistance
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]:
            return "BULLISH STRUCTURE", support, resistance
        if highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]:
            return "BEARISH STRUCTURE", support, resistance
    return "RANGE / NEUTRAL", support, resistance


def po3_phase(candles, lookback=24):
    prior = candles[-lookback-1:-1]
    last = candles[-1]
    hi, lo = max(x["high"] for x in prior), min(x["low"] for x in prior)
    mid = (hi + lo) / 2
    if last["low"] < lo and last["close"] > lo:
        return "PO3 bullish manipulation/reclaim clue"
    if last["high"] > hi and last["close"] < hi:
        return "PO3 bearish manipulation/reclaim clue"
    return "PO3 upper-half range clue" if last["close"] > mid else "PO3 lower-half range clue"


def fundamental_news_guard():
    now_mono = time.monotonic()
    if now_mono - NEWS_CACHE["checked"] < 300:
        return NEWS_CACHE["allowed"], NEWS_CACHE["status"], list(NEWS_CACHE["items"])
    now = datetime.now(timezone.utc)
    recent, sources_ok, seen = [], 0, set()
    for url in NEWS_FEEDS:
        try:
            r = HTTP.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            if getattr(parsed, "bozo", False) and not parsed.entries:
                continue
            sources_ok += 1
            for entry in parsed.entries[:15]:
                title = str(getattr(entry, "title", "") or "").strip()
                if not title or title.casefold() in seen or not any(t in title.casefold() for t in NEWS_TERMS):
                    continue
                seen.add(title.casefold())
                published = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
                if not published:
                    continue
                try:
                    at = datetime(*published[:6], tzinfo=timezone.utc)
                except Exception:
                    continue
                age = (now - at).total_seconds()
                if -300 <= age <= 90 * 60:
                    recent.append(title)
        except Exception as exc:
            log.warning("News feed unavailable %s: %s", url, exc)
    if sources_ok == 0:
        allowed, status = True, "WARNING: news feeds unavailable; technical-only"
    elif recent:
        allowed, status = False, "BLOCKED: recent USD/macro headline"
    else:
        allowed, status = True, "CLEAR: no matching high-impact headline in last 90 minutes"
    NEWS_CACHE.update({"checked": now_mono, "allowed": allowed, "status": status, "items": recent[:5]})
    return allowed, status, list(recent[:5])


def headlines(limit=5):
    if time.monotonic() - HEADLINES_CACHE["checked"] < 300:
        return list(HEADLINES_CACHE["items"][:limit])
    items = []
    for url in NEWS_FEEDS:
        try:
            r = HTTP.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            for e in parsed.entries[:4]:
                title = str(getattr(e, "title", "") or "").strip()
                if title and title not in items:
                    items.append(title)
        except Exception:
            continue
    HEADLINES_CACHE.update({"checked": time.monotonic(), "items": items[:limit]})
    return list(items[:limit])


def analyze():
    h4, h1, m15 = get_candles("4h"), get_candles("1h"), get_candles("15m")
    s4, s1, s15 = timeframe_summary(h4), timeframe_summary(h1), timeframe_summary(m15)
    price = s15["price"]
    structure, support, resistance = market_structure(m15)
    volatility = atr(m15)
    fvg, mss, po3 = detect_fvg(m15), detect_mss(m15), po3_phase(m15)
    volume_avg = sum(c["volume"] for c in m15[-21:-1]) / 20
    volume_ratio = m15[-1]["volume"] / volume_avg if volume_avg else 0.0
    setup = detect_liquidity_entry(m15)
    news_ok, news_status, impact = fundamental_news_guard()

    # Higher timeframes are context; the supplied Liquidity Entry Zones rules are the
    # actual M15 trigger. This avoids turning every EMA/FVG clue into a signal.
    direction = setup["direction"] if setup and news_ok else "WAIT"
    if setup and not news_ok:
        reason = news_status
    elif setup:
        reason = setup["reason"]
    else:
        reason = "No confirmed liquidity-sweep/reclaim setup inside the confirmation window."

    entry = setup["entry"] if setup and news_ok else None
    sl = setup["sl"] if setup and news_ok else None
    tp = setup["tp"] if setup and news_ok else None

    candle_time = datetime.fromtimestamp(m15[-1]["time"], timezone.utc).strftime("%Y-%m-%d %H:%M")
    created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if entry is not None:
        risk = (
            f"ئاراستەی پلان: {direction}\n"
            f"Entry (M15 close): {money(entry)}\n"
            f"Stop Loss (ATR): {money(sl)}\n"
            f"Take Profit ({TP_RR:g}R): {money(tp)}\n"
            f"Risk/Reward: 1:{TP_RR:g}"
        )
    else:
        risk = "⚠️ WAIT — هیچ پلانی BUY/SELL نییە. Entry / SL / TP = N/A"

    news_text = "\n".join("• " + x for x in headlines()) or "هەواڵ بەردەست نییە."
    sweep_text = "NONE"
    if setup:
        sweep_text = f"{setup['direction']} sweep @ {money(setup['sweep_level'])} | +{setup['bars_after']} bar"

    return (
        "🤖 HANO BTC AI | BTC/USD • KRAKEN\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🕒 M15 completed: {candle_time} UTC\n"
        f"🆕 Analysis: {created} UTC\n"
        f"💵 Price: {money(price)}\n\n"
        f"🎯 SIGNAL: {direction}\n"
        f"🧠 Reason: {reason}\n"
        f"📰 News filter: {news_status}\n"
        + ("⚠️ Macro: " + " | ".join(impact) + "\n" if impact else "")
        + "\n"
        "📊 MULTI-TIMEFRAME BIAS\n"
        f"• H4: {s4['trend']} | EMA20 {money(s4['ema20'])} | EMA50 {money(s4['ema50'])}\n"
        f"• H1: {s1['trend']} | EMA20 {money(s1['ema20'])} | EMA50 {money(s1['ema50'])} | RSI {s1['rsi']:.1f}\n"
        f"• M15: {s15['trend']} | RSI {s15['rsi']:.1f}\n\n"
        "💧 LIQUIDITY ENTRY ZONES\n"
        f"• Sweep: {sweep_text}\n"
        f"• Structure: {structure}\n"
        f"• MSS/BOS: {mss}\n"
        f"• FVG: {fvg}\n"
        f"• PO3: {po3}\n"
        f"• Support: {money(support)}\n"
        f"• Resistance: {money(resistance)}\n"
        f"• EMA50 filter: {money(ema(m15[-120:][0:0] if False else [c['close'] for c in m15[-120:]], 50))}\n"
        f"• Volume: {volume_ratio:.2f}x 20-candle average\n"
        f"• ATR14: {money(volatility)}\n\n"
        "🛡️ TRADE PLAN\n" + risk + "\n\n"
        "📰 NEWS\n" + news_text + "\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ تەنها شیکارییە؛ بۆتەکە هیچ مامەڵەیەکی ئۆتۆماتیکی ناکات. BUY/SELL پشتڕاستکراوەکان پێش بەکارهێنان لە Demo تاقی بکەرەوە."
    )


def minimal_signal_message(message):
    import re
    def read(pattern):
        m = re.search(pattern, message)
        return float(m.group(1).replace(",", "")) if m else None
    line = next((x for x in message.splitlines() if x.startswith("🎯 SIGNAL:")), "")
    direction = line.split(":", 1)[1].strip() if ":" in line else "WAIT"
    entry = read(r"Entry \(M15 close\): USD ([0-9,]+\.[0-9]{2})")
    sl = read(r"Stop Loss \(ATR\): USD ([0-9,]+\.[0-9]{2})")
    tp = read(r"Take Profit \(3R\): USD ([0-9,]+\.[0-9]{2})")
    if direction not in ("BUY", "SELL"):
        return "📊 Hano Trading Bot — Bitcoin\n⏱️ M15 — SCALPING\n🚦 SIGNAL: WAIT — NO ACTIVE TRADE\n💰 ENTRY: N/A\n🎯 TP: N/A\n🛑 SL: N/A\n⚠️ Do not open a trade until BUY/SELL is confirmed."
    fmt = lambda x: format(x, ",.2f") if x is not None else "N/A"
    return f"📊 Hano Trading Bot — Bitcoin\n⏱️ M15 — SCALPING\n🚦 SIGNAL: {direction}\n💰 ENTRY: {fmt(entry)}\n🎯 TP: {fmt(tp)}\n🛑 SL: {fmt(sl)}"


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 سیگناڵ", callback_data="menu_signal"), InlineKeyboardButton("🧠 هۆکاری سیگناڵ", callback_data="menu_reason")],
        [InlineKeyboardButton("🛡️ پلانی ڕیسک", callback_data="menu_risk"), InlineKeyboardButton("📰 هەواڵ", callback_data="menu_news")],
        [InlineKeyboardButton("⏱️ تایمفریمەکان", callback_data="menu_timeframes"), InlineKeyboardButton("📚 فێربوون", callback_data="menu_education")],
    ])


def section_text(message, section):
    lines = message.splitlines()
    if section == "menu_signal":
        return minimal_signal_message(message)
    if section == "menu_reason":
        return "🧠 هۆکاری سیگناڵ\n\n" + next((x for x in lines if x.startswith("🧠 Reason:")), "هیچ سیگناڵێکی پشتڕاستکراو نییە.") + "\n\n" + "\n".join(x for x in lines if x.startswith(("• Sweep:", "• Structure:", "• MSS/BOS:", "• FVG:", "• PO3:")))
    if section == "menu_risk":
        return "🛡️ پلانی ڕیسک\n\n" + "\n".join(x for x in lines if x.startswith(("ئاراستەی پلان:", "Entry ", "Stop Loss", "Take Profit", "Risk/Reward", "⚠️ WAIT")))
    if section == "menu_news":
        return "📰 هەواڵ و فەندامێنتاڵ\n\n" + "\n".join(x for x in lines if x.startswith(("📰 News filter:", "⚠️ Macro:", "• ")))[-3500:]
    if section == "menu_timeframes":
        return "📊 تایمفریمەکان\n\n" + "\n".join(x for x in lines if x.startswith(("• H4:", "• H1:", "• M15:")))
    if section == "menu_education":
        return ("📚 فێربوون\n\n"
                "• Liquidity Sweep: نرخ liquidityی بەرز/نزم دەشکێنێت و دەگەڕێتەوە.\n"
                "• Reclaim: داخستنی کەندڵ لە پشتەوەی level.\n"
                "• Wick/Body: بۆ پشکنینی qualityی sweep.\n"
                "• Confirmation Window: دوو کەندڵ بۆ پشتڕاستکردنەوە.\n"
                "• EMA50: فلتەری ئاراستەی ناوخۆ.\n"
                "• ATR: بنەمای SL.\n"
                "• 3R: TP = سێ جار مەودای risk.\n"
                "• FVG / MSS / BOS / PO3: نیشانەی پشتگیرییەکانن، نەک بە تەنیا trigger.")
    return "🤖 HANO BTC AI"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 HANO BTC AI\nسیستەمی نوێی Liquidity Entry Zones بۆ BTC/USD\n\nBUY/SELL تەنها کاتێک دەنێردرێت کە setup پشتڕاست بکرێتەوە.\nبۆتەکە مامەڵە ناکات.", reply_markup=main_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "menu_home":
        await query.edit_message_text("🤖 HANO BTC AI\nبەشێک هەڵبژێرە:", reply_markup=main_menu_keyboard())
        return
    try:
        message = await asyncio.to_thread(analyze)
        body = section_text(message, query.data)
    except Exception:
        log.exception("menu analysis failed")
        body = "⚠️ شیکاری نوێ بەردەست نییە؛ دواتر هەوڵ بدە."
    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ گەڕانەوە بۆ مێنیو", callback_data="menu_home")]]))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📖 /start — مێنیو\n/signal — نوێترین BTC signal\n/help — ڕێنمایی\n\nستراتیژی: Liquidity Sweep → Reclaim → Candle Quality → EMA50 → Midline Confirmation → ATR SL → 3R TP.\n⚠️ تەنها شیکاری؛ هیچ tradeی ئۆتۆماتیکی ناکرێت.")


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        message = await asyncio.to_thread(analyze)
        await update.message.reply_text(minimal_signal_message(message))
    except Exception:
        log.exception("analysis failed")
        await update.message.reply_text("⚠️ ئێستا شیکاری BTC تەواو نەکرا. دواتر /signal دووبارە بکەرەوە.")


async def signal_watch_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID:
        return
    try:
        message = await asyncio.to_thread(analyze)
        lines = message.splitlines()
        direction = next((x.split(":", 1)[1].strip() for x in lines if x.startswith("🎯 SIGNAL:")), "WAIT")
        candle = next((x.split(":", 1)[1].strip() for x in lines if x.startswith("🕒 M15 completed:")), "")
        key = f"{candle}:{direction}"
        old = context.application.bot_data.get("last_alert_key")
        if old is None:
            context.application.bot_data["last_alert_key"] = key
            log.info("Watcher baseline set")
            return
        old_candle = old.rsplit(":", 1)[0]
        if candle and candle != old_candle:
            if direction in ("BUY", "SELL"):
                await context.bot.send_message(chat_id=CHAT_ID, text=minimal_signal_message(message))
                log.info("Alert sent: %s %s", direction, candle)
            context.application.bot_data["last_alert_key"] = key
    except Exception:
        log.exception("signal watch failed")


async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled Telegram update error", exc_info=context.error)


def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_"))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("signal", signal))
    app.add_error_handler(telegram_error_handler)
    if CHAT_ID:
        app.job_queue.run_repeating(signal_watch_job, interval=WATCH_INTERVAL, first=15)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
