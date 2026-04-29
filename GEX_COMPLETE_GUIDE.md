# CHAKRA GEX Complete Enhancement Guide
## All Phases — Original + George Video Insights
### Target: CHAKRA Neural Trading OS v3

---

## OVERVIEW & EXPECTED IMPACT

| Phase | What it builds | Est. Time | Win Rate Impact |
|-------|---------------|-----------|----------------|
| 1 | GEX State Loader | 30 min | Foundation |
| 2 | GEX Gate (walls/regime) | 45 min | +5-8% |
| 3 | Wire gate into ARKA | 30 min | Activates gate |
| 4 | Intraday timeline logging | 30 min | Dashboard fix |
| 5 | Dashboard range levels + chart | 1 hour | Visual |
| 6 | Smart DTE selection | 30 min | Better contracts |
| **7A** | **Regime Call + Dollar Bias** | **2-3 hours** | **+10-15%** |
| 7B | Acceleration + Expected Move | 2-3 hours | +3-5% |
| 7C | Heat Map + Pins + Per-Ticker GEX | 4-5 hours | +3-5% |

**Total expected win rate improvement: 8-15% by avoiding structural traps**

**DO IN THIS ORDER:** 1 → 2 → 3 → 7A → 7B → 4 → 5 → 6 → 7C

---

## PHASE 1: GEX State Loader

**File:** `backend/arka/gex_state.py` (NEW)

```python
"""
GEX State Loader for ARKA Engine
Loads latest GEX data with TTL enforcement and derived metrics
"""
import json, time, os
from datetime import date
from typing import Optional, Dict, Any

def load_gex_state(ticker: str = "SPY") -> Optional[Dict[str, Any]]:
    """
    Load current GEX state with freshness validation.
    Returns None if file missing or older than 10 minutes.
    """
    path = f"logs/gex/gex_latest_{ticker}.json"

    if not os.path.exists(path):
        print(f"⚠️ GEX state not found: {path}")
        return None

    with open(path) as f:
        data = json.load(f)

    # Enforce 10-minute TTL — stale GEX is dangerous
    age_seconds = time.time() - data.get("ts", 0)
    if age_seconds > 600:
        print(f"⚠️ GEX state stale ({age_seconds:.0f}s old) — skipping")
        return None

    spot       = data["spot"]
    zero_gamma = data["zero_gamma"]
    call_wall  = data["call_wall"]
    put_wall   = data["put_wall"]

    return {
        # Core GEX data
        "regime":        data["regime"],           # POSITIVE_GAMMA / NEGATIVE_GAMMA / LOW_VOL
        "regime_call":   data.get("regime_call", "NEUTRAL"),  # SHORT_THE_POPS / BUY_THE_DIPS / FOLLOW_MOMENTUM
        "zero_gamma":    zero_gamma,
        "call_wall":     call_wall,
        "put_wall":      put_wall,
        "net_gex":       data["net_gex"],
        "spot":          spot,

        # Derived metrics
        "pct_to_call_wall":   (call_wall - spot) / spot * 100 if call_wall > spot else 0,
        "pct_to_put_wall":    (spot - put_wall) / spot * 100 if spot > put_wall else 0,
        "above_zero_gamma":   spot > zero_gamma,

        # Dollar exposure by direction (George insight)
        "call_gex_dollars":   data.get("call_gex_dollars", 0),
        "put_gex_dollars":    data.get("put_gex_dollars", 0),
        "bias_ratio":         data.get("bias_ratio", 1.0),  # >1 = bearish lean
        "dominant_side":      data.get("dominant_side", "NEUTRAL"),

        # Acceleration (George insight)
        "accel_up":           data.get("accel_up", 0),
        "accel_down":         data.get("accel_down", 0),

        # Expected move (George insight)
        "expected_move_pts":  data.get("expected_move_pts", 0),
        "upper_1sd":          data.get("upper_1sd", spot * 1.01),
        "lower_1sd":          data.get("lower_1sd", spot * 0.99),

        # Pin strikes (George insight)
        "pin_strikes":        data.get("pin_strikes", []),

        # Cliff detection
        "cliff_today":        data.get("cliff", {}).get("expires_today", False),
        "cliff_strike":       data.get("cliff", {}).get("strike"),

        # Metadata
        "ts":           data["ts"],
        "age_seconds":  age_seconds,
    }


def get_gex_by_expiry(ticker: str = "SPY") -> Optional[Dict[str, Any]]:
    """Load per-expiry GEX breakdown for DTE selection."""
    path = f"logs/gex/gex_term_structure_{ticker}.json"
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)
```

**Test:**
```bash
cd ~/trading-ai && python3 -c "
from backend.arka.gex_state import load_gex_state
gex = load_gex_state('SPY')
if gex:
    print(f'Regime: {gex[\"regime\"]}')
    print(f'Regime Call: {gex[\"regime_call\"]}')
    print(f'Call Wall: \${gex[\"call_wall\"]} ({gex[\"pct_to_call_wall\"]:.2f}% away)')
    print(f'Bias Ratio: {gex[\"bias_ratio\"]:.2f}x ({gex[\"dominant_side\"]})')
else:
    print('No GEX state yet — will populate during market hours')
"
```

---

## PHASE 2: GEX Gate Decision Logic

**File:** `backend/arka/gex_gate.py` (NEW)

