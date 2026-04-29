import sqlite3
import pandas as pd
import numpy as np
import json
import os
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)
BASE            = Path(__file__).resolve().parents[2]
DB_PATH         = str(BASE / "logs/arjun_performance.db")
MODEL_DIR       = BASE / "backend/arjun/models"
DISCORD_WEBHOOK = os.getenv("DISCORD_TRADES_WEBHOOK", "")

# Raw indicator columns used for manifold features
# These come from indicators_json stored at signal time
INDICATOR_KEYS = [
    "rsi", "macd_hist", "ema9", "ema20", "ema50", "ema200",
    "volume_ratio", "bb_position", "atr", "adx",
    "pct_from_52w_high", "pct_from_52w_low",
    "analyst_score", "bull_score", "bear_score",
    "curvature", "gex_net", "iv_skew",
    "dark_pool_conviction", "news_score",
]


def analyze_signal_performance():
    """Query performance DB and print 7-day win rates."""
    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query('SELECT * FROM signals WHERE date > date("now", "-7 days")', conn)
    conn.close()
    if df.empty:
        print("No signals in last 7 days."); return

    wr = df.groupby("signal").apply(lambda x: (x["outcome"] == "WIN").sum() / len(x))
    print("7-Day Win Rates:"); print(wr)

    wins   = df[df["outcome"] == "WIN"]
    losses = df[df["outcome"] == "LOSS"]
    if not wins.empty and not losses.empty:
        print(f"\nAvg Bull Score — Wins: {wins['bull_score'].mean():.1f} | Losses: {losses['bull_score'].mean():.1f}")


def check_performance_degradation():
    """Alert if 7-day win rate drops >10% vs prior 30-day baseline."""
    conn = sqlite3.connect(DB_PATH)
    try:
        recent_wr   = pd.read_sql_query('SELECT (SUM(CASE WHEN outcome="WIN" THEN 1.0 ELSE 0 END) / COUNT(*)) as wr FROM signals WHERE date > date("now", "-7 days")',  conn)["wr"][0]
        baseline_wr = pd.read_sql_query('SELECT (SUM(CASE WHEN outcome="WIN" THEN 1.0 ELSE 0 END) / COUNT(*)) as wr FROM signals WHERE date BETWEEN date("now", "-37 days") AND date("now", "-7 days")', conn)["wr"][0]
    finally:
        conn.close()
    if recent_wr and baseline_wr and recent_wr < baseline_wr - 0.10:
        msg = f"⚠️ ARJUN win rate dropped: {recent_wr:.1%} vs {baseline_wr:.1%} baseline"
        print(msg)
        if DISCORD_WEBHOOK:
            requests.post(DISCORD_WEBHOOK, json={"content": msg})


def _build_feature_matrix(df: pd.DataFrame) -> np.ndarray:
    """
    Build raw feature matrix from DB rows.
    Priority: indicators_json → fallback to agent score columns.
    """
    rows = []
    for _, row in df.iterrows():
        ind = {}
        # Try indicators_json first (full feature set)
        if pd.notna(row.get("indicators_json")):
            try:
                ind = json.loads(row["indicators_json"])
            except Exception:
                pass

        # Build feature vector — use DB columns as fallback
        vec = [
            float(ind.get("rsi",             50)),
            float(ind.get("macd_hist",         0)),
            float(ind.get("ema9",              0)),
            float(ind.get("ema20",             0)),
            float(ind.get("ema50",             0)),
            float(ind.get("ema200",            0)),
            float(ind.get("volume_ratio",      1)),
            float(ind.get("bb_position",     0.5)),
            float(ind.get("atr",               0)),
            float(ind.get("adx",               0)),
            float(ind.get("pct_from_52w_high", 0)),
            float(ind.get("pct_from_52w_low",  0)),
            float(row.get("analyst_score",    50)),
            float(row.get("bull_score",       50)),
            float(row.get("bear_score",       50)),
            float(row.get("curvature",         0)),
            float(ind.get("gex_net",           0)),
            float(ind.get("iv_skew",           0)),
            float(ind.get("dark_pool_conviction", 0)),
            float(ind.get("news_score",        0)),
        ]
        rows.append(vec)
    return np.array(rows, dtype=float)



