"""
CHAKRA — VEX / Vanna Exposure Engine
backend/chakra/modules/vex_engine.py

Vanna = rate of change of delta with respect to IV.
When IV drops (post-FOMC, post-earnings), dealers rebalance Vanna exposure
— driving the classic post-event melt-up or melt-down.

Vanna is calculated from available greeks (delta, vega, gamma) + IV:
  Vanna ≈ vega * (1 - |delta|) / (spot * iv)   [Black-Scholes approximation]

Scenarios:
  Negative VEX + Falling IV → Dealers BUY → Bullish melt-up   ✅ (most common post-event)
  Positive VEX + Falling IV → Dealers SELL → Bearish selloff
  Positive VEX + Rising IV  → Dealers BUY → Bullish support

Integration:
  - ARJUN Bull/Bear agents → ±15 pts on post-event days
  - Daily Briefing         → flag 'Vanna flow day' when macro event within 24h
  - Reads from Polygon     → same chain fetch as GEX, adds vanna calc
"""

import json
import logging
import math
import httpx
import os
import numpy as np
from datetime import date, timedelta, datetime
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

log         = logging.getLogger("chakra.vex")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
VEX_CACHE   = BASE / "logs" / "chakra" / "vex_latest.json"

TICKERS = ["SPY", "QQQ", "IWM"]

# VIX change thresholds
IV_CRUSH_THRESHOLD  = -0.05   # VIX drop > 5% = IV crush
IV_SPIKE_THRESHOLD  = +0.08   # VIX rise > 8% = IV spike


# ══════════════════════════════════════════════════════════════════════
# VANNA CALCULATION
# ══════════════════════════════════════════════════════════════════════

def calc_vanna(delta: float, vega: float, spot: float, iv: float) -> float:
    """
    Approximate Vanna from available greeks.
    Vanna = ∂delta/∂σ = vega * (1 - |delta|) / (spot * iv)
    Positive vanna = dealer long vanna = sells when IV falls
    Negative vanna = dealer short vanna = buys when IV falls
    """
    if not spot or not iv or iv < 0.01:
        return 0.0
    try:
        return float(vega) * (1.0 - abs(float(delta))) / (float(spot) * float(iv))
    except (ValueError, ZeroDivisionError):
        return 0.0


def calculate_vex(chain: list, spot: float, iv_change_pct: float) -> dict:
    """
    Calculate aggregate Vanna Exposure from options chain.

    chain: list of {strike, delta, vega, gamma, oi, type, iv}
    spot: current underlying price
    iv_change_pct: today's VIX % change (negative = IV crush)

    Returns VEX signal with directional bias.
    """
    if not chain or not spot:
        return _empty_vex(iv_change_pct)

    call_vex = 0.0
    put_vex  = 0.0

    for row in chain:
        delta  = row.get("delta", 0) or 0
        vega   = row.get("vega", 0) or 0
        oi     = row.get("oi", 0) or 0
        iv     = row.get("iv", 0.20) or 0.20
        ctype  = row.get("type", "call")

        vanna = calc_vanna(delta, vega, spot, iv)
        vex_contrib = vanna * oi * 100  # per-contract contribution

        if ctype == "call":
            call_vex += vex_contrib
        else:
            put_vex  += vex_contrib

    net_vex = call_vex + put_vex

    # Determine signal based on VEX + IV direction
    iv_falling = iv_change_pct < IV_CRUSH_THRESHOLD
    iv_rising  = iv_change_pct > IV_SPIKE_THRESHOLD
    iv_crush   = iv_change_pct < -0.10   # >10% VIX drop = strong crush

    if iv_falling and net_vex < 0:
        signal    = "BULLISH_MELTUP"
        label     = "🚀 Vanna Melt-Up — Dealers buying into IV crush"
        color     = "00FF9D"
        bull_pts  = 15
        bear_pts  = 0
    elif iv_falling and net_vex > 0:
        signal    = "BEARISH_SELLOFF"
        label     = "📉 Vanna Selloff — Dealers selling into IV crush"
        color     = "FF2D55"
        bull_pts  = 0
        bear_pts  = 15
    elif iv_rising and net_vex > 0:
        signal    = "BULLISH_SUPPORT"
        label     = "📈 Vanna Support — Dealers buying into IV spike"
        color     = "00D4FF"
        bull_pts  = 8
        bear_pts  = 0
    elif iv_rising and net_vex < 0:
        signal    = "BEARISH_PRESSURE"
        label     = "⚠️ Vanna Pressure — Dealers selling into IV spike"
        color     = "FF9500"
        bull_pts  = 0
        bear_pts  = 8
    else:
        signal    = "NEUTRAL"
        label     = "➡️ Vanna Neutral — IV stable"
        color     = "888888"
        bull_pts  = 0
        bear_pts  = 0

    # Scale by crush magnitude
    if iv_crush and signal in ("BULLISH_MELTUP", "BEARISH_SELLOFF"):
        bull_pts = int(bull_pts * 1.3)
        bear_pts = int(bear_pts * 1.3)

    net_vex_k = net_vex / 1000  # scale for readability

    return {
        "net_vex":      round(net_vex_k, 2),
        "call_vex":     round(call_vex / 1000, 2),
        "put_vex":      round(put_vex / 1000, 2),
        "iv_change_pct":round(iv_change_pct * 100, 2),
        "signal":       signal,
        "label":        label,
        "color":        color,
        "bull_pts":     bull_pts,
        "bear_pts":     bear_pts,
        "iv_falling":   iv_falling,
        "iv_rising":    iv_rising,
        "iv_crush":     iv_crush,
    }


