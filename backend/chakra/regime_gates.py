"""
regime_gates.py — CHAKRA Regime Gate Engine
Reads DEX + Internals caches and returns conviction adjustments for ARKA.

Gates:
  1. Gamma Flip Gate   — DEX positioning → trend vs mean-reversion bias
  2. Breadth Gate      — Neural Pulse breadth → suppress weak-breadth longs
  3. VIX Gate          — VIX level → suppress all longs above threshold
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DEX_CACHE       = Path("logs/chakra/dex_latest.json")
INTERNALS_CACHE = Path("logs/internals/internals_latest.json")
MAX_CACHE_AGE   = 60  # minutes — ignore stale caches


def _load_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    age_min = (datetime.now(timezone.utc) -
               datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
               ).total_seconds() / 60
    if age_min > MAX_CACHE_AGE:
        log.warning(f"[GATES] {path.name} is {age_min:.0f}min old — skipping")
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.warning(f"[GATES] Failed to load {path.name}: {e}")
        return None


def get_regime_gates(spy_price: float = None) -> dict:
    """
    Returns gate adjustments to apply to ARKA conviction score.

    Returns:
        {
          "long_threshold_adj":  +N  (raise threshold = harder to go long)
          "short_threshold_adj": -N  (lower threshold = easier to go short)
          "suppress_longs":      bool
          "bias":                "BEARISH" | "BULLISH" | "NEUTRAL"
          "reasons":             [list of strings]
          "regime":              "TREND" | "MEAN_REVERT" | "UNKNOWN"
        }
    """
    result = {
        "long_threshold_adj":  0,
        "short_threshold_adj": 0,
        "suppress_longs":      False,
        "bias":                "NEUTRAL",
        "regime":              "UNKNOWN",
        "reasons":             []
    }

    # ── Gate 1: Gamma Flip (DEX) ─────────────────────────────────────────────
    dex = _load_cache(DEX_CACHE)
    if dex:
        spy_dex = dex.get("tickers", {}).get("SPY", {})
        positioning = spy_dex.get("positioning", "NEUTRAL")   # DEALER_LONG/SHORT/NEUTRAL
        net_dex_b   = spy_dex.get("net_dex_billions", 0)
        flip_level  = spy_dex.get("gamma_flip_level", None)

        if positioning == "DEALER_SHORT":
            # Below gamma flip — trend mode, dealers amplify moves
            result["regime"] = "TREND"
            result["bias"]   = "BEARISH"
            result["long_threshold_adj"]  += 10   # harder to go long
            result["short_threshold_adj"] -= 5    # easier to go short
            result["reasons"].append(
                f"DEX DEALER_SHORT ({net_dex_b:.2f}B) → TREND mode, below gamma flip"
            )
            if flip_level and spy_price:
                result["reasons"].append(
                    f"Gamma flip ~{flip_level:.0f}, SPY {spy_price:.2f} = "
                    f"{'BELOW' if spy_price < flip_level else 'ABOVE'} flip"
                )

        elif positioning == "DEALER_LONG":
            # Above gamma flip — mean-reversion mode
            result["regime"] = "MEAN_REVERT"
            result["bias"]   = "NEUTRAL"
            result["long_threshold_adj"]  -= 3    # slightly easier to go long
            result["reasons"].append(
                f"DEX DEALER_LONG ({net_dex_b:.2f}B) → MEAN-REVERT mode, above gamma flip"
            )
        else:
            result["regime"] = "UNKNOWN"
            result["reasons"].append(f"DEX NEUTRAL — no gamma gate applied")
    else:
        result["reasons"].append("DEX cache unavailable — gamma gate skipped")

    # ── Gate 2: Market Breadth (Internals) ───────────────────────────────────
    internals = _load_cache(INTERNALS_CACHE)
    if internals:
        breadth = internals.get("neural_pulse", {}).get("score", None)
        vix     = internals.get("vix", {}).get("level", None)

        if breadth is not None:
            if breadth < 30:
                result["long_threshold_adj"] += 15
                result["suppress_longs"]      = True
                result["bias"]                = "BEARISH"
                result["reasons"].append(
                    f"Neural Pulse {breadth}/100 — BREADTH COLLAPSED, suppressing longs"
                )
            elif breadth < 45:
                result["long_threshold_adj"] += 10
                result["reasons"].append(
                    f"Neural Pulse {breadth}/100 — weak breadth, +10 long threshold"
                )
            elif breadth > 65:
                result["long_threshold_adj"]  -= 5
                result["short_threshold_adj"] += 8
                result["reasons"].append(
                    f"Neural Pulse {breadth}/100 — strong breadth, -5 long threshold"
                )
            else:
                result["reasons"].append(f"Neural Pulse {breadth}/100 — neutral breadth")

        # ── Gate 3: VIX Level (with UVXY proxy fallback) ─────────────────────
        # Polygon 403 blocks direct VIX. If level missing, derive from UVXY price.
        # UVXY ≈ 1.5x VIX short-term futures. Empirically: VIX ≈ UVXY * 0.55
        if (vix is None or vix <= 0):
            try:
                _idx = internals.get("index_last", {})
                _uvxy = float(_idx.get("UVXY", 0) or 0)
                if _uvxy > 0:
                    vix = round(_uvxy * 0.55, 1)  # rough VIX proxy from UVXY
                    result["reasons"].append(f"VIX proxy from UVXY ${_uvxy:.2f} → VIX≈{vix:.1f}")
            except Exception:
                pass

        if vix is not None and vix > 0:
            if vix >= 27:
                result["suppress_longs"]      = True
                result["long_threshold_adj"]  += 20
                result["short_threshold_adj"] -= 10
                result["bias"]                = "BEARISH"
                result["reasons"].append(
                    f"VIX {vix:.1f} ≥ 27 — HIGHWAY OPEN, longs suppressed, press shorts"
                )
            elif vix >= 25:
                result["long_threshold_adj"]  += 12
                result["short_threshold_adj"] -= 5
                result["bias"]                = "BEARISH"
                result["reasons"].append(
                    f"VIX {vix:.1f} ≥ 25 — neg-GEX highway triggered, raise long threshold"
                )
            elif vix >= 22:
                result["long_threshold_adj"]  += 5
                result["reasons"].append(
                    f"VIX {vix:.1f} elevated — mild long suppression"
                )
            else:
                result["reasons"].append(f"VIX {vix:.1f} — calm, no VIX gate")
        else:
            result["reasons"].append("VIX unavailable (Polygon 403) — VIX gate skipped")
    else:
        result["reasons"].append("Internals cache unavailable — breadth/VIX gates skipped")

    # ── Final bias summary ────────────────────────────────────────────────────
    long_adj  = result["long_threshold_adj"]
    short_adj = result["short_threshold_adj"]
    log.info(f"[GATES] Regime={result['regime']} Bias={result['bias']} "
             f"LongAdj={long_adj:+d} ShortAdj={short_adj:+d} "
             f"SuppressLongs={result['suppress_longs']}")
    for r in result["reasons"]:
        log.info(f"[GATES]   → {r}")

    return result
