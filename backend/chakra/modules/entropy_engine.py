"""
CHAKRA — Market Entropy / Choppiness Engine
backend/chakra/modules/entropy_engine.py

Shannon entropy measures how disordered price action is.
Low entropy  → directional market forming → trade aggressively (1.2x size)
High entropy → noise/chop               → reduce size (0.5x)

More rigorous than Choppiness Index because it measures the actual
probability distribution of returns, not just range vs ATR.

Pure numpy — no API calls. Runs on SPY 5-min bar return history.
Refreshes every 30 min during market hours.

Integration:
  - ARKA threshold → dynamic size adjustment from entropy signal
  - Physics Manifold tab → entropy sparkline as 5th chart
"""

import json
import logging
import numpy as np
import httpx
import os
from datetime import date, timedelta, datetime
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

log           = logging.getLogger("chakra.entropy")
POLYGON_KEY   = os.getenv("POLYGON_API_KEY", "")
ENTROPY_CACHE = BASE / "logs" / "chakra" / "entropy_latest.json"

TICKERS = ["SPY", "QQQ", "IWM"]

# Entropy thresholds (calibrated for 5-min SPY returns)
ENTROPY_DIRECTIONAL = 1.5   # < 1.5 = low entropy = directional
ENTROPY_CHOPPY      = 2.5   # > 2.5 = high entropy = choppy/noise


# ══════════════════════════════════════════════════════════════════════
# CORE CALCULATION
# ══════════════════════════════════════════════════════════════════════

def market_entropy(returns: list, bins: int = 10) -> float:
    """
    Shannon entropy of return distribution.
    Lower = more directional (predictable).
    Higher = more random (noisy).
    """
    if len(returns) < 10:
        return 2.0  # default to neutral if insufficient data

    arr  = np.array(returns, dtype=float)
    arr  = arr[np.isfinite(arr)]   # remove inf/nan

    if len(arr) < 5:
        return 2.0

