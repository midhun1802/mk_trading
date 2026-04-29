"""
CHAKRA Neural Pulse Timeline
Maintains a rolling 30-minute buffer of pulse scores (5-min intervals).
Writes to logs/internals/pulse_timeline.json for dashboard sparkline.
"""
import json, os, asyncio
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)
ET       = ZoneInfo("America/New_York")
LOG_DIR  = BASE / "logs/internals"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TIMELINE_FILE = LOG_DIR / "pulse_timeline.json"
MAX_POINTS    = 36  # 3 hours at 5-min intervals


def load_timeline() -> list:
    if TIMELINE_FILE.exists():
        try:
            return json.loads(TIMELINE_FILE.read_text())
        except Exception:
            pass
    return []


def save_timeline(points: list):
    TIMELINE_FILE.write_text(json.dumps(points[-MAX_POINTS:], indent=2))


def record_pulse_point():
    """Read latest internals and append pulse score to timeline."""
    latest = LOG_DIR / "internals_latest.json"
    if not latest.exists():
        return None
    try:
        data  = json.loads(latest.read_text())
        pulse = data.get("neural_pulse", {})
        score = pulse.get("score", 50)
        label = pulse.get("label", "NEUTRAL")
        arka_mod = data.get("arka_mod", {}).get("modifier", 0)
        point = {
            "time":      datetime.now(ET).strftime("%H:%M"),
            "timestamp": datetime.now(ET).isoformat(),
            "score":     score,
            "label":     label,
            "arka_mod":  arka_mod,
        }
        timeline = load_timeline()
        # Avoid duplicate entries within same minute
        if timeline and timeline[-1]["time"] == point["time"]:
            timeline[-1] = point
        else:
            timeline.append(point)
        save_timeline(timeline)
        return point
    except Exception as e:
        return {"error": str(e)}


def get_timeline() -> dict:
    """API-friendly timeline response."""
    points = load_timeline()
    if not points:
        return {"points": [], "current_score": 50, "trend": "FLAT",
                "high": 50, "low": 50, "avg": 50.0, "data_points": 0}
    scores  = [p["score"] for p in points]
    current = scores[-1]
    avg5    = sum(scores[-5:]) / min(5, len(scores))
    trend   = "RISING" if current > avg5 + 3 else "FALLING" if current < avg5 - 3 else "FLAT"
    return {
        "points":        points,
        "current_score": current,
        "trend":         trend,
        "high":          max(scores),
        "low":           min(scores),
        "avg":           round(sum(scores) / len(scores), 1),
        "data_points":   len(points),
    }


async def run_timeline_loop():
    """Record pulse every 5 minutes during market hours."""
    print("CHAKRA Pulse Timeline — recording every 5min")
    while True:
        now = datetime.now(ET)
        if now.weekday() < 5 and 8 <= now.hour < 17:
            pt = record_pulse_point()
            if pt:
                print(f"  Pulse recorded: {pt.get('score')}/100 @ {pt.get('time')}")
        await asyncio.sleep(300)  # 5 minutes


if __name__ == "__main__":
    import sys
    if "--record" in sys.argv:
        pt = record_pulse_point()
        print(json.dumps(pt, indent=2, default=str))
    elif "--status" in sys.argv:
        print(json.dumps(get_timeline(), indent=2))
    else:
        asyncio.run(run_timeline_loop())
