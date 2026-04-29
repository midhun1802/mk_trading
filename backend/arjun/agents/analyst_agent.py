"""
ARJUN Agent 1: Analyst Agent
Reads XGBoost model output + technical indicators, produces objective technical state.

PATCH v2: Pre-market gap modifier added.
"""
import json
import os
import pickle
import httpx
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODELS_DIR = BASE / "models"


def fetch_bars(ticker: str, timespan: str = "day", limit: int = 60) -> list:
    """Fetch recent OHLCV bars from Polygon."""
    end = datetime.now()
    start = end - timedelta(days=90)
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/{timespan}/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
    r = httpx.get(url, params={"adjusted": "true", "sort": "asc", "limit": limit, "apiKey": POLYGON_KEY},
                  timeout=httpx.Timeout(connect=5, read=10, write=5, pool=5))
    data = r.json()
    return data.get("results", [])


def fetch_premarket_price(ticker: str) -> dict:
    """
    Fetch current pre-market / extended hours price from Polygon snapshot.
    Returns gap % vs previous close.

    Used to apply a directional modifier to the daily technical bias score
    so that ARJUN's 8am signal reflects what the market is actually doing
    right now, not just yesterday's close.
    """
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_KEY},
            timeout=8,
        )
        snap = r.json().get("ticker", {})

        # Last trade price (works pre-market via Polygon extended hours feed)
        last_price = float(
            snap.get("lastTrade", {}).get("p", 0) or
            snap.get("day", {}).get("c", 0) or 0
        )
        prev_close = float(snap.get("prevDay", {}).get("c", 0) or 0)

        if last_price <= 0 or prev_close <= 0:
            return {"available": False, "gap_pct": 0.0, "price": 0, "prev_close": 0}

        gap_pct = ((last_price - prev_close) / prev_close) * 100

        # Flag whether we're actually in pre-market (before 9:30am ET)
        now_et = datetime.now()
        hour = now_et.hour
        minute = now_et.minute
        is_premarket = hour < 9 or (hour == 9 and minute < 30)

        return {
            "available":    True,
            "price":        round(last_price, 2),
            "prev_close":   round(prev_close, 2),
            "gap_pct":      round(gap_pct, 2),
            "is_premarket": is_premarket,
        }
    except Exception as e:
        return {"available": False, "gap_pct": 0.0, "error": str(e)}