```python
"""
GEX Gate for ARKA Engine
Filters/adjusts conviction scores based on gamma exposure structure
Includes George video insights: regime call, dollar bias, acceleration
"""
from typing import Dict, Any

# ── Configuration ──────────────────────────────────────────────────────────────
CALL_WALL_BUFFER_PCT      = 0.4   # Block calls within 0.4% of call wall
PUT_WALL_BUFFER_PCT       = 0.4   # Block puts within 0.4% of put wall
ZERO_GAMMA_EXPLOSIVE_BAND = 1.5   # $ distance to zero gamma for explosive zone
EXTREME_BIAS_RATIO        = 3.0   # Hard block when put/call ratio > 3x vs direction
ACCELERATION_THRESHOLD    = 15    # Boost conviction when acceleration > this


def gex_gate(signal_direction: str, conviction: float,
             gex: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply GEX-based filters and conviction adjustments.

    Args:
        signal_direction: "CALL" or "PUT"
        conviction: Current conviction score (0-100)
        gex: GEX state dict from load_gex_state()

    Returns:
        {"allow": bool, "conviction": float, "reason": str}
    """
    if not gex:
        return {"allow": True, "conviction": conviction,
                "reason": "GEX unavailable — no adjustment"}

    reason_parts = []
    blocked = False

    # ════════════════════════════════════════════════════════════════════
    # GEORGE INSIGHT: REGIME CALL — master bias filter
    # "Short the Pops" or "Buy the Dips" sets directional preference
    # ════════════════════════════════════════════════════════════════════
    regime_call = gex.get("regime_call", "NEUTRAL")

    if regime_call == "SHORT_THE_POPS" and signal_direction == "CALL":
        conviction -= 20
        reason_parts.append("⚠️ Regime: SHORT_THE_POPS — dealers fade upside, penalizing CALL -20")
    elif regime_call == "BUY_THE_DIPS" and signal_direction == "PUT":
        conviction -= 20
        reason_parts.append("⚠️ Regime: BUY_THE_DIPS — dealers support downside, penalizing PUT -20")
    elif regime_call == "FOLLOW_MOMENTUM":
        conviction += 8
        reason_parts.append("✅ Regime: FOLLOW_MOMENTUM — negative gamma amplifies moves +8")

    # ════════════════════════════════════════════════════════════════════
    # GEORGE INSIGHT: DOLLAR BIAS — hard block on extreme directional lean
    # "$5.5B puts vs $1.3B calls" tells you dealer positioning strength
    # ════════════════════════════════════════════════════════════════════
    bias_ratio    = gex.get("bias_ratio", 1.0)
    call_gex_b    = gex.get("call_gex_dollars", 0) / 1e9
    put_gex_b     = gex.get("put_gex_dollars", 0) / 1e9

    if bias_ratio > EXTREME_BIAS_RATIO and signal_direction == "CALL":
        blocked = True
        reason_parts.append(
            f"❌ BLOCKED: Put exposure ${put_gex_b:.1f}B vs Call ${call_gex_b:.1f}B "
            f"— extreme bearish bias ({bias_ratio:.1f}x)"
        )
    elif bias_ratio < (1 / EXTREME_BIAS_RATIO) and signal_direction == "PUT":
        blocked = True
        reason_parts.append(
            f"❌ BLOCKED: Call exposure ${call_gex_b:.1f}B vs Put ${put_gex_b:.1f}B "
            f"— extreme bullish bias ({1/bias_ratio:.1f}x)"
        )
    elif bias_ratio > 2.0 and signal_direction == "CALL":
        conviction -= 10
        reason_parts.append(f"⚠️ Strong put bias {bias_ratio:.1f}x — penalizing CALL -10")
    elif bias_ratio < 0.5 and signal_direction == "PUT":
        conviction -= 10
        reason_parts.append(f"⚠️ Strong call bias {1/bias_ratio:.1f}x — penalizing PUT -10")

    # ════════════════════════════════════════════════════════════════════
    # ORIGINAL: WALL PROXIMITY — block entries too close to walls
    # ════════════════════════════════════════════════════════════════════
    if signal_direction == "CALL" and not blocked:
        if gex["pct_to_call_wall"] < CALL_WALL_BUFFER_PCT:
            blocked = True
            reason_parts.append(
                f"❌ BLOCKED: Call wall ${gex['call_wall']:.2f} only "
                f"{gex['pct_to_call_wall']:.2f}% away (dealers sell into strength)"
            )
        elif gex["pct_to_call_wall"] < 1.0:
            conviction -= 12
            reason_parts.append(f"⚠️ Approaching call wall — conviction -12")

    if signal_direction == "PUT" and not blocked:
        if gex["pct_to_put_wall"] < PUT_WALL_BUFFER_PCT:
            blocked = True
            reason_parts.append(
                f"❌ BLOCKED: Put wall ${gex['put_wall']:.2f} only "
                f"{gex['pct_to_put_wall']:.2f}% away (dealers buy into weakness)"
            )
        elif gex["pct_to_put_wall"] < 1.0:
            conviction -= 12
            reason_parts.append(f"⚠️ Approaching put wall — conviction -12")

    # ════════════════════════════════════════════════════════════════════
    # ORIGINAL: GAMMA REGIME — penalize momentum chasing in positive gamma
    # ════════════════════════════════════════════════════════════════════
    if gex["regime"] == "POSITIVE_GAMMA":
        if signal_direction == "CALL" and gex["above_zero_gamma"]:
            conviction -= 8
            reason_parts.append("⚠️ Positive gamma + above zero gamma: mean-revert risk -8")
        elif signal_direction == "PUT" and not gex["above_zero_gamma"]:
            conviction -= 8
            reason_parts.append("⚠️ Positive gamma + below zero gamma: mean-revert risk -8")
    elif gex["regime"] == "NEGATIVE_GAMMA":
        if signal_direction == "CALL" and gex["above_zero_gamma"]:
            conviction += 10
            reason_parts.append("✅ Negative gamma + above zero: dealers amplifying upside +10")
        elif signal_direction == "PUT" and not gex["above_zero_gamma"]:
            conviction += 10
            reason_parts.append("✅ Negative gamma + below zero: dealers amplifying downside +10")

    # ════════════════════════════════════════════════════════════════════
    # ORIGINAL: EXPLOSIVE ZONE — near zero gamma flip
    # ════════════════════════════════════════════════════════════════════
    distance_to_zero = abs(gex["spot"] - gex["zero_gamma"])
    if distance_to_zero <= ZERO_GAMMA_EXPLOSIVE_BAND:
        conviction += 8
        reason_parts.append(
            f"⚡ Near zero gamma ${gex['zero_gamma']:.2f} — explosive zone +8"
        )

    # ════════════════════════════════════════════════════════════════════
    # ORIGINAL: CLIFF — GEX expiring today means vol expansion
    # ════════════════════════════════════════════════════════════════════
    if gex["cliff_today"]:
        conviction += 6
        reason_parts.append(
            f"🧨 GEX cliff expiring at ${gex.get('cliff_strike', '?'):.2f} — vol expansion +6"
        )

    # ════════════════════════════════════════════════════════════════════
    # GEORGE INSIGHT: ACCELERATION — fast tape boost
    # High acceleration = price will move faster once trending
    # ════════════════════════════════════════════════════════════════════
    if signal_direction == "CALL" and gex.get("accel_up", 0) > ACCELERATION_THRESHOLD:
        conviction += 10
        reason_parts.append(f"⚡ High upside acceleration ({gex['accel_up']}) — fast tape +10")
    elif signal_direction == "PUT" and gex.get("accel_down", 0) > ACCELERATION_THRESHOLD:
        conviction += 10
        reason_parts.append(f"⚡ High downside acceleration ({gex['accel_down']}) — fast tape +10")

    # Clamp conviction to valid range
    conviction = max(0, min(100, conviction))

    return {
        "allow":      not blocked,
        "conviction": conviction,
        "reason":     " | ".join(reason_parts) if reason_parts else "GEX: No adjustment",
        "regime_call": regime_call,
        "bias_ratio":  bias_ratio,
    }
```

