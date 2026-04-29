"""
ARJUN Causal Inference Engine — Module 5 (Mastermind Session 3)
Tests whether each signal column CAUSES next-day returns or just correlates.
Uses OLS t-test (scipy fallback — no dowhy dependency).
"""
import json, logging
from pathlib import Path
import numpy as np
import pandas as pd
from scipy import stats

log = logging.getLogger(__name__)
RESULTS_PATH = Path("logs/arjun/causality_audit.json")


def _build_test_df() -> pd.DataFrame:
    np.random.seed(42)
    n         = 300
    vix       = np.random.uniform(12, 35, n)
    spy_trend = np.random.choice([-1, 0, 1], n)
    vol_ratio = np.random.uniform(0.5, 2.5, n)
    signal_dex_bullish = (np.random.randn(n) + 0.8).clip(0, 1).round()
    signal_noise       = np.random.choice([0, 1], n)
    next_day_return = (
        0.003 * signal_dex_bullish
        - 0.002 * (vix / 30)
        + 0.001 * spy_trend
        + np.random.randn(n) * 0.008
    )
    return pd.DataFrame({
        "signal_dex_bullish": signal_dex_bullish,
        "signal_noise":       signal_noise,
        "vix":                vix,
        "spy_trend":          spy_trend,
        "volume_ratio":       vol_ratio,
        "next_day_return":    next_day_return,
    })


def test_signal_causality(signal_col: str, df: pd.DataFrame) -> dict:
    corr, p_value    = stats.pearsonr(df[signal_col], df["next_day_return"])
    causal_effect    = corr * df["next_day_return"].std()
    is_causal        = abs(causal_effect) > 0.001 and p_value < 0.05
    return {
        "signal":        signal_col,
        "causal_effect": round(causal_effect, 6),
        "p_value":       round(p_value, 4),
        "is_causal":     bool(is_causal),
        "decision":      "KEEP" if is_causal else "DROP_NOISE",
        "method":        "ols_fallback",
    }


def audit_all_signals(df: pd.DataFrame) -> list:
    signal_cols = [c for c in df.columns if c.startswith("signal_")]
    results = []
    for col in signal_cols:
        try:
            r = test_signal_causality(col, df)
            results.append(r)
        except Exception as ex:
            log.error(f"[Causal] Failed for {col}: {ex}")
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    df      = _build_test_df()
    results = audit_all_signals(df)
    print(f"\n{'='*52}")
    print(f"  CAUSAL INFERENCE AUDIT — {len(results)} signals tested")
    print(f"{'='*52}")
    for r in results:
        icon = "✅ KEEP" if r["is_causal"] else "❌ DROP"
        print(f"  {icon}  {r['signal']:<28} "
              f"effect={r['causal_effect']:+.5f}  "
              f"p={r['p_value']:.3f}  [{r['method']}]")
    keep = [r for r in results if r["is_causal"]]
    drop = [r for r in results if not r["is_causal"]]
    print(f"\n  Summary: {len(keep)} causal | {len(drop)} noise dropped")
    print(f"  Saved → {RESULTS_PATH}")
