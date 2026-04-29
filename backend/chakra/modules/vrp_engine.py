"""
CHAKRA — Volatility Risk Premium (VRP) Engine
backend/chakra/modules/vrp_engine.py

VRP = Implied Volatility (VIX-derived) minus Realized Volatility (30-day SPY)

The single most important number for options traders:
  VRP > +2%  → Options EXPENSIVE → favor spreads, reduce lotto size to 0.75x
  VRP 0–2%   → Options FAIR      → normal sizing
  VRP < 0%   → Options CHEAP     → increase lotto/MOC size to 1.25x

Integration:
  - Lotto Engine    → size_mult applied to contract quantity
  - MOC Engine      → skip if VRP > 3% (premium too rich)
  - Daily Briefing  → VRP reading in 7 AM Discord report
  - ARKA scanner    → VRP context in signal output

Runs at 8:30 AM via cron, cached for the day.
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

log         = logging.getLogger("chakra.vrp")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
VRP_CACHE   = BASE / "logs" / "chakra" / "vrp_latest.json"

# VRP thresholds
VRP_EXPENSIVE = 0.020   # +2% = options expensive
VRP_CHEAP     = 0.000   # 0%  = options at or below fair
VRP_SKIP_MOC  = 0.030   # +3% = skip MOC entirely


# ══════════════════════════════════════════════════════════════════════
# CORE CALCULATION
# ══════════════════════════════════════════════════════════════════════

def calculate_vrp(vix_value: float, spy_returns_30d: list) -> dict:
    """
    Calculate Volatility Risk Premium.

    vix_value      : current VIX reading (e.g. 18.5)
    spy_returns_30d: list of 30 daily SPY returns as decimals (e.g. [0.012, -0.008, ...])

    Returns dict with vrp, signal, size_mult, and interpretation.
    """
    if not spy_returns_30d or vix_value <= 0:
        return _empty_vrp(vix_value)

    returns = np.array(spy_returns_30d, dtype=float)

    # IV (monthly) from VIX — VIX is annualized, convert to monthly
    iv_monthly = vix_value / 100 / np.sqrt(12)

    # Realized Volatility (30-day annualized → monthly)
    rv_daily    = np.std(returns)
    rv_monthly  = rv_daily * np.sqrt(21)   # 21 trading days per month

    vrp = round(float(iv_monthly - rv_monthly), 5)

    # Classification
    if vrp > VRP_EXPENSIVE:
        signal    = "EXPENSIVE"
        size_mult = 0.75
        label     = "🔴 Premium Rich — Reduce Lotto Size"
        color     = "FF2D55"
        advice    = "Options overpriced vs realized vol. Favor spreads over naked buys."
    elif vrp < VRP_CHEAP:
        signal    = "CHEAP"
        size_mult = 1.25
        label     = "🟢 Premium Cheap — Increase Lotto Size"
        color     = "00FF9D"
        advice    = "Options underpriced vs realized vol. Edge to buy premium — increase lotto/MOC size."
    else:
        signal    = "FAIR"
        size_mult = 1.0
        label     = "🟡 Premium Fair — Normal Sizing"
        color     = "FFB347"
        advice    = "Options fairly priced. Standard ARKA rules apply."

    # MOC gate
    skip_moc = vrp > VRP_SKIP_MOC

    return {
        "vrp":           vrp,
        "vrp_pct":       round(vrp * 100, 2),
        "iv_monthly":    round(iv_monthly, 4),
        "rv_monthly":    round(rv_monthly, 4),
        "vix":           vix_value,
        "signal":        signal,
        "size_mult":     size_mult,
        "label":         label,
        "color":         color,
        "advice":        advice,
        "skip_moc":      skip_moc,
        "rv_30d_annual": round(rv_daily * np.sqrt(252) * 100, 2),
    }


def _empty_vrp(vix: float = 20.0) -> dict:
    return {
        "vrp": 0, "vrp_pct": 0,
        "iv_monthly": 0, "rv_monthly": 0,
        "vix": vix, "signal": "UNKNOWN",
        "size_mult": 1.0, "label": "VRP unavailable",
        "color": "888888", "advice": "Using default sizing.",
        "skip_moc": False, "rv_30d_annual": 0,
    }


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════════════════

def fetch_vix(from_internals: bool = True) -> float:
    """
    Fetch current VIX value.
    First tries internals_latest.json (already fetched by internals engine).
    Falls back to Polygon direct fetch.
    """
    # Try internals file first (no extra API call)
    internals_path = BASE / "logs" / "internals" / "internals_latest.json"
    if from_internals and internals_path.exists():
        try:
            with open(internals_path) as f:
                data = json.load(f)
            vix = data.get("vix", {}).get("close") or data.get("vix", 0)
            if isinstance(vix, dict):
                vix = vix.get("close", 0) or vix.get("value", 0)
            if vix and float(vix) > 5:
                return float(vix)
        except Exception:
            pass

    # Fallback: fetch VIX from Polygon
    try:
        r = httpx.get(
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/VIXY",
            params={"apiKey": POLYGON_KEY},
            timeout=8
        )
        data = r.json().get("ticker", {})
        price = (data.get("lastTrade", {}).get("p") or
                 data.get("day", {}).get("c") or 20.0)
        return float(price)
    except Exception as e:
        log.warning(f"VIX fetch error: {e} — using default 20.0")
        return 20.0


def fetch_spy_returns(days: int = 35) -> list[float]:
    """Fetch last N daily SPY returns from Polygon."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=days + 10)).isoformat()
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/{start}/{end}",
            params={"apiKey": POLYGON_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 60},
            timeout=12
        )
        bars    = r.json().get("results", [])
        closes  = [float(b["c"]) for b in bars if b.get("c")]
        returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
        ]
        return returns[-days:]
    except Exception as e:
        log.warning(f"SPY returns fetch error: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════
# COMPUTE + CACHE
# ══════════════════════════════════════════════════════════════════════

def compute_and_cache_vrp() -> dict:
    """
    Compute VRP and save to cache.
    Called at market open (8:30 AM) and by Daily Briefing (7 AM).
    """
    log.info("Computing VRP...")

    vix     = fetch_vix()
    returns = fetch_spy_returns(days=30)

    if not returns:
        log.warning("VRP: no SPY returns available — using defaults")
        result = _empty_vrp(vix)
    else:
        result = calculate_vrp(vix, returns)

    result["date"]      = date.today().isoformat()
    result["computed"]  = __import__("datetime").datetime.now().strftime("%H:%M ET")

    # Save cache
    VRP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(VRP_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    log.info(f"  VRP: {result['vrp_pct']:+.2f}%  VIX={vix:.1f}  "
             f"RV={result['rv_30d_annual']:.1f}%  [{result['signal']}]  "
             f"size={result['size_mult']}x  skip_moc={result['skip_moc']}")
    return result


def load_vrp_cache() -> dict:
    """Load cached VRP. Recomputes if stale or missing."""
    try:
        if VRP_CACHE.exists():
            with open(VRP_CACHE) as f:
                data = json.load(f)
            if data.get("date") == date.today().isoformat():
                return data
    except Exception:
        pass
    return compute_and_cache_vrp()


# ══════════════════════════════════════════════════════════════════════
# INTEGRATION HELPERS
# ══════════════════════════════════════════════════════════════════════

def get_lotto_size_mult() -> float:
    """Return VRP-based size multiplier for Lotto Engine."""
    vrp = load_vrp_cache()
    return vrp.get("size_mult", 1.0)


def should_skip_moc() -> tuple[bool, str]:
    """Return (skip, reason) for MOC Engine VRP gate."""
    vrp = load_vrp_cache()
    if vrp.get("skip_moc", False):
        return True, f"VRP={vrp['vrp_pct']:+.2f}% > 3% — premium too expensive for MOC"
    return False, f"VRP={vrp['vrp_pct']:+.2f}% — MOC premium acceptable"


def get_vrp_briefing_line() -> str:
    """One-line VRP summary for Daily Briefing Discord embed."""
    vrp = load_vrp_cache()
    return (f"{vrp['label']}  |  VRP: {vrp['vrp_pct']:+.2f}%  "
            f"|  VIX: {vrp['vix']:.1f}  |  RV30: {vrp['rv_30d_annual']:.1f}%")


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    result = compute_and_cache_vrp()
    print(f"\n── VRP Result ──────────────────────────────────────")
    print(f"  VRP:          {result['vrp_pct']:+.2f}%")
    print(f"  VIX (IV):     {result['vix']:.1f}  →  monthly IV {result['iv_monthly']*100:.2f}%")
    print(f"  Realized Vol: {result['rv_30d_annual']:.1f}% annualized")
    print(f"  Signal:       {result['signal']}")
    print(f"  Label:        {result['label']}")
    print(f"  Lotto mult:   {result['size_mult']}x")
    print(f"  Skip MOC:     {result['skip_moc']}")
    print(f"  Advice:       {result['advice']}")
