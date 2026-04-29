"""
CHAKRA — Hidden Markov Model Regime Detector
backend/chakra/modules/hmm_regime.py

Classifies the market into 4 hidden states using observable features.
Each state triggers different ARKA thresholds and position sizing.

States:
  0 — LOW_VOL_TREND  : VIX low, steady grind → full position, trend-follow
  1 — HIGH_VOL_TREND : VIX elevated, strong direction → full position, wider stops
  2 — CHOPPY_RANGE   : No direction, oscillating → half position, +15 threshold
  3 — CRISIS         : VIX spike, panic → 25% position, +20 threshold

Uses hmmlearn if installed, falls back to rule-based classifier
(same 4 states, deterministic rules) so CHAKRA never breaks.

Integration:
  - Risk Manager     → position size multiplier (0.25x–1.0x)
  - ARKA threshold   → raised in CHOPPY (+15) and CRISIS (+20) states
  - Weekly retrain   → HMM state as feature in XGBoost
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

log         = logging.getLogger("chakra.hmm")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
HMM_CACHE   = BASE / "logs" / "chakra" / "hmm_latest.json"
HMM_MODEL   = BASE / "logs" / "chakra" / "hmm_model.pkl"

# State definitions — same whether using HMM or rule-based
STATE_LABELS = {
    0: {"name": "LOW_VOL_TREND",  "size_mult": 1.0,  "threshold_adj": 0,
        "label": "🟢 Low Vol Trend",  "color": "00FF9D",
        "description": "VIX calm, steady grind — full position, trend-follow"},
    1: {"name": "HIGH_VOL_TREND", "size_mult": 1.0,  "threshold_adj": +5,
        "label": "🔵 High Vol Trend", "color": "00D4FF",
        "description": "VIX elevated but directional — full size, wider stops"},
    2: {"name": "CHOPPY_RANGE",   "size_mult": 0.5,  "threshold_adj": +8,
        "label": "🟡 Choppy Range",   "color": "FFB347",
        "description": "No clear direction, oscillating — half size, fade extremes"},
    3: {"name": "CRISIS",         "size_mult": 0.25, "threshold_adj": +20,
        "label": "🔴 Crisis Mode",    "color": "FF2D55",
        "description": "VIX spike, panic selling — minimal size, hedges only"},
}


# ══════════════════════════════════════════════════════════════════════
# FEATURE EXTRACTION
# ══════════════════════════════════════════════════════════════════════

def extract_features(bars: list, vix_history: list) -> np.ndarray:
    """
    Extract 4 features for HMM from daily bar history.
    Features: [daily_return, vix_change, volume_ratio, spy_qqq_spread]

    bars: list of {o, h, l, c, v} dicts (SPY daily)
    vix_history: list of VIX closes
    Returns: (n_samples, 4) numpy array
    """
    if len(bars) < 5:
        return np.array([]).reshape(0, 4)

    closes  = np.array([float(b.get("c", 0)) for b in bars])
    volumes = np.array([float(b.get("v", 0)) for b in bars])

    # Feature 1: daily returns
    returns = np.diff(closes) / closes[:-1]

    # Feature 2: VIX changes (or zeros if unavailable)
    if len(vix_history) >= len(returns):
        vix_arr    = np.array(vix_history[-len(returns)-1:], dtype=float)
        vix_change = np.diff(vix_arr) / (vix_arr[:-1] + 1e-8)
        vix_change = vix_change[-len(returns):]
    else:
        vix_change = np.zeros(len(returns))

    # Feature 3: volume ratio vs 5-day avg
    vol_ratio = np.ones(len(returns))
    for i in range(5, len(volumes)):
        avg = np.mean(volumes[i-5:i])
        if avg > 0:
            vol_ratio[i-1] = volumes[i] / avg

    vol_ratio = vol_ratio[-len(returns):]

    # Feature 4: momentum (5-day return as regime indicator)
    momentum = np.zeros(len(returns))
    for i in range(5, len(closes)):
        momentum[i-1] = (closes[i] - closes[i-5]) / closes[i-5]
    momentum = momentum[-len(returns):]

    n = min(len(returns), len(vix_change), len(vol_ratio), len(momentum))
    features = np.column_stack([
        returns[-n:],
        vix_change[-n:],
        vol_ratio[-n:],
        momentum[-n:],
    ])

    # Clip to reasonable ranges
    features = np.clip(features, -0.5, 0.5)
    return features


# ══════════════════════════════════════════════════════════════════════
# HMM MODEL — with graceful fallback
# ══════════════════════════════════════════════════════════════════════

def build_hmm_model(features: np.ndarray):
    """Build and fit HMM model. Returns model or None if unavailable."""
    try:
        from hmmlearn import hmm
        model = hmm.GaussianHMM(
            n_components=4,
            covariance_type="full",
            n_iter=200,
            random_state=42,
            tol=0.01,
        )
        model.fit(features)
        return model
    except ImportError:
        return None
    except Exception as e:
        log.warning(f"HMM fit failed: {e}")
        return None


def predict_state_hmm(model, features: np.ndarray) -> int:
    """Predict current state using fitted HMM."""
    try:
        states = model.predict(features)
        return int(states[-1])
    except Exception:
        return 2  # default to CHOPPY if prediction fails


# ══════════════════════════════════════════════════════════════════════
# RULE-BASED FALLBACK — same 4 states, no ML required
# ══════════════════════════════════════════════════════════════════════

def classify_state_rules(latest_return: float, vix: float,
                          vol_ratio: float, momentum_5d: float) -> int:
    """
    Rule-based state classifier — same 4 states as HMM.
    Used when hmmlearn not installed or model fails.

    Returns state index 0-3.
    """
    abs_return  = abs(latest_return)
    abs_momentum = abs(momentum_5d)

    # Crisis: VIX > 30 or extreme daily move > 3%
    if vix > 30 or abs_return > 0.03:
        return 3  # CRISIS

    # Choppy: VIX 20-30, small moves, no momentum
    if vix > 20 and abs_momentum < 0.02:
        return 2  # CHOPPY_RANGE

    # High Vol Trend: VIX 18-25 but clear direction
    if vix > 18 and abs_momentum > 0.02:
        return 1  # HIGH_VOL_TREND

    # Low Vol Trend: VIX < 18, directional
    return 0  # LOW_VOL_TREND


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════════════════

def fetch_spy_bars(days: int = 70) -> list:
    """Fetch SPY daily bars."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=days + 10)).isoformat()
        r = httpx.get(
            "https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/{}/{}".format(start, end),
            params={"apiKey": POLYGON_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 100},
            timeout=12
        )
        return r.json().get("results", [])
    except Exception as e:
        log.warning(f"HMM: SPY fetch failed: {e}")
        return []


