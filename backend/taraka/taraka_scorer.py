"""
taraka_scorer.py — TARAKA Confluence Scorer
Combines analyst track record + Arjun's live signals + market context
to produce a single 0-100 score. Trade fires if score ≥ 65.

Score breakdown:
  Analyst score  (0-40):  win rate, experience, signal quality
  Arjun score    (0-40):  agrees with direction, conviction level
  Quality score  (0-20):  completeness of alert, parse confidence

Total ≥ 65 → real trade
Total  < 65 → paper trade only
"""

import json
import logging
from pathlib import Path
from datetime import datetime
import pytz

log = logging.getLogger("taraka.scorer")

ARJUN_SIGNALS_DIR = Path("logs/signals")
ARKA_LOG_DIR      = Path("logs/arka")
ET = pytz.timezone("America/New_York")


class TarakaScorer:

    def score(self, parsed: dict, analyst: str, analyst_weight: float, session: str) -> tuple[int, dict]:
        """
        Score an alert 0-100. Returns (score, breakdown_dict).

        analyst_weight: 0.0-1.0 from AnalystTracker (based on historical win rate)
        """
        breakdown = {}

        # ── 1. ANALYST SCORE (0-40) ─────────────────────────────────────────
        analyst_score = self._score_analyst(analyst, analyst_weight, breakdown)

        # ── 2. ARJUN SCORE (0-40) ───────────────────────────────────────────
        arjun_score = self._score_arjun(parsed, breakdown)

        # ── 3. QUALITY SCORE (0-20) ─────────────────────────────────────────
        quality_score = self._score_quality(parsed, session, breakdown)

        total = analyst_score + arjun_score + quality_score
        total = min(100, max(0, total))

        breakdown["analyst"]       = analyst_score
        breakdown["arjun"]         = arjun_score
        breakdown["quality"]       = quality_score
        breakdown["session_bonus"] = breakdown.get("session_bonus", 0)
        breakdown["total"]         = total

        log.info(f"  Score breakdown: analyst={analyst_score} arjun={arjun_score} quality={quality_score} → {total}")
        return total, breakdown


    def _score_analyst(self, analyst: str, weight: float, breakdown: dict) -> int:
        """
        Analyst score based on historical performance.
        New analysts (no history) get 20/40 — neutral starting point.
        """
        if weight is None:
            # Brand new analyst — give neutral score, learn over time
            breakdown["analyst_detail"] = "new analyst — neutral score"
            return 20

        # weight is win_rate (0.0 to 1.0)
        # 50% win rate = 20 pts (neutral)
        # 75% win rate = 30 pts
        # 100% win rate = 40 pts
        # 25% win rate = 10 pts
        # 0% win rate  = 0 pts
        score = int(weight * 40)
        breakdown["analyst_detail"] = f"win_rate={weight:.0%} → {score}pts"
        return score


    def _score_arjun(self, parsed: dict, breakdown: dict) -> int:
        """
        Check if Arjun's daily signals agree with the alert direction.
        Also check ARKA's conviction score if available.
        """
        ticker    = parsed["ticker"]
        direction = parsed["direction"]   # CALL or PUT
        score     = 0

        # ── Load today's Arjun signals ──
        arjun_signal = self._get_arjun_signal(ticker)
        arjun_agrees = False

        if arjun_signal:
            sig = arjun_signal.get("signal", "HOLD")
            conf = float(arjun_signal.get("confidence", 50))

            # Direction alignment
            if direction == "CALL" and sig == "BUY":
                score += 25
                arjun_agrees = True
                breakdown["arjun_signal"] = f"Arjun=BUY ({conf:.1f}%) ✅ agrees"
            elif direction == "PUT" and sig == "SELL":
                score += 25
                arjun_agrees = True
                breakdown["arjun_signal"] = f"Arjun=SELL ({conf:.1f}%) ✅ agrees"
            elif sig == "HOLD":
                score += 10   # neutral — partial credit
                breakdown["arjun_signal"] = f"Arjun=HOLD — partial credit"
            else:
                score += 0    # disagrees — no points
                breakdown["arjun_signal"] = f"Arjun={sig} ❌ disagrees with {direction}"

            # Confidence bonus
            if arjun_agrees and conf >= 70:
                score += 10
                breakdown["arjun_conf_bonus"] = f"High confidence {conf:.1f}% +10"
            elif arjun_agrees and conf >= 60:
                score += 5
                breakdown["arjun_conf_bonus"] = f"Medium confidence {conf:.1f}% +5"
        else:
            # No Arjun signal for this ticker today — give neutral
            score += 15
            breakdown["arjun_signal"] = f"No Arjun signal for {ticker} today — neutral +15"

        # ── Check ARKA conviction (SPY/QQQ only) ──
        if ticker in ("SPY", "QQQ"):
            arka_conv = self._get_arka_conviction(ticker)
            if arka_conv is not None:
                if direction == "CALL" and arka_conv >= 55:
                    score += 5
                    breakdown["arka_conv"] = f"ARKA conv={arka_conv:.1f} bullish +5"
                elif direction == "PUT" and arka_conv < 40:
                    score += 5
                    breakdown["arka_conv"] = f"ARKA conv={arka_conv:.1f} bearish +5"
                else:
                    breakdown["arka_conv"] = f"ARKA conv={arka_conv:.1f} neutral"

        return min(40, score)


    def _score_quality(self, parsed: dict, session: str, breakdown: dict) -> int:
        """Score the completeness and quality of the alert itself."""
        score = 0

        # Parse confidence from Claude
        parse_conf = parsed.get("parse_conf", 50)
        score += int(parse_conf / 100 * 8)   # 0-8 pts
        breakdown["parse_confidence"] = f"Claude parse conf={parse_conf}% → {int(parse_conf/100*8)}pts"

        # Has entry price
        if parsed.get("entry"):
            score += 3
        # Has target
        if parsed.get("target"):
            score += 3
        # Has stop
        if parsed.get("stop"):
            score += 3

        # Session timing bonus
        if session == "OPEN":
            score += 3
            breakdown["session_bonus"] = 3
        elif session == "NORMAL":
            score += 2
            breakdown["session_bonus"] = 2
        elif session == "POWER_HOUR":
            score += 1
            breakdown["session_bonus"] = 1

        return min(20, score)


    def _get_arjun_signal(self, ticker: str) -> dict | None:
        """Load today's most recent Arjun signal for a ticker."""
        today = datetime.now(ET).strftime("%Y%m%d")
        # Find today's signal files
        signal_files = sorted(ARJUN_SIGNALS_DIR.glob(f"signals_{today}_*.json"), reverse=True)
        if not signal_files:
            return None
        try:
            with open(signal_files[0]) as f:
                signals = json.load(f)
            for s in signals:
                if s.get("ticker") == ticker:
                    return s
        except Exception as e:
            log.warning(f"Could not load Arjun signals: {e}")
        return None


    def _get_arka_conviction(self, ticker: str) -> float | None:
        """Parse today's ARKA log for latest conviction score."""
        import re
        today = datetime.now(ET).strftime("%Y-%m-%d")
        log_path = ARKA_LOG_DIR / f"arka_{today}.log"
        if not log_path.exists():
            return None
        try:
            with open(log_path) as f:
                lines = f.readlines()
            # Search from bottom for latest scan with this ticker
            pattern = re.compile(rf"{ticker}.*conv=\s*([\d.]+)", re.I)
            for line in reversed(lines):
                m = pattern.search(line)
                if m:
                    return float(m.group(1))
        except Exception as e:
            log.warning(f"Could not read ARKA log: {e}")
        return None
