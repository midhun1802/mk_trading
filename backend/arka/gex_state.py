"""
GEX State Loader for ARKA Engine
Loads latest GEX data with TTL enforcement and derived metrics.

State files are written by gex_calculator.write_gex_state() after every compute.
Location: logs/gex/gex_latest_{TICKER}.json
"""
import json, time, os
from typing import Optional, Dict, Any

TTL = 1800  # 30 minutes — stocks rotate every ~25 min, allow some slack


def load_gex_state(ticker: str = "SPY") -> Optional[Dict[str, Any]]:
    """
    Load current GEX state with freshness validation.
    Returns None if file missing or older than 10 minutes.

    Returns dict with keys:
        regime, regime_call, zero_gamma, call_wall, put_wall, net_gex, spot,
        pct_to_call_wall, pct_to_put_wall, above_zero_gamma,
        call_gex_dollars, put_gex_dollars, bias_ratio, dominant_side,
        accel_up, accel_down, expected_move_pts, upper_1sd, lower_1sd,
        pin_strikes, cliff_today, cliff_strike, ts, age_seconds
    """
    path = f"logs/gex/gex_latest_{ticker.upper()}.json"

    if not os.path.exists(path):
        return None

    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None

    # Enforce 10-minute TTL
    age_seconds = time.time() - data.get("ts", 0)
    if age_seconds > TTL:
        return None

    spot       = float(data.get("spot", 0))
    zero_gamma = float(data.get("zero_gamma", spot))
    call_wall  = float(data.get("call_wall", 0))
    put_wall   = float(data.get("put_wall", 0))

    return {
        # Core GEX data
        "regime":       data.get("regime", "UNKNOWN"),
        "regime_call":  data.get("regime_call", "NEUTRAL"),  # SHORT_THE_POPS / BUY_THE_DIPS / FOLLOW_MOMENTUM
        "zero_gamma":   zero_gamma,
        "call_wall":    call_wall,
        "put_wall":     put_wall,
        "net_gex":      float(data.get("net_gex", 0)),
        "spot":         spot,

        # Derived proximity metrics (always computed from stored values, not cached booleans)
        "pct_to_call_wall":  (call_wall - spot) / spot * 100 if call_wall > spot else 0.0,
        "pct_to_put_wall":   (spot - put_wall) / spot * 100  if spot > put_wall  else 0.0,
        "above_zero_gamma":  spot > zero_gamma,  # recomputed live; scan_ticker injects live_spot

        # Dollar exposure by direction (George insight)
        "call_gex_dollars":  float(data.get("call_gex_dollars", 0)),
        "put_gex_dollars":   float(data.get("put_gex_dollars", 0)),
        "bias_ratio":        float(data.get("bias_ratio", 1.0)),  # >1 = bearish lean
        "dominant_side":     data.get("dominant_side", "NEUTRAL"),

        # Acceleration (George insight)
        "accel_up":    float(data.get("accel_up", 0)),
        "accel_down":  float(data.get("accel_down", 0)),

        # Expected move (George insight)
        "expected_move_pts": float(data.get("expected_move_pts", 0)),
        "upper_1sd":         float(data.get("upper_1sd", spot * 1.01)),
        "lower_1sd":         float(data.get("lower_1sd", spot * 0.99)),

        # Pin strikes — oscillation zones
        "pin_strikes":  data.get("pin_strikes", []),

        # Cliff detection
        "cliff_today":   data.get("cliff", {}).get("expires_today", False),
        "cliff_strike":  data.get("cliff", {}).get("strike"),

        # Metadata
        "ts":          data.get("ts", 0),
        "age_seconds": age_seconds,
    }