---

## PHASE 3: Wire GEX Gate into ARKA Engine

**File:** `backend/arka/arka_engine.py` (MODIFY)

Add these imports at top of file:
```python
from backend.arka.gex_state import load_gex_state, get_gex_by_expiry
from backend.arka.gex_gate import gex_gate
```

Find the conviction calculation section (after flow/ARJUN/technical scoring).
Add AFTER conviction is calculated, BEFORE order placement:

```python
# ── GEX Gate — block/adjust based on gamma structure ──────────────────────────
is_short      = signal["direction"] in ("SHORT", "STRONG_SHORT")
gex_direction = "PUT" if is_short else "CALL"
gex_state     = load_gex_state(t)
gate_result   = gex_gate(gex_direction, signal["conviction"], gex_state)

if not gate_result["allow"]:
    log.info(f"  🚫 GEX GATE BLOCKED {t} {gex_direction}: {gate_result['reason']}")
    self.state.scan_history.append({
        "time":     datetime.now().strftime("%H:%M"),
        "ticker":   t,
        "score":    signal["conviction"],
        "decision": f"GEX_BLOCK",
        "reason":   gate_result["reason"],
    })
    continue  # Skip this signal entirely

# Update conviction with GEX adjustments
if gate_result["conviction"] != signal["conviction"]:
    log.info(f"  📊 GEX adjustment: {signal['conviction']} → {gate_result['conviction']}: {gate_result['reason']}")
    signal["conviction"] = gate_result["conviction"]

# ── 1SD Strike Filter — never buy far-OTM strikes ──────────────────────────────
if gex_state:
    if gex_direction == "CALL" and gex_state.get("upper_1sd"):
        signal["max_strike"] = gex_state["upper_1sd"]
        log.info(f"  📐 Expected move cap (1SD): ${gex_state['upper_1sd']:.2f}")
    elif gex_direction == "PUT" and gex_state.get("lower_1sd"):
        signal["min_strike"] = gex_state["lower_1sd"]
        log.info(f"  📐 Expected move floor (1SD): ${gex_state['lower_1sd']:.2f}")

# ── Regime call signal in Discord notification ─────────────────────────────────
signal["regime_call"] = gate_result.get("regime_call", "NEUTRAL")
signal["gex_bias_ratio"] = gate_result.get("bias_ratio", 1.0)
```

---

## PHASE 4: Intraday GEX Timeline Logging

**File:** `backend/arjun/agents/gex_calculator.py` (MODIFY — ADD AT END)

