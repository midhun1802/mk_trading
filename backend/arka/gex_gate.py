"""
GEX Gate for ARKA Engine
Filters and adjusts conviction scores based on gamma exposure structure.
Includes George video insights: regime call, dollar bias, acceleration.

Called in scan_ticker() after conviction calculation, before order placement.

Usage:
    from backend.arka.gex_state import load_gex_state
    from backend.arka.gex_gate import gex_gate

    gex_state  = load_gex_state(ticker)
    gex_dir    = "PUT" if is_short else "CALL"
    gate_result = gex_gate(gex_dir, signal["conviction"], gex_state)

    if not gate_result["allow"]:
        # skip signal
    else:
        signal["conviction"] = gate_result["conviction"]
"""
from typing import Dict, Any, Optional

# ── Configuration ──────────────────────────────────────────────────────────────
CALL_WALL_BUFFER_PCT      = 0.4    # Block calls within 0.4% of call wall
PUT_WALL_BUFFER_PCT       = 0.4    # Block puts within 0.4% of put wall
ZERO_GAMMA_EXPLOSIVE_BAND = 1.5    # $ distance to zero gamma for explosive zone
EXTREME_BIAS_RATIO        = 3.0    # Hard block when put/call ratio > 3x vs direction
ACCELERATION_THRESHOLD    = 15     # Boost conviction when acceleration > this


