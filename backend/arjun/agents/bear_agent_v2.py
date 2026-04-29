"""
ARJUN Adversarial Bear Agent v2 — Devil's Advocate
Point-by-point invalidation of Bull Agent reasons.
Checks: EMA fakeout age, OPEX noise, falling knife RSI,
        low-volume breakout, gamma flip proximity, regime conflict.
"""
import json, logging
from pathlib import Path

log = logging.getLogger(__name__)


class AdversarialBearV2:

    def challenge(self, bull_reasons: list, market_data: dict) -> dict:
        """
        For each bull reason, generate a specific counter-argument.
        Returns challenges list + total_penalty applied to bear_score.
        """
        challenges   = []
        total_penalty = 0

        for reason in bull_reasons:
            r = reason.lower()

            # ── EMA bull stack / cross ────────────────────────────────────
            if "ema" in r and ("bull" in r or "cross" in r or "stack" in r):
                ema_age = market_data.get("ema_cross_bars_ago", 99)
                if ema_age <= 3:
                    challenges.append({
                        "bull_reason": reason,
                        "counter":     f"EMA cross only {ema_age} bar(s) old — high fakeout risk",
                        "penalty":     -15,
                        "confidence":  0.72,
                    })
                    total_penalty -= 15

            # ── Volume surge ──────────────────────────────────────────────
            if "volume" in r and ("surge" in r or "spike" in r or "high" in r):
                days_to_opex = market_data.get("days_to_opex", 99)
                if days_to_opex <= 2:
                    challenges.append({
                        "bull_reason": reason,
                        "counter":     "Volume surge on OPEX week — likely expiry noise, not accumulation",
                        "penalty":     -10,
                        "confidence":  0.65,
                    })
                    total_penalty -= 10

            # ── RSI oversold ──────────────────────────────────────────────
            if "rsi" in r and ("oversold" in r or "low" in r):
                price_vs_200 = market_data.get("price_vs_200ema", "ABOVE")
                if price_vs_200 == "BELOW":
                    challenges.append({
                        "bull_reason": reason,
                        "counter":     "Oversold RSI in structural downtrend — falling knife, not a bounce",
                        "penalty":     -12,
                        "confidence":  0.78,
                    })
                    total_penalty -= 12

            # ── Breakout / ORB ────────────────────────────────────────────
            if "breakout" in r or "orb" in r:
                vol_ratio = market_data.get("volume_ratio", 1.0)
                if vol_ratio < 1.2:
                    challenges.append({
                        "bull_reason": reason,
                        "counter":     f"Breakout on low volume (ratio={vol_ratio:.2f}) — likely false breakout",
                        "penalty":     -8,
                        "confidence":  0.60,
                    })
                    total_penalty -= 8

            # ── Gamma / GEX bull ─────────────────────────────────────────
            if "gex" in r or "gamma" in r:
                gex_flip = market_data.get("gex_flip_distance_pct", 99)
                if gex_flip < 0.5:
                    challenges.append({
                        "bull_reason": reason,
                        "counter":     f"GEX flip zone only {gex_flip:.2f}% away — dealer pinning risk",
                        "penalty":     -10,
                        "confidence":  0.70,
                    })
                    total_penalty -= 10

            # ── VIX regime conflict ───────────────────────────────────────
            if "trend" in r or "momentum" in r:
                vix = market_data.get("vix", 20)
                hmm_regime = market_data.get("hmm_regime", "LOW_VOL_TREND")
                if vix > 25 and hmm_regime in ("CRISIS", "HIGH_VOL_TREND"):
                    challenges.append({
                        "bull_reason": reason,
                        "counter":     f"Trend signal in VIX={vix:.1f} crisis regime — mean reversion bias",
                        "penalty":     -12,
                        "confidence":  0.75,
                    })
                    total_penalty -= 12

            # ── Hurst below 0.5 (mean-reverting) ─────────────────────────
            if "hurst" in r or "trend" in r:
                hurst = market_data.get("hurst", 0.5)
                if hurst < 0.45:
                    challenges.append({
                        "bull_reason": reason,
                        "counter":     f"Hurst={hurst:.2f} — market is mean-reverting, not trending",
                        "penalty":     -8,
                        "confidence":  0.68,
                    })
                    total_penalty -= 8

        result = {
            "challenges":    challenges,
            "total_penalty": total_penalty,
            "challenge_count": len(challenges),
            "verdict": (
                "HIGH_RISK"   if total_penalty <= -30 else
                "CAUTION"     if total_penalty <= -15 else
                "MILD_RISK"   if total_penalty < 0   else
                "CLEAR"
            ),
        }
        log.info(f"[BearV2] {len(challenges)} challenges | penalty={total_penalty} | {result['verdict']}")
        return result


# ── Singleton ──────────────────────────────────────────────────────────────
_instance = None

def get_bear_v2() -> AdversarialBearV2:
    global _instance
    if _instance is None:
        _instance = AdversarialBearV2()
    return _instance


if __name__ == "__main__":
    bear = AdversarialBearV2()

    bull_reasons = [
        "EMA bull stack formed — 8/21/50 aligned",
        "Volume surge detected — 1.8x average",
        "RSI oversold at 28 — bounce expected",
        "Breakout above ORB high confirmed",
    ]

    market_data = {
        "ema_cross_bars_ago":    2,       # ← fresh cross
        "days_to_opex":          1,       # ← OPEX tomorrow
        "price_vs_200ema":      "BELOW",  # ← downtrend
        "volume_ratio":          1.05,    # ← weak volume
        "gex_flip_distance_pct": 0.3,
        "vix":                   27,
        "hmm_regime":           "CRISIS",
        "hurst":                 0.42,
    }

    result = bear.challenge(bull_reasons, market_data)

    print(f"\n{'='*55}")
    print(f"  ADVERSARIAL BEAR v2 — {result['verdict']}")
    print(f"  Challenges: {result['challenge_count']}  |  Total Penalty: {result['total_penalty']} pts")
    print(f"{'='*55}")
    for i, c in enumerate(result["challenges"], 1):
        print(f"\n  [{i}] BULL: {c['bull_reason']}")
        print(f"      BEAR: {c['counter']}")
        print(f"      Penalty: {c['penalty']} pts | Confidence: {c['confidence']:.0%}")
