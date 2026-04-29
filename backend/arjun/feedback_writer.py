"""
ARJUN Feedback Writer
Records ARKA trade outcomes for historical accuracy tracking and weekly review.
"""
import json
import logging
import time
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("feedback_writer")


def record_outcome(
    ticker: str,
    direction: str,
    entry: float,
    exit_price: float,
    pnl_pct: float,
    reason: str,
    conviction: int,
    signals_used: list,
):
    """Write trade outcome to ARJUN feedback log for weekly learning."""
    Path("logs/arjun/feedback").mkdir(parents=True, exist_ok=True)
    outcome = {
        "ticker":     ticker,
        "direction":  direction,
        "entry":      entry,
        "exit":       exit_price,
        "pnl_pct":    round(pnl_pct, 2),
        "correct":    (direction in ("CALL", "LONG")  and pnl_pct > 0) or
                      (direction in ("PUT",  "SHORT") and pnl_pct > 0),
        "reason":     reason,
        "conviction": conviction,
        "signals":    signals_used,
        "timestamp":  time.time(),
        "datetime":   datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET"),
    }
    path = Path(f"logs/arjun/feedback/outcomes_{date.today().isoformat()}.json")
    history = []
    if path.exists():
        try:
            history = json.loads(path.read_text())
        except Exception:
            history = []
    history.append(outcome)
    path.write_text(json.dumps(history, indent=2))
    log.info(
        f"📝 Outcome logged: {ticker} {direction} {pnl_pct:+.1f}% "
        f"correct={outcome['correct']} reason={reason}"
    )


def get_historical_accuracy_boost(ticker: str, direction: str) -> int:
    """
    Look up past trade outcomes for this ticker.
    Returns conviction adjustment: +8 if historically accurate,
    -8 if historically wrong, 0 if insufficient data.
    """
    import glob

    files = sorted(glob.glob("logs/arjun/feedback/outcomes_*.json"), reverse=True)[:5]
    outcomes = []
    for f in files:
        try:
            data = json.loads(Path(f).read_text())
            outcomes += [o for o in data if o["ticker"] == ticker]
        except Exception:
            pass

    if len(outcomes) < 3:
        return 0  # not enough data

    recent = outcomes[-10:]
    correct = sum(1 for o in recent if o.get("correct"))
    accuracy = correct / len(recent)

    if accuracy >= 0.65:
        log.info(f"  📈 History boost +8: {ticker} {accuracy:.0%} accuracy over {len(recent)} trades")
        return +8
    elif accuracy <= 0.35:
        log.info(f"  📉 History penalty -8: {ticker} {accuracy:.0%} accuracy over {len(recent)} trades")
        return -8
    return 0
