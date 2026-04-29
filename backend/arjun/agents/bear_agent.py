"""
ARJUN Agent 3: Bear Agent
Argues AGAINST long / FOR the short case. Flags risk factors.
Uses Claude API for devil's advocate reasoning.
"""
import json
import os
import httpx
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path


class _SafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.bool_,)):   return bool(obj)
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)):return float(obj)
        if isinstance(obj, (np.ndarray,)): return obj.tolist()
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
        pass
    # S2_VEX_BEAR — VEX Vanna wired by patchsession2.py
    try:
        import json as _j, pathlib as _pl
        _vex_f = _pl.Path("logs/chakra/vex_latest.json")
        if _vex_f.exists():
            _vex = _j.loads(_vex_f.read_text())
            _sig = _vex.get("signal", "NEUTRAL")
            _ivc = abs(float(_vex.get("iv_change_pct", 0)))
            if _sig == "SELLOFF" or _ivc >= 5:
                score = min(100, score + 15)
                arguments.append(f"VEX SELLOFF/IV+{_ivc:.1f}% → +15")
            elif _sig == "IV_CRUSH":
                score = max(0, score - 10)
                arguments.append("VEX IV_CRUSH headwind → -10")
    except Exception:
        pass

    # S3_IVSKEW_BEAR — IV Skew wired by patchsession3.py
    try:
        import json as _j, pathlib as _pl
        _sk_f = _pl.Path("logs/chakra/ivskew_latest.json")
        if _sk_f.exists():
            _sk = _j.loads(_sk_f.read_text())
            _sksig = _sk.get("signal", "NEUTRAL")
            if _sksig == "BEARISH_FEAR":
                score = min(100, score + 15)
                arguments.append("IV Skew BEARISH_FEAR +15")
            elif _sksig == "MELT_UP":
                score = max(0, score - 10)
                arguments.append("IV Skew MELT_UP headwind -10")
    except Exception:
        pass

    # S4_COT_BEAR — COT Smart Money wired by patchsession4.py
    try:
        import json as _j, pathlib as _pl
        _cot_f = _pl.Path("logs/chakra/cot_latest.json")
        if _cot_f.exists():
            _cot = _j.loads(_cot_f.read_text())
            _cotsig = _cot.get("signal", "NEUTRAL")
            if _cotsig == "CROWDED_LONG":
                score = min(100, score + 10)
                arguments.append("COT CROWDED_LONG — smart money bearish +10")
            elif _cotsig == "CROWDED_SHORT":
                score = max(0, score - 8)
                arguments.append("COT CROWDED_SHORT — squeeze risk -8")
    except Exception:
        pass
        return {"sentiment": "NEUTRAL", "score": 0.0, "bull_boost": 0.0, "bear_boost": 0.0,
                "top_headlines": [], "error": str(e)}
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def fetch_macro_context(ticker: str) -> dict:
    """Get SPY context if not already analyzing SPY — macro always matters."""
    if ticker == "SPY":
        return {}
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/SPY/prev",
            params={"apiKey": POLYGON_KEY},
            timeout=8,
        )
        result = r.json().get("results", [{}])[0]
        spy_chg = round((result.get("c", 0) - result.get("o", 0)) / result.get("o", 1) * 100, 2)
        return {"spy_prev_day_pct": spy_chg, "spy_close": result.get("c", 0)}
    except Exception:
        return {}


