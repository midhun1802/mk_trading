import anthropic
import os
import json
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

class SignalExplainer:

    def __init__(self):
        self.client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY")
        )

    def explain(
        self,
        ticker: str,
        signal: dict,
        indicators: dict,
        macro: dict = None
    ) -> dict:
        """
        Takes AI signal + indicators and generates
        a full human-readable trade explanation.
        """

        # Calculate suggested entry/target/stop
        price     = indicators["price"]
        atr       = indicators["atr"]

        if signal["signal"] == "BUY":
            entry       = price
            target      = round(price + (atr * 2.5), 2)
            stop_loss   = round(price - (atr * 1.5), 2)
            risk        = round(price - stop_loss, 2)
            reward      = round(target - price, 2)
        elif signal["signal"] == "SELL":
            entry       = price
            target      = round(price - (atr * 2.5), 2)
            stop_loss   = round(price + (atr * 1.5), 2)
            risk        = round(stop_loss - price, 2)
            reward      = round(price - target, 2)
        else:
            entry       = price
            target      = price
            stop_loss   = price
            risk        = 0
            reward      = 0

        risk_reward = round(reward / risk, 2) if risk > 0 else 0

        # Build macro context string
        macro_context = ""
        if macro:
            macro_context = f"""
Macro Environment:
- Fed Funds Rate:   {macro.get('fed_rate', 'N/A')}%
- Yield Curve:      {macro.get('yield_curve', 'N/A')} (negative = inverted)
- Unemployment:     {macro.get('unemployment', 'N/A')}%
- Macro Risk Score: {macro.get('risk_score', 'N/A')}/6
"""

        # Build the prompt
        prompt = f"""
You are a professional trading analyst at a hedge fund.
The AI model has generated a {signal['signal']} signal for {ticker}.
Your job is to explain this trade clearly and concisely.

═══════════════════════════════════════
SIGNAL DATA
═══════════════════════════════════════
Ticker:          {ticker}
Signal:          {signal['signal']}
Confidence:      {signal['confidence']}%
Bull Probability: {signal['bull_prob']}%
Bear Probability: {signal['bear_prob']}%

═══════════════════════════════════════
CURRENT PRICE ACTION
═══════════════════════════════════════
Price:           ${indicators['price']}
1-Day Change:    {indicators['price_change_1d']}%
Trend:           {indicators['trend']}
52W High:        {indicators['pct_from_52w_high']}% away
52W Low:         {indicators['pct_from_52w_low']}% away

═══════════════════════════════════════
TECHNICAL INDICATORS
═══════════════════════════════════════
RSI:             {indicators['rsi']} ({indicators['rsi_signal']})
MACD:            {indicators['macd_trend']} {'(crossover!)' if indicators['macd_crossover'] else ''}
Stochastic K:    {indicators['stoch_k']}
ADX:             {indicators['adx']} ({indicators['adx_strength']} trend)
BB Position:     {indicators['bb_position']} (0=oversold, 1=overbought)
BB Squeeze:      {'Yes - breakout imminent' if indicators['bb_squeeze'] else 'No'}
ATR:             {indicators['atr']}

═══════════════════════════════════════
VOLUME & FLOW
═══════════════════════════════════════
Volume vs Avg:   {indicators['volume_ratio']}x {'(SURGE!)' if indicators['volume_surge'] else ''}
MFI:             {indicators['mfi']} (money flow)
OBV Trend:       {indicators['obv_trend']}

═══════════════════════════════════════
SUGGESTED TRADE LEVELS
═══════════════════════════════════════
Entry:           ${entry}
Target:          ${target}
Stop Loss:       ${stop_loss}
Risk:            ${risk}
Reward:          ${reward}
Risk/Reward:     1:{risk_reward}

{macro_context}

═══════════════════════════════════════
INSTRUCTIONS
═══════════════════════════════════════
Write a concise professional trade analysis in exactly this format:

**SIGNAL: {signal['signal']} {ticker} @ ${entry}**

**WHY THIS TRADE**
2-3 sentences explaining the key reasons for this signal based on the indicators above. Be specific — mention actual indicator values.

**MARKET CONTEXT**
1-2 sentences on what the broader trend and momentum say.

**TRADE PLAN**
- Entry:      ${entry}
- Target:     ${target} (+{round((reward/price)*100, 1)}%)
- Stop Loss:  ${stop_loss} (-{round((risk/price)*100, 1)}%)
- Risk/Reward: 1:{risk_reward}

**KEY RISKS**
2-3 bullet points of specific risks to watch.

**CONFIDENCE: {signal['confidence']}% — {'HIGH' if signal['confidence'] >= 65 else 'MEDIUM' if signal['confidence'] >= 55 else 'LOW'}**

Keep the entire response under 250 words. Be direct and professional.
No disclaimers. No "I" statements. Just the analysis.
"""

        # Call Claude API with retry logic for overload errors
        max_retries = 3
        retry_delay = 30  # seconds between retries

        for attempt in range(max_retries):
            try:
                message = self.client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=500,
                    messages=[{"role": "user", "content": prompt}]
                )
                break  # success — exit retry loop

            except anthropic.APIStatusError as e:
                if e.status_code == 529 and attempt < max_retries - 1:
                    wait = retry_delay * (attempt + 1)  # 30s, 60s, 90s
                    print(f"  ⚠️  Anthropic overloaded, waiting {wait}s before retry ({attempt + 1}/{max_retries - 1})...")
                    time.sleep(wait)
                else:
                    raise  # not overload error, or out of retries — give up

        explanation = message.content[0].text

        return {
            "ticker":       ticker,
            "signal":       signal["signal"],
            "confidence":   signal["confidence"],
            "price":        price,
            "entry":        entry,
            "target":       target,
            "stop_loss":    stop_loss,
            "risk":         risk,
            "reward":       reward,
            "risk_reward":  risk_reward,
            "explanation":  explanation,
            "timestamp":    datetime.now().isoformat(),
            "indicators":   indicators
        }

    def explain_batch(self, signals: list) -> list:
        """Generate explanations for multiple tickers at once"""
        results = []
        for item in signals:
            try:
                result = self.explain(
                    ticker     = item["ticker"],
                    signal     = item["signal"],
                    indicators = item["indicators"]
                )
                results.append(result)
            except Exception as e:
                print(f"❌ Failed to explain {item['ticker']}: {e}")
        return results
