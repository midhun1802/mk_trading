import pandas as pd
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '../../..'))

from backend.app.indicators.engine import IndicatorEngine
from backend.app.models.train import TradingModel
from backend.app.explainer.signal_explainer import SignalExplainer

class SignalGenerator:

    def __init__(self):
        self.engine    = IndicatorEngine()
        self.explainer = SignalExplainer()
        self.models    = {}

    def load_models(self, tickers: list):
        """Load saved models for each ticker"""
        for ticker in tickers:
            try:
                model = TradingModel()
                model.load(ticker)
                self.models[ticker] = model
            except FileNotFoundError:
                print(f"⚠️  No model found for {ticker} — skipping")

    def generate(self, ticker: str, df: pd.DataFrame) -> dict:
        """
        Full pipeline for one ticker:
        1. Compute indicators
        2. Run AI model → signal
        3. Get Claude explanation
        4. Return complete result
        """
        if ticker not in self.models:
            return None

        # Step 1 — Indicators
        df_indicators = self.engine.compute_all(df)
        summary       = self.engine.get_summary(df_indicators)

        # Step 2 — AI Signal
        model  = self.models[ticker]
        signal = model.predict(df_indicators)

        # Step 3 — Claude Explanation
        result = self.explainer.explain(
            ticker     = ticker,
            signal     = signal,
            indicators = summary
        )

        return result

    def generate_all(self, daily_df: pd.DataFrame) -> list:
        """
        Run full pipeline on all loaded tickers.
        Returns list of signals sorted by confidence.
        """
        results = []

        for ticker in self.models.keys():
            print(f"  📊 Generating signal for {ticker}...")
            df = daily_df[daily_df["ticker"] == ticker].copy()

            if len(df) < 200:
                print(f"     ⚠️  Not enough data for {ticker}")
                continue

            result = self.generate(ticker, df)
            if result:
                results.append(result)

        # Sort by confidence — highest first
        results.sort(
            key=lambda x: x["confidence"],
            reverse=True
        )

        return results

