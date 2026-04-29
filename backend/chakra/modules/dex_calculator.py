"""
CHAKRA — DEX (Delta Exposure) Calculator
backend/chakra/modules/dex_calculator.py

DEX tracks total directional pressure from all outstanding option delta
across the full chain. Complements GEX (gamma hedging) by showing WHERE
dealers are positioned directionally.

Interpretation:
  Positive DEX → Dealers net long delta → SELL into rallies → mean-reversion pressure
  Negative DEX → Dealers net short delta → BUY dips → fuels rallies
  DEX Flip     → Structural regime change → often precedes multi-day trend reversal

Integration:
  - ARKA conviction scorer  → +10 pts if DEX regime aligns with trade direction
  - GEX tab dashboard       → DEX bar alongside GEX heatmap
  - Reads from gex_*.json   → no new API calls needed
"""

import json
import logging
from pathlib import Path
from datetime import date

log = logging.getLogger("chakra.dex")

BASE     = Path(__file__).resolve().parents[3]
GEX_DIR  = BASE / "logs" / "options"
DEX_CACHE = BASE / "logs" / "options" / "dex_latest.json"


# ══════════════════════════════════════════════════════════════════════
# CORE CALCULATION
# ══════════════════════════════════════════════════════════════════════

def calculate_dex(top_strikes: list, spot: float) -> dict:
    """
    Calculate DEX from GEX top_strikes data.

    top_strikes: list of {strike, call_gex, put_gex, net_gex, oi}
    DEX ≈ net directional delta pressure across the chain.

    Since we don't store per-strike deltas in the GEX JSON, we derive
    delta directional pressure from call vs put GEX weighting:
      - Call GEX > 0 at a strike → dealers are long gamma (delta hedging sells)
      - Put GEX > 0 at a strike  → dealers are short gamma (delta hedging buys)

    Net DEX = sum of (call_gex - put_gex) weighted by proximity to spot.
    Positive = dealer net long delta = mean revert pressure.
    Negative = dealer net short delta = trend follow / rally fuel.
    """
    if not top_strikes or not spot:
        return _empty_dex()

    call_dex = 0.0
    put_dex  = 0.0
    total_oi = 0

    for row in top_strikes:
        strike   = row.get("strike", 0)
        call_gex = row.get("call_gex", 0)
        put_gex  = row.get("put_gex", 0)
        oi       = row.get("oi", 0)

        if not strike:
            continue

        # Weight by proximity to spot (closer strikes matter more)
        dist   = abs(strike - spot) / spot
        weight = max(0.1, 1.0 - dist * 5)   # falls off quickly beyond 2%

        call_dex += call_gex * weight
        put_dex  += put_gex  * weight
        total_oi += oi

    net_dex = call_dex - put_dex

    # Normalize to a readable scale (billions)
    net_dex_bn   = net_dex / 1e9
    call_dex_bn  = call_dex / 1e9
    put_dex_bn   = put_dex / 1e9

    # Regime classification
    if net_dex > 0:
        regime = "DEALER_LONG"
        bias   = "MEAN_REVERT"
        label  = "📉 Mean-Reversion Pressure"
        color  = "FF6B35"
    else:
        regime = "DEALER_SHORT"
        bias   = "TREND_FOLLOW"
        label  = "📈 Trend-Follow / Rally Fuel"
        color  = "00FF9D"

    # Magnitude
    mag = abs(net_dex_bn)
    if mag > 5:   strength = "STRONG"
    elif mag > 2: strength = "MODERATE"
    else:         strength = "WEAK"

    return {
        "net_dex":      round(net_dex_bn, 3),
        "call_dex":     round(call_dex_bn, 3),
        "put_dex":      round(put_dex_bn, 3),
        "regime":       regime,
        "bias":         bias,
        "label":        label,
        "strength":     strength,
        "color":        color,
        "total_oi":     total_oi,
    }


def _empty_dex() -> dict:
    return {
        "net_dex": 0, "call_dex": 0, "put_dex": 0,
        "regime": "UNKNOWN", "bias": "NEUTRAL",
        "label": "No data", "strength": "WEAK",
        "color": "888888", "total_oi": 0,
    }


# ══════════════════════════════════════════════════════════════════════
# CONVICTION BOOST
# ══════════════════════════════════════════════════════════════════════

