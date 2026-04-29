import pandas as pd
import numpy as np
import sys
import os
import warnings
warnings.filterwarnings("ignore")

sys.path.append(os.path.join(os.path.dirname(__file__), '../../..'))

from backend.app.models.train import TradingModel

class BacktestEngine:

    def __init__(self, initial_capital: float = 100_000):
        self.initial_capital = initial_capital

    def run(
        self,
        df: pd.DataFrame,
        ticker: str,
        model: TradingModel,
        commission: float = 0.001,
        slippage:   float = 0.001,
        confidence_threshold: float = 0.60
    ) -> dict:

        print(f"\n  Running backtest on {ticker}...")

        df = model.build_features(df.copy())
        df = df.dropna().reset_index(drop=True)

        if len(df) < 100:
            return None

        X        = df[model.feature_cols].values
        X_scaled = model.scaler.transform(X)
        probs    = model.model.predict_proba(X_scaled)

        df["bull_prob"] = probs[:, 1]
        df["bear_prob"] = probs[:, 0]

        # LONG ONLY — BUY on bull signal, EXIT on bear signal
        df["signal"] = np.where(
            df["bull_prob"] >= confidence_threshold, "BUY",
            np.where(df["bear_prob"] >= confidence_threshold, "EXIT", "HOLD")
        )

        capital      = self.initial_capital
        shares       = 0
        entry_price  = 0.0
        entry_date   = None
        trades       = []
        equity_curve = []

        for _, row in df.iterrows():
            price     = float(row["close"])
            signal    = row["signal"]
            timestamp = row["timestamp"]

            # Portfolio value
            portfolio_value = capital + (shares * price)
            equity_curve.append({
                "date":  timestamp,
                "value": portfolio_value
            })

            # ── BUY ──────────────────────────────────
            if signal == "BUY" and shares == 0 and capital > 100:
                buy_price  = price * (1 + slippage)
                n          = int((capital * 0.95) / buy_price)
                cost       = n * buy_price * (1 + commission)
                if n > 0 and cost <= capital:
                    capital    -= cost
                    shares      = n
                    entry_price = buy_price
                    entry_date  = timestamp

            # ── EXIT ─────────────────────────────────
            elif signal == "EXIT" and shares > 0:
                sell_price = price * (1 - slippage)
                proceeds   = shares * sell_price * (1 - commission)
                pnl        = proceeds - (shares * entry_price)
                pnl_pct    = (sell_price - entry_price) / entry_price * 100
                days_held  = (pd.Timestamp(timestamp) - pd.Timestamp(entry_date)).days

                capital += proceeds
                trades.append({
                    "ticker":      ticker,
                    "entry_price": round(entry_price, 2),
                    "exit_price":  round(sell_price, 2),
                    "shares":      shares,
                    "pnl":         round(pnl, 2),
                    "pnl_pct":     round(pnl_pct, 2),
                    "entry_date":  entry_date,
                    "exit_date":   timestamp,
                    "days_held":   days_held
                })
                shares      = 0
                entry_price = 0.0
                entry_date  = None

        # Close any open position at end
        if shares > 0:
            final_price = float(df["close"].iloc[-1])
            proceeds    = shares * final_price * (1 - commission)
            pnl         = proceeds - (shares * entry_price)
            pnl_pct     = (final_price - entry_price) / entry_price * 100
            capital    += proceeds
            trades.append({
                "ticker":      ticker,
                "entry_price": round(entry_price, 2),
                "exit_price":  round(final_price, 2),
                "shares":      shares,
                "pnl":         round(pnl, 2),
                "pnl_pct":     round(pnl_pct, 2),
                "entry_date":  entry_date,
                "exit_date":   df["timestamp"].iloc[-1],
                "days_held":   0
            })

        # ── Statistics ────────────────────────────────
        equity_df    = pd.DataFrame(equity_curve)
        trades_df    = pd.DataFrame(trades) if trades else pd.DataFrame()
        final_value  = capital
        total_return = (final_value - self.initial_capital) / self.initial_capital * 100
        bh_return    = (float(df["close"].iloc[-1]) - float(df["close"].iloc[0])) \
                       / float(df["close"].iloc[0]) * 100

        # Drawdown
        vals        = equity_df["value"].values.astype(float)
        rolling_max = np.maximum.accumulate(vals)
        drawdowns   = (vals - rolling_max) / rolling_max * 100
        max_dd      = float(np.min(drawdowns))

        # Trade stats
        if not trades_df.empty:
            winning       = trades_df[trades_df["pnl"] > 0]
            losing        = trades_df[trades_df["pnl"] <= 0]
            win_rate      = len(winning) / len(trades_df) * 100
            avg_win       = winning["pnl_pct"].mean() if not winning.empty else 0
            avg_loss      = losing["pnl_pct"].mean()  if not losing.empty  else 0
            gross_win     = winning["pnl"].sum()
            gross_loss    = abs(losing["pnl"].sum())
            profit_factor = gross_win / gross_loss if gross_loss > 0 else 0
        else:
            win_rate = avg_win = avg_loss = profit_factor = 0

        # Sharpe
        equity_df["ret"] = equity_df["value"].pct_change()
        std = equity_df["ret"].std()
        sharpe = (equity_df["ret"].mean() / std * np.sqrt(252)) if std > 0 else 0

        return {
            "ticker":          ticker,
            "total_return":    round(total_return, 2),
            "buy_hold_return": round(bh_return, 2),
            "alpha":           round(total_return - bh_return, 2),
            "sharpe_ratio":    round(float(sharpe), 2),
            "max_drawdown":    round(max_dd, 2),
            "total_trades":    len(trades_df),
            "win_rate":        round(win_rate, 1),
            "avg_win_pct":     round(avg_win, 2),
            "avg_loss_pct":    round(avg_loss, 2),
            "profit_factor":   round(profit_factor, 2),
            "final_value":     round(final_value, 2),
            "equity_curve":    equity_df,
            "trades":          trades_df
        }
