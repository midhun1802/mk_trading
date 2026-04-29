"""
ARKA — Model Training (v3 — leakage-free)
Trains two XGBoost models:
  1. Conviction Score  — probability of meaningful price move up in 15 min
  2. Fakeout Filter    — probability current move is a fakeout

Run from ~/trading-ai:
    python3 backend/arka/train_arka.py
"""

import pandas as pd
import numpy as np
import os
import json
import pickle
from datetime import datetime

from sklearn.metrics import (
    accuracy_score, roc_auc_score,
    precision_score, recall_score, f1_score
)
import xgboost as xgb

INPUT_FILE   = "data/arka_features.csv"
MODEL_DIR    = "models/arka"
RESULTS_FILE = "models/arka/training_results.json"

os.makedirs(MODEL_DIR, exist_ok=True)

# ── feature columns ───────────────────────────────────────────────────────────
# raw_bull_score / raw_bear_score are EXCLUDED — they would cause leakage
# because the label was computed using similar logic

CONVICTION_FEATURES = [
    "rsi14", "rsi3", "rsi3_slope", "rsi_overbought", "rsi_oversold",
    "rsi_bullish", "rsi_bearish", "rsi3_bullish",
    "macd_hist", "macd_line", "macd_sig",
    "macd_bullish", "macd_cross_up", "macd_cross_dn",
    "above_vwap", "vwap_dist_pct", "vwap_reclaim", "vwap_lose",
    "above_ema9", "above_ema20", "ema_bullish_stack", "ema_bearish_stack",
    "pct_b", "bb_width", "bb_upper_touch", "bb_lower_touch", "bb_squeeze",
    "above_orb_high", "below_orb_low", "inside_orb",
    "dist_orb_high", "dist_orb_low",
    "vol_ratio", "vol_surge", "vol_dry",
    "price_mom5", "price_mom15", "price_mom30",
    "is_open_30min", "is_lunch", "is_power_hour", "is_close_30min",
    "minutes_to_close", "day_of_week",
    "atr14",
]

FAKEOUT_FEATURES = [
    "wick_ratio_upper", "wick_ratio_lower", "wick_ratio_total",
    "vol_ratio", "low_vol_breakout", "low_vol_breakdown", "vol_surge", "vol_dry",
    "vwap_dist_pct", "vwap_extended",
    "above_orb_high", "below_orb_low", "inside_orb",
    "failed_breakout", "failed_breakdown",
    "opening_trap", "lunch_trap", "is_open_30min", "is_lunch",
    "momentum_divergence",
    "rsi14", "rsi_overbought", "rsi_oversold",
    "atr14",
    "minutes_to_close", "day_of_week",
]

# ── training ──────────────────────────────────────────────────────────────────

def train_model(X_train, y_train, X_val, y_val, label, is_imbalanced=False):
    pos  = y_train.sum()
    neg  = len(y_train) - pos
    spw  = neg / max(pos, 1) if is_imbalanced else 1.0

    params = dict(
        n_estimators=600,
        max_depth=4,
        learning_rate=0.03,
        subsample=0.75,
        colsample_bytree=0.75,
        min_child_weight=20,
        reg_alpha=0.5,
        reg_lambda=2.0,
        scale_pos_weight=spw,
        eval_metric="auc",
        early_stopping_rounds=40,
        random_state=42,
        n_jobs=-1,
        verbosity=0,
    )

    print(f"\n  Training {label}...")
    print(f"    Train: {len(X_train):,}  |  Val: {len(X_val):,}")
    print(f"    Positive rate  train={y_train.mean()*100:.1f}%  val={y_val.mean()*100:.1f}%")
    if is_imbalanced:
        print(f"    scale_pos_weight = {spw:.1f}x")

    model = xgb.XGBClassifier(**params)
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)

    prob = model.predict_proba(X_val)[:, 1]
    pred = (prob >= 0.5).astype(int)
    acc  = accuracy_score(y_val, pred)
    auc  = roc_auc_score(y_val, prob)
    prec = precision_score(y_val, pred, zero_division=0)
    rec  = recall_score(y_val, pred, zero_division=0)
    f1   = f1_score(y_val, pred, zero_division=0)

    print(f"\n    ── Val Results ──")
    print(f"    Accuracy  : {acc*100:.2f}%")
    print(f"    AUC-ROC   : {auc:.4f}")
    print(f"    Precision : {prec*100:.2f}%")
    print(f"    Recall    : {rec*100:.2f}%")
    print(f"    F1        : {f1:.4f}")

    metrics = dict(
        accuracy=round(acc,4), auc=round(auc,4),
        precision=round(prec,4), recall=round(rec,4), f1=round(f1,4),
        pos_rate_train=round(float(y_train.mean()),4),
        pos_rate_val=round(float(y_val.mean()),4),
    )
    return model, metrics


