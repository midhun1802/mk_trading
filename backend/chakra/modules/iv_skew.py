"""
CHAKRA — IV Skew Surface Monitor
backend/chakra/modules/iv_skew.py

The SHAPE of the vol surface reveals where smart money is most fearful.
Steep put skew = institutions buying downside protection (bearish signal).
Positive call skew = melt-up/squeeze being priced in (bullish signal).

Uses IV values estimated from vega (since Polygon doesn't return IV directly
in chain snapshot — same approach as VEX/Charm modules).

Skew = IV(25-delta put) - IV(25-delta call)
  > +5%  → BEARISH_FEAR    → ARJUN Bear agent +15 pts
  < -2%  → MELTUP_SQUEEZE  → flag in briefing, boost Bull agent
  0-5%   → NEUTRAL

Integration:
  - ARJUN Bear agent → steep put skew adds +15 pts
  - GEX Tab         → skew sentiment badge overlay
  - Daily Briefing  → flag on extreme skew days
"""

import json
import logging
import numpy as np
import httpx
import os
from datetime import date, timedelta, datetime
from pathlib import Path
from dotenv import load_dotenv

try:
    from backend.chakra.modules.prob_distribution import implied_vol_from_price as _nr_solver
except ImportError:
    _nr_solver = None

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

log          = logging.getLogger("chakra.ivskew")
POLYGON_KEY  = os.getenv("POLYGON_API_KEY", "")
SKEW_CACHE   = BASE / "logs" / "chakra" / "ivskew_latest.json"

TICKERS = ["SPY", "QQQ", "IWM"]

# Skew thresholds
SKEW_BEARISH  =  0.05   # put IV > call IV by 5% = fear
SKEW_BULLISH  = -0.02   # call IV > put IV by 2% = squeeze


# ══════════════════════════════════════════════════════════════════════
# IV ESTIMATION FROM GREEKS
# ══════════════════════════════════════════════════════════════════════



def _get_iv(vega_or_chain, spot_or_price, strike_or_type=None, dte=30, option_type='call'):
    """
    Flexible IV calculator.
    Called as: _get_iv(vega, spot, strike, dte)  — from calculate_iv_skew
    Or as:     _get_iv(option_dict, spot, type)   — legacy
    """
    import math

    # Detect call signature
    if isinstance(vega_or_chain, dict):
        # Dict call: _get_iv(option_data, underlying_price, option_type)
        opt   = vega_or_chain
        spot  = float(spot_or_price or 0)
        otype = str(strike_or_type or 'call').lower()
        vega  = abs(float(opt.get('vega', 0) or 0))
        strike = float(opt.get('strike_price', opt.get('strike', spot)) or spot)
        dte_v  = float(opt.get('days_to_expiry', opt.get('dte', 30)) or 30)
        mid    = float(opt.get('mid_price', 0) or
                       (opt.get('ask', 0) + opt.get('bid', 0)) / 2 or 0)
    else:
        # Positional call: _get_iv(vega, spot, strike, dte)
        vega   = abs(float(vega_or_chain or 0))
        spot   = float(spot_or_price or 0)
        strike = float(strike_or_type or spot)
        dte_v  = float(dte or 30)
        otype  = 'call' if strike >= spot else 'put'
        mid    = 0.0  # not available from greeks alone

    if spot <= 0:
        return 5.0

    T = max(dte_v, 0.5) / 365.0

    # Try Newton-Raphson from mid_price first (most accurate)
    if mid > 0.01 and _nr_solver is not None:
        try:
            iv = _nr_solver(mid, spot, strike, T, 0.05, otype)
            if iv and 0.01 < iv < 5.0:
                return round(iv * 100, 2)
        except Exception:
            pass

    # Vega-based estimate
    if vega > 0:
        try:
            moneyness  = abs(math.log(spot / strike)) if strike > 0 else 0
            phi_est    = math.exp(-0.5 * moneyness ** 2) / math.sqrt(2 * math.pi)
            iv_decimal = vega / (spot * math.sqrt(T) * phi_est * 100 + 1e-8)
            iv_pct     = iv_decimal * 100
            if 2.0 < iv_pct < 300.0:
                return round(iv_pct, 2)
        except Exception:
            pass

    # OTM floor based on moneyness (always >5% for real options)
    try:
        otm = abs(spot - strike) / spot
        return round(max(5.0 + otm * 50, 5.0), 2)
    except Exception:
        return 5.0

