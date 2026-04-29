import pandas as pd
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '../../..'))
from backend.app.indicators.engine import IndicatorEngine

def test_indicators():
    print("=" * 45)
    print("  INDICATOR ENGINE TEST")
    print("=" * 45)

    # Load SPY daily data
    daily = pd.read_csv("data/historical_daily.csv")
    spy   = daily[daily["ticker"] == "SPY"].copy()

    print(f"\n📊 Testing on SPY — {len(spy)} bars loaded")

    # Run indicator engine
    engine = IndicatorEngine()
    spy_with_indicators = engine.compute_all(spy)

    # Check all columns were created
    expected_cols = [
        "ema_9", "ema_21", "ema_50", "ema_200",
        "macd", "macd_signal", "macd_hist",
        "rsi", "stoch_k", "stoch_d",
        "bb_upper", "bb_lower", "bb_pct",
        "atr", "adx", "obv", "mfi",
        "volume_ratio", "golden_cross",
        "trend", "returns_1d"
    ]

    print("\n🔍 Checking indicator columns:")
    all_good = True
    for col in expected_cols:
        if col in spy_with_indicators.columns:
            val = spy_with_indicators[col].iloc[-1]
            print(f"  ✅ {col:<20} = {round(float(val), 4) if col != 'trend' else val}")
        else:
            print(f"  ❌ {col} — MISSING")
            all_good = False

    # Get summary
    print("\n📋 Latest SPY Indicator Summary:")
    print("-" * 45)
    summary = engine.get_summary(spy_with_indicators)
    for key, val in summary.items():
        print(f"  {key:<25} = {val}")

    print("\n" + "=" * 45)
    if all_good:
        print("  ✅ All indicators computing correctly!")
    else:
        print("  ❌ Some indicators missing — check errors above")
    print("=" * 45)

test_indicators()