```python
import time as _time
import os as _os

def snapshot_gex_intraday(gex_result: dict, ticker: str = "SPY"):
    """
    Log GEX snapshot for intraday timeline tracking.
    Appends to daily log file for dashboard charting.
    """
    from datetime import date as _date
    today = _date.today()
    path  = f"logs/gex/gex_intraday_{ticker}_{today}.json"
    _os.makedirs("logs/gex", exist_ok=True)

    history = []
    if _os.path.exists(path):
        try:
            with open(path) as f:
                history = json.load(f)
        except Exception:
            history = []

    history.append({
        "ts":          _time.time(),
        "datetime":    _time.strftime("%Y-%m-%d %H:%M:%S"),
        "zero_gamma":  gex_result.get("zero_gamma"),
        "call_wall":   gex_result.get("call_wall"),
        "put_wall":    gex_result.get("put_wall"),
        "net_gex":     gex_result.get("net_gex"),
        "regime":      gex_result.get("regime"),
        "regime_call": gex_result.get("regime_call"),
        "spot":        gex_result.get("spot"),
        "bias_ratio":  gex_result.get("bias_ratio", 1.0),
        "accel_up":    gex_result.get("accel_up", 0),
        "accel_down":  gex_result.get("accel_down", 0),
    })

    with open(path, "w") as f:
        json.dump(history, f, indent=2)

    print(f"📊 GEX snapshot logged: {len(history)} points today for {ticker}")


def write_gex_state(gex_result: dict, ticker: str = "SPY"):
    """
    Write current GEX state to logs/gex/gex_latest_{ticker}.json
    This is what load_gex_state() reads. Must be called after every GEX compute.
    """
    import time as _t
    _os.makedirs("logs/gex", exist_ok=True)
    path = f"logs/gex/gex_latest_{ticker}.json"
    gex_result["ts"] = _t.time()
    with open(path, "w") as f:
        json.dump(gex_result, f, indent=2)
    print(f"✅ GEX state written: {path}")
```

**Call both functions after every GEX compute in the codebase:**
```python
# After compute_gex() or wherever GEX result is generated:
write_gex_state(gex_result, ticker)
snapshot_gex_intraday(gex_result, ticker)
```

**API endpoint** — add to `backend/dashboard_api.py`:
```python
@app.get("/api/options/gex/intraday")
async def get_gex_intraday(ticker: str = "SPY"):
    """Return intraday GEX timeline for charting."""
    from datetime import date as _date
    path = f"logs/gex/gex_intraday_{ticker}_{_date.today()}.json"
    if not os.path.exists(path):
        return {"ticker": ticker, "history": []}
    with open(path) as f:
        return {"ticker": ticker, "history": json.load(f)}
```

---

## PHASE 5: Dashboard GEX Tab Enhancements

**File:** `frontend/js/analysis.js` (MODIFY — **careful, this file is sensitive**)

Add range display and intraday chart rendering. Find the GEX tab render section and add:

