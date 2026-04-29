"""
CHAKRA — Charm Exposure Engine
backend/chakra/modules/charm_engine.py

Charm = how delta changes from TIME PASSING ALONE (even with zero price move).
At 3:30 PM daily, 0DTE delta decays so rapidly that dealers aggressively rebalance
— explaining the predictable EOD directional pushes ARKA already trades.

Charm is approximated from available greeks:
  Charm ≈ -theta * delta / (spot * iv * sqrt(T))  [Black-Scholes approximation]
  Or simplified: Charm ≈ theta / (spot * 0.01) for directional pressure

At DTE → 0, charm accelerates exponentially:
  DTE < 1 hr (0.04): CRITICAL urgency
  DTE < 2 hr (0.08): HIGH urgency
  DTE > 1 day:       NORMAL

Integration:
  - MOC Engine   → Charm direction at 3:45 PM confirms or overrides imbalance
  - Lotto Engine → charm_direction as entry gate at 3:30 PM
"""

import json
import logging
import math
import httpx
import os
from datetime import date, timedelta, datetime, time as dtime
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

log         = logging.getLogger("chakra.charm")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
CHARM_CACHE = BASE / "logs" / "chakra" / "charm_latest.json"

TICKERS = ["SPY", "QQQ", "IWM"]


# ══════════════════════════════════════════════════════════════════════
# CHARM CALCULATION
# ══════════════════════════════════════════════════════════════════════

def calc_charm(delta: float, theta: float, spot: float,
               iv: float, dte_days: float) -> float:
    """
    Approximate Charm (dDelta/dT) from available greeks.

    Charm ≈ -phi(d1) * (iv / (2*sqrt(T)) + r*d1 / (iv*sqrt(T)))
    Simplified practical form:
      Charm ≈ theta * delta / (spot * iv * sqrt(T))  [sign-adjusted]

    Negative charm → delta decaying → bullish dealer rebalancing
    Positive charm → delta growing → bearish dealer rebalancing
    """
    if not spot or not iv or dte_days <= 0:
        return 0.0
    try:
        T = max(dte_days / 252, 1e-6)  # years
        # Core charm approximation
        charm = -theta * delta / (spot * iv * math.sqrt(T) + 1e-10)
        return float(charm)
    except (ValueError, ZeroDivisionError, OverflowError):
        return 0.0


def calculate_charm_pressure(chain: list, spot: float) -> dict:
    """
    Calculate aggregate Charm Exposure from 0DTE options chain.

    chain: list of {strike, delta, theta, gamma, oi, type, iv, dte}
    Returns charm_flow direction + urgency level.
    """
    if not chain or not spot:
        return _empty_charm()

    now      = datetime.now()
    # Market hours remaining today (as fraction of day)
    close    = datetime.combine(date.today(), dtime(16, 0))
    mins_left = max(0, (close - now).seconds // 60)
    dte_frac = mins_left / (6.5 * 60)   # fraction of trading day left

    call_charm = 0.0
    put_charm  = 0.0

    # Focus on 0DTE and 1DTE contracts where charm is strongest
    zero_dte_chain = [c for c in chain if c.get("dte", 999) <= 1]
    active_chain   = zero_dte_chain if zero_dte_chain else chain[:50]

    for row in active_chain:
        delta  = row.get("delta", 0) or 0
        theta  = row.get("theta", 0) or 0
        oi     = row.get("oi", 0) or 0
        iv     = row.get("iv", 0.20) or 0.20
        ctype  = row.get("type", "call")
        dte    = row.get("dte", 1)

        # Use intraday DTE for 0DTE contracts
        dte_calc = dte_frac if dte == 0 else dte

        charm     = calc_charm(delta, theta, spot, iv, dte_calc)
        contrib   = charm * oi * 100

        if ctype == "call":
            call_charm += contrib
        else:
            put_charm  += contrib

    net_charm = call_charm + put_charm

    # Direction: negative charm = dealers must BUY = bullish pressure
    direction = "BULLISH" if net_charm < 0 else "BEARISH"
    color     = "00FF9D" if direction == "BULLISH" else "FF2D55"

    # Urgency based on time of day
    if mins_left < 30:    urgency, urgency_label = "CRITICAL", "🔥 CRITICAL (<30 min)"
    elif mins_left < 60:  urgency, urgency_label = "HIGH",     "⚡ HIGH (<1 hr)"
    elif mins_left < 120: urgency, urgency_label = "ELEVATED", "⚠️ Elevated (<2 hr)"
    else:                 urgency, urgency_label = "NORMAL",   "📊 Normal"

    # Magnitude
    mag = abs(net_charm) / 1000
    if mag > 500:   strength = "STRONG"
    elif mag > 100: strength = "MODERATE"
    else:           strength = "WEAK"

    # MOC confirmation signal
    moc_signal = direction if urgency in ("CRITICAL", "HIGH") and strength != "WEAK" else "NEUTRAL"

    return {
        "net_charm":     round(net_charm / 1000, 1),
        "call_charm":    round(call_charm / 1000, 1),
        "put_charm":     round(put_charm / 1000, 1),
        "direction":     direction,
        "urgency":       urgency,
        "urgency_label": urgency_label,
        "strength":      strength,
        "color":         color,
        "moc_signal":    moc_signal,
        "mins_left":     mins_left,
        "dte_frac":      round(dte_frac, 4),
        "contracts_used":len(active_chain),
    }


def _empty_charm() -> dict:
    return {
        "net_charm": 0, "call_charm": 0, "put_charm": 0,
        "direction": "NEUTRAL", "urgency": "NORMAL",
        "urgency_label": "No charm data", "strength": "WEAK",
        "color": "888888", "moc_signal": "NEUTRAL",
        "mins_left": 0, "dte_frac": 0, "contracts_used": 0,
    }


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHER
# ══════════════════════════════════════════════════════════════════════

def fetch_0dte_chain(ticker: str, spot: float) -> list:
    """Fetch today's 0DTE chain with greeks for charm calculation."""
    low  = round(spot * 0.97)
    high = round(spot * 1.03)

    try:
        r = httpx.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker}",
            params={
                "apiKey":            POLYGON_KEY,
                "limit":             250,
                "strike_price.gte":  low,
                "strike_price.lte":  high,
                "expiration_date":   date.today().isoformat(),
            },
            timeout=12
        )
        results = r.json().get("results", [])
        chain   = []

        for c in results:
            greeks  = c.get("greeks", {})
            if not greeks or not greeks.get("delta"):
                continue
            details = c.get("details", {})
            day     = c.get("day", {})
            vega    = greeks.get("vega", 0) or 0
            iv_est  = max(0.10, abs(vega) * 10) if vega else 0.20

            chain.append({
                "strike": details.get("strike_price", 0),
                "type":   details.get("contract_type", "call"),
                "delta":  greeks.get("delta", 0),
                "gamma":  greeks.get("gamma", 0),
                "theta":  greeks.get("theta", 0),
                "vega":   vega,
                "oi":     c.get("open_interest", 0) or 0,
                "volume": day.get("volume", 0) or 0,
                "iv":     iv_est,
                "dte":    0,   # today's expiry
            })

        return chain

    except Exception as e:
        log.warning(f"Charm chain fetch {ticker}: {e}")
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

