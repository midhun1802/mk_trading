"""
CHAKRA Pre-Market Engine
=========================
Runs at 8:00am ET daily. Fetches overnight data, computes bias,
key levels, and auto-generates conditional trade plans for each index.

Outputs:
  logs/premarket/premarket_YYYY-MM-DD.json  — structured game plan
  Posts to Discord via discord_notifier
  Dashboard reads via /api/premarket endpoint

Run manually:
    cd ~/trading-ai
    python3 backend/premarket/premarket_engine.py

Tickers: SPY, QQQ, IWM, SPX, DIA
"""

import asyncio
import httpx
import pandas as pd
import numpy as np
import os
import json
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

BASE_DIR    = Path(__file__).parent.parent.parent
LOG_DIR     = BASE_DIR / "logs/premarket"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_DIR / f"premarket_{date.today()}.log")),
    ]
)
log = logging.getLogger("CHAKRA.PreMarket")

# ── Config ────────────────────────────────────────────────────────────────────
POLYGON_KEY  = os.getenv("POLYGON_API_KEY")
POLYGON_BASE = "https://api.polygon.io"

INDICES = {
    "SPY":  {"name": "S&P 500 ETF",      "multiplier": 10,  "color": "#00d4ff"},
    "QQQ":  {"name": "Nasdaq 100 ETF",   "multiplier": 10,  "color": "#b17fff"},
    "IWM":  {"name": "Russell 2000 ETF", "multiplier": 5,   "color": "#ff7c2a"},
    "DIA":  {"name": "Dow Jones ETF",    "multiplier": 10,  "color": "#ffcc00"},
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rsi_calc(close: pd.Series, n: int = 14) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    ag    = gain.ewm(alpha=1/n, adjust=False).mean()
    al    = loss.ewm(alpha=1/n, adjust=False).mean()
    rs    = ag / al.replace(0, np.nan)
    r     = 100 - (100 / (1 + rs))
    return float(r.iloc[-1])

def atr_calc(h, l, c, n=14) -> float:
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1/n, adjust=False).mean().iloc[-1])


# ── Data fetchers ─────────────────────────────────────────────────────────────