def calculate_iv_skew(chain: list, spot: float) -> dict:
    """
    Calculate put/call IV skew at the 25-delta strikes.

    chain: list of {strike, type, delta, vega, oi, dte}
    spot: current underlying price

    Positive skew = put skew = bearish fear
    Negative skew = call skew = melt-up/squeeze pricing
    """
    if not chain or not spot:
        return _empty_skew()

    # Find 25-delta puts and calls
    # 25-delta put: |delta| between 0.20-0.30, type=put
    # 25-delta call: delta between 0.20-0.30, type=call
    puts_25d  = []
    calls_25d = []

    for c in chain:
        delta = abs(float(c.get("delta", 0) or 0))
        ctype = c.get("type", "")
        vega  = float(c.get("vega", 0) or 0)
        dte   = float(c.get("dte", 1) or 1)
        strike = float(c.get("strike", spot) or spot)

        if not (0.15 <= delta <= 0.35):
            continue

        iv = _get_iv(vega, spot, strike, dte)

        entry = {"strike": strike, "delta": delta, "iv": iv,
                 "oi": c.get("oi", 0), "vega": vega}

        if ctype == "put":
            puts_25d.append(entry)
        elif ctype == "call":
            calls_25d.append(entry)

    if not puts_25d or not calls_25d:
        return _empty_skew(note="insufficient 25-delta contracts")

    # Sort by delta proximity to 0.25
    puts_25d.sort(key=lambda x: abs(x["delta"] - 0.25))
    calls_25d.sort(key=lambda x: abs(x["delta"] - 0.25))

    # Use top 3 nearest to 0.25 delta and weight by OI
    def weighted_iv(contracts: list, n: int = 3) -> float:
        top = contracts[:n]
        weights = [c.get("oi", 1) or 1 for c in top]
        total_w = sum(weights)
        if total_w == 0:
            return np.mean([c["iv"] for c in top])
        return sum(c["iv"] * w for c, w in zip(top, weights)) / total_w

    iv_25p = weighted_iv(puts_25d)
    iv_25c = weighted_iv(calls_25d)
    skew   = round(iv_25p - iv_25c, 4)

    # Classify
    if skew > SKEW_BEARISH:
        sentiment   = "BEARISH_FEAR"
        label       = f"😨 Put Skew — Fear Priced In ({skew*100:+.1f}%)"
        color       = "FF2D55"
        bear_boost  = 15
        bull_boost  = 0
    elif skew < SKEW_BULLISH:
        sentiment   = "MELTUP_SQUEEZE"
        label       = f"🚀 Call Skew — Squeeze Priced In ({skew*100:+.1f}%)"
        color       = "00FF9D"
        bear_boost  = 0
        bull_boost  = 10
    else:
        sentiment   = "NEUTRAL"
        label       = f"➡️ Neutral Skew ({skew*100:+.1f}%)"
        color       = "888888"
        bear_boost  = 0
        bull_boost  = 0

    # Skew magnitude
    if abs(skew) > 0.15:   skew_strength = "EXTREME"
    elif abs(skew) > 0.08: skew_strength = "STRONG"
    elif abs(skew) > 0.04: skew_strength = "MODERATE"
    else:                  skew_strength = "MILD"

    return {
        "skew":         skew,
        "skew_pct":     round(skew * 100, 2),
        "iv_25p":       round(iv_25p, 4),
        "iv_25c":       round(iv_25c, 4),
        "iv_25p_pct":   round(iv_25p * 100, 1),
        "iv_25c_pct":   round(iv_25c * 100, 1),
        "sentiment":    sentiment,
        "label":        label,
        "color":        color,
        "bear_boost":   bear_boost,
        "bull_boost":   bull_boost,
        "skew_strength":skew_strength,
        "puts_used":    len(puts_25d),
        "calls_used":   len(calls_25d),
    }


def _empty_skew(note: str = "") -> dict:
    return {
        "skew": 0, "skew_pct": 0,
        "iv_25p": 0, "iv_25c": 0,
        "iv_25p_pct": 0, "iv_25c_pct": 0,
        "sentiment": "NEUTRAL", "label": f"Skew unavailable {note}",
        "color": "888888", "bear_boost": 0, "bull_boost": 0,
        "skew_strength": "MILD", "puts_used": 0, "calls_used": 0,
    }


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHER
# ══════════════════════════════════════════════════════════════════════