def _empty_vex(iv_change_pct: float = 0.0) -> dict:
    return {
        "net_vex": 0, "call_vex": 0, "put_vex": 0,
        "iv_change_pct": round(iv_change_pct * 100, 2),
        "signal": "NEUTRAL", "label": "Vanna unavailable",
        "color": "888888", "bull_pts": 0, "bear_pts": 0,
        "iv_falling": False, "iv_rising": False, "iv_crush": False,
    }


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════════════════

def fetch_chain_with_greeks(ticker: str, spot: float, strikes_range: float = 0.04) -> list:
    """
    Fetch ATM ± 4% options chain with greeks from Polygon.
    Uses strike filters to get greeks-bearing contracts efficiently.
    """
    low  = round(spot * (1 - strikes_range))
    high = round(spot * (1 + strikes_range))

    try:
        # Fetch both today and next expiry for better vanna coverage
        results = []
        for exp_offset in [0, 7]:
            exp_date = (date.today() + timedelta(days=exp_offset)).isoformat()
            r = httpx.get(
                f"https://api.polygon.io/v3/snapshot/options/{ticker}",
                params={
                    "apiKey":              POLYGON_KEY,
                    "limit":               250,
                    "strike_price.gte":    low,
                    "strike_price.lte":    high,
                    "expiration_date.gte": date.today().isoformat(),
                    "expiration_date.lte": exp_date,
                },
                timeout=12
            )
            results.extend(r.json().get("results", []))

        chain = []
        for c in results:
            greeks = c.get("greeks", {})
            if not greeks or not greeks.get("delta"):
                continue
            details = c.get("details", {})
            day     = c.get("day", {})
            exp     = details.get("expiration_date", "")
            dte     = (date.fromisoformat(exp) - date.today()).days if exp else 1

            # Estimate IV from vega (approximation when not directly available)
            vega    = greeks.get("vega", 0) or 0
            iv_est  = max(0.10, abs(vega) * 10) if vega else 0.20

            chain.append({
                "strike": details.get("strike_price", 0),
                "type":   details.get("contract_type", "call"),
                "delta":  greeks.get("delta", 0),
                "gamma":  greeks.get("gamma", 0),
                "vega":   vega,
                "theta":  greeks.get("theta", 0),
                "oi":     c.get("open_interest", 0) or 0,
                "volume": day.get("volume", 0) or 0,
                "iv":     iv_est,
                "dte":    dte,
            })

        return chain

    except Exception as e:
        log.warning(f"VEX chain fetch {ticker}: {e}")
        return []


def get_iv_change() -> float:
    """
    Get today's VIX % change from internals file.
    Returns decimal (e.g. -0.08 = VIX dropped 8%).
    """
    internals_path = BASE / "logs" / "internals" / "internals_latest.json"
    try:
        if internals_path.exists():
            with open(internals_path) as f:
                data = json.load(f)
            # VIX data
            vix_data = data.get("vix", {})
            vix_now  = vix_data.get("close") or vix_data.get("value", 0)
            if isinstance(vix_now, dict):
                vix_now = vix_now.get("close", 0) or vix_now.get("value", 0)

            # Try to get yesterday's VIX for comparison
            risk = data.get("risk", {})
            desc = risk.get("description", "")
            # Parse VIX from "VIX 20.0 | ..." string
            import re
            m = re.search(r"VIX\s+([\d.]+)", desc)
            vix_val = float(m.group(1)) if m else 20.0

            # Without yesterday's close, use intraday change as proxy
            # VIX classification gives us direction
            vix_cls = vix_data.get("classification", {})
            regime  = vix_cls.get("regime", "NORMAL")

            if regime == "ELEVATED":
                return 0.08   # VIX elevated = IV rising
            elif regime == "CAUTION":
                return 0.03
            else:
                return 0.0

    except Exception:
        pass

    # Fallback: fetch VIX directly
    try:
        r = httpx.get(
            "https://api.polygon.io/v2/aggs/ticker/VIXY/range/1/day",
            params={
                "apiKey": POLYGON_KEY,
                "from":   (date.today() - timedelta(days=3)).isoformat(),
                "to":     date.today().isoformat(),
                "sort":   "asc", "limit": 5,
            },
            timeout=8
        )
        bars = r.json().get("results", [])
        if len(bars) >= 2:
            prev_close = bars[-2]["c"]
            today_open = bars[-1]["o"]
            return (today_open - prev_close) / prev_close
    except Exception:
        pass

    return 0.0


