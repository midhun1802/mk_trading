"""
ARJUN Signal Memory — Episodic pattern recall.
Stores every signal as a feature vector.
At signal time, finds 5 most similar past situations
and returns their outcomes as a confidence modifier (±10 pts).
"""
import sqlite3, json, logging
from pathlib import Path
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

log     = logging.getLogger(__name__)
DB_PATH = Path("logs/arjun/signal_memory.db")

FEATURES = [
    "dex_score", "hurst", "vrp", "entropy",
    "neural_pulse", "vix", "rsi", "volume_ratio"
]


def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            signal_id     TEXT PRIMARY KEY,
            ticker        TEXT,
            features_json TEXT,
            outcome_pct   REAL,
            direction     TEXT,
            timestamp     TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


class SignalMemory:

    def store_signal(self, signal_id: str, ticker: str,
                     features: dict, direction: str = ""):
        vec  = json.dumps([features.get(f, 0.0) for f in FEATURES])
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO signals "
            "(signal_id, ticker, features_json, direction) VALUES (?,?,?,?)",
            (signal_id, ticker, vec, direction)
        )
        conn.commit(); conn.close()
        log.debug(f"[Memory] Stored {signal_id} {ticker} dir={direction}")

    def update_outcome(self, signal_id: str, outcome_pct: float):
        conn = _get_conn()
        conn.execute(
            "UPDATE signals SET outcome_pct=? WHERE signal_id=?",
            (outcome_pct, signal_id)
        )
        conn.commit(); conn.close()
        log.debug(f"[Memory] Outcome: {signal_id} → {outcome_pct:+.2%}")

    def find_analogues(self, current_features: dict,
                       ticker: str = None, top_k: int = 5) -> dict:
        current_vec = np.array([[
            current_features.get(f, 0.0) for f in FEATURES
        ]], dtype=float)

        conn   = _get_conn()
        query  = ("SELECT signal_id, features_json, outcome_pct, direction "
                  "FROM signals WHERE outcome_pct IS NOT NULL")
        params = []
        if ticker:
            query  += " AND ticker=?"
            params.append(ticker)
        rows = conn.execute(query, params).fetchall()
        conn.close()

        if len(rows) < 3:
            return {
                "analogues":        [],
                "confidence_boost": 0,
                "expected_outcome": None,
                "sample_size":      len(rows),
                "note":             f"Need 3+ resolved trades (have {len(rows)})"
            }

        past_vecs = np.array([json.loads(r[1]) for r in rows], dtype=float)
        outcomes  = [r[2] for r in rows]
        dirs      = [r[3] for r in rows]

        sims    = cosine_similarity(current_vec, past_vecs)[0]
        top_idx = np.argsort(sims)[-top_k:][::-1]

        analogues = [{
            "signal_id":   rows[i][0],
            "similarity":  round(float(sims[i]) * 100, 1),
            "outcome_pct": round(float(outcomes[i]), 4),
            "direction":   dirs[i],
        } for i in top_idx]

        avg_outcome      = float(np.mean([a["outcome_pct"] for a in analogues]))
        win_rate         = sum(1 for a in analogues if a["outcome_pct"] > 0) / len(analogues)
        confidence_boost = +10 if avg_outcome > 0.005 else -10

        return {
            "analogues":        analogues,
            "expected_outcome": round(avg_outcome, 4),
            "win_rate":         round(win_rate, 2),
            "confidence_boost": confidence_boost,
            "sample_size":      len(rows),
        }

    def stats(self) -> dict:
        conn  = _get_conn()
        total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE outcome_pct IS NOT NULL"
        ).fetchone()[0]
        wins = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE outcome_pct > 0"
        ).fetchone()[0]
        conn.close()
        return {
            "total_signals":    total,
            "resolved_trades":  resolved,
            "wins":             wins,
            "win_rate":         round(wins / resolved, 3) if resolved > 0 else None,
        }


_instance = None

def get_signal_memory() -> SignalMemory:
    global _instance
    if _instance is None:
        _instance = SignalMemory()
    return _instance


if __name__ == "__main__":
    mem = SignalMemory()

    # Seed realistic test trades
    test_trades = [
        ("SIG-001", "SPY", {"dex_score": 68, "hurst": 0.63, "vrp": 1.15,
          "entropy": 1.9, "neural_pulse": 74, "vix": 17, "rsi": 56, "volume_ratio": 1.4},
         "BUY", +0.014),
        ("SIG-002", "SPY", {"dex_score": 45, "hurst": 0.42, "vrp": 0.88,
          "entropy": 0.8, "neural_pulse": 44, "vix": 23, "rsi": 37, "volume_ratio": 0.75},
         "SELL", -0.009),
        ("SIG-003", "SPY", {"dex_score": 71, "hurst": 0.65, "vrp": 1.22,
          "entropy": 2.1, "neural_pulse": 78, "vix": 15, "rsi": 60, "volume_ratio": 1.6},
         "BUY", +0.021),
        ("SIG-004", "QQQ", {"dex_score": 55, "hurst": 0.50, "vrp": 1.00,
          "entropy": 1.4, "neural_pulse": 58, "vix": 20, "rsi": 50, "volume_ratio": 1.0},
         "HOLD", +0.003),
        ("SIG-005", "SPY", {"dex_score": 38, "hurst": 0.38, "vrp": 0.75,
          "entropy": 0.6, "neural_pulse": 35, "vix": 28, "rsi": 30, "volume_ratio": 0.6},
         "SELL", -0.018),
    ]

    print("Seeding test trades into signal memory...")
    for sid, tkr, feats, direction, outcome in test_trades:
        mem.store_signal(sid, tkr, feats, direction)
        mem.update_outcome(sid, outcome)

    print(f"\nDB Stats: {mem.stats()}")

    # Query analogues for a current signal
    current = {
        "dex_score": 66, "hurst": 0.61, "vrp": 1.12,
        "entropy": 1.85, "neural_pulse": 72, "vix": 18,
        "rsi": 54, "volume_ratio": 1.35
    }
    print("\nFinding analogues for current signal...")
    result = mem.find_analogues(current, ticker="SPY")
    print(f"  Sample size:      {result['sample_size']}")
    print(f"  Analogues found:  {len(result['analogues'])}")
    exp = result["expected_outcome"]
    print(f"  Expected outcome: {exp:+.4f}" if exp is not None else "  Expected outcome: N/A (need more trades)")
    print(f"  Win rate:         {result['win_rate']:.0%}")
    print(f"  Confidence boost: {result['confidence_boost']:+d} pts")
    for a in result["analogues"]:
        print(f"    {a['signal_id']}  sim={a['similarity']}%  "
              f"outcome={a['outcome_pct']:+.3%}  dir={a['direction']}")
