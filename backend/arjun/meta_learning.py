"""
ARJUN Meta-Learning Engine — Module 8 (Mastermind Session 4)
Tracks which RL hyperparameters produce best outcomes weekly.
Grid searches learning_rate, decay, and regime multipliers.
Auto-saves best config to logs/arjun/meta_config.json.
ARJUN optimizes its own learning process.
"""
import json, logging, itertools
from pathlib import Path
from datetime import datetime

import numpy as np

log         = logging.getLogger(__name__)
CONFIG_PATH = Path("logs/arjun/meta_config.json")
HISTORY_PATH= Path("logs/arjun/meta_history.json")

DEFAULT_CONFIG = {
    "rl_learning_rate":  0.05,
    "rl_decay":          0.98,
    "signal_threshold":  20,
    "confidence_floor":  50,
    "regime_multipliers": {
        "LOW_VOL_TREND":  {"bull": 1.2, "bear": 0.8,  "dex": 1.3},
        "HIGH_VOL_TREND": {"bull": 1.0, "bear": 1.0,  "vex": 1.5},
        "CHOPPY_RANGE":   {"bull": 0.7, "bear": 0.7,  "entropy": 1.5},
        "CRISIS":         {"bull": 0.3, "bear": 1.8,  "ivskew": 1.8},
    },
    "last_updated": None,
    "best_win_rate": None,
}

# Grid search space
GRID = {
    "rl_learning_rate": [0.01, 0.03, 0.05, 0.08, 0.10],
    "rl_decay":         [0.95, 0.97, 0.98, 0.99],
    "signal_threshold": [15,   20,   25,   30],
    "confidence_floor": [45,   50,   55],
}


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg["last_updated"] = datetime.now().isoformat()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    log.info(f"[Meta] Config saved → {CONFIG_PATH}")


def _simulate_win_rate(lr: float, decay: float,
                       threshold: int, floor: int,
                       trade_outcomes: list) -> float:
    """
    Simulate what win rate these hyperparams would have produced
    on recent trade history. Approximation via weighted scoring.
    """
    if not trade_outcomes:
        return 0.5

    weights = []
    for i, outcome in enumerate(trade_outcomes):
        # Simulate RL weight evolution
        w = 1.0
        for _ in range(i):
            reward = np.tanh(outcome * 5)
            w      = max(0.2, min(2.0, w * decay + lr * reward * 0.5))
        weights.append(w)

    # Higher-weight trades should correspond to wins
    wins = sum(
        1 for outcome, w in zip(trade_outcomes, weights)
        if outcome > 0 and w > 1.0
    )
    return wins / len(trade_outcomes) if trade_outcomes else 0.5


def run_grid_search(trade_outcomes: list) -> dict:
    """
    Run grid search over hyperparameter combinations.
    Returns best config based on simulated win rate.
    """
    if len(trade_outcomes) < 5:
        log.warning(f"[Meta] Need 5+ trades for grid search, have {len(trade_outcomes)}")
        return load_config()

    best_wr     = -1
    best_params = {}
    results     = []

    combos = list(itertools.product(
        GRID["rl_learning_rate"],
        GRID["rl_decay"],
        GRID["signal_threshold"],
        GRID["confidence_floor"],
    ))

    log.info(f"[Meta] Testing {len(combos)} hyperparameter combinations...")

    for lr, decay, threshold, floor in combos:
        wr = _simulate_win_rate(lr, decay, threshold, floor, trade_outcomes)
        results.append({
            "rl_learning_rate":  lr,
            "rl_decay":          decay,
            "signal_threshold":  threshold,
            "confidence_floor":  floor,
            "simulated_win_rate": round(wr, 4),
        })
        if wr > best_wr:
            best_wr     = wr
            best_params = {"rl_learning_rate": lr, "rl_decay": decay,
                           "signal_threshold": threshold, "confidence_floor": floor}

    # Save history
    history = {"timestamp": datetime.now().isoformat(),
               "trade_count": len(trade_outcomes),
               "combos_tested": len(combos),
               "best_params": best_params,
               "best_win_rate": round(best_wr, 4),
               "top_5": sorted(results, key=lambda r: -r["simulated_win_rate"])[:5]}
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=2))

    # Build and save new config
    cfg = load_config()
    cfg.update(best_params)
    cfg["best_win_rate"] = round(best_wr, 4)
    save_config(cfg)

    log.info(f"[Meta] Best config: lr={best_params['rl_learning_rate']} "
             f"decay={best_params['rl_decay']} "
             f"threshold={best_params['signal_threshold']} "
             f"win_rate={best_wr:.1%}")
    return cfg


if __name__ == "__main__":
    # Simulate 20 recent trade outcomes
    np.random.seed(42)
    outcomes = list(np.random.randn(20) * 0.01)

    print("Running Meta-Learning grid search...")
    print(f"Trades: {len(outcomes)}  |  Combos: "
          f"{len(GRID['rl_learning_rate']) * len(GRID['rl_decay']) * len(GRID['signal_threshold']) * len(GRID['confidence_floor'])}")

    cfg = run_grid_search(outcomes)

    print(f"\n{'='*52}")
    print(f"  META-LEARNING GRID SEARCH COMPLETE")
    print(f"{'='*52}")
    print(f"  Best RL learning rate:  {cfg['rl_learning_rate']}")
    print(f"  Best RL decay:          {cfg['rl_decay']}")
    print(f"  Best signal threshold:  {cfg['signal_threshold']}")
    print(f"  Best confidence floor:  {cfg['confidence_floor']}")
    print(f"  Simulated win rate:     {cfg['best_win_rate']:.1%}")
    print(f"  Config saved → {CONFIG_PATH}")
    print(f"  History saved → {HISTORY_PATH}")