def compute_indicators(bars: list) -> dict:
    """Compute all technical indicators from OHLCV bars."""
    if len(bars) < 20:
        return {}

    closes = np.array([b["c"] for b in bars])
    highs  = np.array([b["h"] for b in bars])
    lows   = np.array([b["l"] for b in bars])
    vols   = np.array([b["v"] for b in bars])

    def ema(arr, n):
        k = 2 / (n + 1)
        e = [arr[0]]
        for v in arr[1:]:
            e.append(v * k + e[-1] * (1 - k))
        return np.array(e)

    # EMAs
    ema9   = ema(closes, 9)[-1]
    ema20  = ema(closes, 20)[-1]
    ema50  = ema(closes, 50)[-1] if len(closes) >= 50 else closes[-1]
    ema200 = ema(closes, 200)[-1] if len(closes) >= 200 else closes[-1]

    # RSI
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-14:])
    avg_loss = np.mean(losses[-14:])
    rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 100

    # MACD
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = ema12 - ema26
    signal_line = ema(macd_line, 9)
    macd_hist = macd_line[-1] - signal_line[-1]
    macd_trend = "bullish" if macd_hist > 0 else "bearish"

    # ADX
    tr = np.maximum(highs[1:] - lows[1:],
         np.maximum(abs(highs[1:] - closes[:-1]), abs(lows[1:] - closes[:-1])))
    adx_val = float(np.mean(tr[-14:])) / closes[-1] * 100 if closes[-1] > 0 else 0

    # Bollinger Bands
    sma20 = np.mean(closes[-20:])
    std20 = np.std(closes[-20:])
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_pos = (closes[-1] - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else 0.5

    # Volume
    avg_vol = np.mean(vols[-20:])
    vol_ratio = float(vols[-1]) / avg_vol if avg_vol > 0 else 1.0

    # ATR
    atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else 0

    # 52w high/low
    w52_high = float(np.max(closes[-252:])) if len(closes) >= 252 else float(np.max(closes))
    w52_low  = float(np.min(closes[-252:])) if len(closes) >= 252 else float(np.min(closes))
    pct_52h  = (closes[-1] - w52_high) / w52_high * 100
    pct_52l  = (closes[-1] - w52_low)  / w52_low  * 100

    price = float(closes[-1])
    prev  = float(closes[-2]) if len(closes) >= 2 else price

    return {
        "price":             round(price, 2),
        "prev_close":        round(prev, 2),
        "price_change_1d":   round((price - prev) / prev * 100, 2),
        "ema9":              round(ema9, 2),
        "ema20":             round(ema20, 2),
        "ema50":             round(ema50, 2),
        "ema200":            round(ema200, 2),
        "above_ema9":        price > ema9,
        "above_ema20":       price > ema20,
        "above_ema50":       price > ema50,
        "above_ema200":      price > ema200,
        "golden_cross":      ema9 > ema20,
        "rsi":               round(rsi, 1),
        "rsi_signal":        "overbought" if rsi > 70 else "oversold" if rsi < 30 else "neutral",
        "macd_hist":         round(float(macd_hist), 4),
        "macd_trend":        macd_trend,
        "adx":               round(adx_val, 1),
        "bb_position":       round(bb_pos, 3),
        "bb_upper":          round(bb_upper, 2),
        "bb_lower":          round(bb_lower, 2),
        "volume_ratio":      round(vol_ratio, 2),
        "atr":               round(atr, 2),
        "pct_from_52w_high": round(pct_52h, 1),
        "pct_from_52w_low":  round(pct_52l, 1),
        "sma20":             round(sma20, 2),
    }


def load_xgboost_signal(ticker: str, indicators: dict) -> dict:
    """Load XGBoost model and get ML signal."""
    model_path = MODELS_DIR / f"{ticker}_model.pkl"
    if not model_path.exists():
        return {"ml_signal": "HOLD", "ml_confidence": 50.0, "ml_available": False}

    try:
        with open(model_path, "rb") as f:
            model = pickle.load(f)

        feature_cols = [
            "price_change_1d", "rsi", "macd_hist", "adx",
            "bb_position", "volume_ratio", "atr",
            "pct_from_52w_high", "pct_from_52w_low",
            "above_ema50", "above_ema200", "golden_cross"
        ]
        features = np.array([[
            indicators.get(c, 0) for c in feature_cols
        ]])

        proba = model.predict_proba(features)[0]
        pred  = model.predict(features)[0]

        classes = list(model.classes_)
        conf = float(max(proba) * 100)

        if hasattr(pred, 'item'):
            pred = pred.item()

        signal_map = {0: "SELL", 1: "HOLD", 2: "BUY"}
        signal = signal_map.get(int(pred), "HOLD")

        return {
            "ml_signal":     signal,
            "ml_confidence": round(conf, 1),
            "ml_available":  True,
            "ml_probas":     {k: round(float(v)*100, 1) for k, v in zip(classes, proba)}
        }
    except Exception as e:
        return {"ml_signal": "HOLD", "ml_confidence": 50.0, "ml_available": False, "error": str(e)}


def run(ticker: str) -> dict:
    """
    Analyst Agent: Fetch data, compute indicators, run XGBoost model.
    Returns objective technical state for the ticker.
    """
    print(f"  [Analyst] Analyzing {ticker}...")

    bars = fetch_bars(ticker, "day", 60)
    if not bars:
        return {"ticker": ticker, "error": "No bar data", "bias": "NEUTRAL", "score": 50}

    indicators = compute_indicators(bars)
    ml = load_xgboost_signal(ticker, indicators)

    # ── Pre-market gap data — fetched early, applied after scoring ────────
    premarket = fetch_premarket_price(ticker)

    price = indicators.get("price", 0)
    rsi   = indicators.get("rsi", 50)
    ema9  = indicators.get("ema9", price)
    ema20 = indicators.get("ema20", price)
    ema50 = indicators.get("ema50", price)
    macd  = indicators.get("macd_trend", "neutral")
    above_200 = indicators.get("above_ema200", True)

    # Score the technical bias (0-100)
    score = 50
    bull_factors = []
    bear_factors = []

    if price > ema9:    score += 5;  bull_factors.append("Price above EMA 9")
    else:               score -= 5;  bear_factors.append("Price below EMA 9")
    if price > ema20:   score += 5;  bull_factors.append("Price above EMA 20")
    else:               score -= 5;  bear_factors.append("Price below EMA 20")
    if price > ema50:   score += 5;  bull_factors.append("Price above EMA 50")
    else:               score -= 5;  bear_factors.append("Price below EMA 50")
    if above_200:       score += 5;  bull_factors.append("Price above EMA 200")
    else:               score -= 5;  bear_factors.append("Price below EMA 200 — long-term bearish")
    if ema9 > ema20:    score += 5;  bull_factors.append("EMA 9/20 golden cross")
    else:               score -= 5;  bear_factors.append("EMA 9/20 death cross")
    if rsi < 30:        score += 10; bull_factors.append(f"RSI oversold at {rsi}")
    elif rsi > 70:      score -= 10; bear_factors.append(f"RSI overbought at {rsi}")
    if macd == "bullish": score += 8; bull_factors.append("MACD bullish momentum")
    else:                 score -= 8; bear_factors.append("MACD bearish momentum")
    if indicators.get("volume_ratio", 1) > 1.5:
        if macd == "bullish": score += 5; bull_factors.append("High volume confirming move")
        else:                 score -= 5; bear_factors.append("High volume on down move")

    # ML model weight
    if ml["ml_available"]:
        if ml["ml_signal"] == "BUY":    score += 8; bull_factors.append(f"XGBoost BUY ({ml['ml_confidence']}% conf)")
        elif ml["ml_signal"] == "SELL": score -= 8; bear_factors.append(f"XGBoost SELL ({ml['ml_confidence']}% conf)")

    # ── MTF Confluence adjustment ──────────────────────────────────────────
    try:
        from backend.arjun.modules.mtf_confluence import apply_mtf_confluence
        m30_signal = "BULLISH" if score >= 60 else "BEARISH" if score <= 40 else "NEUTRAL"
        mtf = apply_mtf_confluence(ticker, m30_signal, score)
        score = mtf["final_score"]
        if mtf["mtf_bonus"] != 0:
            label = "MTF confluence" if mtf["mtf_bonus"] > 0 else "MTF counter-trend"
            if mtf["mtf_bonus"] > 0: bull_factors.append(f"{label}: {mtf['mtf_bonus']:+d}pts ({mtf['d1_bias']} D1, {mtf['m5_bias']} M5)")
            else:                    bear_factors.append(f"{label}: {mtf['mtf_bonus']:+d}pts ({mtf['d1_bias']} D1, {mtf['m5_bias']} M5)")
    except Exception as _mtf_err:
        mtf = {"d1_bias": "NEUTRAL", "m5_bias": "NEUTRAL", "mtf_bonus": 0,
               "final_score": score, "reasons": [], "error": str(_mtf_err)}

    score = max(0, min(100, score))

    # ── Pre-market gap modifier ────────────────────────────────────────────
    # Applied AFTER all technical scoring, BEFORE bias label is set.
    # Lets the 8am signal reflect what the market is actually doing right now,
    # not just what happened at yesterday's close.
    #
    # Scale (intentionally asymmetric — gap is one input, not the only input):
    #   > +1.0%  strong gap up   → +15 pts
    #   +0.5–1%  moderate gap up → +8  pts
    #   +0.2–0.5% mild gap up   → +4  pts
    #   ±0.2%   flat             →  0  pts
    #   -0.2–-0.5% mild gap dn  → -4  pts
    #   -0.5–-1%  moderate gap  → -8  pts
    #   < -1.0%  strong gap dn  → -15 pts
    #
    premarket_modifier = 0
    if premarket.get("available"):
        gap = premarket["gap_pct"]
        if   gap >  1.0:  premarket_modifier = +15; bull_factors.append(f"Pre-market gap UP +{gap:.2f}% (+15 pts)")
        elif gap >  0.5:  premarket_modifier = +8;  bull_factors.append(f"Pre-market gap up +{gap:.2f}% (+8 pts)")
        elif gap >  0.2:  premarket_modifier = +4;  bull_factors.append(f"Pre-market mild gap up +{gap:.2f}% (+4 pts)")
        elif gap < -1.0:  premarket_modifier = -15; bear_factors.append(f"Pre-market gap DOWN {gap:.2f}% (-15 pts)")
        elif gap < -0.5:  premarket_modifier = -8;  bear_factors.append(f"Pre-market gap down {gap:.2f}% (-8 pts)")
        elif gap < -0.2:  premarket_modifier = -4;  bear_factors.append(f"Pre-market mild gap down {gap:.2f}% (-4 pts)")

        if premarket_modifier != 0:
            score = max(0, min(100, score + premarket_modifier))
            print(f"  [Analyst] Pre-market gap {gap:+.2f}% → modifier {premarket_modifier:+d} → score {score}")

    score = max(0, min(100, score))
    bias = "BULLISH" if score >= 60 else "BEARISH" if score <= 40 else "NEUTRAL"

    summary = f"RSI {rsi} ({'oversold' if rsi < 30 else 'overbought' if rsi > 70 else 'neutral'}), "
    summary += f"MACD {macd}, "
    summary += f"price {'above' if price > ema20 else 'below'} EMA 20 ({ema20}), "
    summary += f"EMA 9/20 {'golden' if ema9 > ema20 else 'death'} cross"
    if not above_200:
        summary += f", BELOW EMA 200 ({indicators.get('ema200', 0)}) — structural weakness"
    if premarket.get("available") and abs(premarket["gap_pct"]) > 0.2:
        gap = premarket["gap_pct"]
        summary += f", pre-market {'up' if gap > 0 else 'down'} {gap:+.2f}%"

    return {
        "ticker":       ticker,
        "bias":         bias,
        "score":        score,
        "summary":      summary,
        "bull_factors": bull_factors,
        "bear_factors": bear_factors,
        "indicators":   indicators,
        "ml":           ml,
        "mtf":          mtf,
        "premarket":    premarket,
    }


if __name__ == "__main__":
    result = run("SPY")
    print(json.dumps(result, indent=2, default=str))