def gex_gate(signal_direction: str, conviction: float,
             gex: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Apply GEX-based filters and conviction adjustments.

    Args:
        signal_direction: "CALL" or "PUT"
        conviction:       Current conviction score (0-100)
        gex:              GEX state dict from load_gex_state(), or None

    Returns dict:
        allow:        bool   — False = hard block, do not trade
        conviction:   float  — adjusted score (clamped 0-100)
        reason:       str    — human-readable summary of all adjustments
        regime_call:  str    — SHORT_THE_POPS / BUY_THE_DIPS / FOLLOW_MOMENTUM / NEUTRAL
        bias_ratio:   float  — put/call dollar exposure ratio
    """
    if not gex:
        return {
            "allow":       True,
            "conviction":  conviction,
            "reason":      "GEX unavailable — no adjustment",
            "regime_call": "NEUTRAL",
            "bias_ratio":  1.0,
        }

    reason_parts = []
    blocked      = False

    # Cap total upward GEX adjustments at +20 so persistently bullish GEX
    # conditions don't permanently lock conviction at 100 regardless of technicals.
    # Penalties are never capped — bad setups always get penalized.
    GEX_BOOST_CAP = 20
    _boost_total  = 0

    def _boost(pts: float, label: str) -> float:
        nonlocal _boost_total
        if pts <= 0:
            reason_parts.append(label)
            return pts
        remaining = GEX_BOOST_CAP - _boost_total
        actual    = min(pts, remaining)
        if actual > 0:
            _boost_total += actual
            reason_parts.append(label)
        return actual

    # ════════════════════════════════════════════════════════════════════
    # GEORGE INSIGHT: REGIME CALL — master bias filter
    # "Short the Pops" or "Buy the Dips" sets the directional preference.
    # Against-regime = -20 conviction. Negative gamma = +8 (amplifier).
    # ════════════════════════════════════════════════════════════════════
    regime_call = gex.get("regime_call", "NEUTRAL")

    if regime_call == "SHORT_THE_POPS" and signal_direction == "CALL":
        conviction += _boost(-20, "⚠️ Regime: SHORT_THE_POPS — dealers fade upside, penalizing CALL -20")
    elif regime_call == "BUY_THE_DIPS" and signal_direction == "PUT":
        conviction += _boost(-20, "⚠️ Regime: BUY_THE_DIPS — dealers support downside, penalizing PUT -20")
    elif regime_call == "FOLLOW_MOMENTUM":
        conviction += _boost(8, "✅ Regime: FOLLOW_MOMENTUM — negative gamma amplifies moves +8")

    # ════════════════════════════════════════════════════════════════════
    # GEORGE INSIGHT: DOLLAR BIAS — hard block on extreme directional lean
    # "$5.5B puts vs $1.3B calls" tells you dealer positioning strength.
    # ════════════════════════════════════════════════════════════════════
    bias_ratio = gex.get("bias_ratio", 1.0)
    call_gex_b = gex.get("call_gex_dollars", 0) / 1e9
    put_gex_b  = gex.get("put_gex_dollars",  0) / 1e9

    if bias_ratio > EXTREME_BIAS_RATIO and signal_direction == "CALL":
        blocked = True
        reason_parts.append(
            f"❌ BLOCKED: Put ${put_gex_b:.1f}B vs Call ${call_gex_b:.1f}B "
            f"— extreme bearish bias ({bias_ratio:.1f}x)"
        )
    elif bias_ratio < (1 / EXTREME_BIAS_RATIO) and signal_direction == "PUT":
        blocked = True
        _inv = (1 / bias_ratio) if bias_ratio > 0 else 99.0
        reason_parts.append(
            f"❌ BLOCKED: Call ${call_gex_b:.1f}B vs Put ${put_gex_b:.1f}B "
            f"— extreme bullish bias ({_inv:.1f}x)"
        )
    elif bias_ratio > 2.0 and signal_direction == "CALL":
        conviction -= 10
        reason_parts.append(f"⚠️ Strong put bias {bias_ratio:.1f}x — penalizing CALL -10")
    elif bias_ratio < 0.5 and signal_direction == "PUT":
        conviction -= 10
        _inv = (1 / bias_ratio) if bias_ratio > 0 else 99.0
        reason_parts.append(f"⚠️ Strong call bias {_inv:.1f}x — penalizing PUT -10")

    if blocked:
        return {
            "allow":       False,
            "conviction":  max(0, min(100, conviction)),
            "reason":      " | ".join(reason_parts),
            "regime_call": regime_call,
            "bias_ratio":  bias_ratio,
        }

    # ════════════════════════════════════════════════════════════════════
    # WALL PROXIMITY — block entries too close to walls
    # ════════════════════════════════════════════════════════════════════
    if signal_direction == "CALL":
        call_wall = gex.get("call_wall", 0)
        live_spot = gex.get("live_spot") or gex.get("spot", 0)
        if call_wall and live_spot:
            # Recalculate live: if spot > call_wall, price already broke through — no block
            if live_spot > call_wall:
                pct_to_call = (live_spot - call_wall) / call_wall * 100  # positive = above wall
            else:
                pct_to_call = (call_wall - live_spot) / call_wall * 100  # approaching from below
            approaching = live_spot <= call_wall  # only block if not yet at wall
        else:
            pct_to_call = gex.get("pct_to_call_wall", 100)
            approaching = True
        if approaching and 0 <= pct_to_call < CALL_WALL_BUFFER_PCT:
            blocked = True
            reason_parts.append(
                f"❌ BLOCKED: Call wall ${call_wall:.2f} only "
                f"{pct_to_call:.2f}% away (dealers sell into strength)"
            )
        elif approaching and pct_to_call < 1.0:
            conviction -= 12
            reason_parts.append(f"⚠️ Approaching call wall — conviction -12")

    if signal_direction == "PUT" and not blocked:
        put_wall  = gex.get("put_wall", 0)
        live_spot = gex.get("live_spot") or gex.get("spot", 0)
        if put_wall and live_spot:
            if live_spot < put_wall:
                pct_to_put = (put_wall - live_spot) / put_wall * 100  # positive = below wall
            else:
                pct_to_put = (live_spot - put_wall) / put_wall * 100
            approaching_put = live_spot >= put_wall
        else:
            pct_to_put = gex.get("pct_to_put_wall", 100)
            approaching_put = True
        if approaching_put and 0 <= pct_to_put < PUT_WALL_BUFFER_PCT:
            blocked = True
            reason_parts.append(
                f"❌ BLOCKED: Put wall ${put_wall:.2f} only "
                f"{pct_to_put:.2f}% away (dealers buy into weakness)"
            )
        elif approaching_put and pct_to_put < 1.0:
            conviction -= 12
            reason_parts.append(f"⚠️ Approaching put wall — conviction -12")

    if blocked:
        return {
            "allow":       False,
            "conviction":  max(0, min(100, conviction)),
            "reason":      " | ".join(reason_parts),
            "regime_call": regime_call,
            "bias_ratio":  bias_ratio,
        }

    # ════════════════════════════════════════════════════════════════════
    # GAMMA REGIME — penalize momentum-chasing in positive gamma
    # ════════════════════════════════════════════════════════════════════
    regime = gex.get("regime", "UNKNOWN")
    above  = gex.get("above_zero_gamma", True)

    if regime == "POSITIVE_GAMMA":
        if signal_direction == "CALL" and above:
            conviction += _boost(-8, "⚠️ Positive gamma + above zero: mean-revert risk -8")
        elif signal_direction == "PUT" and not above:
            conviction += _boost(-8, "⚠️ Positive gamma + below zero: mean-revert risk -8")
    elif regime == "NEGATIVE_GAMMA":
        if signal_direction == "CALL" and above:
            conviction += _boost(10, "✅ Negative gamma + above zero: dealers amplifying upside +10")
        elif signal_direction == "PUT" and not above:
            conviction += _boost(10, "✅ Negative gamma + below zero: dealers amplifying downside +10")

    # ════════════════════════════════════════════════════════════════════
    # EXPLOSIVE ZONE — near zero gamma flip
    # ════════════════════════════════════════════════════════════════════
    spot       = gex.get("spot", 0)
    zero_gamma = gex.get("zero_gamma", 0)
    if spot and zero_gamma:
        distance = abs(spot - zero_gamma)
        if distance <= ZERO_GAMMA_EXPLOSIVE_BAND:
            conviction += _boost(
                8,
                f"⚡ Near zero gamma ${zero_gamma:.2f} (±${distance:.2f}) — explosive zone +8"
            )

    # ════════════════════════════════════════════════════════════════════
    # CLIFF — GEX expiring today = vol expansion
    # ════════════════════════════════════════════════════════════════════
    if gex.get("cliff_today"):
        cliff_strike = gex.get("cliff_strike")
        strike_str   = f" at ${cliff_strike:.2f}" if cliff_strike else ""
        conviction  += _boost(6, f"🧨 GEX cliff expiring{strike_str} — vol expansion +6")

    # ════════════════════════════════════════════════════════════════════
    # GEORGE INSIGHT: ACCELERATION — fast tape boost
    # ════════════════════════════════════════════════════════════════════
    if signal_direction == "CALL":
        accel = gex.get("accel_up", 0)
        if accel > ACCELERATION_THRESHOLD:
            conviction += _boost(10, f"⚡ High upside acceleration ({accel:.0f}) — fast tape +10")
    elif signal_direction == "PUT":
        accel = gex.get("accel_down", 0)
        if accel > ACCELERATION_THRESHOLD:
            conviction += _boost(10, f"⚡ High downside acceleration ({accel:.0f}) — fast tape +10")

    conviction = max(0, min(100, conviction))

    return {
        "allow":       True,
        "conviction":  conviction,
        "reason":      " | ".join(reason_parts) if reason_parts else "GEX: No adjustment",
        "regime_call": regime_call,
        "bias_ratio":  bias_ratio,
    }
