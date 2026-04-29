"""
ARJUN Performance Database
Logs all signal outcomes for continuous learning and weekly retraining.
"""
import sqlite3
import json
import os
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]
DB_PATH = BASE / "logs" / "arjun_performance.db"


def init_db():
    """Create tables if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # ── ARKA intraday trade learning table ─────────────────────────────────
    # Captures the full context at ARKA trade entry + actual outcome.
    # This is richer than the ARJUN morning signals table because:
    #   1. It records INTRADAY features (not just morning snapshot)
    #   2. It captures post-loss reversal context explicitly
    #   3. It has 1:1 mapping with real P&L outcomes (no estimation)
    c.execute("""
        CREATE TABLE IF NOT EXISTS arka_trades (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            date                  TEXT NOT NULL,
            ticker                TEXT NOT NULL,
            direction             TEXT NOT NULL,       -- CALL or PUT
            entry_time            TEXT,
            exit_time             TEXT,
            exit_reason           TEXT,                -- TAKE/STOP/TRAIL/EOD

            -- Conviction context at entry
            conviction            REAL,
            threshold             REAL,
            session               TEXT,               -- MORNING/MIDDAY/POWER_HOUR

            -- GEX state at entry
            gex_regime            TEXT,               -- POSITIVE_GAMMA/NEGATIVE_GAMMA/LOW_VOL
            gex_regime_call       TEXT,               -- SHORT_THE_POPS/BUY_THE_DIPS/FOLLOW_MOMENTUM
            gex_bias_ratio        REAL,               -- put/call dollar ratio
            gex_near_zero         INTEGER DEFAULT 0,  -- within $1.50 of zero gamma?
            gex_adj               REAL DEFAULT 0,     -- net GEX conviction adjustment

            -- Options flow at entry
            flow_bias             TEXT,               -- BULLISH/BEARISH/NEUTRAL
            flow_confidence       REAL,
            flow_is_extreme       INTEGER DEFAULT 0,

            -- Technical indicators at entry
            rsi                   REAL,
            vwap_above            INTEGER DEFAULT 0,  -- 1 = above VWAP
            volume_ratio          REAL,
            ema_aligned           INTEGER DEFAULT 0,  -- EMA stack aligned with direction

            -- Post-loss reversal context (THE KEY FEATURE)
            was_post_loss         INTEGER DEFAULT 0,  -- 1 = ticker had a prior loss today
            prior_loss_direction  TEXT,               -- direction of the prior loss (CALL/PUT)
            prior_loss_pnl        REAL,               -- $ loss of the prior trade
            is_reversal_trade     INTEGER DEFAULT 0,  -- 1 = direction OPPOSITE to prior loss

            -- Outcome
            pnl_dollars           REAL,
            pnl_pct               REAL,
            outcome               TEXT,               -- WIN/LOSS
            hold_minutes          REAL,

            -- GEX override flag
            gex_override          INTEGER DEFAULT 0,  -- 1 = traded against GEX block (conviction ≥90)

            created_at            TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migration: add gex_override to existing databases that predate this column
    try:
        c.execute("ALTER TABLE arka_trades ADD COLUMN gex_override INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # column already exists

    c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT NOT NULL,
            ticker        TEXT NOT NULL,
            signal        TEXT NOT NULL,
            confidence    REAL,
            entry_price   REAL,
            target_price  REAL,
            stop_price    REAL,
            exit_price    REAL,
            pnl           REAL,
            outcome       TEXT,
            analyst_bias  TEXT,
            analyst_score INTEGER,
            bull_score    INTEGER,
            bear_score    INTEGER,
            risk_decision TEXT,
            gex_regime    TEXT,
            curvature     REAL,
            agent_json    TEXT,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS weekly_stats (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start   TEXT NOT NULL,
            week_end     TEXT NOT NULL,
            total_trades INTEGER,
            wins         INTEGER,
            losses       INTEGER,
            win_rate     REAL,
            avg_pnl      REAL,
            avg_bull_score_wins  REAL,
            avg_bull_score_loss  REAL,
            avg_bear_score_wins  REAL,
            avg_bear_score_loss  REAL,
            best_ticker  TEXT,
            worst_ticker TEXT,
            notes        TEXT,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def log_arka_trade_entry(
    ticker: str,
    direction: str,         # "CALL" or "PUT"
    conviction: float,
    threshold: float,
    session: str,
    gex_state: dict,        # from load_gex_state()
    flow: dict,             # from get_flow_signal()
    indicators: dict,       # rsi, vwap_above, volume_ratio, ema_aligned
    large_loss_info: dict,  # from state.large_loss_tickers.get(ticker, {})
    gex_override: bool = False,  # True = traded against GEX block (conviction ≥90)
) -> int:
    """
    Log an ARKA trade entry with full context.
    Returns the row ID so the exit can update it later.
    """
    init_db()

    _gex = gex_state or {}
    _was_post_loss = 1 if large_loss_info else 0
    _prior_dir     = large_loss_info.get("direction", "") if large_loss_info else ""
    _prior_pnl     = float(large_loss_info.get("pnl", 0)) if large_loss_info else 0.0
    _is_reversal   = 1 if (_was_post_loss and _prior_dir and _prior_dir != direction) else 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO arka_trades (
            date, ticker, direction, entry_time, session,
            conviction, threshold,
            gex_regime, gex_regime_call, gex_bias_ratio, gex_near_zero, gex_adj,
            flow_bias, flow_confidence, flow_is_extreme,
            rsi, vwap_above, volume_ratio, ema_aligned,
            was_post_loss, prior_loss_direction, prior_loss_pnl, is_reversal_trade,
            gex_override
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now().strftime("%Y-%m-%d"),
        ticker, direction,
        datetime.now().strftime("%H:%M"),
        session,
        float(conviction), float(threshold),
        _gex.get("regime", ""),
        _gex.get("regime_call", ""),
        float(_gex.get("bias_ratio", 1.0)),
        1 if abs(float(_gex.get("spot", 0)) - float(_gex.get("zero_gamma", 0) or 0)) <= 1.5 else 0,
        0.0,  # gex_adj — filled by caller if available
        flow.get("bias", "NEUTRAL"),
        float(flow.get("confidence", 0)),
        1 if flow.get("is_extreme") else 0,
        float(indicators.get("rsi", 50)),
        1 if indicators.get("vwap_above") else 0,
        float(indicators.get("volume_ratio", 1.0)),
        1 if indicators.get("ema_aligned") else 0,
        _was_post_loss, _prior_dir, _prior_pnl, _is_reversal,
        1 if gex_override else 0,
    ))
    row_id = c.lastrowid
    conn.commit()
    conn.close()
    return row_id


def log_arka_trade_exit(
    row_id: int,
    exit_reason: str,
    pnl_dollars: float,
    pnl_pct: float,
    hold_minutes: float,
):
    """Update an ARKA trade record with exit data + outcome."""
    if not row_id:
        return
    init_db()
    outcome = "WIN" if pnl_dollars > 0 else "LOSS"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE arka_trades
        SET exit_time=?, exit_reason=?, pnl_dollars=?, pnl_pct=?, outcome=?, hold_minutes=?
        WHERE id=?
    """, (
        datetime.now().strftime("%H:%M"),
        exit_reason, float(pnl_dollars), float(pnl_pct),
        outcome, float(hold_minutes), row_id,
    ))
    conn.commit()
    conn.close()


def log_signal(signal_data: dict):
    """Log a new signal when generated."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    agents = signal_data.get("agents", {})
    gex    = signal_data.get("gex", {})
    bear   = agents.get("bear", {})
    curv   = bear.get("curvature", {})

    c.execute("""
        INSERT INTO signals
        (date, ticker, signal, confidence, entry_price, target_price, stop_price,
         analyst_bias, analyst_score, bull_score, bear_score, risk_decision,
         gex_regime, curvature, agent_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d"),
        signal_data.get("ticker", ""),
        signal_data.get("signal", ""),
        float(signal_data.get("confidence", 50)),
        float(signal_data.get("entry", 0)),
        float(signal_data.get("target", 0)),
        float(signal_data.get("stop_loss", 0)),
        agents.get("analyst", {}).get("bias", ""),
        int(agents.get("analyst", {}).get("score", 50)),
        int(agents.get("bull", {}).get("score", 50)),
        int(agents.get("bear", {}).get("score", 50)),
        agents.get("risk_manager", {}).get("decision", ""),
        gex.get("regime", ""),
        float(curv.get("curvature", 0)),
        json.dumps(agents),
    ))
    row_id = c.lastrowid
    conn.commit()
    conn.close()
    return row_id


def update_outcome(signal_id: int, exit_price: float, outcome: str, pnl: float):
    """Update signal with actual trade outcome."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE signals SET exit_price=?, outcome=?, pnl=? WHERE id=?
    """, (exit_price, outcome, pnl, signal_id))
    conn.commit()
    conn.close()


def get_recent_performance(days: int = 7) -> dict:
    """Get win rate and agent score analysis for recent period."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        SELECT ticker, signal, confidence, bull_score, bear_score,
               gex_regime, outcome, pnl, risk_decision
        FROM signals
        WHERE date >= date('now', ?) AND outcome IS NOT NULL
    """, (f"-{days} days",))

    rows = c.fetchall()
    conn.close()

    if not rows:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "message": "No completed trades"}

    total  = len(rows)
    wins   = sum(1 for r in rows if r[6] == "WIN")
    losses = sum(1 for r in rows if r[6] == "LOSS")
    wr     = wins / total if total > 0 else 0

    win_rows  = [r for r in rows if r[6] == "WIN"]
    loss_rows = [r for r in rows if r[6] == "LOSS"]

    avg_bull_wins  = sum(r[3] for r in win_rows)  / len(win_rows)  if win_rows  else 0
    avg_bull_loss  = sum(r[3] for r in loss_rows) / len(loss_rows) if loss_rows else 0
    avg_bear_wins  = sum(r[4] for r in win_rows)  / len(win_rows)  if win_rows  else 0
    avg_bear_loss  = sum(r[4] for r in loss_rows) / len(loss_rows) if loss_rows else 0

    # Ticker breakdown
    tickers = {}
    for r in rows:
        t = r[0]
        if t not in tickers:
            tickers[t] = {"wins": 0, "losses": 0}
        if r[6] == "WIN":  tickers[t]["wins"]   += 1
        if r[6] == "LOSS": tickers[t]["losses"] += 1

    for t in tickers:
        tot = tickers[t]["wins"] + tickers[t]["losses"]
        tickers[t]["win_rate"] = round(tickers[t]["wins"] / tot, 3) if tot > 0 else 0

    return {
        "period_days":        days,
        "total":              total,
        "wins":               wins,
        "losses":             losses,
        "win_rate":           round(wr, 3),
        "avg_pnl":            round(sum(r[7] or 0 for r in rows) / total, 2),
        "bull_score_wins":    round(avg_bull_wins, 1),
        "bull_score_losses":  round(avg_bull_loss, 1),
        "bear_score_wins":    round(avg_bear_wins, 1),
        "bear_score_losses":  round(avg_bear_loss, 1),
        "ticker_breakdown":   tickers,
        "recommendation":     _get_weight_recommendation(avg_bull_wins, avg_bull_loss, avg_bear_wins, avg_bear_loss),
    }


def _get_weight_recommendation(bw, bl, bew, bel) -> str:
    """Suggest agent weight adjustments based on performance data."""
    notes = []
    if bw - bl > 10:
        notes.append("Bull score is predictive — increase bull agent weight")
    elif bl - bw > 10:
        notes.append("Bull score NOT predictive — reduce bull agent weight")
    if bel - bew > 10:
        notes.append("Bear score catches losses well — increase bear agent weight")
    return "; ".join(notes) if notes else "Insufficient data for weight adjustment"


if __name__ == "__main__":
    init_db()
    perf = get_recent_performance(30)
    print(json.dumps(perf, indent=2))
    print(f"\nDB location: {DB_PATH}")