```javascript
// ── GEX Range Bound Levels + Regime Banner ─────────────────────────────
async function _renderGEXRangeLevels(ticker, data) {
  const el = $('gexRangeLevels');
  if (!el) return;

  const regime      = data.regime || 'UNKNOWN';
  const regimeCall  = data.regime_call || 'NEUTRAL';
  const callWall    = data.call_wall || 0;
  const putWall     = data.put_wall  || 0;
  const zeroGamma   = data.zero_gamma || 0;
  const spot        = data.spot || 0;
  const netGex      = (data.net_gex || 0) / 1e9;
  const callGexB    = (data.call_gex_dollars || 0) / 1e9;
  const putGexB     = (data.put_gex_dollars  || 0) / 1e9;
  const biasRatio   = data.bias_ratio || 1;
  const above       = spot > zeroGamma;
  const em          = data.expected_move_pts || 0;

  // Regime call banner colors
  const regimeColors = {
    'SHORT_THE_POPS':  { bg: 'rgba(255,61,90,0.1)',  border: 'var(--red)',   text: 'var(--red)',  label: '📉 SHORT THE POPS — Dealers fade upside. Favor PUTS on rallies.' },
    'BUY_THE_DIPS':    { bg: 'rgba(0,208,132,0.1)',  border: 'var(--green)', text: 'var(--green)', label: '📈 BUY THE DIPS — Dealers support downside. Favor CALLS on dips.' },
    'FOLLOW_MOMENTUM': { bg: 'rgba(255,179,71,0.12)', border: 'var(--gold)', text: 'var(--gold)',  label: '⚡ FOLLOW MOMENTUM — Negative gamma amplifies all moves.' },
    'NEUTRAL':         { bg: 'var(--bg3)',            border: 'var(--border)', text: 'var(--sub)', label: '😐 NEUTRAL — No strong directional bias.' },
  };
  const rc = regimeColors[regimeCall] || regimeColors['NEUTRAL'];

  el.innerHTML = `
    <!-- REGIME CALL BANNER -->
    <div style="padding:12px 16px;border-radius:7px;margin-bottom:10px;
      background:${rc.bg};border:1px solid ${rc.border}">
      <div style="font-size:10px;letter-spacing:1.5px;color:${rc.text};font-weight:800;margin-bottom:4px">
        TODAY'S GEX REGIME
      </div>
      <div style="font-size:14px;font-weight:800;color:${rc.text}">${regimeCall.replace(/_/g,' ')}</div>
      <div style="font-size:10px;color:var(--sub);margin-top:3px">${rc.label}</div>
    </div>

    <!-- DOLLAR BIAS ROW -->
    <div style="background:var(--bg3);border-radius:6px;padding:10px 12px;margin-bottom:8px">
      <div style="font-size:8px;letter-spacing:1px;color:var(--sub);margin-bottom:8px;text-transform:uppercase">
        Dollar Exposure by Direction
      </div>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:5px">
        <span style="font-size:9px;color:var(--green);width:50px">Calls</span>
        <div style="flex:1;height:8px;background:var(--bg2);border-radius:4px;overflow:hidden">
          <div style="width:${Math.min(100, callGexB/(callGexB+putGexB)*100)}%;height:100%;background:var(--green);border-radius:4px"></div>
        </div>
        <span style="font-size:9px;font-family:'JetBrains Mono',monospace;color:var(--green);width:50px;text-align:right">$${callGexB.toFixed(1)}B</span>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="font-size:9px;color:var(--red);width:50px">Puts</span>
        <div style="flex:1;height:8px;background:var(--bg2);border-radius:4px;overflow:hidden">
          <div style="width:${Math.min(100, putGexB/(callGexB+putGexB)*100)}%;height:100%;background:var(--red);border-radius:4px"></div>
        </div>
        <span style="font-size:9px;font-family:'JetBrains Mono',monospace;color:var(--red);width:50px;text-align:right">$${putGexB.toFixed(1)}B</span>
      </div>
      <div style="font-size:8px;color:var(--sub);margin-top:6px">
        Bias ratio: ${biasRatio.toFixed(1)}x ${biasRatio > 1 ? '(bearish lean)' : '(bullish lean)'}
        ${biasRatio > 3 ? ' ⚠️ EXTREME — ARKA hard block active' : ''}
      </div>
    </div>

    <!-- THREE KEY LEVELS -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:8px">
      <div style="background:var(--bg3);border-radius:6px;padding:10px;border-top:2px solid var(--green)">
        <div style="font-size:7px;color:var(--sub);letter-spacing:.8px;margin-bottom:4px">📈 CALL WALL</div>
        <div style="font-size:15px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--green)">$${callWall.toFixed(2)}</div>
        <div style="font-size:8px;color:var(--sub);margin-top:2px">
          ${((callWall-spot)/spot*100).toFixed(2)}% away
          ${((callWall-spot)/spot*100) < 0.5 ? ' ⚠️ NEAR' : ''}
        </div>
      </div>
      <div style="background:var(--bg3);border-radius:6px;padding:10px;border-top:2px solid var(--gold)">
        <div style="font-size:7px;color:var(--sub);letter-spacing:.8px;margin-bottom:4px">⚡ ZERO GAMMA</div>
        <div style="font-size:15px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--gold)">$${zeroGamma.toFixed(2)}</div>
        <div style="font-size:8px;color:${above?'var(--green)':'var(--red)'};margin-top:2px;font-weight:700">
          ${above ? '▲ ABOVE' : '▼ BELOW'} FLIP POINT
        </div>
      </div>
      <div style="background:var(--bg3);border-radius:6px;padding:10px;border-top:2px solid var(--red)">
        <div style="font-size:7px;color:var(--sub);letter-spacing:.8px;margin-bottom:4px">📉 PUT WALL</div>
        <div style="font-size:15px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--red)">$${putWall.toFixed(2)}</div>
        <div style="font-size:8px;color:var(--sub);margin-top:2px">
          ${((spot-putWall)/spot*100).toFixed(2)}% away
          ${((spot-putWall)/spot*100) < 0.5 ? ' ⚠️ NEAR' : ''}
        </div>
      </div>
    </div>

    <!-- EXPECTED MOVE -->
    ${em > 0 ? `<div style="background:var(--bg3);border-radius:6px;padding:8px 12px;margin-bottom:8px;
      display:flex;justify-content:space-between;align-items:center">
      <div>
        <div style="font-size:7px;color:var(--sub);letter-spacing:.8px">EXPECTED MOVE (1SD 0DTE)</div>
        <div style="font-size:12px;font-weight:700;font-family:'JetBrains Mono',monospace;margin-top:3px">
          ±$${em.toFixed(2)} &nbsp;
          <span style="font-size:9px;color:var(--sub)">Range: $${(data.lower_1sd||0).toFixed(2)} – $${(data.upper_1sd||0).toFixed(2)}</span>
        </div>
      </div>
      <span style="font-size:9px;padding:3px 8px;border-radius:4px;
        background:${spot >= (data.lower_1sd||0) && spot <= (data.upper_1sd||0) ? 'var(--green)18' : 'var(--red)18'};
        color:${spot >= (data.lower_1sd||0) && spot <= (data.upper_1sd||0) ? 'var(--green)' : 'var(--red)'}">
        ${spot >= (data.lower_1sd||0) && spot <= (data.upper_1sd||0) ? 'Within range ✓' : 'Outside range ⚠️'}
      </span>
    </div>` : ''}

    <!-- CLIFF ALERT -->
    ${data.cliff && data.cliff.expires_today ? `<div style="padding:10px 14px;border-radius:6px;margin-bottom:8px;
      background:rgba(255,179,71,0.12);border:1px solid var(--gold)">
      🧨 <strong style="color:var(--gold)">GEX Cliff Expiring Today</strong>
      at $${data.cliff.strike} — expect volatility expansion
    </div>` : ''}
  `;
}
```