def calculate_curvature_risk(price_history: list) -> dict:
    """
    Calculate scalar curvature of recent price path.
    High curvature = sharp turns = elevated risk.
    """
    try:
        import numpy as np
        from scipy.interpolate import splprep, splev

        if len(price_history) < 10:
            return {"curvature": 0.0, "regime": "UNKNOWN", "position_mult": 1.0}

        prices = np.array(price_history[-20:], dtype=float)
        t = np.arange(len(prices), dtype=float)

        # Normalize to avoid scale issues
        p_norm = (prices - prices.mean()) / (prices.std() + 1e-8)

        tck, u = splprep([t, p_norm], s=0.5, k=min(3, len(prices) - 1))
        dx, dy     = splev(u, tck, der=1)
        d2x, d2y   = splev(u, tck, der=2)

        num = np.abs(dx * d2y - dy * d2x)
        den = (dx**2 + dy**2) ** 1.5
        curvature = float(np.mean(num / (den + 1e-8)))

        if curvature > 0.5:
            regime = "HIGH_RISK"
            mult   = 0.5
        elif curvature > 0.2:
            regime = "MODERATE_RISK"
            mult   = 0.75
        else:
            regime = "LOW_RISK"
            mult   = 1.0

        return {"curvature": round(curvature, 4), "regime": regime, "position_mult": mult}

    except Exception:
        # scipy not available — use simpler volatility proxy
        try:
            import numpy as np
            prices = np.array(price_history[-20:], dtype=float)
            returns = np.diff(prices) / prices[:-1]
            vol = float(np.std(returns))
            regime = "HIGH_RISK" if vol > 0.02 else "MODERATE_RISK" if vol > 0.01 else "LOW_RISK"
            mult   = 0.5 if vol > 0.02 else 0.75 if vol > 0.01 else 1.0
            return {"curvature": round(vol * 10, 4), "regime": regime, "position_mult": mult}
        except Exception:
            return {"curvature": 0.0, "regime": "UNKNOWN", "position_mult": 1.0}


