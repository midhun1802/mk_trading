"""
CHAKRA Signal Analyst Agent
Takes ResearchReport, applies multi-factor scoring with memory,
generates typed TradeSignal objects.
"""
import os, logging
from datetime import datetime

log = logging.getLogger("CHAKRA.Analyst")

# Scoring weights — CHAKRA calibrated
WEIGHTS = {
    "gex_proximity":  0.30,
    "rsi_momentum":   0.25,
    "manifold_score": 0.20,
    "news_sentiment": 0.15,
    "options_flow":   0.10,
}


def score_ticker(snap: dict, report: dict) -> dict:
    """Multi-factor scoring for one ticker. Returns bull/bear scores and reasons."""
    bull_score = 0.0
    bear_score = 0.0
    reasons    = []

    rsi         = snap.get("rsi_14", 50)
    above_vwap  = snap.get("above_vwap", False)
    chg_pct     = snap.get("change_pct", 0)
    regime_call = snap.get("regime_call","NEUTRAL")
    flow_dir    = snap.get("flow_dir","NEUTRAL")
    flow_conf   = snap.get("flow_conf", 0)
    dp_pct      = snap.get("dark_pool_pct", 0)

    # GEX scoring (30% weight)
    if regime_call == "BUY_THE_DIPS":
        bull_score += 30
        reasons.append("GEX: BUY_THE_DIPS — dealer support")
    elif regime_call == "SHORT_THE_POPS":
        bear_score += 30
        reasons.append("GEX: SHORT_THE_POPS — dealer resistance")
    elif regime_call == "FOLLOW_MOMENTUM":
        if chg_pct > 0:
            bull_score += 20
        else:
            bear_score += 20
        reasons.append(f"GEX: FOLLOW_MOMENTUM ({chg_pct:+.1f}%)")

    # RSI scoring (25% weight)
    if rsi < 30:
        bull_score += 25
        reasons.append(f"RSI deeply oversold ({rsi:.0f})")
    elif rsi < 40:
        bull_score += 15
        reasons.append(f"RSI oversold ({rsi:.0f})")
    elif rsi > 70:
        bear_score += 25
        reasons.append(f"RSI overbought ({rsi:.0f})")
    elif rsi > 60:
        bear_score += 15
        reasons.append(f"RSI elevated ({rsi:.0f})")

    # VWAP scoring
    if above_vwap:
        bull_score += 15
        reasons.append("Above VWAP — bullish bias")
    else:
        bear_score += 15
        reasons.append("Below VWAP — bearish bias")

    # Momentum scoring
    if chg_pct > 1.0:
        bull_score += 20
        reasons.append(f"Strong momentum +{chg_pct:.1f}%")
    elif chg_pct > 0.3:
        bull_score += 10
    elif chg_pct < -1.0:
        bear_score += 20
        reasons.append(f"Strong selloff {chg_pct:.1f}%")
    elif chg_pct < -0.3:
        bear_score += 10

    # Flow scoring (10% weight)
    if flow_dir in ("BULLISH","CALL") and flow_conf >= 65:
        bull_score += 15
        reasons.append(f"Dark pool bullish ({dp_pct:.0f}%)")
    elif flow_dir in ("BEARISH","PUT") and flow_conf >= 65:
        bear_score += 15
        reasons.append(f"Dark pool bearish ({dp_pct:.0f}%)")

    # VIX risk flag
    vix = report.get("vix", 20)
    if vix > 25:
        bull_score *= 0.85
        bear_score *= 0.85
        reasons.append(f"VIX {vix:.1f} — reducing conviction")

    return {
        "bull_score": round(min(100, bull_score), 1),
        "bear_score": round(min(100, bear_score), 1),
        "reasons":    reasons,
    }


