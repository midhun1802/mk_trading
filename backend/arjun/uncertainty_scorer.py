"""
ARJUN Uncertainty Scorer — Module 6 (Mastermind Session 3)
Shannon entropy across agent scores → position size multiplier.
Low entropy  (agents agree)    → 1.2x size
High entropy (agents disagree) → 0.5x size
"""
import logging
import numpy as np
from scipy.stats import entropy as shannon_entropy

log = logging.getLogger(__name__)


class UncertaintyScorer:
    HIGH_UNCERTAINTY_THRESHOLD = 1.5
    LOW_UNCERTAINTY_THRESHOLD  = 0.5

    def score(self, agent_scores: dict) -> dict:
        if not agent_scores:
            return {"uncertainty": 1.0, "position_multiplier": 1.0,
                    "regime": "UNKNOWN", "agent_count": 0}
        raw = np.array(list(agent_scores.values()), dtype=float)
        unc = float(np.std(raw))          # std of raw scores — scales with agent count
        if unc > 20.0:                    # agents wildly disagree
            regime, multiplier = "HIGH_UNCERTAINTY", 0.5
        elif unc < 8.0:                   # agents tightly aligned
            regime, multiplier = "LOW_UNCERTAINTY",  1.2
        else:
            regime, multiplier = "MODERATE",         1.0
        return {
            "uncertainty":         round(unc, 4),
            "position_multiplier": multiplier,
            "regime":              regime,
            "agent_count":         len(agent_scores),
            "spread":              round(float(raw.max() - raw.min()), 2),
        }


_instance = None

def get_uncertainty_scorer() -> UncertaintyScorer:
    global _instance
    if _instance is None:
        _instance = UncertaintyScorer()
    return _instance


if __name__ == "__main__":
    scorer = UncertaintyScorer()
    scenarios = [
        ("All agents agree BUY",
         {"bull_agent": 80, "dex": 78, "hurst": 75, "vex": 72, "hmm": 77}),
        ("Mixed signals",
         {"bull_agent": 72, "bear_agent": 65, "dex": 55, "hurst": 48, "vex": 70}),
        ("High disagreement",
         {"bull_agent": 85, "bear_agent": 20, "dex": 90, "hurst": 15, "vex": 80}),
    ]
    print(f"\n{'='*55}")
    print(f"  UNCERTAINTY SCORING — 3 SCENARIOS")
    print(f"{'='*55}")
    for label, scores in scenarios:
        r = scorer.score(scores)
        print(f"\n  {label}")
        print(f"    Entropy:    {r['uncertainty']:.4f}")
        print(f"    Spread:     {r['spread']} pts")
        print(f"    Regime:     {r['regime']}")
        print(f"    Size mult:  {r['position_multiplier']}x")
