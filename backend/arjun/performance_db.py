import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict

DB_PATH = "logs/arjun_performance.db"

def init_db():
    """Create performance tracking database."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT,
            ticker       TEXT,
            signal       TEXT,
            entry_price  REAL,
            exit_price   REAL,
            analyst_score REAL,
            bull_score   REAL,
            bear_score   REAL,
            outcome      TEXT
        )
    """)
    conn.commit()
    conn.close()

def log_signal_outcome(ticker: str, signal: str, entry_price: float,
                       exit_price: float, agent_scores: Dict, outcome: str):
    """Store signal decision and outcome. outcome = WIN | LOSS | BREAKEVEN"""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO signals (date, ticker, signal, entry_price, exit_price,
                             analyst_score, bull_score, bear_score, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now().isoformat(), ticker, signal, entry_price, exit_price,
          agent_scores.get('analyst', {}).get('score', 0),
          agent_scores.get('bull',    {}).get('score', 0),
          agent_scores.get('bear',    {}).get('score', 0),
          outcome))
    conn.commit()
    conn.close()
