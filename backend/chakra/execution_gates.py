"""
CHAKRA Execution Gates — 5-gate pass/fail system
All gates GREEN = full position sizing
Any RED gate = reduced size or blocked
"""
import os, json, sqlite3
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)
ET = ZoneInfo("America/New_York")


def _get_gex_regime() -> str:
    import glob
    files = sorted(glob.glob(str(BASE / "logs/arka/gex_heatmap_*.json")), reverse=True)
    if not files:
        files = sorted(glob.glob(str(BASE / "logs/arka/gex-heatmap-*.json")), reverse=True)
    if files:
        try:
            return json.loads(Path(files[0]).read_text()).get("regime", "UNKNOWN")
        except Exception:
            pass
    return "UNKNOWN"


def _get_neural_pulse() -> int:
    files = [
        BASE / "logs/internals/internals_latest.json",
        BASE / f"logs/internals/internals_{date.today()}.json",
    ]
    for f in files:
        if f.exists():
            try:
                data = json.loads(f.read_text())
                return data.get("neural_pulse", {}).get("score", 50)
            except Exception:
                pass
    return 50  # neutral fallback


def _get_macro_events() -> list:
    try:
        from backend.arjun.modules.macro_calendar import fetch_upcoming_events
        return fetch_upcoming_events(hours_ahead=4, hours_before=2)
    except Exception:
        return []


def _get_vix() -> float:
    files = [
        BASE / "logs/internals/internals_latest.json",
        BASE / f"logs/internals/internals_{date.today()}.json",
    ]
    for f in files:
        if f.exists():
            try:
                return json.loads(f.read_text()).get("vix", {}).get("close", 20.0)
            except Exception:
                pass
    return 20.0


def _get_loss_streak() -> int:
    """Count consecutive losses from performance DB."""
    db = BASE / "logs/arjun_performance.db"
    if not db.exists():
        return 0
    try:
        conn   = sqlite3.connect(str(db))
        rows   = conn.execute(
            "SELECT outcome FROM signals WHERE outcome IS NOT NULL ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        conn.close()
        streak = 0
        for (outcome,) in rows:
            if outcome == "LOSS":
                streak += 1
            else:
                break
        return streak
    except Exception:
        return 0


def calculate_execution_gates() -> dict:
    """
    Evaluate all 5 CHAKRA execution gates.
    Returns per-gate pass/fail + overall GO / CAUTION / NO-GO.
    """
    gex_regime   = _get_gex_regime()
    pulse_score  = _get_neural_pulse()
    macro_events = _get_macro_events()
    vix          = _get_vix()
    loss_streak  = _get_loss_streak()

    gates = {}

    # Gate 1 — GEX Regime
    gex_pass = gex_regime not in ["NEGATIVE_GAMMA", "UNKNOWN"]
    gates["gex"] = {
        "label":   "GEX Regime",
        "pass":    gex_pass,
        "value":   gex_regime,
        "icon":    "✅" if gex_pass else "🔴",
        "impact":  "50% size reduction" if gex_regime == "NEGATIVE_GAMMA"
                   else "Unknown — use caution" if gex_regime == "UNKNOWN"
                   else f"{gex_regime} — normal sizing",
    }

    # Gate 2 — Neural Pulse
    pulse_pass = pulse_score >= 50
    from backend.internals.market_internals import get_dynamic_arka_threshold
    threshold_data = get_dynamic_arka_threshold(pulse_score)
    gates["pulse"] = {
        "label":   "Neural Pulse",
        "pass":    pulse_pass,
        "value":   pulse_score,
        "icon":    "✅" if pulse_score >= 70 else "🟡" if pulse_score >= 50 else "🔴",
        "impact":  threshold_data["note"],
        "threshold": threshold_data["threshold"],
    }

    # Gate 3 — Macro Events
    macro_pass = len(macro_events) == 0
    gates["macro"] = {
        "label":   "Macro Events",
        "pass":    macro_pass,
        "value":   len(macro_events),
        "icon":    "✅" if macro_pass else "🔴",
        "impact":  "No macro events" if macro_pass
                   else f"ALL ENTRIES BLOCKED — {macro_events[0].get('name','EVENT')}",
        "events":  [e.get("name") for e in macro_events],
    }

    # Gate 4 — VIX Level
    vix_pass = vix < 25
    gates["vix"] = {
        "label":   "VIX Level",
        "pass":    vix_pass,
        "value":   round(vix, 1),
        "icon":    "✅" if vix < 20 else "🟡" if vix < 25 else "🔴",
        "impact":  "Normal" if vix < 20
                   else "CAUTION mode — reduce size" if vix < 25
                   else "BLOCKED — extreme fear",
    }

    # Gate 5 — Loss Streak
    streak_pass = loss_streak < 3
    gates["loss_streak"] = {
        "label":   "Loss Streak",
        "pass":    streak_pass,
        "value":   loss_streak,
        "icon":    "✅" if loss_streak == 0 else "🟡" if loss_streak < 3 else "🔴",
        "impact":  "No streak" if loss_streak == 0
                   else f"{loss_streak} consecutive losses — caution" if loss_streak < 3
                   else "TRADING HALTED — 3+ consecutive losses",
    }

    # Overall verdict
    hard_blocks  = [g for g in gates.values() if not g["pass"]]
    soft_caution = [g for g in gates.values() if g["icon"] == "🟡"]

    if not macro_pass or not streak_pass or not vix_pass:
        overall = "NO-GO"
        overall_icon = "🔴"
    elif len(hard_blocks) > 0:
        overall = "CAUTION"
        overall_icon = "🟡"
    elif len(soft_caution) > 0:
        overall = "CAUTION"
        overall_icon = "🟡"
    else:
        overall = "GO"
        overall_icon = "🟢"

    return {
        "overall":      overall,
        "overall_icon": overall_icon,
        "gates":        gates,
        "gates_passed": sum(1 for g in gates.values() if g["pass"]),
        "gates_total":  len(gates),
        "gex_regime":   gex_regime,
        "pulse_score":  pulse_score,
        "vix":          vix,
        "loss_streak":  loss_streak,
        "timestamp":    datetime.now(ET).isoformat(),
    }


if __name__ == "__main__":
    result = calculate_execution_gates()
    print(json.dumps(result, indent=2, default=str))