def top_features(model, names, n=15):
    pairs = sorted(zip(names, model.feature_importances_), key=lambda x: x[1], reverse=True)
    return {k: round(float(v), 4) for k, v in pairs[:n]}


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*55)
    print("  ARKA — MODEL TRAINING  (v3 leakage-free)")
    print("="*55)

    print(f"\n📂 Loading {INPUT_FILE}...")
    df = pd.read_csv(INPUT_FILE)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values(["ticker", "timestamp"]).reset_index(drop=True)
    print(f"   {len(df):,} rows")

    all_results = {}

    for ticker in ["SPY", "QQQ"]:
        print(f"\n{'='*55}")
        print(f"  {ticker}")
        print(f"{'='*55}")

        grp = df[df["ticker"] == ticker].copy().reset_index(drop=True)

        # ── time-based split ──
        split_idx = int(len(grp) * 0.80)
        train = grp.iloc[:split_idx]
        val   = grp.iloc[split_idx:]
        print(f"\n  Train: {train['timestamp'].min().date()} → {train['timestamp'].max().date()}  ({len(train):,})")
        print(f"  Val  : {val['timestamp'].min().date()} → {val['timestamp'].max().date()}  ({len(val):,})")

        ticker_results = {}

        # ════════════════════════════════════════
        # MODEL 1 — Conviction Score
        # ════════════════════════════════════════
        print(f"\n{'─'*40}")
        print(f"  MODEL 1: Conviction Score")
        print(f"{'─'*40}")

        # Label: forward 15-bar return > 0  (strictly price-only, no leakage)
        # Filter: only bars with vol_ratio > 0.5 and outside lunch
        #         These are SESSION filters, not signal features
        c = grp["close"]
        fwd = c.shift(-15) / c - 1

        # keep bars that have non-trivial volume and aren't in the lunch dead zone
        active = (grp["vol_ratio"] > 0.5) & (grp["is_lunch"] == 0)
        grp_c  = grp[active].copy()
        grp_c["label_c"] = (fwd[active] > 0).astype(int)

        # drop the last 15 rows per day (NaN forward returns near close)
        grp_c = grp_c.dropna(subset=["label_c"])

        split_c = int(len(grp_c) * 0.80)
        tr_c = grp_c.iloc[:split_c]
        va_c = grp_c.iloc[split_c:]

        print(f"\n  Active bars (vol>0.5, not lunch): {len(grp_c):,}  ({len(grp_c)/len(grp)*100:.0f}%)")
        print(f"  Label positive rate: {grp_c['label_c'].mean()*100:.1f}%")

        cf = [f for f in CONVICTION_FEATURES if f in grp_c.columns]
        model_c, met_c = train_model(
            tr_c[cf].fillna(0), tr_c["label_c"],
            va_c[cf].fillna(0), va_c["label_c"],
            label=f"{ticker} Conviction",
        )
        tf_c = top_features(model_c, cf)
        print(f"\n    Top features: {list(tf_c.keys())[:5]}")

        path_c = os.path.join(MODEL_DIR, f"arka_conviction_{ticker.lower()}.pkl")
        with open(path_c, "wb") as fh:
            pickle.dump({"model": model_c, "features": cf}, fh)
        print(f"    💾 Saved → {path_c}")
        ticker_results["conviction"] = {**met_c, "top_features": tf_c}

        # ════════════════════════════════════════
        # MODEL 2 — Fakeout Filter
        # ════════════════════════════════════════
        print(f"\n{'─'*40}")
        print(f"  MODEL 2: Fakeout Filter")
        print(f"{'─'*40}")

        ff = [f for f in FAKEOUT_FEATURES if f in grp.columns]
        model_f, met_f = train_model(
            train[ff].fillna(0), train["label_fakeout"],
            val[ff].fillna(0),   val["label_fakeout"],
            label=f"{ticker} Fakeout",
            is_imbalanced=True,
        )
        tf_f = top_features(model_f, ff)
        print(f"\n    Top features: {list(tf_f.keys())[:5]}")

        path_f = os.path.join(MODEL_DIR, f"arka_fakeout_{ticker.lower()}.pkl")
        with open(path_f, "wb") as fh:
            pickle.dump({"model": model_f, "features": ff}, fh)
        print(f"    💾 Saved → {path_f}")
        ticker_results["fakeout"] = {**met_f, "top_features": tf_f}

        all_results[ticker] = ticker_results

    # ── save results ──
    out = {
        "trained_at": datetime.now().isoformat(),
        "version": "v3-leakage-free",
        "tickers": all_results,
    }
    with open(RESULTS_FILE, "w") as fh:
        json.dump(out, fh, indent=2)

    # ── summary ──
    print("\n\n" + "="*55)
    print("  ARKA TRAINING COMPLETE")
    print("="*55)
    for t, r in all_results.items():
        print(f"\n  {t}")
        print(f"    Conviction  acc={r['conviction']['accuracy']*100:.1f}%  auc={r['conviction']['auc']:.3f}")
        print(f"    Fakeout     acc={r['fakeout']['accuracy']*100:.1f}%  auc={r['fakeout']['auc']:.3f}")

    print(f"\n  📊 Results  → {RESULTS_FILE}")
    print(f"  📦 Models   → {MODEL_DIR}/\n")

    print("  Target thresholds:")
    print("    Conviction AUC > 0.54  →  real edge")
    print("    Fakeout    AUC > 0.70  →  reliable blocker")
    print("\n  Live rule:")
    print("    conviction_prob >= 0.55 AND fakeout_prob < 0.45  →  ENTER")
    print("    anything else                                     →  STAY FLAT")


if __name__ == "__main__":
    main()
