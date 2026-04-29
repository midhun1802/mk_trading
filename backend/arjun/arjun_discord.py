"""
ARJUN Discord Notifier
Posts to the #arjun-alerts channel.

Message types:
  - Morning brief (daily at 8:05am ET — top 3 conviction trades)
  - Trade conviction alert (when ARKA enters with ARJUN confidence >= 70%)
  - Agent disagreement warning (when bull vs bear spread >= 40 pts)
  - Regime call for the day
"""
import os
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

ARJUN_WEBHOOK = os.getenv("DISCORD_ARJUN_ALERTS", "")


def _send(payload: dict) -> bool:
    if not ARJUN_WEBHOOK:
        print("[ARJUN Discord] No DISCORD_ARJUN_ALERTS webhook — skipping")
        return False
    try:
        r = requests.post(ARJUN_WEBHOOK, json=payload, timeout=8)
        if r.status_code not in (200, 204):
            print(f"[ARJUN Discord] Error: {r.status_code} {r.text[:120]}")
            return False
        return True
    except Exception as e:
        print(f"[ARJUN Discord] Send failed: {e}")
        return False


def _et_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def _regime_emoji(regime: str) -> str:
    r = (regime or "").upper()
    if "NEGATIVE" in r: return "🔴"
    if "POSITIVE" in r: return "🟢"
    return "🟡"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def post_trade_conviction(signal: dict, arka_entry: dict) -> bool:
    """
    Post when ARKA enters a trade that ARJUN rated >= 70% confidence.
    Called from arka_engine after ARJUN agreement check.
    """
    ticker     = signal.get("ticker", "?")
    direction  = arka_entry.get("direction", "CALL").upper()
    conviction = int(arka_entry.get("conviction", 50))
    confidence = float(signal.get("confidence", 0))
    bull_score = int(signal.get("agents", {}).get("bull", {}).get("score", 50))
    bear_score = int(signal.get("agents", {}).get("bear", {}).get("score", 50))
    risk_dec   = signal.get("agents", {}).get("risk_manager", {}).get("decision", "APPROVE")
    gex_regime = signal.get("gex", {}).get("regime", "UNKNOWN")
    strike     = arka_entry.get("strike", "—")
    qty        = int(arka_entry.get("qty", 1))
    is_call    = direction == "CALL"
    color      = 0x00FF88 if is_call else 0xFF4444
    emoji      = "🟢" if is_call else "🔴"
    regime_col = _regime_emoji(gex_regime)

    dominant   = "Bull" if bull_score >= bear_score else "Bear"
    dominated  = bear_score if dominant == "Bull" else bull_score
    dominator  = bull_score if dominant == "Bull" else bear_score

    embed = {
        "title": f"🧠 ARJUN TRADE CONVICTION — {ticker} {direction}",
        "color": color,
        "description": (
            f"{emoji} **{dominant} agent dominant** ({dominator} vs {dominated}). "
            f"Risk Manager: **{risk_dec}**.\n"
            f"{regime_col} GEX regime: **{gex_regime}**. "
            f"Conviction: **{conviction}%**.\n"
            f"ARKA entering **{qty}x {ticker} 0DTE {direction}** @ **{strike}** strike."
        ),
        "fields": [
            {"name": "🧠 ARJUN Confidence", "value": f"{confidence:.1f}%",  "inline": True},
            {"name": "⚡ ARKA Conviction",   "value": f"{conviction}/100",   "inline": True},
            {"name": "📈 Bull Score",         "value": str(bull_score),       "inline": True},
            {"name": "📉 Bear Score",         "value": str(bear_score),       "inline": True},
            {"name": f"{regime_col} GEX",     "value": gex_regime,            "inline": True},
            {"name": "⚖️ Risk Decision",      "value": risk_dec,              "inline": True},
        ],
        "footer": {
            "text": f"ARJUN Multi-Agent System • Confidence: {confidence:.1f}% • {_et_now().strftime('%I:%M %p ET')}"
        },
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    return _send({"embeds": [embed]})


def post_agent_disagreement(signal: dict) -> bool:
    """
    Post when bull and bear agents strongly disagree (spread >= 40 pts).
    This is a warning — ARKA may not fire but traders should know.
    """
    ticker     = signal.get("ticker", "?")
    bull_score = int(signal.get("agents", {}).get("bull", {}).get("score", 50))
    bear_score = int(signal.get("agents", {}).get("bear", {}).get("score", 50))
    spread     = abs(bull_score - bear_score)
    if spread < 40:
        return False   # not worth posting

    dominant   = "Bull" if bull_score > bear_score else "Bear"
    price      = float(signal.get("price", 0))
    confidence = float(signal.get("confidence", 50))

    embed = {
        "title": f"⚠️ ARJUN AGENT CONFLICT — {ticker}",
        "color": 0xFF9900,
        "description": (
            f"**{dominant} agent dominant** by {spread} points.\n"
            f"High internal disagreement — trade with extra caution.\n"
            f"Price: **${price:.2f}** | Confidence: **{confidence:.1f}%**"
        ),
        "fields": [
            {"name": "📈 Bull Score", "value": str(bull_score), "inline": True},
            {"name": "📉 Bear Score", "value": str(bear_score), "inline": True},
            {"name": "📊 Spread",     "value": f"{spread} pts", "inline": True},
        ],
        "footer": {"text": f"ARJUN Multi-Agent System • {_et_now().strftime('%I:%M %p ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    return _send({"embeds": [embed]})


def post_regime_call(regime_call: str, gex_regime: str, ticker: str = "SPY") -> bool:
    """Post ARJUN's regime call for the day."""
    emoji_map = {
        "SHORT_THE_POPS":   "🔴 SHORT THE POPS",
        "BUY_THE_DIPS":     "🟢 BUY THE DIPS",
        "FOLLOW_MOMENTUM":  "🟡 FOLLOW MOMENTUM",
    }
    label  = emoji_map.get(regime_call, f"🟡 {regime_call}")
    color  = 0xFF4444 if "SHORT" in regime_call else 0x00FF88 if "BUY" in regime_call else 0xFFAA00

    embed = {
        "title": f"🎯 ARJUN REGIME CALL — {ticker}",
        "color": color,
        "description": f"Today's playbook: **{label}**\nGEX regime: **{gex_regime}**",
        "footer": {"text": f"ARJUN Multi-Agent System • {_et_now().strftime('%b %d, %Y')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    return _send({"embeds": [embed]})


def post_morning_brief(signals: list, regime_call: str = "", gex_regime: str = "",
                       neural_pulse: int = 50, risk_mode: str = "NORMAL") -> bool:
    """
    Post ARJUN's morning brief at 8:05am ET.
    signals: list of signal dicts from coordinator, sorted by confidence desc.
    """
    today     = _et_now().strftime("%B %d, %Y")
    top3      = sorted(
        [s for s in signals if s.get("signal") in ("BUY", "SELL")],
        key=lambda x: float(x.get("confidence", 0)),
        reverse=True
    )[:3]

    # Build top conviction trades text
    trade_lines = []
    for i, sig in enumerate(top3, 1):
        direction  = "BULLISH" if sig["signal"] == "BUY" else "BEARISH"
        ticker     = sig["ticker"]
        conf       = float(sig.get("confidence", 0))
        bull_sc    = sig.get("agents", {}).get("bull", {}).get("score", "—")
        bear_sc    = sig.get("agents", {}).get("bear", {}).get("score", "—")
        arrow      = "🟢" if sig["signal"] == "BUY" else "🔴"
        trade_lines.append(
            f"{i}. {arrow} **{ticker}** {direction} — {conf:.1f}% ({bull_sc} vs {bear_sc})"
        )

    trades_text = "\n".join(trade_lines) if trade_lines else "No high-conviction setups today."

    # Regime
    regime_map = {
        "SHORT_THE_POPS":  "SHORT THE POPS 🔴",
        "BUY_THE_DIPS":    "BUY THE DIPS 🟢",
        "FOLLOW_MOMENTUM": "FOLLOW MOMENTUM 🟡",
    }
    regime_label = regime_map.get(regime_call, regime_call or "ANALYZING")
    gex_col      = _regime_emoji(gex_regime)

    color = 0x00FF88 if "BUY" in regime_call else 0xFF4444 if "SHORT" in regime_call else 0xFFAA00

    embed = {
        "title": f"📊 ARJUN MORNING BRIEF — {today}",
        "color": color,
        "description": (
            f"**Regime:** {regime_label} | **Neural Pulse:** {neural_pulse}/100\n\n"
            f"**Top conviction trades:**\n{trades_text}\n\n"
            f"{gex_col} GEX today: **{gex_regime}**\n"
            f"⚙️ Risk mode: **{risk_mode}**"
        ),
        "footer": {"text": "ARJUN Multi-Agent System • Daily Brief"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    return _send({"embeds": [embed]})


def post_arka_arjun_boost(ticker: str, direction: str, conviction_before: int,
                          conviction_after: int, arjun_confidence: float) -> bool:
    """Post when ARJUN boosts an ARKA trade conviction."""
    is_call = direction.upper() in ("CALL", "LONG", "BUY")
    color   = 0x00FF88 if is_call else 0xFF4444
    boost   = conviction_after - conviction_before

    embed = {
        "title": f"⚡ ARJUN BOOSTS ARKA — {ticker} {direction.upper()}",
        "color": color,
        "description": (
            f"ARJUN confirms ARKA's direction with **{arjun_confidence:.1f}%** confidence.\n"
            f"Conviction boosted: **{conviction_before}** → **{conviction_after}** (+{boost} pts)"
        ),
        "footer": {"text": f"ARJUN Multi-Agent System • {_et_now().strftime('%I:%M %p ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    return _send({"embeds": [embed]})


if __name__ == "__main__":
    # Quick smoke test
    print("Testing ARJUN Discord notifier...")
    ok = post_morning_brief(
        signals=[
            {"ticker": "SPY", "signal": "BUY",  "confidence": "74.5",
             "agents": {"bull": {"score": 72}, "bear": {"score": 31}}},
            {"ticker": "QQQ", "signal": "SELL", "confidence": "68.0",
             "agents": {"bull": {"score": 28}, "bear": {"score": 69}}},
        ],
        regime_call="SHORT_THE_POPS",
        gex_regime="POSITIVE_GAMMA",
        neural_pulse=62,
        risk_mode="DEFENSIVE",
    )
    print(f"Morning brief: {'✅ sent' if ok else '❌ failed (check DISCORD_ARJUN_ALERTS in .env)'}")