def get_spot(ticker: str) -> float:
    """Get current spot price from Polygon snapshot."""
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_KEY},
            timeout=8
        )
        t = r.json().get("ticker", {})
        return float(t.get("lastTrade", {}).get("p", 0) or
                     t.get("day", {}).get("c", 0) or 0)
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════════════
# COMPUTE + CACHE
# ══════════════════════════════════════════════════════════════════════

def compute_and_cache_vex() -> dict:
    """Compute VEX for all tickers and save cache. Run at 8:30 AM."""
    iv_change = get_iv_change()
    log.info(f"VEX: IV change = {iv_change*100:+.1f}%")

    result = {
        "date":       date.today().isoformat(),
        "computed":   datetime.now().strftime("%H:%M ET"),
        "iv_change":  round(iv_change * 100, 2),
        "tickers":    {},
    }

    for ticker in TICKERS:
        spot  = get_spot(ticker)
        if not spot:
            log.warning(f"VEX: no spot for {ticker}")
            result["tickers"][ticker] = _empty_vex(iv_change)
            continue

        chain = fetch_chain_with_greeks(ticker, spot)
        vex   = calculate_vex(chain, spot, iv_change)
        vex["ticker"] = ticker
        vex["spot"]   = spot
        vex["chain_size"] = len(chain)
        result["tickers"][ticker] = vex

        log.info(f"  VEX {ticker}: {vex['signal']} "
                 f"net={vex['net_vex']:.1f}K bull_pts={vex['bull_pts']} "
                 f"bear_pts={vex['bear_pts']}")

    VEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(VEX_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    return result


def load_vex_cache(ticker: str = "SPY") -> dict:
    """Load cached VEX. Falls back to fresh compute if stale."""
    try:
        if VEX_CACHE.exists():
            with open(VEX_CACHE) as f:
                data = json.load(f)
            if data.get("date") == date.today().isoformat():
                return data.get("tickers", {}).get(ticker, _empty_vex())
    except Exception:
        pass
    result = compute_and_cache_vex()
    return result.get("tickers", {}).get(ticker, _empty_vex())


# ══════════════════════════════════════════════════════════════════════
# ARJUN INTEGRATION
# ══════════════════════════════════════════════════════════════════════

def get_vex_agent_boost(ticker: str) -> dict:
    """
    Returns bull/bear score adjustments for ARJUN agents.
    Called by coordinator.py before final signal.
    """
    vex = load_vex_cache(ticker)
    return {
        "bull_boost": vex.get("bull_pts", 0),
        "bear_boost": vex.get("bear_pts", 0),
        "signal":     vex.get("signal", "NEUTRAL"),
        "label":      vex.get("label", ""),
        "iv_crush":   vex.get("iv_crush", False),
        "vex":        vex,
    }


def get_vex_briefing_line() -> str:
    """One-line VEX summary for Daily Briefing."""
    try:
        if VEX_CACHE.exists():
            with open(VEX_CACHE) as f:
                data = json.load(f)
            spy = data.get("tickers", {}).get("SPY", {})
            return (f"{spy.get('label', 'Vanna neutral')}  |  "
                    f"IV change: {spy.get('iv_change_pct', 0):+.1f}%  |  "
                    f"Net VEX: {spy.get('net_vex', 0):.1f}K")
    except Exception:
        pass
    return "Vanna data unavailable"


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    result = compute_and_cache_vex()
    print(f"\n── VEX Results ── IV change: {result['iv_change']:+.1f}% ──────────────")
    for ticker, vex in result.get("tickers", {}).items():
        print(f"  {ticker:5s}  {vex['signal']:20s}  "
              f"net={vex['net_vex']:+.1f}K  "
              f"bull+{vex['bull_pts']} bear+{vex['bear_pts']}  "
              f"{vex['label']}")