def market_entropy(returns: list, bins: int = 10) -> float:
    """
    Composite market disorder score combining:
    1. Shannon entropy of return distribution (normalized 0-1)
    2. Choppiness score: total path / net move (1.0 = trending, high = choppy)
    3. Direction consistency: % of bars moving in dominant direction

    Output scaled to 0-3 range to match thresholds:
      < 1.5 = directional  |  1.5-2.5 = normal  |  > 2.5 = choppy
    """
    if len(returns) < 10:
        return 2.0

    arr = np.array(returns, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 5:
        return 2.0

    # 1. Shannon entropy (normalized to 0-1)
    counts, _ = np.histogram(arr, bins=min(bins, len(arr) // 3))
    total     = counts.sum()
    probs     = counts[counts > 0] / total
    shannon   = -np.sum(probs * np.log(probs))
    max_h     = np.log(len(probs))
    norm_h    = shannon / max_h if max_h > 0 else 0.5  # 0=ordered, 1=random

    # 2. Choppiness: sum(|r|) / |sum(r)|  — high = choppy, low = trending
    abs_sum  = np.sum(np.abs(arr))
    net_move = abs(np.sum(arr))
    chop     = abs_sum / (net_move + 1e-8)
    # Normalize: trending = ~1.0, choppy = ~10+. Cap and scale to 0-1
    norm_chop = min(1.0, (chop - 1.0) / 9.0)

    # 3. Direction consistency: fraction of bars in dominant direction
    n_pos   = np.sum(arr > 0)
    n_neg   = np.sum(arr < 0)
    consist = max(n_pos, n_neg) / len(arr)   # 0.5=equal, 1.0=all same dir
    # Invert: high consistency = low disorder
    norm_dir = 1.0 - consist   # 0=all same dir (trending), 0.5=50/50 (random)

    # Composite score — blend all three
    disorder = norm_h * 0.4 + norm_chop * 0.4 + norm_dir * 0.2

    # Scale to 0-3 range matching original thresholds
    scaled = disorder * 3.0
    return round(float(np.clip(scaled, 0.0, 3.0)), 4)


def entropy_trend(entropy_history: list) -> str:
    """Detect if entropy is rising (getting choppier) or falling (getting cleaner)."""
    if len(entropy_history) < 3:
        return "STABLE"
    recent = np.mean(entropy_history[-3:])
    older  = np.mean(entropy_history[:3])
    delta  = recent - older
    if delta > 0.3:   return "RISING"    # getting choppier
    elif delta < -0.3:return "FALLING"   # getting cleaner
    return "STABLE"


def entropy_signal(entropy_val: float) -> dict:
    """
    Classify entropy value into CHAKRA trading mode.
    """
    if entropy_val < ENTROPY_DIRECTIONAL:
        return {
            "entropy":     entropy_val,
            "mode":        "DIRECTIONAL",
            "size_mult":   1.2,
            "threshold_adj": -5,   # lower threshold = more aggressive
            "label":       "🎯 Low Entropy — Directional Market",
            "description": "Price action is predictable — trade aggressively",
            "color":       "00FF9D",
        }
    elif entropy_val < ENTROPY_CHOPPY:
        return {
            "entropy":     entropy_val,
            "mode":        "NORMAL",
            "size_mult":   1.0,
            "threshold_adj": 0,
            "label":       "📊 Normal Entropy",
            "description": "Standard market conditions — normal ARKA rules",
            "color":       "00D4FF",
        }
    else:
        return {
            "entropy":     entropy_val,
            "mode":        "CHOPPY",
            "size_mult":   0.5,
            "threshold_adj": +8,   # raise threshold = more selective
            "label":       "⚠️ High Entropy — Choppy Market",
            "description": "Price action is noisy — reduce size 50%, skip marginal setups",
            "color":       "FF9500",
        }


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHER
# ══════════════════════════════════════════════════════════════════════

def fetch_intraday_returns(ticker: str, bars: int = 40,
                           interval_min: int = 5) -> list[float]:
    """
    Fetch last N 5-min bar returns from Polygon.
    Returns list of decimal returns (e.g. [0.002, -0.001, ...])
    """
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=3)).isoformat()

        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/"
            f"{interval_min}/minute/{start}/{end}",
            params={
                "apiKey":  POLYGON_KEY,
                "adjusted":"true",
                "sort":    "asc",
                "limit":   200,
            },
            timeout=12
        )
        bars_data = r.json().get("results", [])
        closes    = [float(b["c"]) for b in bars_data if b.get("c")]

        if len(closes) < 2:
            return []

        returns = [
            (closes[i] - closes[i-1]) / closes[i-1]
            for i in range(1, len(closes))
        ]
        return returns[-bars:]

    except Exception as e:
        log.warning(f"Entropy fetch {ticker}: {e}")
        return []


