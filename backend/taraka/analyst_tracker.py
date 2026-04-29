"""
analyst_tracker.py — Analyst Performance Tracker
Tracks every analyst's alerts and outcomes.
Computes win rate, P&L, and weight for future scoring.

Data saved to: logs/taraka/analyst_stats.json
Updated daily by taraka_journal.py at 4pm.
"""

import json
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger("taraka.tracker")

STATS_FILE = Path("logs/taraka/analyst_stats.json")


class AnalystTracker:

    def __init__(self):
        self.stats = self._load()

    def _load(self) -> dict:
        if STATS_FILE.exists():
            try:
                with open(STATS_FILE) as f:
                    return json.load(f)
            except:
                pass
        return {}

    def _save(self):
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATS_FILE, "w") as f:
            json.dump(self.stats, f, indent=2)

    def get_weight(self, analyst: str) -> float | None:
        """
        Returns analyst's win rate (0.0-1.0) or None if new analyst.
        Used by TarakaScorer to weight the alert.
        """
        if analyst not in self.stats:
            return None   # new analyst — scorer gives neutral 20/40
        s = self.stats[analyst]
        completed = s.get("wins", 0) + s.get("losses", 0)
        if completed < 3:
            return None   # too few trades to rate — stay neutral
        return s["wins"] / completed

    def log_alert(self, analyst: str, alert_id: str, parsed: dict, score: int, real: bool):
        """Record that an alert was received and acted on."""
        if analyst not in self.stats:
            self.stats[analyst] = {
                "first_seen":  datetime.now().isoformat(),
                "alerts":      0,
                "real_trades": 0,
                "paper_trades":0,
                "wins":        0,
                "losses":      0,
                "total_pnl":   0.0,
                "avg_score":   0.0,
                "pending":     {},
            }

        s = self.stats[analyst]
        s["alerts"] += 1
        if real:
            s["real_trades"] += 1
        else:
            s["paper_trades"] += 1

        # Update rolling average score
        prev_avg = s.get("avg_score", 0)
        s["avg_score"] = round((prev_avg * (s["alerts"]-1) + score) / s["alerts"], 1)

        # Store pending alert waiting for outcome
        s["pending"][alert_id] = {
            "ticker":    parsed["ticker"],
            "direction": parsed["direction"],
            "timestamp": datetime.now().isoformat(),
            "real":      real,
            "score":     score,
        }

        self._save()
        log.info(f"  Logged alert for @{analyst} — total alerts: {s['alerts']}")

    def record_outcome(self, analyst: str, alert_id: str, won: bool, pnl: float):
        """Called by taraka_journal.py at 4pm with actual outcome."""
        if analyst not in self.stats:
            return
        s = self.stats[analyst]

        if won:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["total_pnl"] = round(s.get("total_pnl", 0) + pnl, 2)

        # Remove from pending
        s["pending"].pop(alert_id, None)

        # Recompute weight
        completed = s["wins"] + s["losses"]
        win_rate  = s["wins"] / completed if completed else 0
        s["win_rate"]   = round(win_rate, 3)
        s["total_trades"] = completed

        self._save()
        result = "✅ WIN" if won else "❌ LOSS"
        log.info(f"  Outcome recorded: @{analyst} {result} P&L=${pnl:.2f} | "
                 f"WR={win_rate:.0%} ({s['wins']}/{completed})")

    def get_leaderboard(self) -> list[dict]:
        """Return analysts sorted by win rate (min 3 completed trades)."""
        board = []
        for analyst, s in self.stats.items():
            completed = s.get("wins", 0) + s.get("losses", 0)
            win_rate  = s["wins"] / completed if completed else 0
            board.append({
                "analyst":     analyst,
                "alerts":      s["alerts"],
                "completed":   completed,
                "wins":        s.get("wins", 0),
                "losses":      s.get("losses", 0),
                "win_rate":    round(win_rate * 100, 1),
                "total_pnl":   s.get("total_pnl", 0),
                "avg_score":   s.get("avg_score", 0),
                "weight":      self._weight_label(win_rate, completed),
                "pending":     len(s.get("pending", {})),
            })
        return sorted(board, key=lambda x: (x["completed"] >= 3, x["win_rate"]), reverse=True)

    def _weight_label(self, win_rate: float, completed: int) -> str:
        if completed < 3:
            return "NEW"
        if win_rate >= 0.70:
            return "HIGH"
        if win_rate >= 0.55:
            return "MEDIUM"
        if win_rate >= 0.40:
            return "LOW"
        return "IGNORE"

    def get_summary(self) -> dict:
        """Quick summary for dashboard."""
        total_alerts = sum(s["alerts"] for s in self.stats.values())
        total_trades = sum(s.get("real_trades", 0) for s in self.stats.values())
        total_wins   = sum(s.get("wins", 0) for s in self.stats.values())
        total_losses = sum(s.get("losses", 0) for s in self.stats.values())
        total_pnl    = sum(s.get("total_pnl", 0) for s in self.stats.values())
        completed    = total_wins + total_losses
        return {
            "analysts":     len(self.stats),
            "total_alerts": total_alerts,
            "total_trades": total_trades,
            "wins":         total_wins,
            "losses":       total_losses,
            "win_rate":     round(total_wins / completed * 100, 1) if completed else 0,
            "total_pnl":    round(total_pnl, 2),
        }
