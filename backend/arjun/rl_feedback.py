"""
ARJUN Reinforcement Learning Feedback Loop
After every trade closes → reward/penalize agents that contributed.
Weights saved to logs/arjun/agent_weights.json.
Learning rate 0.05, decay 0.98, weight floor 0.2, ceiling 2.0.
"""
import json, logging
from pathlib import Path
import numpy as np

log = logging.getLogger(__name__)

WEIGHTS_FILE = Path("logs/arjun/agent_weights.json")

DEFAULT_WEIGHTS = {
    "bull_agent":   1.0,
    "bear_agent":   1.0,
    "risk_manager": 1.0,
    "dex":          1.0,
    "hurst":        1.0,
    "vex":          1.0,
    "hmm":          1.0,
    "entropy":      1.0,
    "iv_skew":      1.0,
    "iceberg":      1.0,
    "lambda":       1.0,
    "cot":          1.0,
    "vrp":          1.0,
    "charm":        1.0,
    "prob_dist":    1.0,
}

LEARNING_RATE = 0.05
DECAY         = 0.98
WEIGHT_MIN    = 0.2
WEIGHT_MAX    = 2.0


class ARJUNReinforcementLearner:

    def __init__(self):
        self.weights = self._load()

    def _load(self) -> dict:
        try:
            if WEIGHTS_FILE.exists():
                saved  = json.loads(WEIGHTS_FILE.read_text())
                merged = {**DEFAULT_WEIGHTS, **saved}
                return merged
        except Exception as e:
            log.warning(f"[RL] Load failed: {e}")
        return dict(DEFAULT_WEIGHTS)

    def update_on_close(self, trade_id: str, pnl_pct: float,
                        contributing_agents: dict) -> dict:
        """
        pnl_pct:              +0.15 = +15% win,  -0.08 = -8% loss
        contributing_agents:  {'bull_agent': 72, 'dex': 65, ...}
        """
        reward  = float(np.tanh(pnl_pct * 5))   # squash to -1 .. +1
        updates = {}

        for agent, score in contributing_agents.items():
            if agent in self.weights:
                strength = score / 100.0
                delta    = LEARNING_RATE * reward * strength
                old_w    = self.weights[agent]
                new_w    = max(WEIGHT_MIN, min(WEIGHT_MAX,
                               old_w * DECAY + delta))
                self.weights[agent] = round(new_w, 4)
                updates[agent] = {
                    "old":   round(old_w, 4),
                    "new":   round(new_w, 4),
                    "delta": round(delta,  4),
                }

        self._save()
        log.info(f"[RL] {trade_id}  pnl={pnl_pct:+.2%}  "
                 f"reward={reward:+.3f}  updated={list(updates.keys())}")
        return updates

    def get_weight(self, agent: str) -> float:
        return self.weights.get(agent, 1.0)

    def apply_weights(self, agent_scores: dict) -> dict:
        return {
            agent: round(score * self.get_weight(agent), 2)
            for agent, score in agent_scores.items()
        }

    def _save(self):
        WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        WEIGHTS_FILE.write_text(json.dumps(self.weights, indent=2))

    def summary(self) -> str:
        lines = ["── ARJUN RL Weights ──────────────────"]
        for agent, w in sorted(self.weights.items()):
            bar  = "█" * int(w * 10)
            diff = w - 1.0
            flag = f" {'▲' if diff>0.05 else '▼' if diff<-0.05 else '─'} {diff:+.3f}"
            lines.append(f"  {agent:<14} {w:.3f}  {bar}{flag}")
        return "\n".join(lines)


_instance = None

def get_rl_learner() -> ARJUNReinforcementLearner:
    global _instance
    if _instance is None:
        _instance = ARJUNReinforcementLearner()
    return _instance


if __name__ == "__main__":
    rl = ARJUNReinforcementLearner()
    print(rl.summary())

    print("\n--- Simulating +12% win (bull_agent + dex voted strongly) ---")
    rl.update_on_close("TEST-WIN-001", pnl_pct=+0.12,
        contributing_agents={"bull_agent": 78, "dex": 65, "hurst": 55, "vex": 48})

    print("\n--- Simulating -6% loss (hmm + entropy gave bad signal) ---")
    rl.update_on_close("TEST-LOSS-001", pnl_pct=-0.06,
        contributing_agents={"hmm": 70, "entropy": 60, "bull_agent": 52})

    print("\nUpdated weights:")
    print(rl.summary())