async def analyst_node(state: dict) -> dict:
    """Analyst Agent node. Scores each ticker and generates TradeSignals."""
    from backend.arjun.schemas.trade_signal import TradeSignal
    from backend.arjun.memory.signal_memory import query_similar, store_signal

    report  = state.get("research_report",{})
    tickers = report.get("tickers",[])
    signals = []

    log.info(f"📊 Analyst Agent: scoring {len(tickers)} tickers")

    for snap in tickers:
        ticker = snap.get("ticker","?")
        try:
            scores = score_ticker(snap, report)
            bull   = scores["bull_score"]
            bear   = scores["bear_score"]

            # Query memory for similar past setups
            past = query_similar(
                ticker    = ticker,
                regime    = snap.get("gex_regime","UNKNOWN"),
                rsi       = snap.get("rsi_14", 50),
                direction = "BULLISH" if bull > bear else "BEARISH",
                n_results = 5
            )

            # Memory boost/penalty
            mem_boost   = 0
            mem_context = ""
            if past:
                wins     = [p for p in past if p.get("result")=="WIN"]
                win_rate = len(wins)/len(past)*100
                avg_pnl  = sum(p.get("pnl_pct",0) for p in past)/len(past)
                mem_context = (f"{len(past)} similar trades: "
                               f"{win_rate:.0f}% wins, avg {avg_pnl:+.1f}%")
                if win_rate >= 70 and len(past) >= 3:
                    mem_boost = 8
                    log.info(f"  🧠 {ticker}: memory boost +8 ({win_rate:.0f}% hist win rate)")
                elif win_rate <= 30 and len(past) >= 3:
                    mem_boost = -10
                    log.info(f"  🧠 {ticker}: memory penalty -10 ({win_rate:.0f}% hist win rate)")

            if bull >= bear:
                bull = min(100, bull + mem_boost)
            else:
                bear = min(100, bear + abs(mem_boost) if mem_boost < 0 else bear - mem_boost)

            # Determine action
            price = snap.get("price", 0)

            if bull >= 55 and bull > bear + 10:
                action      = "BUY"
                direction   = "LONG"
                contract    = "CALL"
                confidence  = round(bull/100, 2)
                conviction  = "HIGH" if bull >= 75 else "MED" if bull >= 60 else "LOW"
                stop_loss   = round(price * 0.97, 2)
                take_profit = round(price * 1.03, 2)
            elif bear >= 55 and bear > bull + 10:
                action      = "SELL"
                direction   = "SHORT"
                contract    = "PUT"
                confidence  = round(bear/100, 2)
                conviction  = "HIGH" if bear >= 75 else "MED" if bear >= 60 else "LOW"
                stop_loss   = round(price * 1.03, 2)
                take_profit = round(price * 0.97, 2)
            else:
                action      = "HOLD"
                direction   = "NONE"
                contract    = "NONE"
                confidence  = 0.45
                conviction  = "LOW"
                stop_loss   = 0
                take_profit = 0

            rationale = (
                f"{ticker}: Bull={bull:.0f} Bear={bear:.0f}. "
                f"{'; '.join(scores['reasons'][:3])}. "
                f"{mem_context}"
            )

            signal = TradeSignal(
                ticker            = ticker,
                action            = action,
                direction         = direction,
                confidence        = confidence,
                conviction        = conviction,
                entry_price       = price,
                stop_loss         = stop_loss,
                take_profit       = take_profit,
                contract_type     = contract,
                dte_preference    = 0 if ticker in ("SPY","QQQ","SPX") else 1,
                rationale         = rationale,
                indicators_used   = ["RSI","VWAP","GEX","FLOW","MEMORY"],
                bull_score        = bull,
                bear_score        = bear,
                gex_aligned       = snap.get("regime_call","") != "NEUTRAL",
                flow_confirmed    = snap.get("flow_conf",0) >= 65,
                similar_past_signals = past[:3],
            )

            signals.append(signal.model_dump())

            # Store signal in memory
            store_signal({
                "ticker":      ticker,
                "action":      action,
                "direction":   direction,
                "confidence":  confidence,
                "gex_regime":  snap.get("gex_regime","?"),
                "regime_call": snap.get("regime_call","?"),
                "rsi":         snap.get("rsi_14",50),
                "vwap_bias":   "ABOVE" if snap.get("above_vwap") else "BELOW",
                "bull_score":  bull,
                "bear_score":  bear,
                "rationale":   rationale,
            })

            log.info(f"  📈 {ticker}: {action} ({direction}) conf={confidence:.0%} "
                     f"bull={bull:.0f} bear={bear:.0f}")

        except Exception as e:
            log.error(f"  Analyst error for {ticker}: {e}")

    # Only pass MED/HIGH conviction actionable signals to executor
    actionable = [s for s in signals if s["action"] != "HOLD"
                  and s["confidence"] >= 0.55]

    state["trade_signals"] = actionable
    state["all_signals"]   = signals

    log.info(f"✅ Analyst complete: {len(actionable)}/{len(signals)} actionable")
    return state