def run(ticker: str, analyst_result: dict, gex_data: dict) -> dict:
    """
    Bear Agent: Build the strongest possible case AGAINST going long (or FOR short).
    Returns score 0-100 and list of risk arguments.
    """
    print(f"  [Bear] Building bear case for {ticker}...")

    ind      = analyst_result.get("indicators", {})
    ml       = analyst_result.get("ml", {})
    bias     = analyst_result.get("bias", "NEUTRAL")
    a_score  = analyst_result.get("score", 50)

    price    = ind.get("price", 0)
    rsi      = ind.get("rsi", 50)
    ema50    = ind.get("ema50", price)
    ema200   = ind.get("ema200", price)
    macd     = ind.get("macd_trend", "neutral")
    vol_r    = ind.get("volume_ratio", 1.0)
    bb_pos   = ind.get("bb_position", 0.5)
    atr      = ind.get("atr", 0)
    pct_52h  = ind.get("pct_from_52w_high", 0)
    adx      = ind.get("adx", 0)

    gex_regime   = gex_data.get("regime", "UNKNOWN")
    call_wall    = gex_data.get("call_wall", 0)
    put_wall     = gex_data.get("put_wall", 0)
    net_gex      = gex_data.get("net_gex", 0)
    iv_skew      = gex_data.get("iv_skew", 0)

    # Price history for curvature
    price_hist = [b.get("c", price) for b in analyst_result.get("_bars", [])]
    curvature  = calculate_curvature_risk(price_hist if price_hist else [price] * 5)

    macro    = fetch_macro_context(ticker)
    dp_data  = _fetch_dark_pool_data(ticker)
    sent_data = _fetch_news_sentiment(ticker)

    context = {
        "ticker":   ticker,
        "price":    price,
        "indicators": {
            "rsi":          rsi,
            "macd":         macd,
            "adx":          adx,
            "adx_strength": ind.get("adx_strength", "weak"),
            "ema50":        ema50,
            "ema200":       ema200,
            "above_ema200": ind.get("above_ema200", True),
            "bb_position":  bb_pos,
            "volume_ratio": vol_r,
            "atr":          atr,
            "pct_52w_high": pct_52h,
            "golden_cross": ind.get("golden_cross", False),
        },
        "ml_signal":       ml.get("ml_signal", "HOLD"),
        "ml_confidence":   ml.get("ml_confidence", 50),
        "analyst_bias":    bias,
        "analyst_score":   a_score,
        "gex_regime":      gex_regime,
        "net_gex_billions": net_gex,
        "call_wall":       call_wall,
        "put_wall":        put_wall,
        "iv_skew":         iv_skew,
        "curvature_regime": curvature["regime"],
        "curvature_value":  curvature["curvature"],
        "macro_spy_pct":   macro.get("spy_prev_day_pct", 0),
        "dark_pool_bias":    dp_data.get("dark_pool_bias", "NEUTRAL"),
        "dark_pool_conviction": dp_data.get("conviction", 0),
        "news_sentiment":    sent_data.get("sentiment", "NEUTRAL"),
        "news_score":        sent_data.get("score", 0.0),
        "news_bear_boost":   sent_data.get("bear_boost", 0.0),
    }

    prompt = f"""You are the Bear Agent in a multi-agent trading system analyzing {ticker}.

Your job: Find every reason this trade could FAIL or go WRONG. Be the devil's advocate.
You are protecting the portfolio from bad trades. Be specific, cite numbers, be honest.

Market Data:
{_dumps(context)}

Respond ONLY with valid JSON in this exact structure:
{{
  "score": <integer 0-100, your conviction in the BEAR case — higher = more bearish/risky>,
  "arguments": [
    "<specific risk/bear argument with numbers>",
    "<specific risk/bear argument with numbers>",
    "<specific risk/bear argument with numbers>"
  ],
  "key_risk": "<single most important risk to the long trade>",
  "stop_level": <float, where the trade should be stopped out>,
  "regime_flag": "<NORMAL|CAUTION|DANGER — your overall risk assessment>"
}}

Rules:
- Score 70+ means you have serious concerns about going long right now
- Always flag GEX negative gamma as a danger signal
- Always flag overbought RSI (>70) and bearish MACD together
- If price is near 52w high with low volume, that's distribution — flag it
- Curvature HIGH_RISK = reduce conviction in any direction
- Be honest — don't over-inflate bear score in a clear bull market"""

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
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        result["curvature"] = curvature
        result["ticker"]    = ticker
        return result

    except Exception as e:
        pass
        score     = 50
        arguments = []
        if bias == "BEARISH":           score += 15; arguments.append(f"Analyst bias: BEARISH (score {a_score})")
        if macd == "bearish":           score += 10; arguments.append("MACD bearish — momentum fading")
        if rsi > 70:                    score += 10; arguments.append(f"RSI overbought at {rsi}")
        if not ind.get("above_ema200"): score += 10; arguments.append(f"Below EMA 200 (${ema200}) — structural weakness")
        if gex_regime == "NEGATIVE_GAMMA": score += 12; arguments.append("NEGATIVE GAMMA — dealers amplify moves")
        if pct_52h > -2:                score += 8;  arguments.append(f"Near 52-week high ({pct_52h}%) — distribution risk")
        if adx < 15:                    score += 5;  arguments.append(f"ADX weak ({adx}) — choppy, no clear trend")
        if vol_r < 0.7:                 score += 5;  arguments.append(f"Low volume ({vol_r}x) — no conviction in move")
        if ml.get("ml_signal") == "SELL": score += 8; arguments.append(f"XGBoost SELL signal ({ml.get('ml_confidence')}% conf)")
        if dp_data.get('dark_pool_bias') == 'BEARISH':
            score += 15; arguments.append(f"Dark pool distribution detected ({dp_data.get('conviction',0)} conviction)")
        if sent_data.get('sentiment') == 'NEGATIVE':
            score += int(sent_data.get('bear_boost', 0)); arguments.append(f"Negative news sentiment (score: {sent_data.get('score',0):.2f})")
        if sent_data.get('sentiment') == 'POSITIVE':
            score -= int(sent_data.get('bull_boost', 0)); arguments.append(f"Positive news reduces bear case (score: {sent_data.get('score',0):.2f})")
        stop = round(price * 0.982, 2)
        flag = "DANGER" if score >= 70 else "CAUTION" if score >= 55 else "NORMAL"
        return {
            "score":       min(100, score),
            "arguments":   arguments or ["No strong bear factors identified"],
            "key_risk":    arguments[0] if arguments else "No major risks identified",
            "stop_level":  stop,
            "regime_flag": flag,
            "curvature":   curvature,
            "ticker":      ticker,
            "fallback":    True,
            "error":       str(e),
        }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from analyst_agent  import run as analyst_run
    from gex_calculator import get_gex_for_ticker

    analyst = analyst_run("SPY")
    gex     = get_gex_for_ticker("SPY", analyst["indicators"]["price"])
    result  = run("SPY", analyst, gex)
    print(json.dumps(result, indent=2))