def _build_arka_feature_matrix(df: pd.DataFrame) -> np.ndarray:
    """
    Build feature matrix from arka_trades table.
    These are richer intraday features with post-loss reversal context.
    Feature order must be kept consistent with ARKA_FEATURE_NAMES below.
    """
    GEX_REGIME_MAP  = {"POSITIVE_GAMMA": 1, "NEGATIVE_GAMMA": -1, "LOW_VOL": 0}
    REGIME_CALL_MAP = {"SHORT_THE_POPS": -1, "FOLLOW_MOMENTUM": 1, "BUY_THE_DIPS": 0, "NEUTRAL": 0}
    FLOW_BIAS_MAP   = {"STRONG_BULLISH": 2, "BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1, "STRONG_BEARISH": -2}
    SESSION_MAP     = {"MORNING": 1, "MIDDAY": 0, "POWER_HOUR": 2, "LUNCH": -1}
    DIRECTION_MAP   = {"CALL": 1, "PUT": -1}

    rows = []
    for _, row in df.iterrows():
        vec = [
            float(row.get("conviction",      55)),
            float(row.get("threshold",       55)),
            float(row.get("conviction", 55)) - float(row.get("threshold", 55)),  # margin above threshold
            float(GEX_REGIME_MAP.get(row.get("gex_regime", ""),  0)),
            float(REGIME_CALL_MAP.get(row.get("gex_regime_call", ""), 0)),
            float(row.get("gex_bias_ratio",  1.0)),
            float(row.get("gex_near_zero",   0)),
            float(FLOW_BIAS_MAP.get(row.get("flow_bias", "NEUTRAL"), 0)),
            float(row.get("flow_confidence", 0)),
            float(row.get("flow_is_extreme", 0)),
            float(row.get("rsi",             50)),
            float(row.get("vwap_above",       0)),
            float(row.get("volume_ratio",     1.0)),
            float(row.get("ema_aligned",      0)),
            float(SESSION_MAP.get(row.get("session", ""), 0)),
            float(DIRECTION_MAP.get(row.get("direction", "CALL"), 1)),
            # ── POST-LOSS REVERSAL FEATURES — what ARJUN needs to learn ──
            float(row.get("was_post_loss",      0)),
            float(row.get("is_reversal_trade",  0)),
            float(row.get("prior_loss_pnl",     0)),  # $ size of prior loss (negative = loss)
            # Interaction: reversal with strong flow is the best signal
            float(row.get("is_reversal_trade", 0)) * float(row.get("flow_is_extreme", 0)),
            float(row.get("is_reversal_trade", 0)) * float(row.get("conviction", 55)),
        ]
        rows.append(vec)
    return np.array(rows, dtype=float)

ARKA_FEATURE_NAMES = [
    "conviction", "threshold", "conviction_margin",
    "gex_regime", "gex_regime_call", "gex_bias_ratio", "gex_near_zero",
    "flow_bias", "flow_confidence", "flow_is_extreme",
    "rsi", "vwap_above", "volume_ratio", "ema_aligned", "session", "direction",
    "was_post_loss", "is_reversal_trade", "prior_loss_pnl",
    "reversal_x_flow", "reversal_x_conviction",
]


def retrain_arka_model():
    """
    Train a dedicated intraday scalp model on ARKA trade outcomes.
    This model learns:
      1. Which conviction+GEX+flow combinations win intraday
      2. Whether post-loss reversal trades succeed (key learning)
      3. Session-specific patterns

    Runs in addition to (not replacing) the ARJUN morning signal model.
    Saves to arka_scalp_model.json.
    """
    import sys as _sys
    _sys.path.insert(0, str(BASE))
    from xgboost import XGBClassifier

    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            'SELECT * FROM arka_trades WHERE date > date("now", "-60 days") AND outcome IS NOT NULL',
            conn
        )
    except Exception as e:
        print(f"  arka_trades table not ready: {e}")
        conn.close()
        return None
    conn.close()

    if len(df) < 15:
        print(f"  Not enough ARKA trades to retrain ({len(df)}, need 15+)")
        return None

    df["label"] = (df["outcome"] == "WIN").astype(int)
    X = _build_arka_feature_matrix(df)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    y = df["label"].values

    model = XGBClassifier(
        n_estimators  = 150,
        max_depth     = 4,
        learning_rate = 0.05,
        subsample     = 0.8,
        eval_metric   = "logloss",
    )
    model.fit(X, y)
    win_rate = df["label"].mean()

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MODEL_DIR / "arka_scalp_model.json"
    model.save_model(str(out_path))

    # Feature importance — identify what actually matters
    fi = dict(zip(ARKA_FEATURE_NAMES, model.feature_importances_))
    top_features = sorted(fi.items(), key=lambda x: -x[1])[:8]

    # Reversal-specific stats
    rev_df = df[df["is_reversal_trade"] == 1]
    same_df = df[(df["was_post_loss"] == 1) & (df["is_reversal_trade"] == 0)]
    print(f"\n  === ARKA Scalp Model Results ===")
    print(f"  Trades: {len(df)} | Win rate: {win_rate:.1%}")
    if len(rev_df) > 0:
        print(f"  Post-loss REVERSAL trades: {len(rev_df)} | Win rate: {rev_df['label'].mean():.1%}")
    if len(same_df) > 0:
        print(f"  Post-loss SAME-DIR trades: {len(same_df)} | Win rate: {same_df['label'].mean():.1%}")
    print(f"  Top features: {[f[0] for f in top_features[:5]]}")
    print(f"  Saved: {out_path}")

    return {
        "n_trades":     len(df),
        "win_rate":     float(win_rate),
        "top_features": top_features,
        "reversal_wr":  float(rev_df["label"].mean()) if len(rev_df) > 0 else None,
        "sameDir_wr":   float(same_df["label"].mean()) if len(same_df) > 0 else None,
    }


