"""
ARJUN Agent 2: Bull Agent
Argues FOR the long position using technical summary + news sentiment.
Uses Claude API for reasoning.
"""
import json
import os
import httpx
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pathlib import Path


class _SafeEncoder(json.JSONEncoder):
    """Handles numpy booleans, ints, floats that standard json chokes on."""
    def default(self, obj):
        if isinstance(obj, (np.bool_,)):          return bool(obj)
        if isinstance(obj, (np.integer,)):         return int(obj)
        if isinstance(obj, (np.floating,)):        return float(obj)
        if isinstance(obj, (np.ndarray,)):         return obj.tolist()
        return super().default(obj)

def _dumps(obj):
    return json.dumps(obj, indent=2, cls=_SafeEncoder)

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

POLYGON_KEY   = os.getenv("POLYGON_API_KEY", "")

def _fetch_dark_pool_data(ticker: str) -> dict:
    """Fetch recent trades from Polygon and detect dark pool bias."""
    try:
        from backend.arjun.modules.dark_pool_scanner import detect_smart_money_activity
        r = httpx.get(
            f"https://api.polygon.io/v3/trades/{ticker}",
            params={"apiKey": POLYGON_KEY, "limit": 500, "order": "desc"},
            timeout=10,
        )
        trades = r.json().get("results", [])
        # Polygon trades use 'conditions' not 'exchange' directly — map exchange field
        normalized = [{"exchange": t.get("exchange", 0), "size": t.get("size", 0),
                       "side": "buy" if t.get("conditions") and 14 not in t.get("conditions", []) else "sell"}
                      for t in trades]
        return detect_smart_money_activity(ticker, normalized)
    except Exception as e:
        return {"dark_pool_bias": "NEUTRAL", "dark_pool_volume": 0, "conviction": 0, "error": str(e)}

def _fetch_news_sentiment(ticker: str) -> dict:
    """Fetch and score news sentiment for ticker."""
    try:
        from backend.arjun.modules.news_sentiment import analyze_news_sentiment
        return analyze_news_sentiment(ticker)
    except Exception as e:
        return {"sentiment": "NEUTRAL", "score": 0.0, "bull_boost": 0.0, "bear_boost": 0.0,
                "top_headlines": [], "error": str(e)}
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def fetch_news_sentiment(ticker: str) -> dict:
    """Fetch recent news headlines from Polygon for the ticker."""
    url = f"https://api.polygon.io/v2/reference/news"
    try:
        r = httpx.get(url, params={"ticker": ticker, "limit": 8, "apiKey": POLYGON_KEY}, timeout=10)
        articles = r.json().get("results", [])
        headlines = [a.get("title", "") for a in articles if a.get("title")]
        return {
            "headlines":     headlines[:6],
            "article_count": len(headlines),
            "fetched_at":    datetime.now().isoformat(),
        }
    except Exception as e:
        return {"headlines": [], "article_count": 0, "error": str(e)}