def check_zero_gamma_shift(ticker: str, current_zero: float) -> dict:
    """
    Detect when the zero-gamma LEVEL itself has shifted between scans.
    A shifting zero-gamma = the regime is in flux = amplified opportunity.
    Persists prior values to logs/gex/gamma_flip_state.json.

    Returns:
        shifted   (bool)   — True if zero gamma moved >0.5%
        direction (str)    — "UP" or "DOWN"
        shift_pct (float)  — magnitude of the shift
        prev_zero (float)
        current_zero (float)
    """
    _FLIP_STATE = "logs/gex/gamma_flip_state.json"
    no_shift    = {"shifted": False, "direction": "", "shift_pct": 0.0,
                   "prev_zero": current_zero, "current_zero": current_zero}

    if not current_zero or current_zero <= 0:
        return no_shift

    # Load persisted state
    state: dict = {}
    try:
        import pathlib
        p = pathlib.Path(_FLIP_STATE)
        if p.exists():
            state = json.loads(p.read_text())
    except Exception:
        pass

    prev_data = state.get(ticker.upper(), {})
    prev_zero = float(prev_data.get("last_zero_gamma", current_zero))

    # Compute shift
    shift_pct = abs(current_zero - prev_zero) / prev_zero * 100 if prev_zero else 0.0

    # Persist updated zero for next call
    try:
        state[ticker.upper()] = {
            "last_zero_gamma": current_zero,
            "ts": time.time(),
        }
        import pathlib
        pathlib.Path(_FLIP_STATE).write_text(json.dumps(state, indent=2))
    except Exception:
        pass

    if shift_pct <= 0.5:
        return no_shift

    return {
        "shifted":      True,
        "direction":    "UP" if current_zero > prev_zero else "DOWN",
        "shift_pct":    round(shift_pct, 2),
        "prev_zero":    round(prev_zero, 2),
        "current_zero": round(current_zero, 2),
    }


_FLIP_STATE_PATH = "logs/gex/gamma_flip_state.json"
_REGIME_COOLDOWN = 1800   # 30 min minimum between alerts for same ticker

_REGIME_LABELS = {
    "POSITIVE_GAMMA": "Positive (Pin)",
    "NEGATIVE_GAMMA": "Negative (Explosive)",
    "LOW_VOL":        "Low Vol (Drifting)",
    "UNKNOWN":        "Unknown",
}

_REGIME_DESCRIPTIONS = {
    ("POSITIVE_GAMMA", "NEGATIVE_GAMMA"):
        "Dealers now **amplify** moves. Expect larger swings and faster breakouts.",
    ("NEGATIVE_GAMMA", "POSITIVE_GAMMA"):
        "Dealers now **absorb** moves. Expect mean-reversion and compressed range.",
    ("LOW_VOL", "NEGATIVE_GAMMA"):
        "Breakout from low-vol drift into explosive regime. High-momentum setup.",
    ("NEGATIVE_GAMMA", "LOW_VOL"):
        "Explosive regime cooling. Momentum slowing — reduce size.",
    ("LOW_VOL", "POSITIVE_GAMMA"):
        "Low-vol drift shifting to pinning regime. Range-bound conditions ahead.",
    ("POSITIVE_GAMMA", "LOW_VOL"):
        "Pinning regime relaxing into low-vol drift. Watch for direction pick.",
}


def _load_flip_state() -> dict:
    try:
        import pathlib
        p = pathlib.Path(_FLIP_STATE_PATH)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