---

## PHASE 6: Smart DTE Selection

**File:** `backend/arka/arka_engine.py` (MODIFY — add function)

```python
def select_optimal_dte(ticker: str, signal_direction: str, log_fn=None) -> int:
    """
    Select DTE based on per-expiry GEX structure.
    - 0DTE negative gamma → prefer 0DTE (fast mover, dealers amplify)
    - 0DTE positive gamma → prefer 1DTE (avoid pinning risk)
    - No data → default 0DTE
    """
    from backend.arka.gex_state import get_gex_by_expiry
    gex_by_expiry = get_gex_by_expiry(ticker)

    if not gex_by_expiry:
        return 0  # Default to 0DTE

    today_expiry = sorted(gex_by_expiry.keys())[0] if gex_by_expiry else None
    if today_expiry:
        today_gex = gex_by_expiry[today_expiry]
        if today_gex.get("net_gex", 0) < 0:
            if log_fn: log_fn(f"✅ 0DTE NEGATIVE GAMMA — selecting 0DTE for fast tape")
            return 0
        else:
            if log_fn: log_fn(f"⚠️ 0DTE POSITIVE GAMMA — selecting 1DTE to avoid pin risk")
            return 1
    return 0
```

---

## PHASE 7A: George Insights — Regime Call + Dollar Bias (HIGHEST ROI)

**File:** `backend/arjun/agents/gex_calculator.py` (MODIFY)

Add these functions to the existing gex_calculator.py:

```python
import math

def get_regime_call(net_gex: float, above_zero_gamma: bool,
                    bias_ratio: float = 1.0) -> str:
    """
    Generate top-level directional trading bias — George's "Short the Pops"
    
    Logic:
    - Positive gamma above zero = dealers fade upside → SHORT_THE_POPS
    - Positive gamma below zero = dealers support downside → BUY_THE_DIPS
    - Negative gamma = dealers amplify moves → FOLLOW_MOMENTUM
    """
    if net_gex > 0 and above_zero_gamma:
        return "SHORT_THE_POPS"
    elif net_gex > 0 and not above_zero_gamma:
        return "BUY_THE_DIPS"
    elif net_gex < 0:
        return "FOLLOW_MOMENTUM"
    return "NEUTRAL"


def compute_directional_exposure(options_chain: list) -> dict:
    """
    Calculate gross gamma exposure by direction in dollars.
    George shows "$5.5B puts vs $1.3B calls" — the ratio matters more than net GEX.
    """
    call_gex_total = 0.0
    put_gex_total  = 0.0

    for contract in options_chain:
        gex_val = abs(contract.get("gamma_exposure", 0))
        if contract.get("type") == "call" or contract.get("contract_type") == "call":
            call_gex_total += gex_val
        elif contract.get("type") == "put" or contract.get("contract_type") == "put":
            put_gex_total += gex_val

    bias_ratio = put_gex_total / call_gex_total if call_gex_total > 0.01 else 99.0

    if bias_ratio > 1.5:
        dominant = "PUT"
    elif bias_ratio < 0.67:
        dominant = "CALL"
    else:
        dominant = "NEUTRAL"

    return {
        "call_gex_dollars": call_gex_total,
        "put_gex_dollars":  put_gex_total,
        "bias_ratio":       round(bias_ratio, 2),
        "dominant_side":    dominant,
    }


def compute_acceleration(gex_by_strike: dict, spot: float,
                          direction: str) -> float:
    """
    Measure gamma concentration gradient near spot price.
    Higher score = faster expected price movement in that direction.
    George shows "+21 acceleration upside" or "+7 downside".
    """
    nearby = {
        k: v for k, v in gex_by_strike.items()
        if abs(float(k) - spot) <= 10
    }
    if direction == "UP":
        relevant = {k: v for k, v in nearby.items() if float(k) > spot}
    else:
        relevant = {k: v for k, v in nearby.items() if float(k) < spot}

    if not relevant:
        return 0.0

    total_gamma  = sum(abs(v) for v in relevant.values())
    acceleration = (total_gamma / spot) * 1000  # Normalize and scale
    return round(acceleration, 1)


def compute_expected_move(spot: float, iv: float, dte: int = 1) -> dict:
    """
    Calculate IV-implied expected move (1 standard deviation).
    Formula: EM = Spot × IV × sqrt(DTE / 252)
    
    ARKA should never buy strikes outside this range for 0DTE.
    Probability of reaching outside 1SD = <16%.
    """
    daily_move = spot * iv * math.sqrt(dte / 252)
    return {
        "expected_move_pts": round(daily_move, 2),
        "upper_1sd":         round(spot + daily_move, 2),
        "lower_1sd":         round(spot - daily_move, 2),
    }


def find_pin_strikes(options_chain: list, min_combined_oi: int = 15000) -> list:
    """
    Identify pin levels where price tends to oscillate.
    George: "You sweep that strike once down, once up — that's where you look for reversal."
    
    Pin = strike with elevated OI on BOTH call and put sides.
    Different from walls: walls = one-time bounce, pins = oscillation zones.
    """
    strikes = {}
    for contract in options_chain:
        strike = contract.get("strike_price") or contract.get("strike")
        if not strike:
            continue
        strike = float(strike)
        if strike not in strikes:
            strikes[strike] = {"call_oi": 0, "put_oi": 0}
        oi = contract.get("open_interest", 0) or 0
        if contract.get("type") == "call" or contract.get("contract_type") == "call":
            strikes[strike]["call_oi"] += oi
        else:
            strikes[strike]["put_oi"] += oi

    pins = []
    for strike, data in strikes.items():
        call_oi  = data["call_oi"]
        put_oi   = data["put_oi"]
        combined = call_oi + put_oi
        if call_oi > 5000 and put_oi > 5000 and combined >= min_combined_oi:
            pins.append({
                "strike":   strike,
                "call_oi":  call_oi,
                "put_oi":   put_oi,
                "strength": combined,
            })

    return sorted(pins, key=lambda x: -x["strength"])[:5]  # Top 5 pins
```

