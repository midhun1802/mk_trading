"""
MTF Confluence Engine — Multi-Timeframe Signal Alignment
Adds +15 to conviction when Daily + 30min + 5min all aligned.
Subtracts up to -20 when trading against daily trend.
File: backend/arjun/modules/mtf_confluence.py
"""
import os, httpx
from datetime import date, timedelta
from dotenv import load_dotenv
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")


def _fetch_bars(ticker: str, multiplier: int, timespan: str, limit: int) -> list:
    """Fetch OHLC bars from Polygon."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=10)).isoformat()
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start}/{end}",
            params={"apiKey": POLYGON_KEY, "adjusted": "true", "sort": "asc", "limit": limit},
            timeout=10,
        )
        return r.json().get("results", [])
    except Exception:
        return []


def _ema(values: list, period: int) -> float:
    """Simple EMA calculation."""
    if len(values) < period:
        return values[-1] if values else 0
    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return round(ema, 4)


def _get_ema_bias(bars: list, fast: int = 9, slow: int = 20) -> str:
    """Returns BULLISH / BEARISH / NEUTRAL based on EMA crossover."""
    if len(bars) < slow:
        return "NEUTRAL"
    closes = [b["c"] for b in bars]
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    price    = closes[-1]
    if price > ema_fast > ema_slow:
        return "BULLISH"
    if price < ema_fast < ema_slow:
        return "BEARISH"
    return "NEUTRAL"


def get_daily_bias(ticker: str) -> str:
    """D1 — Daily EMA 9/20 bias."""
    bars = _fetch_bars(ticker, 1, "day", 30)
    return _get_ema_bias(bars, fast=9, slow=20)


def get_30min_bias(ticker: str) -> str:
    """M30 — 30-minute EMA 9/20 bias."""
    bars = _fetch_bars(ticker, 30, "minute", 50)
    return _get_ema_bias(bars, fast=9, slow=20)


def get_5min_bias(ticker: str) -> str:
    """M5 — 5-minute EMA 9/20 bias (entry timing)."""
    bars = _fetch_bars(ticker, 5, "minute", 30)
    return _get_ema_bias(bars, fast=9, slow=20)


def apply_mtf_confluence(ticker: str, m30_signal: str, m30_score: int) -> dict:
    """
    Fetch D1 + M5 and apply confluence bonus/penalty to M30 score.

    Rules:
      D1 + M30 + M5 all aligned  → +15 pts (strong confluence)
      D1 + M30 aligned, M5 neutral → +8 pts
      D1 opposite to M30           → -20 pts (counter-trend)
      M5 opposite to M30           → -10 pts (bad entry timing)
    """
    d1_bias = get_daily_bias(ticker)
    m5_bias = get_5min_bias(ticker)

    bonus   = 0
    reasons = []

    # Daily trend alignment
    if d1_bias == m30_signal and m5_bias == m30_signal:
        bonus += 15
        reasons.append(f"All 3 timeframes aligned {m30_signal} — strong confluence (+15)")
    elif d1_bias == m30_signal and m5_bias == "NEUTRAL":
        bonus += 8
        reasons.append(f"D1 + M30 aligned {m30_signal}, M5 neutral (+8)")
    elif d1_bias == m30_signal and m5_bias != m30_signal:
        bonus += 3
        reasons.append(f"D1 + M30 aligned {m30_signal}, M5 lagging (+3)")
    elif d1_bias != m30_signal and d1_bias != "NEUTRAL":
        bonus -= 20
        reasons.append(f"Trading against D1 trend ({d1_bias} vs {m30_signal} signal) — CAUTION (-20)")
    elif m5_bias != m30_signal and m5_bias != "NEUTRAL":
        bonus -= 10
        reasons.append(f"M5 timing against signal ({m5_bias}) — wait for alignment (-10)")

    new_score = min(100, max(0, m30_score + bonus))

    return {
        "ticker":        ticker,
        "m30_signal":    m30_signal,
        "m30_score_raw": m30_score,
        "d1_bias":       d1_bias,
        "m5_bias":       m5_bias,
        "mtf_bonus":     bonus,
        "final_score":   new_score,
        "reasons":       reasons,
        "confluent":     bonus > 0,
        "counter_trend": bonus <= -20,
    }


if __name__ == "__main__":
    import json
    result = apply_mtf_confluence("SPY", "BULLISH", 72)
    print(json.dumps(result, indent=2))