def fetch_vix_history(days: int = 70) -> list:
    """Fetch VIX proxy (VIXY ETF) closes."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=days + 10)).isoformat()
        r = httpx.get(
            "https://api.polygon.io/v2/aggs/ticker/VIXY/range/1/day/{}/{}".format(start, end),
            params={"apiKey": POLYGON_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 100},
            timeout=12
        )
        bars = r.json().get("results", [])
        return [float(b["c"]) for b in bars if b.get("c")]
    except Exception as e:
        log.warning(f"HMM: VIX fetch failed: {e}")
        return []


def get_current_vix() -> float:
    """Get latest VIX from internals file."""
    internals_path = BASE / "logs" / "internals" / "internals_latest.json"
    try:
        if internals_path.exists():
            with open(internals_path) as f:
                data = json.load(f)
            risk = data.get("risk", {})
            desc = risk.get("description", "")
            import re
            m = re.search(r"VIX\s+([\d.]+)", desc)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return 20.0


# ══════════════════════════════════════════════════════════════════════
# COMPUTE + CACHE
# ══════════════════════════════════════════════════════════════════════

def compute_and_cache_hmm() -> dict:
    """
    Fit HMM (or use rules) and classify current market state.
    Run at 8:30 AM daily and after Sunday retrain.
    """
    log.info("HMM: Fetching market data...")
    spy_bars    = fetch_spy_bars(days=70)
    vix_history = fetch_vix_history(days=70)
    current_vix = get_current_vix()

    if len(spy_bars) < 20:
        log.warning("HMM: insufficient data — defaulting to CHOPPY")
        state   = 2
        method  = "default"
        features = None
    else:
        features = extract_features(spy_bars, vix_history)

        # Try HMM first
        model  = None
        method = "rules"

        if len(features) >= 30:
            model = build_hmm_model(features)

        if model is not None:
            state  = predict_state_hmm(model, features)
            method = "hmm"
            # Save model for reuse
            try:
                import pickle
                HMM_MODEL.parent.mkdir(parents=True, exist_ok=True)
                with open(HMM_MODEL, "wb") as f:
                    pickle.dump(model, f)
            except Exception:
                pass
        else:
            # Rule-based fallback
            if len(features) > 0:
                latest     = features[-1]
                latest_ret = float(latest[0])
                vol_ratio  = float(latest[2])
                momentum   = float(latest[3])
            else:
                latest_ret = 0.0
                vol_ratio  = 1.0
                momentum   = 0.0
            state  = classify_state_rules(latest_ret, current_vix, vol_ratio, momentum)
            method = "rules"

    state_info = STATE_LABELS[state].copy()
    state_info.update({
        "state":     state,
        "method":    method,
        "vix":       current_vix,
        "date":      date.today().isoformat(),
        "computed":  datetime.now().strftime("%H:%M ET"),
        "bars_used": len(spy_bars),
    })

    log.info(f"  HMM state={state} [{state_info['name']}] via {method} "
             f"VIX={current_vix:.1f} size={state_info['size_mult']}x "
             f"threshold+{state_info['threshold_adj']}")

    HMM_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(HMM_CACHE, "w") as f:
        json.dump(state_info, f, indent=2)

    return state_info


def load_hmm_cache() -> dict:
    """Load cached HMM state. Recomputes if stale."""
    try:
        if HMM_CACHE.exists():
            with open(HMM_CACHE) as f:
                data = json.load(f)
            if data.get("date") == date.today().isoformat():
                return data
    except Exception:
        pass
    return compute_and_cache_hmm()


# ══════════════════════════════════════════════════════════════════════
# INTEGRATION HELPERS
# ══════════════════════════════════════════════════════════════════════

def get_hmm_arka_params() -> dict:
    """ARKA integration — returns size_mult and threshold_adj."""
    state = load_hmm_cache()
    return {
        "size_mult":     state.get("size_mult", 1.0),
        "threshold_adj": state.get("threshold_adj", 0),
        "state":         state.get("name", "UNKNOWN"),
        "label":         state.get("label", ""),
    }


def get_hmm_risk_mult() -> float:
    """Risk Manager integration — position size multiplier."""
    return load_hmm_cache().get("size_mult", 1.0)


def get_hmm_feature_for_retrain() -> dict:
    """Weekly retrain integration — HMM state as XGBoost feature."""
    state = load_hmm_cache()
    return {
        "hmm_state":       state.get("state", 2),
        "hmm_state_name":  state.get("name", "CHOPPY_RANGE"),
        "hmm_size_mult":   state.get("size_mult", 1.0),
        "hmm_threshold":   state.get("threshold_adj", 0),
    }


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    # Check if hmmlearn is available
    try:
        import hmmlearn
        print(f"  hmmlearn {hmmlearn.__version__} available — using ML model")
    except ImportError:
        print("  hmmlearn not installed — using rule-based classifier")
        print("  Install with: pip install hmmlearn --break-system-packages")

    result = compute_and_cache_hmm()
    print(f"\n── HMM Regime ────────────────────────────────────────")
    print(f"  State:      {result['state']} — {result['name']}")
    print(f"  Label:      {result['label']}")
    print(f"  Method:     {result['method']}")
    print(f"  VIX:        {result['vix']:.1f}")
    print(f"  Size mult:  {result['size_mult']}x")
    print(f"  Thr adj:    {result['threshold_adj']:+d}")
    print(f"  Desc:       {result['description']}")
