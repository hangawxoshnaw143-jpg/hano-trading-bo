import os
import asyncio
import logging
from datetime import datetime, timezone

import time

NEWS_GUARD_FEEDS = [
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.bls.gov/feed/cpi.rss",
    "https://www.bls.gov/feed/empsit.rss",
    "https://www.bls.gov/feed/bls_latest.rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]
NEWS_CACHE_SECONDS = 300
NEWS_EVENT_WINDOW_MINUTES = 90
_NEWS_CACHE = {"checked": 0.0, "allowed": False, "status": "News not checked", "items": []}
_NEWS_TERMS = (
    "fomc", "federal reserve", "fed rate", "interest rate decision", "rate decision",
    "consumer price index", "cpi", "inflation", "nonfarm payroll", "payrolls",
    "employment situation", "unemployment rate", "jobs report", "personal consumption expenditures",
    "pce price", "gross domestic product", "gdp", "powell", "monetary policy",
)

def fundamental_news_guard():
    """Conservative event-risk guard; checks recent headlines, not macro surprise or sentiment."""
    now_mono = time.monotonic()
    if now_mono - _NEWS_CACHE["checked"] < NEWS_CACHE_SECONDS:
        return _NEWS_CACHE["allowed"], _NEWS_CACHE["status"], list(_NEWS_CACHE["items"])
    now = datetime.now(timezone.utc)
    recent = []
    sources_ok = 0
    seen = set()
    for url in NEWS_GUARD_FEEDS:
        try:
            response = HTTP.get(url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            if getattr(parsed, "bozo", False) and not parsed.entries:
                continue
            sources_ok += 1
            for entry in parsed.entries[:15]:
                title = str(getattr(entry, "title", "") or "").strip()
                if not title or title.casefold() in seen:
                    continue
                seen.add(title.casefold())
                if not any(term in title.casefold() for term in _NEWS_TERMS):
                    continue
                published = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
                if not published:
                    continue
                try:
                    published_at = datetime(*published[:6], tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
                age = (now - published_at).total_seconds()
                if -300 <= age <= NEWS_EVENT_WINDOW_MINUTES * 60:
                    recent.append(title)
        except Exception as exc:
            log.warning("Fundamental RSS feed unavailable (%s): %s", url, exc)
    if sources_ok == 0:
        # Do not silently suppress every technical signal just because an RSS provider is down.
        # Make the missing news verification explicit so the user can choose whether to trade.
        allowed, status = True, "WARNING: fundamental-news feeds unavailable; signal is technical-only"
    elif recent:
        allowed, status = False, "BLOCKED: recent high-impact USD/macro headline; wait for volatility to settle"
    else:
        allowed, status = True, "CLEAR: RSS sources reachable; no matching high-impact headline in the last 90 minutes"
    _NEWS_CACHE.update({"checked": now_mono, "allowed": allowed, "status": status, "items": recent[:5]})
    return allowed, status, list(recent[:5])


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
WATCH_INTERVAL = max(30, int(os.getenv("WATCH_INTERVAL_SECONDS", "30")))
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.bls.gov/feed/cpi.rss",
    "https://www.bls.gov/feed/empsit.rss",
    "https://www.bls.gov/feed/bls_latest.rss",
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


def swing_points(candles, left=2, right=2):
    """Return confirmed local swing highs/lows; right candles confirm a pivot."""
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        window = candles[i-left:i+right+1]
        high, low = candles[i]["high"], candles[i]["low"]
        if high == max(c["high"] for c in window) and sum(c["high"] == high for c in window) == 1:
            highs.append((i, high))
        if low == min(c["low"] for c in window) and sum(c["low"] == low for c in window) == 1:
            lows.append((i, low))
    return highs, lows


def market_structure(candles, lookback=20):
    recent = candles[-(lookback + 1):-1]
    last = candles[-1]
    resistance = max(c["high"] for c in recent)
    support = min(c["low"] for c in recent)
    highs, lows = swing_points(candles[-120:])
    if last["close"] > resistance:
        return "BULLISH BOS", support, resistance
    if last["close"] < support:
        return "BEARISH BOS", support, resistance
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]:
            return "BEARISH STRUCTURE", support, resistance
        if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]:
            return "BULLISH STRUCTURE", support, resistance
    return "RANGE / NEUTRAL", support, resistance


def pullback_rejection_confirmed(candles, direction, left=2, right=2):
    """Require a confirmed LH/HL pullback followed by a continuation trigger."""
    if len(candles) < 30:
        return False
    window = candles[-120:]
    highs, lows = swing_points(window, left, right)
    # Latest pivot must be confirmed by right completed candles; do not trade the pivot itself.
    last_confirmable_index = len(window) - right - 1
    if len(highs) < 2 or len(lows) < 2:
        return False
    last, prev = window[-1], window[-2]
    if direction == "SELL":
        previous_high, pullback_high = highs[-2], highs[-1]
        if pullback_high[1] >= previous_high[1] or pullback_high[0] >= last_confirmable_index:
            return False
        if not any(previous_high[0] < low[0] < pullback_high[0] for low in lows):
            return False
        return last["close"] < last["open"] and last["close"] < prev["low"] and last["close"] < pullback_high[1]
    if direction == "BUY":
        previous_low, pullback_low = lows[-2], lows[-1]
        if pullback_low[1] <= previous_low[1] or pullback_low[0] >= last_confirmable_index:
            return False
        if not any(previous_low[0] < high[0] < pullback_low[0] for high in highs):
            return False
        return last["close"] > last["open"] and last["close"] > prev["high"] and last["close"] > pullback_low[1]
    return False


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


TRANSLATION_CACHE = {}

def translate_to_sorani(text):
    """Translate a headline to Central Kurdish (Sorani), caching results to avoid rate limits."""
    if text in TRANSLATION_CACHE:
        return TRANSLATION_CACHE[text]
    try:
        response = HTTP.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ckb", "dt": "t", "q": text},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        translated = "".join(part[0] for part in payload[0] if part and part[0])
        result = translated.strip() or text
        TRANSLATION_CACHE[text] = result
        return result
    except Exception as exc:
        log.warning("Sorani headline translation unavailable: %s", exc)
        TRANSLATION_CACHE[text] = text
        return text


HEADLINES_CACHE = {"checked": 0.0, "items": []}

def headlines(limit=5):
    if time.monotonic() - HEADLINES_CACHE["checked"] < 300:
        return list(HEADLINES_CACHE["items"][:limit])
    items = []
    for url in RSS_FEEDS:
        try:
            response = HTTP.get(url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            for entry in parsed.entries[:4]:
                title = getattr(entry, "title", "").strip()
                if title and title not in items:
                    items.append(title)
        except Exception as exc:
            log.warning("RSS feed unavailable (%s): %s", url, exc)
    translated_items = [(title, translate_to_sorani(title)) for title in items[:limit]]
    HEADLINES_CACHE.update({"checked": time.monotonic(), "items": translated_items})
    return list(translated_items)


def money(value):
    return "USD " + format(value, ",.2f")



def detect_liquidity_sweep(candles, lookback=20):
    """Heuristic sweep: wick takes prior range liquidity, close returns inside."""
    if len(candles) < lookback + 2:
        return "NONE"
    prior = candles[-(lookback + 1):-1]
    last = candles[-1]
    prior_high = max(x["high"] for x in prior)
    prior_low = min(x["low"] for x in prior)
    if last["high"] > prior_high and last["close"] < prior_high:
        return "BUY-SIDE LIQUIDITY SWEPT (bearish clue)"
    if last["low"] < prior_low and last["close"] > prior_low:
        return "SELL-SIDE LIQUIDITY SWEPT (bullish clue)"
    return "NONE"


def detect_mss(candles, lookback=40):
    """Simple close-confirmed break of recent swing; heuristic MSS, not canonical labeling."""
    if len(candles) < lookback + 3:
        return "NONE"
    prior = candles[-(lookback + 1):-1]
    last = candles[-1]
    prior_high = max(x["high"] for x in prior)
    prior_low = min(x["low"] for x in prior)
    if last["close"] > prior_high:
        return "BULLISH MSS/BOS"
    if last["close"] < prior_low:
        return "BEARISH MSS/BOS"
    return "NONE"


def detect_order_block(candles, direction, search=12):
    """Basic OB candidate: last opposite candle before a strong directional candle."""
    if len(candles) < search + 2:
        return "NONE"
    last = candles[-1]
    recent = candles[-(search + 1):-1]
    avg_range = sum(x["high"] - x["low"] for x in recent) / max(len(recent), 1)
    if avg_range <= 0:
        return "NONE"
    if direction == "BUY":
        if last["close"] <= last["open"] or (last["high"] - last["low"]) < 1.25 * avg_range:
            return "NONE"
        for offset, candle in enumerate(reversed(recent)):
            if offset > 3:
                break
            if candle["close"] < candle["open"]:
                return f"BULLISH OB CANDIDATE {candle['low']:.2f}-{candle['high']:.2f}"
    elif direction == "SELL":
        if last["close"] >= last["open"] or (last["high"] - last["low"]) < 1.25 * avg_range:
            return "NONE"
        for offset, candle in enumerate(reversed(recent)):
            if offset > 3:
                break
            if candle["close"] > candle["open"]:
                return f"BEARISH OB CANDIDATE {candle['low']:.2f}-{candle['high']:.2f}"
    return "NONE"


def adx_value(candles, period=14):
    """Wilder-style ADX estimate from completed candles."""
    if len(candles) < period * 2 + 2:
        return 0.0
    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        up = cur["high"] - prev["high"]
        down = prev["low"] - cur["low"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"])))
    def smooth(values):
        out = [sum(values[:period])]
        for val in values[period:]:
            out.append(out[-1] - out[-1] / period + val)
        return out
    tr_s, p_s, m_s = smooth(tr), smooth(plus_dm), smooth(minus_dm)
    dx = []
    for t, p, m in zip(tr_s, p_s, m_s):
        if t <= 0:
            dx.append(0.0)
        else:
            pdi, mdi = 100 * p / t, 100 * m / t
            dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    if len(dx) < period:
        return sum(dx) / len(dx) if dx else 0.0
    value = sum(dx[:period]) / period
    for item in dx[period:]:
        value = ((period - 1) * value + item) / period
    return value


def po3_phase(candles, lookback=24):
    """Loose accumulation/manipulation/distribution clue; informational only."""
    if len(candles) < lookback + 2:
        return "UNKNOWN"
    prior = candles[-(lookback + 1):-1]
    last = candles[-1]
    hi = max(x["high"] for x in prior)
    lo = min(x["low"] for x in prior)
    mid = (hi + lo) / 2
    if last["low"] < lo and last["close"] > lo:
        return "PO3 bullish manipulation/reclaim clue"
    if last["high"] > hi and last["close"] < hi:
        return "PO3 bearish manipulation/reclaim clue"
    if last["close"] > mid:
        return "PO3 range upper half (not confirmation)"
    return "PO3 range lower half (not confirmation)"


def _build_independent_candidate(strategy, direction, candles, volatility, reason, stop_price=None):
    """Build a risk-checked signal from a completed candle; this never places a trade."""
    if not candles or volatility <= 0:
        return None
    last = candles[-1]
    entry = float(last["close"])
    buffer = max(volatility * 0.15, entry * 0.0003)
    if stop_price is None:
        stop_price = last["low"] - buffer if direction == "BUY" else last["high"] + buffer
    stop = float(stop_price)
    risk = entry - stop if direction == "BUY" else stop - entry
    min_risk = max(volatility * 0.15, entry * 0.00025)
    max_risk = max(volatility * 3.5, entry * 0.02)
    if risk < min_risk or risk > max_risk:
        return None
    target = entry + 2.0 * risk if direction == "BUY" else entry - 2.0 * risk
    candle_epoch = int(last["time"])
    candle_time = datetime.fromtimestamp(candle_epoch, timezone.utc).strftime("%Y-%m-%d %H:%M")
    return {
        "strategy": strategy,
        "direction": direction,
        "entry": entry,
        "sl": stop,
        "tp": target,
        "risk_reward": "1:2",
        "reason": reason,
        "candle_time": candle_time,
        "candle_epoch": candle_epoch,
        "key": f"{strategy}:{direction}:{candle_epoch}",
    }


def _order_block_retest_zone(candles, direction, volatility, lookback=18):
    """Find a recent opposite candle, displacement away, then a confirmed retest."""
    if len(candles) < 12 or volatility <= 0:
        return None
    last = candles[-1]
    start = max(1, len(candles) - lookback - 3)
    for index in range(start, len(candles) - 2):
        ob = candles[index]
        before = candles[max(0, index - 5):index]
        after = candles[index + 1:min(index + 4, len(candles) - 1)]
        if not before or not after:
            continue
        if direction == "BUY" and ob["close"] < ob["open"]:
            boundary = max(c["high"] for c in before)
            displaced = any(c["close"] > boundary and c["close"] > c["open"]
                            and c["close"] - c["open"] >= 0.6 * volatility for c in after)
            retested = last["low"] <= ob["high"] and last["close"] > ob["high"] and last["close"] > last["open"]
            if displaced and retested:
                return ob["low"], ob["high"]
        elif direction == "SELL" and ob["close"] > ob["open"]:
            boundary = min(c["low"] for c in before)
            displaced = any(c["close"] < boundary and c["close"] < c["open"]
                            and c["open"] - c["close"] >= 0.6 * volatility for c in after)
            retested = last["high"] >= ob["low"] and last["close"] < ob["low"] and last["close"] < last["open"]
            if displaced and retested:
                return ob["low"], ob["high"]
    return None


def independent_signal_candidates(m15, h1, h4):
    """Return independent, completed-candle setup candidates with their own entry/SL/TP."""
    if min(len(m15), len(h1), len(h4)) < 60:
        raise RuntimeError("Not enough completed candles for independent signal scan")
    last, previous = m15[-1], m15[-2]
    volatility = atr(m15)
    if volatility <= 0:
        return []
    average_volume = sum(c["volume"] for c in m15[-21:-1]) / 20
    volume_ratio = last["volume"] / average_volume if average_volume > 0 else 0.0
    candidates = []

    def add(strategy, direction, reason, stop_price=None):
        candidate = _build_independent_candidate(
            strategy, direction, m15, volatility, reason, stop_price=stop_price
        )
        if candidate:
            candidates.append(candidate)

    # 1) Liquidity sweep reversal: wick takes a prior extreme, then candle closes back inside.
    liquidity = detect_liquidity_sweep(m15, lookback=20)
    if "BUY-SIDE LIQUIDITY SWEPT" in liquidity and last["close"] < last["open"]:
        add("Liquidity Sweep", "SELL", "وەرگرتنی liquidityی سەرەوە و داخستنی شمع بە ئاراستەی دابەزین.", last["high"] + 0.15 * volatility)
    elif "SELL-SIDE LIQUIDITY SWEPT" in liquidity and last["close"] > last["open"]:
        add("Liquidity Sweep", "BUY", "وەرگرتنی liquidityی خوارەوە و داخستنی شمع بە ئاراستەی بەرزبوونەوە.", last["low"] - 0.15 * volatility)

    # 2) Breakout/BOS: completed candle closes beyond the previous 20-candle range.
    prior20 = m15[-21:-1]
    range_high = max(c["high"] for c in prior20)
    range_low = min(c["low"] for c in prior20)
    body = abs(last["close"] - last["open"])
    if last["close"] > range_high and last["close"] > last["open"] and body >= 0.35 * volatility and volume_ratio >= 0.80:
        add("BOS Breakout", "BUY", "داخستنی شمعی تەواوبوو لە سەرووی بەرزی ٢٠ شمعی پێشوو.", min(last["low"], range_high) - 0.15 * volatility)
    elif last["close"] < range_low and last["close"] < last["open"] and body >= 0.35 * volatility and volume_ratio >= 0.80:
        add("BOS Breakout", "SELL", "داخستنی شمعی تەواوبوو لە خوار نزمترین ٢٠ شمعی پێشوو.", max(last["high"], range_low) + 0.15 * volatility)

    # 3) Confirmed pullback/rejection continuation.
    if pullback_rejection_confirmed(m15, "BUY"):
        add("Pullback Rejection", "BUY", "HLی پشتڕاستکراو و شمعی بەهێزی بەردەوامبوونی بەرزبوونەوە.", min(c["low"] for c in m15[-5:]) - 0.15 * volatility)
    if pullback_rejection_confirmed(m15, "SELL"):
        add("Pullback Rejection", "SELL", "LHی پشتڕاستکراو و شمعی بەهێزی بەردەوامبوونی دابەزین.", max(c["high"] for c in m15[-5:]) + 0.15 * volatility)

    # 4) FVG formation: gap plus directional candle/body confirmation.
    a = m15[-3]
    if last["low"] > a["high"]:
        gap = last["low"] - a["high"]
        if last["close"] > last["open"] and 0 < gap <= 0.8 * volatility and body >= 0.35 * volatility:
            add("FVG", "BUY", "FVGی بەرزبوونەوە دروست بووە و شمعی کۆتایی بەهێز داخراوە.", last["low"] - 0.15 * volatility)
    elif last["high"] < a["low"]:
        gap = a["low"] - last["high"]
        if last["close"] < last["open"] and 0 < gap <= 0.8 * volatility and body >= 0.35 * volatility:
            add("FVG", "SELL", "FVGی دابەزین دروست بووە و شمعی کۆتایی بەهێز داخراوە.", last["high"] + 0.15 * volatility)

    # 5) Order-block retest: displacement away from an opposite candle, then rejection at its zone.
    buy_zone = _order_block_retest_zone(m15, "BUY", volatility)
    if buy_zone:
        add("Order Block Retest", "BUY", f"گەڕانەوە و ڕەتکردنەوەی ناوچەی OBی بەرزبوونەوە ({buy_zone[0]:.2f}-{buy_zone[1]:.2f}).", buy_zone[0] - 0.15 * volatility)
    sell_zone = _order_block_retest_zone(m15, "SELL", volatility)
    if sell_zone:
        add("Order Block Retest", "SELL", f"گەڕانەوە و ڕەتکردنەوەی ناوچەی OBی دابەزین ({sell_zone[0]:.2f}-{sell_zone[1]:.2f}).", sell_zone[1] + 0.15 * volatility)

    # 6) PO3-style manipulation of the H1 range, independently of the M15 range sweep.
    h1_prior = h1[-25:-1]
    h1_high = max(c["high"] for c in h1_prior)
    h1_low = min(c["low"] for c in h1_prior)
    if last["low"] < h1_low and last["close"] > h1_low and last["close"] > last["open"]:
        add("PO3 H1 Range Sweep", "BUY", "شمعی M15 نزمترین سنووری H1ی شکاند و لە سەرووی سنوورەکە داخرا.")
    elif last["high"] > h1_high and last["close"] < h1_high and last["close"] < last["open"]:
        add("PO3 H1 Range Sweep", "SELL", "شمعی M15 بەرزترین سنووری H1ی شکاند و لە خوار سنوورەکە داخرا.")

    # 7) Engulfing: a completed candle engulfs the previous candle body with sufficient momentum.
    if previous["close"] < previous["open"] and last["close"] > last["open"]:
        bullish_engulf = last["open"] <= previous["close"] and last["close"] >= previous["open"]
        if bullish_engulf and body >= 0.5 * volatility and volume_ratio >= 0.90:
            add("Engulfing", "BUY", "شمعی بەرزبوونەوە جەستەی شمعی پێشوو داپۆشیوە و هێزی جوڵە هەیە.")
    elif previous["close"] > previous["open"] and last["close"] < last["open"]:
        bearish_engulf = last["open"] >= previous["close"] and last["close"] <= previous["open"]
        if bearish_engulf and body >= 0.5 * volatility and volume_ratio >= 0.90:
            add("Engulfing", "SELL", "شمعی دابەزین جەستەی شمعی پێشوو داپۆشیوە و هێزی جوڵە هەیە.")

    # Keep one alert per strategy/direction/candle; separate strategies remain independent.
    unique = {}
    for candidate in candidates:
        unique[candidate["key"]] = candidate
    return list(unique.values())


def format_independent_signal(candidate, fundamental_status):
    return (
        "🤖 HANO BTC AI | BTC/USD • KRAKEN\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🚦 سیگناڵ: {candidate['direction']}\n"
        f"🧠 ستراتیژی: {candidate['strategy']}\n"
        "⏱️ تایمفریم: M15 (شمعی تەواوبوو)\n"
        f"🕯️ کاتی پشتڕاستکردنەوە (UTC): {candidate['candle_time']}\n"
        f"💵 Entry: {money(candidate['entry'])}\n"
        f"🛑 Stop Loss: {money(candidate['sl'])}\n"
        f"🎯 Take Profit: {money(candidate['tp'])}\n"
        f"📐 Risk/Reward: {candidate['risk_reward']}\n"
        f"🔎 هۆکار: {candidate['reason']}\n"
        f"📰 فلتەری هەواڵ: {fundamental_status}\n\n"
        "⚠️ ئەمە سیگناڵی شیکارییە، نەک دڵنیایی لە قازانج. بۆتەکە مامەڵەی ئۆتۆماتیکی ناکات."
    )


def analyze():
    h4 = get_candles("4h")
    h1 = get_candles("1h")
    m15 = get_candles("15m")
    s4, s1, s15 = timeframe_summary(h4), timeframe_summary(h1), timeframe_summary(m15)
    price = s15["price"]
    structure, support, resistance = market_structure(m15)
    volatility = atr(m15)
    volume_avg = sum(c["volume"] for c in m15[-21:-1]) / 20
    volume_ratio = m15[-1]["volume"] / volume_avg if volume_avg > 0 else 0
    fvg = detect_fvg(m15)
    liquidity = detect_liquidity_sweep(m15)
    mss = detect_mss(m15)
    bullish_ob = detect_order_block(m15, "BUY")
    bearish_ob = detect_order_block(m15, "SELL")
    adx = adx_value(m15)
    po3 = po3_phase(m15)

    # Weighted confirmation model: higher timeframes guide direction, but do not all
    # have to align. Pullback rejection is supportive evidence, not a mandatory gate.
    buy_score = 0
    sell_score = 0

    for summary, weight in ((s4, 1), (s1, 2), (s15, 2)):
        if summary["trend"] == "BULLISH":
            buy_score += weight
        elif summary["trend"] == "BEARISH":
            sell_score += weight

    if 45 <= s15["rsi"] <= 72:
        buy_score += 1
    elif s15["rsi"] < 35 or s15["rsi"] > 78:
        buy_score -= 1
    if 28 <= s15["rsi"] <= 55:
        sell_score += 1
    elif s15["rsi"] < 22 or s15["rsi"] > 65:
        sell_score -= 1

    if structure in ("BULLISH BOS", "BULLISH STRUCTURE"):
        buy_score += 2
    elif structure in ("BEARISH BOS", "BEARISH STRUCTURE"):
        sell_score += 2

    if pullback_rejection_confirmed(m15, "BUY"):
        buy_score += 1
    if pullback_rejection_confirmed(m15, "SELL"):
        sell_score += 1
    if fvg.startswith("BULLISH FVG"):
        buy_score += 1
    elif fvg.startswith("BEARISH FVG"):
        sell_score += 1
    if volume_ratio >= 1.10:
        if m15[-1]["close"] > m15[-1]["open"]:
            buy_score += 1
        elif m15[-1]["close"] < m15[-1]["open"]:
            sell_score += 1

    # Extra SMC/ICT-style clues. These are heuristic confirmations, never standalone entries.
    if "SELL-SIDE LIQUIDITY SWEPT" in liquidity or mss == "BULLISH MSS/BOS":
        buy_score += 1
    if "BUY-SIDE LIQUIDITY SWEPT" in liquidity or mss == "BEARISH MSS/BOS":
        sell_score += 1
    if bullish_ob.startswith("BULLISH OB"):
        buy_score += 1
    if bearish_ob.startswith("BEARISH OB"):
        sell_score += 1
    if adx >= 20:
        if s15["trend"] == "BULLISH":
            buy_score += 1
        elif s15["trend"] == "BEARISH":
            sell_score += 1
    # PO3 clue is displayed for context only; do not double-count the same sweep.

    # Calibrated for more valid opportunities while retaining score separation.
    # This remains a heuristic; validate on historical data and demo before live use.
    buy_ok = buy_score >= 4 and buy_score >= sell_score + 1
    sell_ok = sell_score >= 4 and sell_score >= buy_score + 1

    fundamental_ok, fundamental_status, impact_headlines = fundamental_news_guard()
    if not fundamental_ok:
        buy_ok = False
        sell_ok = False

    direction, reason = "WAIT", (
        fundamental_status if not fundamental_ok
        else f"نمرەی BUY: {buy_score} | نمرەی SELL: {sell_score}؛ نمرەی پێویست 4 و جیاوازی لانیکەم 1 ـە."
    )
    entry = sl = tp = None
    if buy_ok:
        direction = "BUY"
        reason = f"BUY: نمرە {buy_score}؛ پێکهاتە={structure}؛ liquidity={liquidity}؛ MSS/BOS={mss}؛ ADX={adx:.1f}."
        entry = price
        sl = min(price - 1.5 * volatility, support - 0.15 * volatility)
        tp = entry + 2.0 * (entry - sl)
    elif sell_ok:
        direction = "SELL"
        reason = f"SELL: نمرە {sell_score}؛ پێکهاتە={structure}؛ liquidity={liquidity}؛ MSS/BOS={mss}؛ ADX={adx:.1f}."
        entry = price
        sl = max(price + 1.5 * volatility, resistance + 0.15 * volatility)
        tp = entry - 2.0 * (sl - entry)

    candle_time = datetime.fromtimestamp(m15[-1]["time"], timezone.utc).strftime("%Y-%m-%d %H:%M")
    created_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    news = headlines()
    news_text = "\n".join(
        "• " + translated + ("\n  (Original: " + original + ")" if translated == original else "")
        for original, translated in news
    ) if news else "هەواڵی RSS بەردەست نییە."
    if entry is not None:
        risk_text = (
            "ئاراستەی پلان: " + direction + "\n"
            "Entry (نرخی کەندڵی M15): " + money(entry) + "\n"
            "Stop Loss (SL): " + money(sl) + "\nTake Profit (TP): " + money(tp) + "\n"
            "Risk/Reward ئامانج: 1:2 (خەمڵاندنی پلان، نەک دڵنیایی)"
        )
    else:
        # WAIT means no active setup. Do not manufacture a directional plan from
        # the price's position inside support/resistance; that can contradict the scores.
        risk_text = (
            "⚠️ WAIT — هیچ پلانی BUY/SELL نیشان نادرێت، چونکە سیگناڵ پشتڕاست نەکراوەتەوە.\n"
            f"نمرەی BUY: {buy_score} | نمرەی SELL: {sell_score}\n"
            "Entry / SL / TP: N/A تا کاتی پشتڕاستبوونەوەی سیگناڵ.\n"
            "تێبینی: لەم دۆخەدا هیچ مامەڵەیەک مەکەرەوە."
        )
    return (
        "🤖 HANO BTC AI | BTC/USD • KRAKEN\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🕒 کەندڵی M15 (UTC): {candle_time}\n"
        f"🆕 کاتی دروستکردنی شیکاری (UTC): {created_time}\n"
        f"💵 نرخی دوا کەندڵی تەواوبوو: {money(price)}\n\n"
        f"🎯 ئەنجام: {direction}\n"
        f"🧠 هۆکاری سەرەکی: {reason}\n"
        f"📰 فلتەری هەواڵ: {fundamental_status}\n"
        + ("\n".join("⚠️ هەواڵی ماکرۆ: " + title for title in impact_headlines) + "\n" if impact_headlines else "")
        + "\n"
        "📊 ئاراستەی تایمفریمەکان\n"
        f"• H4: {s4['trend']} | EMA20: {money(s4['ema20'])} | EMA50: {money(s4['ema50'])}\n"
        f"• H1: {s1['trend']} | EMA20: {money(s1['ema20'])} | EMA50: {money(s1['ema50'])} | RSI: {s1['rsi']:.1f}\n"
        f"• M15: {s15['trend']} | RSI: {s15['rsi']:.1f}\n\n"
        "🔎 نیشانە تەکنیکییەکان\n"
        f"• Market Structure: {structure}\n"
        f"• Support: {money(support)}\n"
        f"• Resistance: {money(resistance)}\n"
        f"• FVG لە M15: {fvg}\n"
        f"• Liquidity Sweep: {liquidity}\n"
        f"• MSS/BOS (heuristic): {mss}\n"
        f"• Order Block BUY: {bullish_ob}\n"
        f"• Order Block SELL: {bearish_ob}\n"
        f"• ADX(14) خەمڵێنراو: {adx:.1f}\n"
        f"• PO3 clue (heuristic): {po3}\n"

        f"• Volumeی M15 بەرامبەر ناوەندی 20 کەندڵ: {volume_ratio:.2f}x\n"
        f"• ATR(14) لە M15: {money(volatility)}\n\n"
        "🛡️ پلانی مامەڵە و ڕیسک\n" + risk_text + "\n\n"
        "📰 هەواڵ و فەندامێنتاڵ بە کوردیی سۆرانی (RSS؛ فلتەری ڕیسکی ماکرۆ چالاکە)\n" + news_text + "\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ تەنها شیکارییە؛ بۆتەکە مامەڵە ناکات. Entry/SL/TP خەمڵاندنی پلاندانانە و دڵنیایی نییە. پێش بەکارهێنان لە Demo تاقی بکەرەوە."
    )


def minimal_signal_message(message):
    """Format a concise signal without presenting a WAIT plan as an active trade."""
    import re

    def read_price(pattern):
        match = re.search(pattern, message)
        if not match:
            return None
        try:
            return float(match.group(1).replace(",", ""))
        except (TypeError, ValueError):
            return None

    lines = message.splitlines()
    outcome_line = next(
        (line for line in lines
         if line.startswith("🎯 ئەنجام:") or line.startswith("🚦 SIGNAL:")),
        "",
    )
    outcome = outcome_line.split(":", 1)[1].strip() if ":" in outcome_line else "WAIT"
    plan_line = next((line for line in lines if line.startswith("ئاراستەی پلان:")), "")
    plan_direction = plan_line.split(":", 1)[1].strip() if ":" in plan_line else "N/A"

    # Prefer the confirmed setup's M15 entry; otherwise use the labelled WAIT plan.
    entry = read_price(r"Entry \(نرخی کەندڵی M15\): USD ([0-9,]+\.[0-9]{2})")
    stop = read_price(r"Stop Loss \(SL\): USD ([0-9,]+\.[0-9]{2})")
    target = read_price(r"Take Profit \(TP\): USD ([0-9,]+\.[0-9]{2})")
    if entry is None:
        entry = read_price(r"Entry: USD ([0-9,]+\.[0-9]{2})")

    def price(value):
        return format(value, ",.2f") if value is not None else "N/A"

    if outcome not in ("BUY", "SELL"):
        return (
            "📊 Hano Trading Bot — Bitcoin\n"
            "⏱️ Timeframe: M15 — SCALPING\n"
            "🚦 SIGNAL: WAIT — NO ACTIVE TRADE\n"
            "🧭 PLAN: NONE — WAIT FOR CONFIRMATION\n"
            "💰 ENTRY: N/A\n"
            "🎯 TP: N/A\n"
            "🛑 SL: N/A\n"
            "⚠️ Do not open a trade until BUY/SELL is confirmed."
        )

    return (
        "📊 Hano Trading Bot — Bitcoin\n"
        "⏱️ Timeframe: M15 — SCALPING\n"
        f"🚦 SIGNAL: {outcome}\n"
        f"💰 ENTRY: {price(entry)}\n"
        f"🎯 TP: {price(target)}\n"
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
            ["📰 هەواڵ و فەندامێنتاڵ بە کوردیی سۆرانی (RSS؛ فلتەری ڕیسکی ماکرۆ چالاکە)"],
        )
        return risk or "زانیاریی پلانی ڕیسک بەردەست نییە."
    if section == "menu_reason":
        reason = next((line for line in lines if line.startswith("🧠 هۆکاری سەرەکی:")), "")
        technical = after_heading("🔎 نیشانە تەکنیکییەکان", ["🛡️ پلانی مامەڵە و ڕیسک"])
        return "\n".join(part for part in [reason, technical] if part) or "وردەکاریی هۆکاری سیگناڵ بەردەست نییە."
    if section == "menu_news":
        news = after_heading(
            "📰 هەواڵ و فەندامێنتاڵ بە کوردیی سۆرانی (RSS؛ فلتەری ڕیسکی ماکرۆ چالاکە)",
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
            "• Market Structure و BOS: پێکهاتە و شکاندنی پێکهاتە\n"
            "• MSS: نیشانەی گۆڕانی ئاراستە (خەمڵاندنی سادە)\n"
            "• FVG: بۆشایی نرخی نێوان کەندڵەکان\n"
            "• Liquidity Sweep: شکاندنی کاتیی بەرز/نزم و گەڕانەوە\n"
            "• Order Block: کەندڵی پێچەوانە پێش جوڵەی بەهێز (خەمڵاندن)\n"
            "• ADX: پێوانەی بەهێزی ترێند، نەک ئاراستە\n"
            "• PO3: خەمڵاندنی فازی کۆکردنەوە/فێڵ/بڵاوبوونەوە\n"
            "• Support/Resistance و ATR: پشتگیری/بەرگری و نوسان\n\n"
            "ئەم دۆزینەوانە خەمڵاندنی کۆدن؛ پێویستیان بە backtest و demo هەیە."
        )
    return "بەخێربێیت بۆ HANO BTC AI."


async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled Telegram update error", exc_info=context.error)


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
        message = await asyncio.to_thread(analyze)
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
        message = await asyncio.to_thread(analyze)
        await update.message.reply_text(minimal_signal_message(message), parse_mode="HTML")
    except Exception:
        log.exception("analysis failed")
        await update.message.reply_text(
            "⚠️ ئێستا نەتوانرا شیکاری BTC تەواو بکرێت.\n"
            "هۆکارەکە لە log ـی بۆتەکە تۆمار کراوە؛ تکایە دواتر /signal دووبارە بکەرەوە."
        )


async def signal_watch_job(context: ContextTypes.DEFAULT_TYPE):
    """Scan frequently; alerts are triggered by newly detected setups, not by a timer."""
    if not CHAT_ID:
        return
    try:
        m15, h1, h4 = await asyncio.gather(
            asyncio.to_thread(get_candles, "15m"),
            asyncio.to_thread(get_candles, "1h"),
            asyncio.to_thread(get_candles, "4h"),
        )
        candidates = await asyncio.to_thread(independent_signal_candidates, m15, h1, h4)
        fundamental_ok, fundamental_status, _ = await asyncio.to_thread(fundamental_news_guard)
        if not fundamental_ok:
            log.info("Independent signal scan blocked by macro-news guard: %s", fundamental_status)
            candidates = []

        bot_data = context.application.bot_data
        seen = bot_data.setdefault("seen_signal_keys", {})
        if not bot_data.get("independent_watcher_initialized"):
            for candidate in candidates:
                seen[candidate["key"]] = time.time()
            bot_data["independent_watcher_initialized"] = True
            log.info("Independent watcher baseline set; existing setups suppressed to avoid stale/repeated alerts (%d candidates)", len(candidates))
            return

        now = time.time()
        for key, seen_at in list(seen.items()):
            if now - seen_at > 48 * 60 * 60:
                seen.pop(key, None)

        for candidate in candidates:
            key = candidate["key"]
            if key in seen:
                continue
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=format_independent_signal(candidate, fundamental_status),
            )
            seen[key] = now
            log.info(
                "Independent signal sent: %s %s | strategy=%s | candle=%s",
                candidate["direction"], candidate["entry"], candidate["strategy"], candidate["candle_time"],
            )

        if len(seen) > 500:
            oldest = sorted(seen.items(), key=lambda item: item[1])[:len(seen) - 350]
            for key, _ in oldest:
                seen.pop(key, None)
    except Exception:
        log.exception("independent signal watcher failed")


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