**Wire all new fields into the main GEX output:**
```python
# In your main compute_gex() function, after computing core GEX:

# Add George insights to output
directional = compute_directional_exposure(options_chain)
gex_output.update(directional)

gex_output["regime_call"] = get_regime_call(
    gex_output["net_gex"],
    gex_output["spot"] > gex_output["zero_gamma"],
    directional["bias_ratio"]
)

gex_output["accel_up"]   = compute_acceleration(gex_by_strike, spot, "UP")
gex_output["accel_down"] = compute_acceleration(gex_by_strike, spot, "DOWN")

# Expected move requires IV — get from ATM option
atm_iv = get_atm_iv(options_chain, spot)  # Your existing IV function
if atm_iv:
    em = compute_expected_move(spot, atm_iv, dte=1)
    gex_output.update(em)

gex_output["pin_strikes"] = find_pin_strikes(options_chain)

# Write state and snapshot
write_gex_state(gex_output, ticker)
snapshot_gex_intraday(gex_output, ticker)
```

---

## PHASE 7B: Per-Ticker GEX for Swings

**File:** `backend/arka/arka_swings.py` (MODIFY)

```python
# Top 10 tickers to compute GEX for swings universe
SWING_TICKERS_WITH_GEX = [
    "NVDA", "TSLA", "AMZN", "AAPL", "MSFT",
    "META", "GOOGL", "AMD", "COIN", "NFLX"
]

def apply_gex_to_swing_score(ticker: str, direction: str, score: int) -> tuple:
    """
    Adjust swing score based on ticker-specific GEX structure.
    Prevents buying calls near call walls, or puts near put walls.
    Returns (adjusted_score, gex_reason)
    """
    from backend.arka.gex_state import load_gex_state
    gex = load_gex_state(ticker)

    if not gex:
        return score, "GEX: no data"

    reasons = []
    regime_call = gex.get("regime_call", "NEUTRAL")

    # Penalize entries against regime
    if regime_call == "SHORT_THE_POPS" and direction == "LONG":
        score -= 15
        reasons.append(f"GEX SHORT_THE_POPS — penalizing CALL setup")
    elif regime_call == "BUY_THE_DIPS" and direction == "SHORT":
        score -= 15
        reasons.append(f"GEX BUY_THE_DIPS — penalizing PUT setup")

    # Penalize entries near walls
    if direction == "LONG" and gex["pct_to_call_wall"] < 2.0:
        score -= 10
        reasons.append(f"Approaching call wall ${gex['call_wall']:.2f}")
    elif direction == "SHORT" and gex["pct_to_put_wall"] < 2.0:
        score -= 10
        reasons.append(f"Approaching put wall ${gex['put_wall']:.2f}")

    # Boost entries aligned with momentum in negative gamma
    if regime_call == "FOLLOW_MOMENTUM":
        score += 8
        reasons.append("GEX FOLLOW_MOMENTUM — negative gamma amplifies +8")

    return max(0, score), " | ".join(reasons) if reasons else "GEX: aligned"
```

Add call to swing scorer (in `score_ticker` function, after main scoring):
```python
# Apply per-ticker GEX if available
if ticker in SWING_TICKERS_WITH_GEX:
    score, gex_reason = apply_gex_to_swing_score(ticker, direction, score)
    if gex_reason and "no data" not in gex_reason:
        reasons.append(gex_reason)
```

---

## PHASE 7C: GEX Heat Map API

**File:** `backend/dashboard_api.py` (ADD endpoint)