def fetch_chain_for_skew(ticker: str, spot: float) -> list:
    """
    Fetch options chain with greeks for skew calculation.
    Uses wider strike range (±15%) to capture 25-delta strikes.
    """
    low  = round(spot * 0.85)
    high = round(spot * 1.15)

    try:
        # Fetch 7-30 DTE contracts for more stable skew (0DTE skew is noisy)
        exp_start = (date.today() + timedelta(days=7)).isoformat()
        exp_end   = (date.today() + timedelta(days=35)).isoformat()

        r = httpx.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker}",
            params={
                "apiKey":              POLYGON_KEY,
                "limit":               250,
                "strike_price.gte":    low,
                "strike_price.lte":    high,
                "expiration_date.gte": exp_start,
                "expiration_date.lte": exp_end,
            },
            timeout=12
        )
        results = r.json().get("results", [])
        chain   = []

        for c in results:
            greeks = c.get("greeks", {})
            if not greeks or not greeks.get("delta"):
                continue
            details = c.get("details", {})
            exp     = details.get("expiration_date", "")
            dte     = (date.fromisoformat(exp) - date.today()).days if exp else 30

            chain.append({
                "strike": details.get("strike_price", 0),
                "type":   details.get("contract_type", "call"),
                "delta":  greeks.get("delta", 0),
                "gamma":  greeks.get("gamma", 0),
                "vega":   greeks.get("vega", 0),
                "theta":  greeks.get("theta", 0),
                "mid_price": round((float(c.get("details", {}).get("ask", 0) or c.get("ask", 0) or 0) + float(c.get("details", {}).get("bid", 0) or c.get("bid", 0) or 0)) / 2, 4),
                "oi":     c.get("open_interest", 0) or 0,
                "dte":    dte,
            })

        return chain

    except Exception as e:
        log.warning(f"Skew chain fetch {ticker}: {e}")
        return []


def get_spot(ticker: str) -> float:
    """Get current spot price."""
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

def compute_and_cache_skew() -> dict:
    """Compute IV skew for all tickers. Run at 8:30 AM and every 30 min."""
    result = {
        "date":     date.today().isoformat(),
        "computed": datetime.now().strftime("%H:%M ET"),
        "tickers":  {},
    }

    for ticker in TICKERS:
        spot  = get_spot(ticker)
        if not spot:
            result["tickers"][ticker] = _empty_skew("no spot")
            continue

        chain = fetch_chain_for_skew(ticker, spot)
        skew  = calculate_iv_skew(chain, spot)
        skew.update({"ticker": ticker, "spot": spot,
                     "chain_size": len(chain)})
        result["tickers"][ticker] = skew

        log.info(f"  Skew {ticker}: {skew['sentiment']} "
                 f"skew={skew['skew_pct']:+.1f}% "
                 f"25p={skew['iv_25p_pct']:.1f}% 25c={skew['iv_25c_pct']:.1f}% "
                 f"bear+{skew['bear_boost']} bull+{skew['bull_boost']}")

    SKEW_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(SKEW_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    return result


def load_skew_cache(ticker: str = "SPY") -> dict:
    """Load cached skew. Recomputes if > 30 min old."""
    try:
        if SKEW_CACHE.exists():
            import time
            age = time.time() - SKEW_CACHE.stat().st_mtime
            if age < 1800:
                with open(SKEW_CACHE) as f:
                    data = json.load(f)
                t = data.get("tickers", {}).get(ticker)
                if t:
                    return t
    except Exception:
        pass
    result = compute_and_cache_skew()
    return result.get("tickers", {}).get(ticker, _empty_skew())


# ══════════════════════════════════════════════════════════════════════
# ARJUN INTEGRATION
# ══════════════════════════════════════════════════════════════════════

def get_skew_agent_boost(ticker: str) -> dict:
    """
    Returns bear/bull score adjustments for ARJUN agents based on skew.
    Steep put skew → bear agent +15 pts.
    Call skew → bull agent +10 pts.
    """
    skew = load_skew_cache(ticker)
    return {
        "bear_boost":   skew.get("bear_boost", 0),
        "bull_boost":   skew.get("bull_boost", 0),
        "sentiment":    skew.get("sentiment", "NEUTRAL"),
        "label":        skew.get("label", ""),
        "skew_pct":     skew.get("skew_pct", 0),
        "skew":         skew,
    }


def get_skew_gex_badge(ticker: str = "SPY") -> dict:
    """GEX tab badge — sentiment label and color for overlay."""
    skew = load_skew_cache(ticker)
    return {
        "label":     skew.get("label", ""),
        "color":     skew.get("color", "888888"),
        "sentiment": skew.get("sentiment", "NEUTRAL"),
        "skew_pct":  skew.get("skew_pct", 0),
    }


def get_skew_briefing_line(ticker: str = "SPY") -> str:
    """One-line skew summary for Daily Briefing."""
    skew = load_skew_cache(ticker)
    return (f"{skew.get('label', 'Skew N/A')}  |  "
            f"25P IV: {skew.get('iv_25p_pct', 0):.1f}%  |  "
            f"25C IV: {skew.get('iv_25c_pct', 0):.1f}%")


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    result = compute_and_cache_skew()
    print(f"\n── IV Skew Results ({result['computed']}) ──────────────────────")
    for ticker, s in result.get("tickers", {}).items():
        print(f"  {ticker:5s}  {s['sentiment']:15s}  "
              f"skew={s['skew_pct']:+.1f}%  "
              f"25P={s['iv_25p_pct']:.1f}%  25C={s['iv_25c_pct']:.1f}%  "
              f"[{s['skew_strength']}]  "
              f"bear+{s['bear_boost']} bull+{s['bull_boost']}")