def compute_and_cache_charm() -> dict:
    """Compute charm pressure for all tickers. Most useful 3:00-4:00 PM."""
    result = {
        "date":     date.today().isoformat(),
        "computed": datetime.now().strftime("%H:%M ET"),
        "tickers":  {},
    }

    for ticker in TICKERS:
        spot  = get_spot(ticker)
        if not spot:
            result["tickers"][ticker] = _empty_charm()
            continue

        chain = fetch_0dte_chain(ticker, spot)
        charm = calculate_charm_pressure(chain, spot)
        charm["ticker"] = ticker
        charm["spot"]   = spot
        result["tickers"][ticker] = charm

        log.info(f"  Charm {ticker}: {charm['direction']} "
                 f"[{charm['urgency']}] {charm['moc_signal']} "
                 f"net={charm['net_charm']:.1f}K")

    CHARM_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHARM_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    return result


def load_charm_cache(ticker: str = "SPY") -> dict:
    """Load cached charm. Always recomputes — charm changes throughout day."""
    # Charm is time-sensitive — recompute if cache > 10 min old
    try:
        if CHARM_CACHE.exists():
            import time
            age = time.time() - CHARM_CACHE.stat().st_mtime
            if age < 600:  # 10 minutes
                with open(CHARM_CACHE) as f:
                    data = json.load(f)
                return data.get("tickers", {}).get(ticker, _empty_charm())
    except Exception:
        pass
    result = compute_and_cache_charm()
    return result.get("tickers", {}).get(ticker, _empty_charm())


# ══════════════════════════════════════════════════════════════════════
# MOC + LOTTO INTEGRATION
# ══════════════════════════════════════════════════════════════════════

def get_moc_charm_signal(ticker: str = "SPY") -> dict:
    """
    Get charm direction for MOC Engine confirmation at 3:45 PM.
    Returns: {confirm: bool, direction: str, urgency: str, reason: str}
    """
    charm = load_charm_cache(ticker)
    direction = charm.get("moc_signal", "NEUTRAL")
    urgency   = charm.get("urgency", "NORMAL")

    return {
        "direction": direction,
        "urgency":   urgency,
        "confirm":   direction != "NEUTRAL",
        "strength":  charm.get("strength", "WEAK"),
        "reason":    f"Charm {direction} [{urgency}] — {charm.get('urgency_label', '')}",
        "charm":     charm,
    }


def get_lotto_charm_gate(ticker: str = "SPY") -> dict:
    """
    Charm gate for Lotto Engine at 3:30 PM.
    Returns direction signal + whether to proceed.
    """
    charm    = load_charm_cache(ticker)
    urgency  = charm.get("urgency", "NORMAL")
    strength = charm.get("strength", "WEAK")

    # Only use charm as gate if it's meaningful
    if urgency in ("CRITICAL", "HIGH") and strength in ("STRONG", "MODERATE"):
        return {
            "proceed":   True,
            "direction": charm.get("direction", "NEUTRAL"),
            "reason":    f"Charm {charm['direction']} confirmed [{urgency}]",
            "charm":     charm,
        }
    return {
        "proceed":   True,   # don't block, just no signal
        "direction": "NEUTRAL",
        "reason":    f"Charm weak [{urgency}] — no directional gate",
        "charm":     charm,
    }


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    result = compute_and_cache_charm()
    print(f"\n── Charm Results ({result['computed']}) ──────────────────────")
    for ticker, c in result.get("tickers", {}).items():
        print(f"  {ticker:5s}  {c['direction']:8s}  [{c['urgency']:8s}]  "
              f"net={c['net_charm']:+.1f}K  "
              f"MOC={c['moc_signal']}  {c['urgency_label']}")

    print("\n  MOC Signal (SPY):")
    moc = get_moc_charm_signal("SPY")
    print(f"    {moc['reason']}")