def retrain_model():
    """
    Retrain XGBoost on 30-day performance data.
    Uses Isomap manifold features when enough data exists (>=30 rows),
    otherwise falls back to agent score columns only.
    Also runs ARKA intraday model retrain.
    """
    import sys as _sys
    _sys.path.insert(0, str(BASE))

    from xgboost import XGBClassifier

    conn   = sqlite3.connect(DB_PATH)
    recent = pd.read_sql_query('SELECT * FROM signals WHERE date > date("now", "-30 days") AND outcome IS NOT NULL', conn)
    conn.close()

    if len(recent) < 10:
        print(f"Not enough data to retrain ({len(recent)} signals, need 10+)."); return

    recent["label"] = (recent["outcome"] == "WIN").astype(int)
    y = recent["label"].values

    use_manifold = len(recent) >= 30

    if use_manifold:
        try:
            from backend.arjun.modules.manifold_features import extract_manifold_features
            X_raw  = _build_feature_matrix(recent)
            X_raw  = np.nan_to_num(X_raw, nan=0.0, posinf=0.0, neginf=0.0)
            n_comp = min(5, X_raw.shape[1] - 1)
            X      = extract_manifold_features(X_raw, n_components=n_comp)
            method = f"Isomap ({n_comp} manifold features from {X_raw.shape[1]} raw)"
            print(f"  Using manifold features: {method}")
        except Exception as e:
            print(f"  Manifold failed ({e}), falling back to agent scores")
            X      = recent[["analyst_score", "bull_score", "bear_score"]].values
            method = "agent scores (manifold fallback)"
    else:
        X      = recent[["analyst_score", "bull_score", "bear_score"]].values
        method = f"agent scores ({len(recent)} signals, need 30 for manifold)"
        print(f"  Using {method}")

    model = XGBClassifier(n_estimators=100, max_depth=4, learning_rate=0.1,
                          eval_metric="logloss")
    model.fit(X, y)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import date
    out_path = MODEL_DIR / f"xgboost_retrained_{date.today()}.json"
    model.save_model(str(out_path))
    model.save_model(str(MODEL_DIR / "xgboost_model_updated.json"))

    win_rate = recent["label"].mean()
    print(f"✅ Model retrained — {len(recent)} signals | Win rate: {win_rate:.1%} | Method: {method}")
    print(f"   Saved: {out_path}")

    # ── Also retrain the ARKA intraday scalp model ─────────────────────
    print("\n--- ARKA Intraday Scalp Model ---")
    arka_result = retrain_arka_model()

    if DISCORD_WEBHOOK:
        arka_str = ""
        if arka_result:
            rev_str  = f" | Reversal WR: {arka_result['reversal_wr']:.1%}" if arka_result.get("reversal_wr") else ""
            arka_str = (f"\n📊 **ARKA Intraday Model**: {arka_result['n_trades']} trades | "
                        f"WR: {arka_result['win_rate']:.1%}{rev_str}")
        msg = (f"🔄 **ARJUN Weekly Retrain Complete**\n"
               f"Signals: {len(recent)} | Win Rate: {win_rate:.1%}\n"
               f"Method: {method}\n"
               f"Model: {out_path.name}{arka_str}")
        requests.post(DISCORD_WEBHOOK, json={"content": msg})


# META_LEARNING_WIRED (Mastermind Session 4)
try:
    from backend.arjun.meta_learning import run_grid_search as _meta_gs
    if "closed_pnls" in dir() and len(closed_pnls) >= 5:
        _meta_gs(closed_pnls)
except Exception:
    pass

if __name__ == "__main__":
    print("=== ARJUN Weekly Retrain ===")
    analyze_signal_performance()
    check_performance_degradation()
    retrain_model()

# META_LEARNING_WIRED (Mastermind Session 4)
try:
    from backend.arjun.meta_learning import run_grid_search as _meta_gs
    if "closed_pnls" in dir() and len(closed_pnls) >= 5:
        _meta_gs(closed_pnls)
except Exception:
    pass