```python
@app.get("/api/options/gex/heatmap")
async def get_gex_heatmap(ticker: str = "SPY"):
    """Return GEX distribution by strike for heat map chart."""
    import httpx as _hx, os as _os
    from datetime import date as _date, timedelta as _td

    key    = _os.getenv("POLYGON_API_KEY", "")
    today  = _date.today()
    exp_end = (today + _td(days=2)).isoformat()

    try:
        r = _hx.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker}",
            params={"apiKey": key, "limit": 250,
                    "expiration_date.gte": today.isoformat(),
                    "expiration_date.lte": exp_end},
            timeout=10
        )
        contracts = r.json().get("results", [])

        # Get spot price
        sp = _hx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": key}, timeout=5
        )
        spot = float(sp.json().get("ticker", {}).get("day", {}).get("c", 0) or 0)

        # Build strike-level GEX
        by_strike = {}
        for c in contracts:
            det    = c.get("details", {})
            greeks = c.get("greeks", {})
            strike = float(det.get("strike_price", 0))
            gamma  = float(greeks.get("gamma", 0) or 0)
            oi     = float(c.get("open_interest", 0) or 0)
            ctype  = det.get("contract_type", "")
            gex_val = gamma * oi * spot * 100

            if strike not in by_strike:
                by_strike[strike] = {"call_gex": 0, "put_gex": 0}
            if ctype == "call":
                by_strike[strike]["call_gex"] += gex_val
            elif ctype == "put":
                by_strike[strike]["put_gex"] -= gex_val  # flip puts negative

        strikes  = sorted(by_strike.keys())
        call_gex = [by_strike[s]["call_gex"] / 1e6 for s in strikes]  # in $M
        put_gex  = [by_strike[s]["put_gex"]  / 1e6 for s in strikes]

        return {
            "ticker":   ticker,
            "spot":     spot,
            "strikes":  strikes,
            "call_gex": call_gex,
            "put_gex":  put_gex,
        }
    except Exception as e:
        return {"error": str(e), "ticker": ticker, "strikes": [], "call_gex": [], "put_gex": []}
```

---

## ACCEPTANCE CRITERIA — FULL CHECKLIST

### Phase 1-3 (GEX Gate Core)
- [ ] `backend/arka/gex_state.py` created
- [ ] `backend/arka/gex_gate.py` created
- [ ] `load_gex_state()` returns None for stale/missing files
- [ ] GEX gate runs before every ARKA order
- [ ] Blocked signals appear in scan feed with reason
- [ ] Conviction adjustments logged in arka_engine.log

### Phase 7A (George — Regime + Dollar Bias)
- [ ] `regime_call` field in `gex_latest_SPY.json`
- [ ] SHORT_THE_POPS penalizes CALL entries by -20
- [ ] BUY_THE_DIPS penalizes PUT entries by -20
- [ ] bias_ratio > 3.0 triggers hard block
- [ ] Dashboard GEX tab shows regime banner prominently
- [ ] Dollar exposure bars visible in dashboard

### Phase 7B (George — Acceleration + Expected Move)
- [ ] `accel_up` and `accel_down` in GEX output
- [ ] `expected_move_pts`, `upper_1sd`, `lower_1sd` in GEX output
- [ ] ARKA filters 0DTE strikes to within 1SD range
- [ ] Conviction +10 when acceleration > 15 aligns with direction

### Phase 4-5 (Dashboard)
- [ ] Intraday timeline logging writes `logs/gex/gex_intraday_SPY_*.json`
- [ ] `/api/options/gex/intraday` returns history array
- [ ] Range levels display (call wall, zero gamma, put wall) on GEX tab
- [ ] Cliff alert banner shows when applicable

### Phase 7C (Heat Map + Pins + Per-Ticker)
- [ ] `/api/options/gex/heatmap` endpoint returns strike data
- [ ] Heat map renders on GEX tab (Plotly bar chart)
- [ ] Pin strikes detected for top 5 highest combined OI
- [ ] Per-ticker GEX applied to SWING_TICKERS_WITH_GEX list

---

## CRON SCHEDULE ADDITIONS

```bash
# Add to crontab -e:

# GEX for Swings universe — every 30 min during market hours
*/30 9-16 * * 1-5 cd ~/trading-ai && venv/bin/python3 -c "
from backend.arjun.agents.gex_calculator import compute_and_write_gex
for t in ['NVDA','TSLA','AMZN','AAPL','MSFT','META','GOOGL','AMD','COIN','NFLX']:
    compute_and_write_gex(t)
" >> logs/gex/swings_gex.log 2>&1

# Ensure logs/gex exists
@reboot mkdir -p /Users/midhunkrothapalli/trading-ai/logs/gex
```

---

## IMPLEMENTATION SEQUENCE FOR CLAUDE CODE

Tell Claude Code:

**Monday morning (pre-market):**
```
Read GEX_COMPLETE_GUIDE.md. Implement Phase 1 (gex_state.py) and 
Phase 2 (gex_gate.py) as new files. Then Phase 3: wire the gate 
into arka_engine.py after conviction calculation, before order placement.
Run syntax checks after each file.
```

**After Phase 1-3 working:**
```
Now implement Phase 7A from GEX_COMPLETE_GUIDE.md: add get_regime_call(),
compute_directional_exposure(), and write_gex_state() to 
gex_calculator.py. Wire regime_call and bias_ratio into the GEX gate.
Add the regime banner to the dashboard GEX tab.
```

**During lunch:**
```
Implement Phase 7B: add compute_acceleration() and compute_expected_move()
to gex_calculator.py. Wire the 1SD strike filter into ARKA's contract 
selection. Wire acceleration boost into conviction pipeline.
```

**Afternoon:**
```
Implement Phase 4 (intraday logging) and Phase 5 (dashboard range levels).
Be very careful with analysis.js — add new functions, do not modify 
existing rendering code. Add the gexRangeLevels div to dashboard.html
if it doesn't exist.
```

---

*CHAKRA GEX Complete Enhancement Guide v2.0*
*Includes: Original 6 phases + George video insights (7A/7B/7C)*
*Last updated: March 28, 2026*