def _save_flip_state(state: dict) -> None:
    try:
        import pathlib
        pathlib.Path(_FLIP_STATE_PATH).write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def check_regime_change(ticker: str, current_regime: str, gex_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Detect GEX regime change (POSITIVE_GAMMA ↔ NEGATIVE_GAMMA ↔ LOW_VOL).

    Args:
        ticker:         Underlying ticker (SPY, QQQ, NVDA, …)
        current_regime: Regime string from gex_latest file
        gex_data:       Full GEX state dict (for enriching the alert)

    Returns dict:
        changed      (bool)  — True if regime flipped vs last known
        old_regime   (str)
        new_regime   (str)
        old_label    (str)   — Human-readable old regime
        new_label    (str)   — Human-readable new regime
        description  (str)   — Dealer behaviour summary
        severity     (str)   — STRONG / MODERATE / MILD
        dealer_bias  (str)   — Bullish / Bearish / Neutral
        net_gex_m    (float) — Total GEX in $M
        accel_old    (float)
        accel_new    (float)
        spot         (float)
        ts           (float) — Unix timestamp of detection
    """
    no_change = {"changed": False, "old_regime": current_regime, "new_regime": current_regime}
    if not current_regime or current_regime == "UNKNOWN":
        return no_change

    key        = f"{ticker.upper()}_regime"
    ts_key     = f"{ticker.upper()}_regime_ts"
    accel_key  = f"{ticker.upper()}_accel"
    state      = _load_flip_state()

    old_regime  = state.get(key, "")
    last_flip_ts = float(state.get(ts_key, 0))
    old_accel   = float(state.get(accel_key, 0))
    now         = time.time()

    # Compute accel (net of up/down)
    accel_up   = float(gex_data.get("accel_up",   0) or 0)
    accel_down = float(gex_data.get("accel_down", 0) or 0)
    accel_now  = round(accel_up - accel_down, 1)

    # Always update persisted state
    state[key]       = current_regime
    state[accel_key] = accel_now
    _save_flip_state(state)

    # No previous state — first time seeing this ticker
    if not old_regime or old_regime == current_regime:
        return no_change

    # Cooldown: don't re-alert within 30 min
    if (now - last_flip_ts) < _REGIME_COOLDOWN:
        return no_change

    # Regime actually changed — stamp flip timestamp
    state[ts_key] = now
    _save_flip_state(state)

    # Severity: POSITIVE→NEGATIVE is highest impact
    flip_pair = (old_regime, current_regime)
    if flip_pair in (("POSITIVE_GAMMA", "NEGATIVE_GAMMA"), ("NEGATIVE_GAMMA", "POSITIVE_GAMMA")):
        severity = "STRONG"
    elif "LOW_VOL" in flip_pair:
        severity = "MODERATE"
    else:
        severity = "MILD"

    # Dealer bias from dollar exposure
    call_b = float(gex_data.get("call_gex_dollars", 0) or 0)
    put_b  = float(gex_data.get("put_gex_dollars",  0) or 0)
    if call_b > put_b * 1.2:
        dealer_bias = "Bullish"
    elif put_b > call_b * 1.2:
        dealer_bias = "Bearish"
    else:
        dealer_bias = "Neutral"

    net_gex_m = round(float(gex_data.get("net_gex", 0) or 0) * 1e3, 1)  # stored as billions → to millions

    description = _REGIME_DESCRIPTIONS.get(
        flip_pair,
        f"Regime shifted from {old_regime} to {current_regime}."
    )

    return {
        "changed":     True,
        "old_regime":  old_regime,
        "new_regime":  current_regime,
        "old_label":   _REGIME_LABELS.get(old_regime, old_regime),
        "new_label":   _REGIME_LABELS.get(current_regime, current_regime),
        "description": description,
        "severity":    severity,
        "dealer_bias": dealer_bias,
        "net_gex_m":   net_gex_m,
        "accel_old":   round(old_accel, 1),
        "accel_new":   accel_now,
        "spot":        float(gex_data.get("spot", 0) or 0),
        "call_wall":   float(gex_data.get("call_wall", 0) or 0),
        "put_wall":    float(gex_data.get("put_wall",  0) or 0),
        "regime_call": gex_data.get("regime_call", "NEUTRAL"),
        "ticker":      ticker.upper(),
        "ts":          now,
    }


def get_gex_by_expiry(ticker: str = "SPY") -> Optional[Dict[str, Any]]:
    """Load per-expiry GEX breakdown for smart DTE selection."""
    path = f"logs/gex/gex_term_structure_{ticker.upper()}.json"
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None
