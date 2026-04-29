"""
ARKA — Feature Engineer
Converts raw 1-minute OHLCV bars into George-inspired features for
the Conviction Score model and Fakeout Filter model.

Run from ~/trading-ai:
    python3 backend/arka/feature_engineer.py
"""

import pandas as pd
import numpy as np
import os

INPUT_FILE  = "data/arka_minute_combined_fresh.csv"
OUTPUT_FILE = "data/arka_features.csv"

# ── helpers ───────────────────────────────────────────────────────────────────

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1/n, adjust=False).mean()
    avg_l = loss.ewm(alpha=1/n, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def macd(close: pd.Series, fast=12, slow=26, signal=9):
    macd_line   = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram

def bollinger(close: pd.Series, n=20, k=2):
    mid  = close.rolling(n).mean()
    std  = close.rolling(n).std()
    upper = mid + k * std
    lower = mid - k * std
    pct_b = (close - lower) / (upper - lower + 1e-9)
    bw    = (upper - lower) / (mid + 1e-9)
    return mid, upper, lower, pct_b, bw

# ── per-day opening range ─────────────────────────────────────────────────────

def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Compute intraday VWAP reset each day."""
    df = df.copy()
    df["date"] = df["timestamp"].dt.date
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tp_vol"] = df["typical_price"] * df["volume"]
    df["cum_tp_vol"] = df.groupby(["ticker","date"])["tp_vol"].cumsum()
    df["cum_vol"]    = df.groupby(["ticker","date"])["volume"].cumsum()
    df["vwap"] = df["cum_tp_vol"] / (df["cum_vol"] + 1e-9)
    df = df.drop(columns=["typical_price","tp_vol","cum_tp_vol","cum_vol"])
    return df

def add_orb(df: pd.DataFrame, orb_minutes: int = 15) -> pd.DataFrame:
    """Opening Range Breakout — first N minutes high/low as key levels."""
    df = df.copy()
    df["date"] = df["timestamp"].dt.date

    orb_open  = df["timestamp"].dt.time <= pd.Timestamp(f"09:{str(9+orb_minutes//60).zfill(2)}:{str(orb_minutes%60).zfill(2)}").time()
    orb_df    = df[orb_open].groupby(["ticker", "date"]).agg(
        orb_high=("high", "max"),
        orb_low =("low",  "min"),
    ).reset_index()

    df = df.merge(orb_df, on=["ticker", "date"], how="left")
    df["orb_high"] = df.groupby(["ticker","date"])["orb_high"].ffill()
    df["orb_low"]  = df.groupby(["ticker","date"])["orb_low"].ffill()

    df["above_orb_high"] = (df["close"] > df["orb_high"]).astype(int)
    df["below_orb_low"]  = (df["close"] < df["orb_low"]).astype(int)
    df["inside_orb"]     = (
        (df["close"] <= df["orb_high"]) & (df["close"] >= df["orb_low"])
    ).astype(int)
    df["orb_range"]      = df["orb_high"] - df["orb_low"]

    # distance from ORB boundaries normalised by ATR
    df["dist_orb_high"]  = (df["close"] - df["orb_high"]) / (df["atr14"] + 1e-9)
    df["dist_orb_low"]   = (df["close"] - df["orb_low"])  / (df["atr14"] + 1e-9)
    return df

# ── session time features ─────────────────────────────────────────────────────

def session_features(df: pd.DataFrame) -> pd.DataFrame:
    t = df["timestamp"]
    minutes_since_open = (t.dt.hour - 9) * 60 + t.dt.minute - 30

    df["is_open_30min"]   = (minutes_since_open <= 30).astype(int)
    df["is_lunch"]        = (
        (t.dt.hour == 11) & (t.dt.minute >= 30) |
        (t.dt.hour == 12) |
        (t.dt.hour == 13) & (t.dt.minute < 30)
    ).astype(int)
    df["is_power_hour"]   = (
        (t.dt.hour == 14) & (t.dt.minute >= 30) |
        (t.dt.hour == 15)
    ).astype(int)
    df["is_close_30min"]  = (
        (t.dt.hour == 15) & (t.dt.minute >= 30)
    ).astype(int)
    df["minutes_to_close"]= ((15 * 60 + 58) - minutes_since_open).clip(0, 390)
    df["day_of_week"]     = t.dt.dayofweek   # 0=Mon … 4=Fri
    return df

# ── fakeout detection features (George's core logic) ─────────────────────────

def fakeout_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    George blocks trades when fakeout confidence ≥ 60%.
    These features train the fakeout model.
    """
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # Rejection wick ratio — large upper/lower wick vs body = reversal signal
    body          = (c - df["open"]).abs()
    upper_wick    = h - c.clip(upper=h)
    lower_wick    = c.clip(lower=l) - l
    df["wick_ratio_upper"] = upper_wick / (body + 1e-9)
    df["wick_ratio_lower"] = lower_wick / (body + 1e-9)
    df["wick_ratio_total"] = (upper_wick + lower_wick) / (body + 1e-9)

    # Volume suspicion — low volume breakout = likely fakeout
    vol_ma20 = v.rolling(20).mean()
    df["vol_ratio"]        = v / (vol_ma20 + 1e-9)
    df["low_vol_breakout"] = (
        (df["above_orb_high"] == 1) & (df["vol_ratio"] < 0.8)
    ).astype(int)
    df["low_vol_breakdown"]= (
        (df["below_orb_low"] == 1) & (df["vol_ratio"] < 0.8)
    ).astype(int)

    # VWAP extension — price far from VWAP = extended, prone to snap back
    df["vwap_dist_pct"]    = (c - df["vwap"]) / (df["vwap"] + 1e-9) * 100
    df["vwap_extended"]    = (df["vwap_dist_pct"].abs() > 0.5).astype(int)

    # Quick reversal — close back inside ORB after breakout
    prev_above = df["above_orb_high"].shift(1)
    prev_below = df["below_orb_low"].shift(1)
    df["failed_breakout"]  = (
        (prev_above == 1) & (df["inside_orb"] == 1)
    ).astype(int)
    df["failed_breakdown"] = (
        (prev_below == 1) & (df["inside_orb"] == 1)
    ).astype(int)

    # Time-based trap flags (George explicitly calls these out)
    df["opening_trap"]     = (df["is_open_30min"] & (df["wick_ratio_total"] > 2)).astype(int)
    df["lunch_trap"]       = (df["is_lunch"] & (df["vol_ratio"] < 0.6)).astype(int)

    # Price action momentum check — 3-bar vs 10-bar momentum divergence
    mom3  = c - c.shift(3)
    mom10 = c - c.shift(10)
    df["momentum_divergence"] = (
        ((mom3 > 0) & (mom10 < 0)) | ((mom3 < 0) & (mom10 > 0))
    ).astype(int)

    return df

# ── conviction score features (George's discomfort score) ────────────────────

def conviction_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    George's discomfort score — how many signals align toward a direction.
    These features train the conviction model.
    """
    c = df["close"]

    # RSI signals
    df["rsi_bullish"]    = (df["rsi14"] > 50).astype(int)
    df["rsi_bearish"]    = (df["rsi14"] < 50).astype(int)
    df["rsi_overbought"] = (df["rsi14"] > 70).astype(int)
    df["rsi_oversold"]   = (df["rsi14"] < 30).astype(int)
    df["rsi3_bullish"]   = (df["rsi3"]  > 50).astype(int)
    df["rsi3_slope"]     = df["rsi3"].diff(3)

    # MACD signals
    df["macd_bullish"]   = (df["macd_hist"] > 0).astype(int)
    df["macd_cross_up"]  = (
        (df["macd_hist"] > 0) & (df["macd_hist"].shift(1) <= 0)
    ).astype(int)
    df["macd_cross_dn"]  = (
        (df["macd_hist"] < 0) & (df["macd_hist"].shift(1) >= 0)
    ).astype(int)

    # VWAP signals (most important intraday level)
    df["above_vwap"]     = (c > df["vwap"]).astype(int)
    df["vwap_reclaim"]   = (
        (c > df["vwap"]) & (c.shift(1) <= df["vwap"])
    ).astype(int)
    df["vwap_lose"]      = (
        (c < df["vwap"]) & (c.shift(1) >= df["vwap"])
    ).astype(int)

    # EMA stack (trend direction)
    df["above_ema9"]     = (c > df["ema9"]).astype(int)
    df["above_ema20"]    = (c > df["ema20"]).astype(int)
    df["ema_bullish_stack"] = (
        (df["ema9"] > df["ema20"]) & (c > df["ema9"])
    ).astype(int)
    df["ema_bearish_stack"] = (
        (df["ema9"] < df["ema20"]) & (c < df["ema9"])
    ).astype(int)

    # Bollinger Band position
    df["bb_upper_touch"] = (df["pct_b"] > 0.95).astype(int)
    df["bb_lower_touch"] = (df["pct_b"] < 0.05).astype(int)
    df["bb_squeeze"]     = (df["bb_width"] < df["bb_width"].rolling(20).mean() * 0.8).astype(int)

    # Volume confirmation
    df["vol_surge"]      = (df["vol_ratio"] > 1.5).astype(int)
    df["vol_dry"]        = (df["vol_ratio"] < 0.7).astype(int)

    # Price momentum
    df["price_mom5"]     = c.pct_change(5)
    df["price_mom15"]    = c.pct_change(15)
    df["price_mom30"]    = c.pct_change(30)

    # Conviction alignment score (raw count — used as feature AND sanity check)
    bull_signals = (
        df["rsi_bullish"] + df["macd_bullish"] + df["above_vwap"] +
        df["above_ema9"]  + df["ema_bullish_stack"] + df["rsi3_bullish"] +
        df["above_orb_high"]
    )
    bear_signals = (
        df["rsi_bearish"] + (1 - df["macd_bullish"]) + (1 - df["above_vwap"]) +
        (1 - df["above_ema9"]) + df["ema_bearish_stack"] + (1 - df["rsi3_bullish"]) +
        df["below_orb_low"]
    )
    df["raw_bull_score"] = bull_signals / 7.0 * 100
    df["raw_bear_score"] = bear_signals / 7.0 * 100

    return df

# ── labelling ─────────────────────────────────────────────────────────────────

def add_labels(df: pd.DataFrame, forward_bars: int = 15) -> pd.DataFrame:
    """
    Conviction label:  is price higher 15 minutes from now?
                       Binary up/down — naturally ~50% base rate.

    Fakeout label:     did price reverse direction within 10 bars?
                       Captures failed momentum moves broadly.
    """
    c = df["close"]

    # ── Conviction label ──────────────────────────────────────────────────────
    # Simple: is price higher 15 minutes from now?
    # Gives ~50% base rate naturally — model has real work to do
    fwd_ret = c.shift(-forward_bars) / c - 1
    df["label_conviction"] = (fwd_ret > 0).astype(int)

    # ── Fakeout label ─────────────────────────────────────────────────────────
    # Price went one direction for 5 bars then reversed by bar 10
    fwd5  = c.shift(-5)  / c - 1
    fwd10 = c.shift(-10) / c - 1

    up_then_down  = (fwd5 >  0.001) & (fwd10 < 0)
    down_then_up  = (fwd5 < -0.001) & (fwd10 > 0)

    # Low-volume ORB breaks are classic fakeouts regardless of reversal
    low_vol_orb_break = (
        (df["above_orb_high"] | df["below_orb_low"]) &
        (df["vol_ratio"] < 0.85)
    )

    df["label_fakeout"] = (up_then_down | down_then_up | low_vol_orb_break).astype(int)

    return df

# ── main ──────────────────────────────────────────────────────────────────────

def engineer(df_raw: pd.DataFrame) -> pd.DataFrame:
    df = df_raw.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df = df.sort_values(["ticker", "timestamp"]).reset_index(drop=True)

    results = []
    for ticker, grp in df.groupby("ticker"):
        grp = grp.copy().reset_index(drop=True)
        c = grp["close"]
        h = grp["high"]
        l = grp["low"]
        v = grp["volume"]

        # ── core indicators ──
        grp["rsi14"]      = rsi(c, 14)
        grp["rsi3"]       = rsi(c, 3)
        grp["ema9"]       = ema(c, 9)
        grp["ema20"]      = ema(c, 20)
        grp["ema50"]      = ema(c, 50)
        grp["atr14"]      = atr(h, l, c, 14)

        macd_l, macd_s, macd_h = macd(c)
        grp["macd_line"]  = macd_l
        grp["macd_sig"]   = macd_s
        grp["macd_hist"]  = macd_h

        mid, upper, lower, pct_b, bw = bollinger(c)
        grp["bb_mid"]     = mid
        grp["bb_upper"]   = upper
        grp["bb_lower"]   = lower
        grp["pct_b"]      = pct_b
        grp["bb_width"]   = bw

        # session features first (needed by fakeout/conviction)
        grp = session_features(grp)

        # ORB needs atr14 to exist first
        grp = add_vwap(grp)
        grp = add_orb(grp, orb_minutes=15)

        # George's two score families
        grp = fakeout_features(grp)
        grp = conviction_features(grp)

        # labels
        grp = add_labels(grp, forward_bars=15)

        results.append(grp)
        print(f"  ✅ {ticker:>4}  features built — {len(grp):,} rows")

    out = pd.concat(results, ignore_index=True)
    return out


if __name__ == "__main__":
    print("\n" + "="*55)
    print("  ARKA — FEATURE ENGINEERING")
    print("="*55)

    print(f"\n📂 Loading {INPUT_FILE}...")
    df_raw = pd.read_csv(INPUT_FILE)
    print(f"   {len(df_raw):,} raw bars loaded")

    print("\n⚙️  Building features...")
    df_feat = engineer(df_raw)

    # drop rows with NaN labels or key features (lookback warmup)
    before = len(df_feat)
    df_feat = df_feat.dropna(subset=["label_conviction", "label_fakeout", "rsi14", "macd_hist"])
    print(f"\n🧹 Dropped {before - len(df_feat):,} warmup/NaN rows")
    print(f"   Final dataset: {len(df_feat):,} rows")

    df_feat.to_csv(OUTPUT_FILE, index=False)
    print(f"\n💾 Saved → {OUTPUT_FILE}")

    # quick sanity check
    print("\n📊 Label distribution:")
    for ticker, grp in df_feat.groupby("ticker"):
        cv = grp["label_conviction"].mean() * 100
        fk = grp["label_fakeout"].mean() * 100
        print(f"   {ticker}  conviction_bullish={cv:.1f}%  fakeout_rate={fk:.1f}%")

    print("\n✅ Feature engineering complete — ready to train ARKA!")