def run(ticker: str, analyst_result: dict, gex_data: dict) -> dict:
    """
    Bull Agent: Build the strongest possible case FOR going long on this ticker.
    Returns score 0-100 and list of arguments.
    """
    print(f"  [Bull] Building bull case for {ticker}...")

    ind      = analyst_result.get("indicators", {})
    ml       = analyst_result.get("ml", {})
    bias     = analyst_result.get("bias", "NEUTRAL")
    a_score  = analyst_result.get("score", 50)
    news     = fetch_news_sentiment(ticker)
    dp_data  = _fetch_dark_pool_data(ticker)
    sent_data = _fetch_news_sentiment(ticker)

    price    = ind.get("price", 0)
    rsi      = ind.get("rsi", 50)
    ema9     = ind.get("ema9", price)
    ema20    = ind.get("ema20", price)
    ema50    = ind.get("ema50", price)
    ema200   = ind.get("ema200", price)
    macd     = ind.get("macd_trend", "neutral")
    vol_r    = ind.get("volume_ratio", 1.0)
    bb_pos   = ind.get("bb_position", 0.5)
    atr      = ind.get("atr", 0)
    pct_52l  = ind.get("pct_from_52w_low", 0)
    pct_52h  = ind.get("pct_from_52w_high", 0)

    gex_regime   = gex_data.get("regime", "UNKNOWN")
    call_wall    = gex_data.get("call_wall", 0)
    put_wall     = gex_data.get("put_wall", 0)
    room_to_call = gex_data.get("room_to_call", 0)

    # ── Build structured prompt for Claude ──────────────────────────────
    context = {
        "ticker":     ticker,
        "price":      price,
        "indicators": {
            "rsi":           rsi,
            "macd":          macd,
            "ema9":          ema9,
            "ema20":         ema20,
            "ema50":         ema50,
            "ema200":        ema200,
            "golden_cross":  ind.get("golden_cross", False),
            "volume_ratio":  vol_r,
            "bb_position":   bb_pos,
            "atr":           atr,
            "pct_52w_high":  pct_52h,
            "pct_52w_low":   pct_52l,
        },
        "ml_signal":       ml.get("ml_signal", "HOLD"),
        "ml_confidence":   ml.get("ml_confidence", 50),
        "analyst_bias":    bias,
        "analyst_score":   a_score,
        "gex_regime":      gex_regime,
        "call_wall":       call_wall,
        "put_wall":        put_wall,
        "room_to_call_pts": room_to_call,
        "recent_headlines": news["headlines"],
        "dark_pool_bias":    dp_data.get("dark_pool_bias", "NEUTRAL"),
        "dark_pool_volume":  dp_data.get("dark_pool_volume", 0),
        "dark_pool_conviction": dp_data.get("conviction", 0),
        "news_sentiment":    sent_data.get("sentiment", "NEUTRAL"),
        "news_score":        sent_data.get("score", 0.0),
        "news_bull_boost":   sent_data.get("bull_boost", 0.0),
        "sentiment_headlines": sent_data.get("top_headlines", []),
    }

    prompt = f"""You are the Bull Agent in a multi-agent trading system analyzing {ticker}.

Your job: Build the STRONGEST possible bull case for going LONG on {ticker} right now.
Be an advocate — find every reason this could go up. Be specific with numbers.

Market Data:
{_dumps(context)}

Respond ONLY with valid JSON in this exact structure:
{{
  "score": <integer 0-100, your conviction in the bull case>,
  "arguments": [
    "<specific bull argument with numbers>",
    "<specific bull argument with numbers>",
    "<specific bull argument with numbers>"
  ],
  "key_catalyst": "<single most important bullish factor>",
  "target_price": <float, your bull target>,
  "invalidation": "<what would kill the bull case>"
}}

Rules:
- Score 70+ only if you have 3+ strong technical confirmations
- Always reference specific price levels and indicator values
- Include GEX regime in your reasoning if relevant
- Consider news headlines for fundamental support
- Be honest about the score — don't inflate it"""

    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 600,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=12,
        )
        raw = r.json()["content"][0]["text"].strip()
        # Strip any markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        result["news_headlines"] = news["headlines"]
        result["ticker"] = ticker
        return result

    except Exception as e:
        # Fallback: rule-based scoring
        score = 50
        arguments = []
        if bias == "BULLISH":          score += 15; arguments.append(f"Analyst bias: BULLISH (score {a_score})")
        if macd == "bullish":          score += 10; arguments.append(f"MACD bullish momentum confirmed")
        if rsi < 40:                   score += 10; arguments.append(f"RSI oversold at {rsi} — bounce setup")
        if ind.get("golden_cross"):    score += 8;  arguments.append(f"EMA 9/20 golden cross active")
        if price > ema50:              score += 5;  arguments.append(f"Holding above EMA 50 (${ema50})")
        if gex_regime == "POSITIVE_GAMMA": score += 5; arguments.append("GEX positive — dealer stabilization")
        if vol_r > 1.3 and macd == "bullish": score += 5; arguments.append(f"Volume surge ({vol_r}x) on bullish move")
        if ml.get("ml_signal") == "BUY": score += 8; arguments.append(f"XGBoost BUY signal ({ml.get('ml_confidence')}% conf)")
        target = round(price * 1.03, 2)
        return {
            "score":        min(100, score),
            "arguments":    arguments or ["Insufficient data for strong bull case"],
            "key_catalyst": arguments[0] if arguments else "Weak technical setup",
            "target_price": target,
            "invalidation": f"Break below EMA 50 (${ema50})",
            "news_headlines": news["headlines"],
            "ticker":        ticker,
            "fallback":      True,
            "error":         str(e),
        }