def fetch_daily_returns(ticker: str, days: int = 20) -> list[float]:
    """Fetch last N daily returns for longer-term entropy."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=days + 10)).isoformat()

        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"apiKey": POLYGON_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 50},
            timeout=12
        )
        bars    = r.json().get("results", [])
        closes  = [float(b["c"]) for b in bars if b.get("c")]
        returns = [(closes[i] - closes[i-1]) / closes[i-1]
                   for i in range(1, len(closes))]
        return returns[-days:]

    except Exception as e:
        log.warning(f"Entropy daily {ticker}: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════
# COMPUTE + CACHE
# ══════════════════════════════════════════════════════════════════════

def compute_and_cache_entropy() -> dict:
    """
    Compute entropy for all tickers.
    Runs at 8:30 AM and every 30 min during market hours.
    """
    result = {
        "date":     date.today().isoformat(),
        "computed": datetime.now().strftime("%H:%M ET"),
        "tickers":  {},
        "market":   {},
    }

    for ticker in TICKERS:
        # Intraday entropy (primary — 5-min bars, last 40 bars = ~3.3 hrs)
        intra_returns  = fetch_intraday_returns(ticker, bars=40)
        entropy_intra  = market_entropy(intra_returns, bins=10) if len(intra_returns) >= 10 else 2.0

        # Daily entropy (regime context — 20 days)
        daily_returns  = fetch_daily_returns(ticker, days=20)
        entropy_daily  = market_entropy(daily_returns, bins=8) if len(daily_returns) >= 10 else 2.0

        # Blend: intraday matters more for ARKA decisions
        entropy_blend  = round(entropy_intra * 0.7 + entropy_daily * 0.3, 4)

        signal = entropy_signal(entropy_blend)
        signal.update({
            "ticker":         ticker,
            "entropy_intra":  entropy_intra,
            "entropy_daily":  entropy_daily,
            "entropy_blend":  entropy_blend,
            "bars_used":      len(intra_returns),
        })

        result["tickers"][ticker] = signal
        log.info(f"  Entropy {ticker}: intra={entropy_intra:.3f} "
                 f"daily={entropy_daily:.3f} blend={entropy_blend:.3f} "
                 f"[{signal['mode']}] size={signal['size_mult']}x")

    # Market-wide (SPY primary)
    spy = result["tickers"].get("SPY", entropy_signal(2.0))
    result["market"] = {
        "entropy":       spy.get("entropy_blend", spy.get("entropy", 2.0)),
        "mode":          spy.get("mode", "NORMAL"),
        "size_mult":     spy.get("size_mult", 1.0),
        "threshold_adj": spy.get("threshold_adj", 0),
        "label":         spy.get("label", ""),
    }

    ENTROPY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(ENTROPY_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    return result


def load_entropy_cache(ticker: str = "SPY") -> dict:
    """Load cached entropy. Recomputes if > 30 min old."""
    try:
        if ENTROPY_CACHE.exists():
            import time
            age = time.time() - ENTROPY_CACHE.stat().st_mtime
            if age < 1800:   # 30 minutes
                with open(ENTROPY_CACHE) as f:
                    data = json.load(f)
                t = data.get("tickers", {}).get(ticker)
                if t:
                    return t
    except Exception:
        pass
    result = compute_and_cache_entropy()
    return result.get("tickers", {}).get(ticker, entropy_signal(2.0))


def get_market_entropy() -> dict:
    """Get market-wide entropy for ARKA threshold/size adjustment."""
    try:
        if ENTROPY_CACHE.exists():
            import time
            age = time.time() - ENTROPY_CACHE.stat().st_mtime
            if age < 1800:
                with open(ENTROPY_CACHE) as f:
                    data = json.load(f)
                m = data.get("market")
                if m:
                    return m
    except Exception:
        pass
    result = compute_and_cache_entropy()
    return result.get("market", entropy_signal(2.0))


# ══════════════════════════════════════════════════════════════════════
# ARKA INTEGRATION
# ══════════════════════════════════════════════════════════════════════

def get_entropy_arka_params(ticker: str = "SPY") -> dict:
    """
    Get entropy-based parameters for ARKA engine.
    Returns size_mult and threshold_adj to apply in conviction scorer.
    """
    sig = load_entropy_cache(ticker)
    return {
        "size_mult":     sig.get("size_mult", 1.0),
        "threshold_adj": sig.get("threshold_adj", 0),
        "mode":          sig.get("mode", "NORMAL"),
        "label":         sig.get("label", ""),
        "entropy":       sig.get("entropy_blend", sig.get("entropy", 2.0)),
    }


def get_entropy_history_for_chart(ticker: str = "SPY",
                                   points: int = 20) -> list[dict]:
    """
    Generate entropy history for dashboard sparkline.
    Computes entropy on rolling 20-bar windows of 5-min returns.
    """
    returns = fetch_intraday_returns(ticker, bars=60)
    if len(returns) < 20:
        return []

    history = []
    for i in range(20, len(returns) + 1, 2):
        window  = returns[max(0, i-20):i]
        e       = market_entropy(window)
        sig     = entropy_signal(e)
        history.append({
            "index":   i,
            "entropy": e,
            "mode":    sig["mode"],
            "color":   sig["color"],
        })

    return history[-points:]


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    result = compute_and_cache_entropy()
    print(f"\n── Entropy Results ({result['computed']}) ──────────────────────")
    for ticker, sig in result.get("tickers", {}).items():
        print(f"  {ticker:5s}  entropy={sig.get('entropy_blend', sig.get('entropy', 0)):.3f}  "
              f"[{sig['mode']:12s}]  size={sig['size_mult']}x  "
              f"thr_adj={sig['threshold_adj']:+d}  {sig['label']}")

    mkt = result.get("market", {})
    print(f"\n  Market: {mkt.get('mode')} entropy={mkt.get('entropy', 0):.3f} "
          f"→ ARKA size={mkt.get('size_mult')}x "
          f"threshold_adj={mkt.get('threshold_adj', 0):+d}")
