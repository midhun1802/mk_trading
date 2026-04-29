import pandas as pd
import numpy as np
import ta
import warnings
warnings.filterwarnings("ignore")

class IndicatorEngine:

    def compute_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute all technical indicators on OHLCV dataframe.
        Input:  raw OHLCV dataframe
        Output: same dataframe with indicator columns added
        """
        df = df.copy()
        df = df.sort_values("timestamp").reset_index(drop=True)

        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]

        # ── Trend Indicators ──────────────────────────────
        df["ema_9"]   = ta.trend.EMAIndicator(close, window=9).ema_indicator()
        df["ema_21"]  = ta.trend.EMAIndicator(close, window=21).ema_indicator()
        df["ema_50"]  = ta.trend.EMAIndicator(close, window=50).ema_indicator()
        df["ema_200"] = ta.trend.EMAIndicator(close, window=200).ema_indicator()
        df["sma_20"]  = ta.trend.SMAIndicator(close, window=20).sma_indicator()

        # MACD
        macd_obj         = ta.trend.MACD(close)
        df["macd"]       = macd_obj.macd()
        df["macd_signal"]= macd_obj.macd_signal()
        df["macd_hist"]  = macd_obj.macd_diff()

        # ADX (trend strength)
        adx_obj    = ta.trend.ADXIndicator(high, low, close)
        df["adx"]  = adx_obj.adx()
        df["adx_pos"] = adx_obj.adx_pos()
        df["adx_neg"] = adx_obj.adx_neg()

        # ── Momentum Indicators ───────────────────────────
        df["rsi"] = ta.momentum.RSIIndicator(close, window=14).rsi()

        stoch_obj      = ta.momentum.StochasticOscillator(high, low, close)
        df["stoch_k"]  = stoch_obj.stoch()
        df["stoch_d"]  = stoch_obj.stoch_signal()

        df["roc"] = ta.momentum.ROCIndicator(close, window=10).roc()

        # ── Volatility Indicators ─────────────────────────
        bb_obj         = ta.volatility.BollingerBands(close, window=20)
        df["bb_upper"] = bb_obj.bollinger_hband()
        df["bb_mid"]   = bb_obj.bollinger_mavg()
        df["bb_lower"] = bb_obj.bollinger_lband()
        df["bb_pct"]   = bb_obj.bollinger_pband()
        df["bb_width"] = bb_obj.bollinger_wband()

        df["atr"] = ta.volatility.AverageTrueRange(high, low, close).average_true_range()

        # ── Volume Indicators ─────────────────────────────
        df["obv"]        = ta.volume.OnBalanceVolumeIndicator(close, volume).on_balance_volume()
        df["volume_sma"] = ta.trend.SMAIndicator(volume.astype(float), window=20).sma_indicator()
        df["volume_ratio"] = volume / df["volume_sma"]

        # MFI (Money Flow Index) — combines price + volume
        df["mfi"] = ta.volume.MFIIndicator(high, low, close, volume, window=14).money_flow_index()

        # ── Price Action Features ─────────────────────────
        df["returns_1d"]  = close.pct_change(1)
        df["returns_3d"]  = close.pct_change(3)
        df["returns_5d"]  = close.pct_change(5)
        df["returns_10d"] = close.pct_change(10)

        df["high_low_pct"] = (high - low) / close          # daily range as % of price
        df["close_position"] = (close - low) / (high - low + 1e-10)  # where close sits in day's range

        # 52 week high/low position
        df["52w_high"] = high.rolling(252).max()
        df["52w_low"]  = low.rolling(252).min()
        df["pct_from_52w_high"] = (close - df["52w_high"]) / df["52w_high"]
        df["pct_from_52w_low"]  = (close - df["52w_low"])  / df["52w_low"]

        # ── Pattern Signals (0 or 1) ──────────────────────
        df["golden_cross"] = (
            (df["ema_9"] > df["ema_21"]) &
            (df["ema_9"].shift(1) <= df["ema_21"].shift(1))
        ).astype(int)

        df["death_cross"] = (
            (df["ema_9"] < df["ema_21"]) &
            (df["ema_9"].shift(1) >= df["ema_21"].shift(1))
        ).astype(int)

        df["macd_bull_cross"] = (
            (df["macd"] > df["macd_signal"]) &
            (df["macd"].shift(1) <= df["macd_signal"].shift(1))
        ).astype(int)

        df["macd_bear_cross"] = (
            (df["macd"] < df["macd_signal"]) &
            (df["macd"].shift(1) >= df["macd_signal"].shift(1))
        ).astype(int)

        df["rsi_oversold"]   = (df["rsi"] < 30).astype(int)
        df["rsi_overbought"] = (df["rsi"] > 70).astype(int)
        df["volume_surge"]   = (df["volume_ratio"] > 1.5).astype(int)

        # ── Trend Classification ──────────────────────────
        df["trend"] = np.where(
            df["ema_9"] > df["ema_21"],
            np.where(df["ema_21"] > df["ema_50"], "strong_uptrend", "weak_uptrend"),
            np.where(df["ema_21"] < df["ema_50"], "strong_downtrend", "weak_downtrend")
        )

        return df

    def get_summary(self, df: pd.DataFrame) -> dict:
        """
        Returns clean summary of latest indicator values.
        This gets passed to Claude for trade explanations.
        """
        row  = df.iloc[-1]
        prev = df.iloc[-2]

        return {
            # Price
            "price":          round(float(row["close"]), 2),
            "price_change_1d": round(float(row["returns_1d"]) * 100, 2),

            # Momentum
            "rsi":            round(float(row["rsi"]), 1),
            "rsi_signal": (
                "oversold"   if row["rsi"] < 30 else
                "overbought" if row["rsi"] > 70 else
                "neutral"
            ),
            "macd_trend":     "bullish" if row["macd"] > row["macd_signal"] else "bearish",
            "macd_crossover": bool(row["macd_bull_cross"]),
            "stoch_k":        round(float(row["stoch_k"]), 1),

            # Trend
            "trend":          str(row["trend"]),
            "adx":            round(float(row["adx"]), 1),
            "adx_strength": (
                "strong"  if row["adx"] > 25 else
                "weak"
            ),
            "above_ema50":    bool(row["close"] > row["ema_50"]),
            "above_ema200":   bool(row["close"] > row["ema_200"]),
            "golden_cross":   bool(row["golden_cross"]),
            "death_cross":    bool(row["death_cross"]),

            # Volatility
            "bb_position":    round(float(row["bb_pct"]), 2),
            "bb_squeeze":     bool(row["bb_width"] < df["bb_width"].quantile(0.2)),
            "atr":            round(float(row["atr"]), 2),

            # Volume
            "volume_ratio":   round(float(row["volume_ratio"]), 2),
            "volume_surge":   bool(row["volume_surge"]),
            "mfi":            round(float(row["mfi"]), 1),
            "obv_trend":      "rising" if row["obv"] > prev["obv"] else "falling",

            # Position
            "pct_from_52w_high": round(float(row["pct_from_52w_high"]) * 100, 1),
            "pct_from_52w_low":  round(float(row["pct_from_52w_low"])  * 100, 1),
        }