if __name__ == "__main__":
    from analyst_agent import run as analyst_run
    from gex_calculator import get_gex_for_ticker

    analyst = analyst_run("SPY")
    gex     = get_gex_for_ticker("SPY", analyst["indicators"]["price"])
    result  = run("SPY", analyst, gex)
    print(json.dumps(result, indent=2))

# ── Dark Pool + Sentiment boost (append inside build_bull_case) ───────────

def build_bull_case_v2(ticker, technical_summary, dark_pool_data, news_sentiment):
    """Enhanced bull case with dark pool and news sentiment inputs."""
    arguments = []
    score     = 50

    if technical_summary.get('bias') == 'BULLISH':
        arguments.append(f"Technical bias: {technical_summary.get('summary','')}")
        score += 20

    # Dark pool boost
    if dark_pool_data.get('dark_pool_bias') == 'BULLISH':
        arguments.append(f"Dark pool accumulation: {dark_pool_data.get('dark_pool_volume',0):,} shares")
        score += dark_pool_data.get('conviction', 0) * 0.3

    # News sentiment boost
    if news_sentiment.get('sentiment') == 'POSITIVE':
        arguments.append(f"Positive news sentiment: {news_sentiment.get('score', 0):.2f}")
        for h in news_sentiment.get('top_headlines', []):
            arguments.append(f"• {h}")
        score += news_sentiment.get('score', 0) * 20


    # S2_VEX_BULL — VEX Vanna wired by patchsession2.py
    try:
        import json as _j, pathlib as _pl
        _vex_f = _pl.Path("logs/chakra/vex_latest.json")
        if _vex_f.exists():
            _vex = _j.loads(_vex_f.read_text())
            _sig = _vex.get("signal", "NEUTRAL")
            _ivc = abs(float(_vex.get("iv_change_pct", 0)))
            if _sig == "MELT_UP" or _ivc >= 5:
                score = min(100, score + 15)
                arguments.append(f"VEX MELT_UP / IV+{_ivc:.1f}% → +15")
            elif _sig == "IV_CRUSH":
                score = min(100, score + 10)
                arguments.append("VEX IV_CRUSH tailwind → +10")
    except Exception:
        pass

    # S3_IVSKEW_BULL — IV Skew wired by patchsession3.py
    try:
        import json as _j, pathlib as _pl
        _sk_f = _pl.Path("logs/chakra/ivskew_latest.json")
        if _sk_f.exists():
            _sk = _j.loads(_sk_f.read_text())
            _sksig = _sk.get("signal", "NEUTRAL")
            if _sksig == "MELT_UP":
                score = min(100, score + 10)
                arguments.append("IV Skew MELT_UP +10")
            elif _sksig == "SQUEEZE":
                score = min(100, score + 8)
                arguments.append("IV Skew SQUEEZE +8")
    except Exception:
        pass

    # S4_COT_BULL — COT Smart Money wired by patchsession4.py
    try:
        import json as _j, pathlib as _pl
        _cot_f = _pl.Path("logs/chakra/cot_latest.json")
        if _cot_f.exists():
            _cot = _j.loads(_cot_f.read_text())
            _cotsig = _cot.get("signal", "NEUTRAL")
            if _cotsig == "CROWDED_SHORT":
                score = min(100, score + 12)
                arguments.append("COT CROWDED_SHORT — smart money contrarian +12")
            elif _cotsig == "CROWDED_LONG":
                score = max(0, score - 8)
                arguments.append("COT CROWDED_LONG — exhaustion risk -8")
    except Exception:
        pass
    return {'score': min(100, score), 'arguments': arguments}
