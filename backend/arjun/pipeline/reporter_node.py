"""
CHAKRA Reporter Node
Updates dashboard state and logs results after each pipeline cycle.
"""
import json, logging, os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("CHAKRA.Reporter")
ET  = ZoneInfo("America/New_York")


async def reporter_node(state: dict) -> dict:
    """Reporter node — logs results and updates dashboard cache."""
    report  = state.get("research_report",{}) or {}
    signals = state.get("all_signals", state.get("trade_signals",[]))
    results = state.get("execution_results",[])

    now    = datetime.now(ET)
    placed = [r for r in (results or []) if r.get("status")=="placed"]

    log.info(f"📢 Reporter: {len(signals or [])} signals, {len(placed)} placed")

    # Serialize signals (convert datetime objects)
    def _safe(obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return str(obj)

    pipeline_state = {
        "last_cycle":    now.isoformat(),
        "market_regime": report.get("market_regime","?"),
        "vix":           report.get("vix",20),
        "neural_pulse":  report.get("neural_pulse",50),
        "regime_call":   report.get("regime_call","NEUTRAL"),
        "signals_count": len(signals or []),
        "placed_count":  len(placed),
        "signals":       (signals or [])[:10],
        "executions":    (results or [])[:10],
    }

    Path("logs/arjun").mkdir(parents=True, exist_ok=True)
    with open("logs/arjun/pipeline_latest.json","w") as f:
        json.dump(pipeline_state, f, indent=2, default=_safe)

    state["pipeline_state"] = pipeline_state
    return state