def get_dex_conviction_boost(ticker: str, trade_direction: str) -> dict:
    """
    Returns conviction boost for ARKA based on DEX alignment.

    trade_direction: "LONG" or "SHORT"
    Returns: {"boost": int, "reason": str, "dex": dict}
    """
    dex = load_dex_cache(ticker)
    if not dex or dex.get("regime") == "UNKNOWN":
        return {"boost": 0, "reason": "DEX unavailable", "dex": dex}

    regime    = dex.get("regime", "UNKNOWN")
    strength  = dex.get("strength", "WEAK")
    bias      = dex.get("bias", "NEUTRAL")

    # Boost table
    strength_pts = {"STRONG": 10, "MODERATE": 7, "WEAK": 4}
    pts = strength_pts.get(strength, 0)

    if trade_direction == "LONG" and regime == "DEALER_SHORT":
        # DEX negative = dealers buy dips = aligns with LONG
        return {
            "boost":  pts,
            "reason": f"DEX {bias} ({dex['net_dex']:.2f}B) aligns LONG ✅",
            "dex":    dex,
        }
    elif trade_direction == "SHORT" and regime == "DEALER_LONG":
        # DEX positive = dealers sell rallies = aligns with SHORT
        return {
            "boost":  pts,
            "reason": f"DEX {bias} ({dex['net_dex']:.2f}B) aligns SHORT ✅",
            "dex":    dex,
        }
    elif trade_direction == "LONG" and regime == "DEALER_LONG":
        return {
            "boost":  -pts,
            "reason": f"DEX mean-revert pressure ({dex['net_dex']:.2f}B) opposes LONG ⚠️",
            "dex":    dex,
        }
    else:
        return {"boost": 0, "reason": f"DEX {regime} — neutral for {trade_direction}", "dex": dex}


# ══════════════════════════════════════════════════════════════════════
# CACHE — reads from existing GEX json files
# ══════════════════════════════════════════════════════════════════════

def compute_and_cache_dex() -> dict:
    """
    Read latest GEX json, compute DEX for all tickers, save to cache.
    Called by the options engine after GEX update.
    """
    import glob
    files = sorted(
        (BASE / "logs" / "options").glob("gex_*.json"),
        reverse=True
    )
    if not files:
        log.warning("No GEX file found for DEX calculation")
        return {}

    try:
        with open(files[0]) as f:
            gex_data = json.load(f)
    except Exception as e:
        log.error(f"DEX: could not read GEX file: {e}")
        return {}

    result = {"date": date.today().isoformat(), "tickers": {}}

    for ticker, tdata in gex_data.get("tickers", {}).items():
        spot        = tdata.get("spot", 0)
        top_strikes = tdata.get("gex", {}).get("top_strikes", [])
        dex         = calculate_dex(top_strikes, spot)
        dex["spot"] = spot
        dex["ticker"] = ticker
        result["tickers"][ticker] = dex
        log.info(f"  DEX {ticker}: {dex['net_dex']:.2f}B [{dex['regime']}] {dex['label']}")

    # Save cache
    DEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(DEX_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    return result


def load_dex_cache(ticker: str = "SPY") -> dict:
    """Load cached DEX for a ticker."""
    try:
        if DEX_CACHE.exists():
            with open(DEX_CACHE) as f:
                data = json.load(f)
            return data.get("tickers", {}).get(ticker, _empty_dex())
    except Exception:
        pass
    return _empty_dex()


def get_all_dex() -> dict:
    """Return full DEX cache for dashboard."""
    try:
        if DEX_CACHE.exists():
            with open(DEX_CACHE) as f:
                return json.load(f)
    except Exception:
        pass
    # Compute fresh if no cache
    return compute_and_cache_dex()


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    result = compute_and_cache_dex()
    print("\n── DEX Results ──────────────────────────────")
    for ticker, dex in result.get("tickers", {}).items():
        print(f"  {ticker:5s}  Net DEX: {dex['net_dex']:+.3f}B  "
              f"[{dex['regime']}]  {dex['strength']}  {dex['label']}")

    # Test boost
    boost = get_dex_conviction_boost("SPY", "LONG")
    print(f"\n  Boost for SPY LONG: {boost['boost']:+d} pts — {boost['reason']}")