async def fetch_daily_bars(ticker: str, days: int = 30) -> pd.DataFrame | None:
    """Fetch daily OHLCV bars."""
    end   = date.today()
    start = end - timedelta(days=days + 10)
    url   = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50, "apiKey": POLYGON_KEY}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r    = await client.get(url, params=params)
            data = r.json()
        results = data.get("results", [])
        if not results:
            log.warning(f"  {ticker}: no daily bars")
            return None
        df = pd.DataFrame(results).rename(columns={
            "t":"timestamp","o":"open","h":"high","l":"low","c":"close","v":"volume","vw":"vwap"
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df if len(df) >= 5 else None
    except Exception as e:
        log.error(f"  {ticker} daily fetch error: {e}")
        return None


async def fetch_premarket_bars(ticker: str) -> pd.DataFrame | None:
    """
    Fetch pre-market bars using the options snapshot prev-close.
    5-min intraday bars require Stocks Starter plan (403 on Options Advanced).
    Returns None gracefully — engine falls back to daily levels.
    """
    return None  # fallback to daily levels — intraday requires Stocks plan


async def fetch_arjun_signal(ticker: str) -> dict | None:
    """Load today's Arjun signal for this ticker from logs."""
    today   = date.today().isoformat()
    log_dir = BASE_DIR / "logs/signals"
    for pattern in [f"signals_{today}.json", f"{today}.json"]:
        path = log_dir / pattern
        if path.exists():
            try:
                with open(path) as f:
                    signals = json.load(f)
                for s in (signals if isinstance(signals, list) else [signals]):
                    if s.get("ticker") == ticker:
                        return s
            except:
                pass
    return None


# ── Analysis engine ───────────────────────────────────────────────────────────

def compute_key_levels(daily: pd.DataFrame, premarket: pd.DataFrame | None) -> dict:
    """Compute key price levels: VWAP, HOD/LOD, weekly high/low, EMAs, etc."""

    last  = daily.iloc[-1]
    prev  = daily.iloc[-2] if len(daily) >= 2 else last

    close     = float(last["close"])
    prev_close = float(prev["close"])
    prev_high  = float(prev["high"])
    prev_low   = float(prev["low"])
    prev_vwap  = float(prev.get("vwap", prev_close))

    # Weekly high/low (last 5 days)
    week = daily.tail(5)
    week_high = float(week["high"].max())
    week_low  = float(week["low"].min())

    # Monthly high/low (last 20 days)
    month      = daily.tail(20)
    month_high = float(month["high"].max())
    month_low  = float(month["low"].min())

    # EMAs
    e9  = float(ema(daily["close"], 9).iloc[-1])
    e20 = float(ema(daily["close"], 20).iloc[-1])
    e50 = float(ema(daily["close"], 50).iloc[-1])

    # ATR for range estimate
    atr = atr_calc(daily["high"], daily["low"], daily["close"], 14)

    # Pre-market levels
    pm_high = pm_low = pm_last = None
    if premarket is not None and len(premarket) > 0:
        pm_high = float(premarket["high"].max())
        pm_low  = float(premarket["low"].min())
        pm_last = float(premarket["close"].iloc[-1])

    # Key support/resistance
    levels = {
        "close":         round(close, 2),
        "prev_close":    round(prev_close, 2),
        "prev_high":     round(prev_high, 2),
        "prev_low":      round(prev_low, 2),
        "prev_vwap":     round(prev_vwap, 2),
        "week_high":     round(week_high, 2),
        "week_low":      round(week_low, 2),
        "month_high":    round(month_high, 2),
        "month_low":     round(month_low, 2),
        "ema9":          round(e9, 2),
        "ema20":         round(e20, 2),
        "ema50":         round(e50, 2),
        "atr":           round(atr, 2),
        "pm_high":       round(pm_high, 2) if pm_high else None,
        "pm_low":        round(pm_low, 2)  if pm_low  else None,
        "pm_last":       round(pm_last, 2) if pm_last else None,

        # Expected range for today based on ATR
        "expected_high": round(close + atr * 0.75, 2),
        "expected_low":  round(close - atr * 0.75, 2),

        # Magnet levels (round numbers attract price)
        "magnet_above":  round(round(close / 5) * 5 + 5, 2),
        "magnet_below":  round(round(close / 5) * 5 - 5, 2),
    }

    return levels


def compute_bias(daily: pd.DataFrame, arjun: dict | None) -> dict:
    """
    Compute pre-market directional bias.
    Combines: trend, momentum, Arjun ML signal, pre-market action.
    """
    close  = daily["close"]
    rsi    = rsi_calc(close, 14)
    e9     = float(ema(close, 9).iloc[-1])
    e20    = float(ema(close, 20).iloc[-1])
    e50    = float(ema(close, 50).iloc[-1])
    price  = float(close.iloc[-1])
    prev_c = float(close.iloc[-2]) if len(close) > 1 else price

    # 5-day momentum
    mom5   = (price - float(close.iloc[-5])) / float(close.iloc[-5]) * 100 if len(close) >= 5 else 0

    score  = 50  # neutral
    factors = []

    # Trend
    if e9 > e20 > e50:
        score += 15; factors.append("EMA bullish stack")
    elif e9 < e20 < e50:
        score -= 15; factors.append("EMA bearish stack")
    elif e9 > e20:
        score += 7;  factors.append("short-term uptrend")
    else:
        score -= 7;  factors.append("short-term downtrend")

    # RSI
    if rsi > 60:   score += 8;  factors.append(f"RSI {rsi:.0f} bullish")
    elif rsi < 40: score -= 8;  factors.append(f"RSI {rsi:.0f} bearish")
    elif rsi > 50: score += 3
    else:          score -= 3

    # Momentum
    if mom5 > 1:   score += 7;  factors.append(f"+{mom5:.1f}% 5-day momentum")
    elif mom5 < -1:score -= 7;  factors.append(f"{mom5:.1f}% 5-day momentum")

    # Price vs prev close
    if price > prev_c: score += 5;  factors.append("closed above prev close")
    else:              score -= 5;  factors.append("closed below prev close")

    # Arjun ML signal
    arjun_conf = 0
    arjun_signal = None
    if arjun:
        arjun_signal = arjun.get("signal", "HOLD")
        arjun_conf   = arjun.get("confidence", arjun.get("win_rate", 50))
        if arjun_signal == "BUY":
            boost = min(10, (arjun_conf - 50) / 5)
            score += boost; factors.append(f"Arjun ML: BUY ({arjun_conf:.0f}%)")
        elif arjun_signal == "SELL":
            boost = min(10, (arjun_conf - 50) / 5)
            score -= boost; factors.append(f"Arjun ML: SELL ({arjun_conf:.0f}%)")

    score = max(0, min(100, score))

    if score >= 65:   bias = "BULLISH";        bias_strength = "STRONG"
    elif score >= 55: bias = "BULLISH";        bias_strength = "MODERATE"
    elif score >= 45: bias = "NEUTRAL";        bias_strength = "MIXED"
    elif score >= 35: bias = "BEARISH";        bias_strength = "MODERATE"
    else:             bias = "BEARISH";        bias_strength = "STRONG"

    return {
        "bias":          bias,
        "strength":      bias_strength,
        "score":         round(score, 1),
        "rsi":           round(rsi, 1),
        "ema_stack":     "bullish" if e9 > e20 else "bearish",
        "momentum_5d":   round(mom5, 2),
        "arjun_signal":  arjun_signal,
        "arjun_conf":    round(arjun_conf, 1),
        "factors":       factors,
    }


def generate_game_plans(ticker: str, levels: dict, bias: dict) -> list[dict]:
    """
    Auto-generate conditional trade plans matching George's format:
    'If lows swept (sell-side taken) → BUY CALLS'
    'If highs swept (buy-side taken) → BUY PUTS'
    """
    close   = levels["close"]
    atr     = levels["atr"]
    p_high  = levels["prev_high"]
    p_low   = levels["prev_low"]
    p_vwap  = levels["prev_vwap"]
    w_high  = levels["week_high"]
    w_low   = levels["week_low"]
    pm_high = levels.get("pm_high")
    pm_low  = levels.get("pm_low")

    # Use pre-market levels if available, else previous day
    sweep_high = pm_high if pm_high else p_high
    sweep_low  = pm_low  if pm_low  else p_low

    # Target = 1.5x ATR from entry, Stop = 0.5x ATR
    call_entry  = round(sweep_high + atr * 0.1, 2)
    call_target = round(sweep_high + atr * 1.5, 2)
    call_stop   = round(sweep_high - atr * 0.5, 2)

    put_entry   = round(sweep_low - atr * 0.1, 2)
    put_target  = round(sweep_low - atr * 1.5, 2)
    put_stop    = round(sweep_low + atr * 0.5, 2)

    vwap_reclaim_target = round(p_vwap + atr * 1.0, 2)
    vwap_lose_target    = round(p_vwap - atr * 1.0, 2)

    plans = []

    # Plan 1 — Lows swept → long (sell-side liquidity taken)
    plans.append({
        "id":        "lows_swept",
        "condition": f"If lows swept (sell-side taken) below ${sweep_low:.2f}",
        "action":    "BUY CALLS",
        "direction": "LONG",
        "entry":     call_entry,
        "target":    call_target,
        "stop":      call_stop,
        "note":      f"Price reverses above VWAP at ${p_vwap:.2f}",
        "expiry":    "0DTE / same day",
        "confidence": "HIGH" if bias["bias"] == "BULLISH" else "MODERATE",
        "icon":      "🟢",
    })

    # Plan 2 — Highs swept → short (buy-side liquidity taken)
    plans.append({
        "id":        "highs_swept",
        "condition": f"If highs swept (buy-side taken) above ${sweep_high:.2f}",
        "action":    "BUY PUTS",
        "direction": "SHORT",
        "entry":     put_entry,
        "target":    put_target,
        "stop":      put_stop,
        "note":      f"Price reverses below VWAP at ${p_vwap:.2f}",
        "expiry":    "0DTE / same day",
        "confidence": "HIGH" if bias["bias"] == "BEARISH" else "MODERATE",
        "icon":      "🔴",
    })

    # Plan 3 — VWAP reclaim → long continuation
    if bias["bias"] == "BULLISH":
        plans.append({
            "id":        "vwap_reclaim",
            "condition": f"If price reclaims VWAP ${p_vwap:.2f} and holds",
            "action":    "BUY CALLS",
            "direction": "LONG",
            "entry":     round(p_vwap + 0.10, 2),
            "target":    vwap_reclaim_target,
            "stop":      round(p_vwap - atr * 0.3, 2),
            "note":      "Direction confirmed by VWAP reclaim after 9:45am",
            "expiry":    "0DTE / same day",
            "confidence": "MODERATE",
            "icon":      "🟡",
        })
    else:
        # Plan 3 bearish — VWAP rejection → puts
        plans.append({
            "id":        "vwap_rejection",
            "condition": f"If price rejects VWAP ${p_vwap:.2f} and rolls over",
            "action":    "BUY PUTS",
            "direction": "SHORT",
            "entry":     round(p_vwap - 0.10, 2),
            "target":    vwap_lose_target,
            "stop":      round(p_vwap + atr * 0.3, 2),
            "note":      "Confirmed by failed reclaim after open",
            "expiry":    "0DTE / same day",
            "confidence": "MODERATE",
            "icon":      "🟡",
        })

    return plans


def generate_levels_to_watch(levels: dict) -> list[dict]:
    """Generate ordered list of key levels to watch with labels."""
    items = []

    def add(label: str, price: float | None, note: str = "", importance: str = "normal"):
        if price is not None:
            items.append({"label": label, "price": price, "note": note, "importance": importance})

    add("📅 Prev High",    levels["prev_high"],  "Yesterday's high — buy-side liquidity",   "high")
    add("📅 Prev Low",     levels["prev_low"],   "Yesterday's low — sell-side liquidity",   "high")
    add("💧 Prev VWAP",    levels["prev_vwap"],  "Yesterday's VWAP — key magnet level",     "high")
    add("📅 PM High",      levels["pm_high"],    "Pre-market high",                         "high")
    add("📅 PM Low",       levels["pm_low"],     "Pre-market low",                          "high")
    add("📈 EMA 9",        levels["ema9"],       "Short-term trend",                        "normal")
    add("📈 EMA 20",       levels["ema20"],      "Medium-term trend",                       "normal")
    add("📈 EMA 50",       levels["ema50"],      "Long-term trend",                         "normal")
    add("📅 Week High",    levels["week_high"],  "5-day high — major resistance",           "normal")
    add("📅 Week Low",     levels["week_low"],   "5-day low — major support",               "normal")
    add("🎯 Magnet ↑",     levels["magnet_above"],"Round number above",                    "normal")
    add("🎯 Magnet ↓",     levels["magnet_below"],"Round number below",                    "normal")
    add("📊 Exp High",     levels["expected_high"],"ATR-based expected range top",          "low")
    add("📊 Exp Low",      levels["expected_low"], "ATR-based expected range bottom",       "low")

    # Sort by price descending
    items.sort(key=lambda x: x["price"], reverse=True)
    return items


# ── Main analysis runner ──────────────────────────────────────────────────────

async def analyze_ticker(ticker: str) -> dict:
    """Full pre-market analysis for one ticker."""
    log.info(f"  Analyzing {ticker}...")

    daily    = await fetch_daily_bars(ticker, days=60)
    premarket = await fetch_premarket_bars(ticker)
    arjun    = await fetch_arjun_signal(ticker)

    if daily is None or len(daily) < 5:
        log.warning(f"  {ticker}: insufficient data")
        return {"ticker": ticker, "error": "insufficient data"}

    levels     = compute_key_levels(daily, premarket)
    bias       = compute_bias(daily, arjun)
    game_plans = generate_game_plans(ticker, levels, bias)
    watch_list = generate_levels_to_watch(levels)

    result = {
        "ticker":      ticker,
        "name":        INDICES[ticker]["name"],
        "color":       INDICES[ticker]["color"],
        "timestamp":   datetime.now(ET).isoformat(),
        "date":        date.today().isoformat(),
        "bias":        bias,
        "levels":      levels,
        "watch_list":  watch_list,
        "game_plans":  game_plans,
        "has_premarket": premarket is not None,
    }

    log.info(f"  {ticker}: bias={bias['bias']} ({bias['strength']}) score={bias['score']}")
    return result


async def run_premarket_analysis() -> dict:
    """Analyze all indices and save to JSON."""
    log.info("\n" + "="*50)
    log.info("  CHAKRA PRE-MARKET ENGINE")
    log.info(f"  {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}")
    log.info("="*50)

    # Skip weekends
    if datetime.now(ET).weekday() >= 5:
        log.info("Weekend — skipping")
        return {}

    results = {}
    for ticker in INDICES:
        try:
            results[ticker] = await analyze_ticker(ticker)
            await asyncio.sleep(2.0)  # avoid 429 — Options Advanced has lower rate limit
        except Exception as e:
            log.error(f"  {ticker} error: {e}")
            results[ticker] = {"ticker": ticker, "error": str(e)}

    # Save to JSON
    output = {
        "date":       date.today().isoformat(),
        "generated":  datetime.now(ET).strftime("%I:%M %p ET"),
        "tickers":    results,
        "phase":      "PRE-MARKET",
    }

    out_path = LOG_DIR / f"premarket_{date.today()}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"  💾 Saved → {out_path}")

    # Post to Discord
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR))
        from backend.arka.discord_notifier import post_premarket_brief
        await post_premarket_brief(output)
        log.info("  📣 Posted to Discord")
    except Exception as e:
        log.error(f"  Discord post failed: {e}")

    return output


if __name__ == "__main__":
    asyncio.run(run_premarket_analysis())
