"""
CHAKRA — Hurst Exponent Engine
backend/chakra/modules/hurst_engine.py

The Hurst Exponent (H) measures the statistical memory of price series —
whether today's move predicts tomorrow's direction.

H > 0.6 → Trending    → ARKA uses BREAKOUT mode, full size
H ≈ 0.5 → Random      → ARKA reduces size 50%, skip marginal setups
H < 0.4 → Mean-Rev    → ARKA uses FADE mode, tighter targets

Pure numpy — no API calls. Runs on Polygon daily bars.
Computed once at 8:30 AM, cached for the day.
"""

import json
import logging
import numpy as np
import httpx
import os
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

log          = logging.getLogger("chakra.hurst")
POLYGON_KEY  = os.getenv("POLYGON_API_KEY", "")
HURST_CACHE  = BASE / "logs" / "chakra" / "hurst_latest.json"
TICKERS      = ["SPY", "QQQ", "IWM"]


# ══════════════════════════════════════════════════════════════════════
# CORE CALCULATION
# ══════════════════════════════════════════════════════════════════════

def hurst_exponent(price_series: list, max_lag: int = 20) -> float:
    """
    Calculate Hurst Exponent using R/S analysis.

    Returns H between 0 and 1:
      H > 0.6 = trending (momentum persists)
      H ≈ 0.5 = random walk (no edge)
      H < 0.4 = mean-reverting (oscillating)
    """
    if len(price_series) < max_lag + 5:
        return 0.5  # default to random if not enough data

    prices = np.array(price_series, dtype=float)
    lags   = range(2, min(max_lag, len(prices) // 2))

    # Standard deviation of lagged differences
    tau = []
    for lag in lags:
        diff = np.subtract(prices[lag:], prices[:-lag])
        std  = np.std(diff)
        tau.append(std if std > 0 else 1e-10)

    if len(tau) < 2:
        return 0.5

    # Linear regression of log(lag) vs log(tau) → slope = H
    log_lags = np.log(list(lags))
    log_tau  = np.log(tau)

    H = np.polyfit(log_lags, log_tau, 1)[0]
    return round(float(np.clip(H, 0.1, 0.9)), 4)


def hurst_regime(H: float) -> dict:
    """
    Classify H value into CHAKRA trading regime.
    Returns regime dict with ARKA action parameters.
    """
    if H > 0.6:
        return {
            "H":           H,
            "regime":      "TRENDING",
            "arka_mode":   "BREAKOUT",
            "size_mult":   1.0,
            "threshold_adj": 0,
            "label":       "📈 Trending",
            "description": "Momentum persists — follow breakouts, widen stops",
            "color":       "00FF9D",
        }
    elif H < 0.4:
        return {
            "H":           H,
            "regime":      "MEAN_REVERTING",
            "arka_mode":   "FADE",
            "size_mult":   0.8,
            "threshold_adj": 0,
            "label":       "🔄 Mean-Reverting",
            "description": "Fade extremes — RSI reversals, tighter targets",
            "color":       "00D4FF",
        }
    else:
        return {
            "H":           H,
            "regime":      "RANDOM",
            "arka_mode":   "REDUCE",
            "size_mult":   0.5,
            "threshold_adj": +10,
            "label":       "⚠️ Random / Choppy",
            "description": "No statistical edge — reduce size 50%, skip marginal setups",
            "color":       "FFB347",
        }


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHER
# ══════════════════════════════════════════════════════════════════════

def fetch_daily_closes(ticker: str, days: int = 60) -> list[float]:
    """Fetch last N daily closes from Polygon."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=days + 10)).isoformat()
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"apiKey": POLYGON_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 100},
            timeout=12
        )
        bars = r.json().get("results", [])
        closes = [float(b["c"]) for b in bars if b.get("c")]
        return closes[-days:] if len(closes) >= days else closes
    except Exception as e:
        log.warning(f"Hurst: could not fetch closes for {ticker}: {e}")
        return []


def fetch_intraday_closes(ticker: str, bars: int = 60) -> list[float]:
    """Fetch last N 5-min bar closes for intraday Hurst."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=3)).isoformat()
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/5/minute/{start}/{end}",
            params={"apiKey": POLYGON_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 200},
            timeout=12
        )
        bars_data = r.json().get("results", [])
        closes = [float(b["c"]) for b in bars_data if b.get("c")]
        return closes[-bars:] if len(closes) >= bars else closes
    except Exception as e:
        log.warning(f"Hurst intraday: {ticker}: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════
# COMPUTE + CACHE
# ══════════════════════════════════════════════════════════════════════

def compute_and_cache_hurst(tickers: list = None) -> dict:
    """
    Compute Hurst for all tickers, save to cache.
    Call at market open (8:30 AM) and every hour.
    """
    if tickers is None:
        tickers = TICKERS

    result = {
        "date":    date.today().isoformat(),
        "tickers": {},
        "market":  {},
    }

    for ticker in tickers:
        # Daily Hurst (multi-day trend context)
        daily_closes = fetch_daily_closes(ticker, days=60)
        H_daily      = hurst_exponent(daily_closes, max_lag=20) if len(daily_closes) >= 25 else 0.5

        # Intraday Hurst (current session character)
        intra_closes = fetch_intraday_closes(ticker, bars=60)
        H_intra      = hurst_exponent(intra_closes, max_lag=15) if len(intra_closes) >= 20 else 0.5

        # Use weighted blend — intraday has more weight for ARKA decisions
        H_blend = round(H_daily * 0.4 + H_intra * 0.6, 4)

        regime = hurst_regime(H_blend)
        regime.update({
            "ticker":  ticker,
            "H_daily": H_daily,
            "H_intra": H_intra,
            "H_blend": H_blend,
        })

        result["tickers"][ticker] = regime
        log.info(f"  Hurst {ticker}: daily={H_daily:.3f} intra={H_intra:.3f} "
                 f"blend={H_blend:.3f} [{regime['regime']}]")

    # Market-wide Hurst (SPY is the primary market proxy)
    spy_regime = result["tickers"].get("SPY", {})
    result["market"] = {
        "regime":       spy_regime.get("regime", "RANDOM"),
        "H":            spy_regime.get("H_blend", 0.5),
        "arka_mode":    spy_regime.get("arka_mode", "REDUCE"),
        "size_mult":    spy_regime.get("size_mult", 1.0),
        "threshold_adj":spy_regime.get("threshold_adj", 0),
        "label":        spy_regime.get("label", "Unknown"),
    }

    # Save cache
    HURST_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(HURST_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    log.info(f"  Market Hurst: {result['market']['regime']} "
             f"H={result['market']['H']:.3f} mode={result['market']['arka_mode']}")
    return result


def load_hurst_cache(ticker: str = "SPY") -> dict:
    """Load cached Hurst regime for a ticker."""
    try:
        if HURST_CACHE.exists():
            with open(HURST_CACHE) as f:
                data = json.load(f)
            # Check if cache is from today
            if data.get("date") == date.today().isoformat():
                return data.get("tickers", {}).get(ticker, hurst_regime(0.5))
    except Exception:
        pass
    # Compute fresh
    result = compute_and_cache_hurst([ticker])
    return result.get("tickers", {}).get(ticker, hurst_regime(0.5))


def get_market_hurst() -> dict:
    """Get market-wide Hurst regime for ARKA threshold adjustment."""
    try:
        if HURST_CACHE.exists():
            with open(HURST_CACHE) as f:
                data = json.load(f)
            if data.get("date") == date.today().isoformat():
                return data.get("market", hurst_regime(0.5))
    except Exception:
        pass
    result = compute_and_cache_hurst()
    return result.get("market", hurst_regime(0.5))


# ══════════════════════════════════════════════════════════════════════
# ARKA INTEGRATION
# ══════════════════════════════════════════════════════════════════════

def get_hurst_conviction_boost(ticker: str, trade_direction: str) -> dict:
    """
    Returns conviction boost/penalty for ARKA based on Hurst regime.
    Breakout setups get boosted in TRENDING regime.
    Fade setups get boosted in MEAN_REVERTING regime.
    Both get penalized in RANDOM regime.
    """
    regime_data = load_hurst_cache(ticker)
    regime      = regime_data.get("regime", "RANDOM")
    H           = regime_data.get("H_blend", 0.5)
    mode        = regime_data.get("arka_mode", "REDUCE")

    if regime == "TRENDING":
        return {
            "boost":  8,
            "reason": f"Hurst TRENDING (H={H:.2f}) — momentum mode ✅",
            "hurst":  regime_data,
        }
    elif regime == "MEAN_REVERTING":
        return {
            "boost":  0,
            "reason": f"Hurst MEAN_REV (H={H:.2f}) — fade extremes only",
            "hurst":  regime_data,
        }
    else:  # RANDOM
        return {
            "boost":  -5,
            "reason": f"Hurst RANDOM (H={H:.2f}) — choppy, reduce size ⚠️",
            "hurst":  regime_data,
        }


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    result = compute_and_cache_hurst()
    print("\n── Hurst Results ────────────────────────────────────────")
    for ticker, r in result.get("tickers", {}).items():
        print(f"  {ticker:5s}  H={r['H_blend']:.3f}  "
              f"daily={r['H_daily']:.3f}  intra={r['H_intra']:.3f}  "
              f"[{r['regime']:15s}]  mode={r['arka_mode']}  "
              f"size={r['size_mult']}x  {r['label']}")

    mkt = result.get("market", {})
    print(f"\n  Market: {mkt.get('regime')} H={mkt.get('H'):.3f} "
          f"→ ARKA size={mkt.get('size_mult')}x "
          f"threshold_adj={mkt.get('threshold_adj'):+d}")
