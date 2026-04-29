"""
ARJUN Master Coordinator
Synthesizes Analyst + Bull + Bear + Risk Manager outputs into final signal.
Uses Claude API for natural language explanation generation.
"""
import json
import os
import httpx
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path


class _SafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.bool_,)):   return bool(obj)
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)):return float(obj)
        if isinstance(obj, (np.ndarray,)): return obj.tolist()
        return super().default(obj)

def _dumps(obj):
    return json.dumps(obj, indent=2, cls=_SafeEncoder)

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ── Session 3: IV Skew ───────────────────────────────────────────────
try:
    from backend.chakra.modules.iv_skew import get_skew_agent_boost
    _SKEW_AVAILABLE = True
except ImportError:
    _SKEW_AVAILABLE = False

# ── Session 2: VEX Vanna Exposure ───────────────────────────────────
try:
    from backend.chakra.modules.vex_engine import get_vex_agent_boost
    _VEX_AVAILABLE = False
except ImportError:
    _VEX_AVAILABLE = False

# ── ChromaDB Signal Memory ───────────────────────────────────────────
try:
    from backend.arjun.memory.signal_memory import (
        store_signal as _mem_store, query_similar as _mem_query
    )
    _MEMORY_AVAILABLE = True
except ImportError:
    _MEMORY_AVAILABLE = False

def run(ticker: str, analyst: dict, bull: dict, bear: dict, risk: dict, gex: dict) -> dict:
    """
    Master Coordinator: Synthesize all agent outputs into final BUY/SELL/HOLD signal.
    Produces the rich JSON format with full reasoning.
    """
    _pos_mult   = 1.0
    _unc_regime = "MODERATE"
    _unc_result = {}
    _transformer_signal = None
    _meta_cfg           = {}
    print(f"  [Coordinator] Synthesizing final signal for {ticker}...")

    ind          = analyst.get("indicators", {})
    price        = ind.get("price", 0)
    bull_score   = bull.get("score", 50)
    bear_score   = bear.get("score", 50)

    # ── Pre-market catalyst penalty ──────────────────────────────────
    try:
        from backend.arjun.catalyst_check import get_catalyst_penalty as _cat_pen
        _penalty = _cat_pen(ticker)
        if _penalty != 0:
            bull_score = max(0, bull_score + _penalty)
            bear_score = max(0, bear_score + _penalty)
            print(f"  [Coordinator] Catalyst penalty {_penalty} applied to {ticker}")
    except Exception:
        pass

    # ── Historical accuracy boost from ARKA feedback ─────────────────
    try:
        from backend.arjun.feedback_writer import get_historical_accuracy_boost as _hist_boost
        _acc = _hist_boost(ticker, "CALL")
        if _acc > 0:
            bull_score = min(100, bull_score + _acc)
        elif _acc < 0:
            bull_score = max(0, bull_score + _acc)
    except Exception:
        pass

    # RL_WEIGHTS_WIRED — apply learned agent weights (Mastermind Session 1)
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))))
        from backend.arjun.rl_feedback import get_rl_learner as _get_rl
        _rl = _get_rl()
        bull_score = round(bull_score * _rl.get_weight("bull_agent"), 2)
        bear_score = round(bear_score * _rl.get_weight("bear_agent"), 2)
        # BEAR_V2_WIRED — Adversarial Bear v2 (Mastermind Session 2)
        try:
            from backend.arjun.agents.bear_agent_v2 import get_bear_v2 as _get_bv2
            import json as _j_bv2, pathlib as _pl_bv2
            _bv2 = _get_bv2()
            # Load market context for adversarial checks
            _md = {}
            for _f, _k in [
                ("logs/chakra/hurst_latest.json",   "hurst_exponent"),
                ("logs/chakra/dex_latest.json",      "dex_score"),
                ("logs/internals/internals_latest.json", "vix"),
                ("logs/chakra/hmm_latest.json",      "regime"),
            ]:
                try:
                    _d = _j_bv2.loads(_pl_bv2.Path(_f).read_text())
                    _md.update(_d)
                except Exception:
                    pass
            # Map field names to what bear_v2 expects
            _bear_md = {
                "hurst":        float(_md.get("hurst_exponent", 0.5)),
                "vix":          float(_md.get("vix", 20)),
                "hmm_regime":   str(_md.get("regime", "LOW_VOL_TREND")),
                "volume_ratio": float(_md.get("volume_ratio", 1.0)),
                "days_to_opex": int(_md.get("days_to_opex", 99)),
                "price_vs_200ema": str(_md.get("price_vs_200ema", "ABOVE")),
                "ema_cross_bars_ago": int(_md.get("ema_cross_bars_ago", 99)),
                "gex_flip_distance_pct": float(_md.get("gex_flip_distance_pct", 99)),
            }
            _bull_reasons = _md.get("bull_reasons", []) or []
            if _bull_reasons:
                _bv2_result = _bv2.challenge(_bull_reasons, _bear_md)
                _penalty    = _bv2_result.get("total_penalty", 0)
                if _penalty < 0:
                    bear_score = min(100, max(0, round(bear_score - _penalty, 2)))
        except Exception as _bv2e:
            pass  # bear_v2 unavailable — non-fatal

        bull_score = min(100, bull_score)
        bear_score = min(100, bear_score)
    except Exception as _rle:
        pass  # RL unavailable — use raw scores

    # REGIME_WEIGHTS_WIRED — dynamic agent weights by HMM regime (Mastermind Session 1)
    try:
        import json as _j_rw, pathlib as _pl_rw
        _REGIME_W = {
            "LOWVOL_TREND":  {"bull_agent": 1.2, "bear_agent": 0.8},
            "HIGHVOL_TREND": {"bull_agent": 1.0, "bear_agent": 1.0},
            "CHOPPY_RANGE":  {"bull_agent": 0.7, "bear_agent": 0.7},
            "CRISIS":        {"bull_agent": 0.3, "bear_agent": 1.8},
        }
        _hmm_f = _pl_rw.Path("logs/chakra/hmm_latest.json")
        if _hmm_f.exists():
            _hmm_d  = _j_rw.loads(_hmm_f.read_text())
            _regime = _hmm_d.get("regime", _hmm_d.get("name", "LOWVOL_TREND"))
            _rw     = _REGIME_W.get(_regime, _REGIME_W["LOWVOL_TREND"])
            bull_score = min(100, round(bull_score * _rw.get("bull_agent", 1.0), 2))
            bear_score = min(100, round(bear_score * _rw.get("bear_agent", 1.0), 2))
    except Exception as _rwe:
        pass  # regime weights unavailable — use RL-adjusted scores

    # MANIFOLD_WIRED — Ricci curvature geometry modifier (Mastermind Session 1)
    try:
        import json as _j_mf, pathlib as _pl_mf
        _mf_cache = _pl_mf.Path(f"logs/chakra/manifold_{ticker.lower()}_latest.json")
        if not _mf_cache.exists():
            # fall back to SPY manifold cache
            _mf_cache = _pl_mf.Path("logs/chakra/manifold_spy_latest.json")
        if _mf_cache.exists():
            _mf       = _j_mf.loads(_mf_cache.read_text())
            _mf_reg   = _mf.get("regime", {})
            _mf_mod   = int(_mf_reg.get("arjun_score_modifier", 0))
            _mf_name  = _mf_reg.get("regime", "UNKNOWN")
            if _mf_mod != 0:
                # positive modifier boosts bull, negative dampens bull
                if _mf_mod > 0:
                    bull_score = min(100, round(bull_score + _mf_mod * 0.5, 2))
                else:
                    bull_score = max(0, round(bull_score + _mf_mod * 0.5, 2))
    except Exception as _mfe:
        pass  # manifold cache unavailable

    # ── VEX Vanna boost ──────────────────────────────────────────────
    vex_data = {}
    if _VEX_AVAILABLE:
        try:
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                _fut = _ex.submit(get_vex_agent_boost, ticker)
                try:
                    vex_boost = _fut.result(timeout=5)
                except _cf.TimeoutError:
                    vex_boost = {"signal": "NEUTRAL", "bull_boost": 0, "bear_boost": 0, "vex": {}}
            bull_pts = vex_boost.get("bull_boost", 0)
            bear_pts = vex_boost.get("bear_boost", 0)
            if bull_pts > 0:
                bull_score = min(100, bull_score + bull_pts)
            if bear_pts > 0:
                bear_score = min(100, bear_score + bear_pts)
            vex_data = vex_boost.get("vex", {})
            if bull_pts or bear_pts:
                import logging
                logging.getLogger("coordinator").info(
                    f"  VEX {ticker}: {vex_boost['signal']} bull+{bull_pts} bear+{bear_pts}"
                )
        except Exception:
            pass

    # ── IV Skew bear/bull boost ───────────────────────────────────────
    skew_data = {}
    if _SKEW_AVAILABLE:
        try:
            skew_boost = get_skew_agent_boost(ticker)
            skew_bear  = skew_boost.get("bear_boost", 0)
            skew_bull  = skew_boost.get("bull_boost", 0)
            if skew_bear > 0:
                bear_score = min(100, bear_score + skew_bear)
            if skew_bull > 0:
                bull_score = min(100, bull_score + skew_bull)
            skew_data = skew_boost.get("skew", {})
            if skew_bear or skew_bull:
                import logging
                logging.getLogger("coordinator").info(
                    f"  Skew {ticker}: {skew_boost['sentiment']} bear+{skew_bear} bull+{skew_bull}"
                )
        except Exception:
            pass
    analyst_bias = analyst.get("bias", "NEUTRAL")
    risk_dec     = risk.get("decision", "BLOCK")
    risk_entry   = risk.get("entry", price)
    risk_stop    = risk.get("stop", price * 0.98)
    risk_target  = risk.get("target", price * 1.03)
    sizing       = risk.get("position_size", {})
    gex_regime   = gex.get("regime", "UNKNOWN")
    call_wall    = gex.get("call_wall", 0)
    put_wall     = gex.get("put_wall", 0)

    # ── Derive raw signal from agent scores ────────────────────────────
    if risk_dec == "BLOCK":
        raw_signal = "HOLD"
        # Still compute real confidence so dashboard shows meaningful data
        net_score = bull_score - bear_score
        confidence = round(50 + abs(net_score) * 0.3, 1)
    else:
        pass  # signal_memory disabled
        _pos_mult = 1.0
        _unc_regime = 'MODERATE'
        _unc_result = {}
        # CAUSAL_UNCERTAINTY_WIRED — Modules 5+6 (Mastermind Session 3)
        try:
            from backend.arjun.uncertainty_scorer import get_uncertainty_scorer as _get_unc
            _unc_scorer = _get_unc()
            _agent_scores = {
                "bull_agent":  bull_score,
                "bear_agent":  bear_score,
            }
            # Pull module scores from cache if available
            import json as _j_unc, pathlib as _pl_unc
            for _f, _k in [
                ("logs/chakra/dex_latest.json",     "dex_score"),
                ("logs/chakra/hurst_latest.json",   "hurst"),
                ("logs/chakra/vex_latest.json",     "vex"),
                ("logs/chakra/entropy_latest.json", "entropy"),
                ("logs/chakra/hmm_latest.json",     "hmm"),
            ]:
                try:
                    _d = _j_unc.loads(_pl_unc.Path(_f).read_text())
                    _v = _d.get("score", _d.get("value", _d.get("hurst_exponent", None)))
                    if _v is not None:
                        _agent_scores[_k] = float(_v)
                except Exception:
                    pass
            _unc_result = _unc_scorer.score(_agent_scores)
            _pos_mult   = _unc_result.get("position_multiplier", 1.0)
            _unc_regime = _unc_result.get("regime", "MODERATE")
        except Exception as _unce:
            _pos_mult   = 1.0
            _unc_regime = "MODERATE"
            _unc_result = {}
        # TRANSFORMER_META_WIRED — Modules 7+8 (Mastermind Session 4)
        _transformer_signal = None
        _meta_cfg           = {}
        try:
            # Module 8 — load meta config (learning rate, thresholds)
            import json as _j_meta
            from pathlib import Path as _Pm
            _meta_cfg = _j_meta.loads(_Pm("logs/arjun/meta_config.json").read_text())
        except Exception:
            pass

        try:
            # Module 7 — Temporal Transformer prediction
            import numpy as _np_tr
            from backend.arjun.transformer_engine import predict as _tr_predict, FEATURE_COLS as _TR_COLS
            import json as _j_tr
            from pathlib import Path as _Ptr

            # Build 30-day sequence from cache (use last known values repeated as fallback)
            _cache_vals = {}
            for _f, _k in [
                ("logs/chakra/dex_latest.json",     "dex"),
                ("logs/chakra/hurst_latest.json",   "hurst"),
                ("logs/chakra/vrp_latest.json",     "vrp"),
                ("logs/chakra/entropy_latest.json", "entropy"),
                ("logs/internals/internals_latest.json", "neural_pulse"),
            ]:
                try:
                    _d = _j_tr.loads(_Ptr(_f).read_text())
                    for _key in _TR_COLS:
                        if _key in _d:
                            _cache_vals[_key] = float(_d[_key])
                except Exception:
                    pass

            _row = [_cache_vals.get(c, 0.5) for c in _TR_COLS]
            _seq = _np_tr.array([_row] * 30, dtype=_np_tr.float32)
            _tr_result = _tr_predict(_seq)
            _transformer_signal = _tr_result.get("signal", "HOLD")
            _tr_conf  = _tr_result.get("confidence", 50.0)

            # Blend: XGBoost 70% + Transformer 30%
            if _transformer_signal == "BUY":
                bull_score = round(bull_score * 0.7 + _tr_conf * 0.3, 2)
            elif _transformer_signal == "SELL":
                bear_score = round(bear_score * 0.7 + _tr_conf * 0.3, 2)
        except Exception as _tre:
            pass
        net_score = bull_score - bear_score
        # Weight: analyst adds direction, ml adds conviction
        ml_signal = analyst.get("ml", {}).get("ml_signal", "HOLD")
        ml_conf   = analyst.get("ml", {}).get("ml_confidence", 50)

        if net_score >= 10 and analyst_bias in ("BULLISH", "NEUTRAL"):
            raw_signal = "BUY"
            confidence = min(95, 50 + (net_score * 0.8) + (ml_conf - 50) * 0.3 if ml_signal == "BUY" else net_score * 0.8)
        elif net_score <= -10 or analyst_bias == "BEARISH":
            raw_signal = "SELL"
            confidence = min(95, 50 + (abs(net_score) * 0.8))
        else:
            raw_signal = "HOLD"
            confidence = 50 + abs(net_score) * 0.3

        # ML boost
        if ml_signal == raw_signal and ml_conf > 60:
            confidence = min(95, confidence + 5)

        # ── ChromaDB Memory Boost ──────────────────────────────────
        _memory_context = ""
        if _MEMORY_AVAILABLE and raw_signal != "HOLD":
            try:
                _rsi_val = float(ind.get("rsi", ind.get("rsi_14", 50)) or 50)
                _past = _mem_query(
                    ticker    = ticker,
                    regime    = gex_regime,
                    rsi       = _rsi_val,
                    direction = "BULLISH" if raw_signal == "BUY" else "BEARISH",
                    n_results = 5,
                )
                if _past:
                    _wins    = sum(1 for p in _past if p.get("result") == "WIN")
                    _wr      = _wins / len(_past) * 100
                    _avg_pnl = sum(p.get("pnl_pct",0) for p in _past) / len(_past)
                    _memory_context = (
                        f"{len(_past)} similar trades: {_wr:.0f}% wins, "
                        f"avg {_avg_pnl:+.1f}%"
                    )
                    if _wr >= 70 and len(_past) >= 3:
                        confidence = min(95, confidence + 5)
                        print(f"  [Memory] {ticker}: +5 boost ({_wr:.0f}% hist win rate)")
                    elif _wr <= 30 and len(_past) >= 3:
                        confidence = max(40, confidence - 10)
                        print(f"  [Memory] {ticker}: -10 penalty ({_wr:.0f}% hist win rate)")
            except Exception as _me:
                pass  # memory is non-fatal

    confidence = round(confidence, 1)

    # ── Build context for Claude explanation ───────────────────────────
    agent_summary = {
        "ticker":        ticker,
        "price":         price,
        "raw_signal":    raw_signal,
        "confidence":    confidence,
        "analyst": {
            "bias":    analyst_bias,
            "score":   analyst.get("score", 50),
            "summary": analyst.get("summary", ""),
        },
        "bull": {
            "score":       bull_score,
            "key_catalyst": bull.get("key_catalyst", ""),
            "target_price": bull.get("target_price", risk_target),
            "top_arguments": bull.get("arguments", [])[:3],
        },
        "bear": {
            "score":     bear_score,
            "key_risk":  bear.get("key_risk", ""),
            "stop_level": bear.get("stop_level", risk_stop),
            "top_risks": bear.get("arguments", [])[:3],
            "regime_flag": bear.get("regime_flag", "NORMAL"),
        },
        "risk_manager": {
            "decision":    risk_dec,
            "reason":      risk.get("reason", ""),
            "shares":      sizing.get("shares", 0),
            "risk_dollars": sizing.get("risk_dollars", 0),
            "risk_pct":    sizing.get("risk_pct", 0),
        },
        "gex": {
            "regime":    gex_regime,
            "call_wall": call_wall,
            "put_wall":  put_wall,
            "net_gex":   gex.get("net_gex", 0),
        },
        "entry":  risk_entry,
        "stop":   risk_stop,
        "target": risk_target,
        "rr":     risk.get("risk_reward", 0),
    }

    # ── Claude: Generate professional trade explanation ─────────────────
    prompt = f"""You are the Master Coordinator for ARJUN, an institutional-grade ML trading system.

Four specialized agents have analyzed {ticker}. Synthesize their findings into a professional trade briefing.

Agent Outputs:
{_dumps(agent_summary)}

Write a professional trade explanation. Format EXACTLY like this (use ** for bold):

**SIGNAL: {raw_signal} {ticker} @ ${price:.2f}**

**WHY THIS TRADE**
[2-3 sentences: the core thesis. Be specific with indicator values and levels.]

**MARKET CONTEXT**
[2-3 sentences: GEX regime, trend context, key levels to watch. Mention call wall/put wall if relevant.]

**AGENT CONSENSUS**
- Bull Agent ({bull_score}/100): [bull's key argument]
- Bear Agent ({bear_score}/100): [bear's key concern]  
- Risk Manager: {risk_dec} — [risk's reason]

**TRADE PLAN**
- Entry: ${risk_entry:.2f}
- Target: ${risk_target:.2f} ([calculate % move])
- Stop Loss: ${risk_stop:.2f} ([calculate % move])
- Risk/Reward: 1:{risk.get("risk_reward", 0):.2f}
- Position: {sizing.get("shares", 0)} shares (${sizing.get("dollar_value", 0):,.0f} | {sizing.get("risk_pct", 0):.1f}% risk)

**KEY RISKS**
[2-3 bullet points with the bear agent's top concerns]

**CONFIDENCE: {confidence}% — {'HIGH' if confidence >= 75 else 'MEDIUM' if confidence >= 60 else 'LOW'}**

[One closing sentence about position management.]"""

    # Use fast fallback explanation (Claude call disabled - too slow)
    explanation = _fallback_explanation(ticker, raw_signal, confidence, agent_summary, "")

    # ── Store signal in ChromaDB memory ───────────────────────────────
    if _MEMORY_AVAILABLE:
        try:
            _mem_store({
                "ticker":      ticker,
                "action":      raw_signal,
                "direction":   "BULLISH" if raw_signal == "BUY" else ("BEARISH" if raw_signal == "SELL" else "NEUTRAL"),
                "confidence":  confidence / 100.0,
                "gex_regime":  gex_regime,
                "regime_call": gex.get("regime_call", "NEUTRAL"),
                "rsi":         float(ind.get("rsi", ind.get("rsi_14", 50)) or 50),
                "vwap_bias":   "ABOVE" if ind.get("price", 0) > ind.get("vwap", 0) else "BELOW",
                "bull_score":  bull_score,
                "bear_score":  bear_score,
                "rationale":   explanation[:200],
            })
        except Exception:
            pass  # memory store is non-fatal

    # ── Final signal output — matches existing CHAKRA format + agent data ──
    return {
        "ticker":      ticker,
        "position_multiplier": _pos_mult,
            "transformer_signal": _transformer_signal,
            "meta_config_lr": _meta_cfg.get("rl_learning_rate", 0.05),
        "uncertainty_regime": _unc_regime,
        "signal":      raw_signal,
        "confidence":  str(round(confidence, 1)),
        "price":       price,
        "entry":       round(risk_entry, 2),
        "target":      round(risk_target, 2),
        "stop_loss":   round(risk_stop, 2),
        "risk":        round(abs(risk_entry - risk_stop), 2),
        "reward":      round(abs(risk_target - risk_entry), 2),
        "risk_reward": risk.get("risk_reward", 0),
        "explanation": explanation,
        "timestamp":   datetime.now().isoformat(),
        # Full agent reasoning
        "agents": {
            "analyst": {
                "bias":         analyst_bias,
                "score":        analyst.get("score", 50),
                "summary":      analyst.get("summary", ""),
                "bull_factors": analyst.get("bull_factors", []),
                "bear_factors": analyst.get("bear_factors", []),
                "ml":           analyst.get("ml", {}),
            },
            "bull": {
                "score":        bull_score,
                "vex":          vex_data,
                "arguments":    bull.get("arguments", []),
                "key_catalyst": bull.get("key_catalyst", ""),
                "target_price": bull.get("target_price", risk_target),
                "invalidation": bull.get("invalidation", ""),
            },
            "bear": {
                "score":        bear_score,
                "arguments":    bear.get("arguments", []),
                "key_risk":     bear.get("key_risk", ""),
                "stop_level":   bear.get("stop_level", risk_stop),
                "regime_flag":  bear.get("regime_flag", "NORMAL"),
                "curvature":    bear.get("curvature", {}),
            },
            "risk_manager": {
                "decision":      risk_dec,
                "reason":        risk.get("reason", ""),
                "position_size": sizing,
                "regime":        gex_regime,
                "macro_events":  risk.get("macro_events", {}),
                "blocks":        risk.get("blocks", []),
                "reduces":       risk.get("reduces", []),
            },
        },
        "gex": gex,
        "indicators": ind,
    }


def _fallback_explanation(ticker, signal, conf, summary, error):
    bull = summary.get("bull", {})
    bear = summary.get("bear", {})
    risk = summary.get("risk_manager", {})
    return (
        f"**SIGNAL: {signal} {ticker} @ ${summary.get('price', 0):.2f}**\n\n"
        f"**WHY THIS TRADE**\n{summary.get('analyst', {}).get('summary', 'Technical analysis complete.')}\n\n"
        f"**AGENT CONSENSUS**\n"
        f"- Bull Agent ({bull.get('score', 50)}/100): {bull.get('key_catalyst', 'Technical setup favorable')}\n"
        f"- Bear Agent ({bear.get('score', 50)}/100): {bear.get('key_risk', 'Monitor for reversals')}\n"
        f"- Risk Manager: {risk.get('decision', 'APPROVE')} — {risk.get('reason', '')}\n\n"
        f"**CONFIDENCE: {conf}% — {'HIGH' if conf >= 75 else 'MEDIUM' if conf >= 60 else 'LOW'}**"
    )


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from analyst_agent      import run as analyst_run
    from bull_agent         import run as bull_run
    from bear_agent         import run as bear_run
    from risk_manager_agent import run as risk_run
    from gex_calculator     import get_gex_for_ticker

    analyst = analyst_run("SPY")
    price   = analyst["indicators"]["price"]
    gex     = get_gex_for_ticker("SPY", price)
    bull    = bull_run("SPY", analyst, gex)
    bear    = bear_run("SPY", analyst, gex)
    risk    = risk_run("SPY", analyst, bull, bear, gex)
    result  = run("SPY", analyst, bull, bear, risk, gex)
    print(json.dumps(result, indent=2, default=str))
    # ── Flow Signal Validator (called by Tarak) ──────────────────────────
    async def validate_external_signal(self, ctx: dict) -> dict:
        """
        Arjun reviews a flow signal from Tarak and returns a conviction score.
        Enriches with regime, internals, RSI/VWAP, fakeout probability.
        """
        import json, os
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ticker    = ctx.get("ticker", "")
        direction = ctx.get("direction", "LONG")
        dte       = ctx.get("dte", 0)
        is_bull   = direction == "LONG"
        reasons   = []
        score     = float(ctx.get("flow_confidence", 50))  # start from CHAKRA's score

        # ── Read neural pulse from internals ────────────────────────────
        try:
            pulse_path = "logs/internals/internals_latest.json"
            if os.path.exists(pulse_path):
                with open(pulse_path) as f:
                    internals = json.load(f)
                pulse  = internals.get("neural_pulse", {}).get("score", 50)
                regime = internals.get("gex_regime", "UNKNOWN")

                if is_bull:
                    if pulse > 65:
                        score += 8; reasons.append(f"Neural Pulse {pulse:.0f} — market internals strong")
                    elif pulse < 35:
                        score -= 10; reasons.append(f"Neural Pulse {pulse:.0f} — internals weak, caution")
                    if "NEGATIVE" in regime:
                        score += 5; reasons.append(f"GEX {regime} — dealers amplify upside moves")
                    elif "POSITIVE" in regime:
                        score -= 3
                else:
                    if pulse < 35:
                        score += 8; reasons.append(f"Neural Pulse {pulse:.0f} — internals bearish")
                    elif pulse > 65:
                        score -= 8; reasons.append(f"Neural Pulse {pulse:.0f} — internals strong, fade risk")
                    if "POSITIVE" in regime:
                        score += 5; reasons.append(f"GEX {regime} — dealers amplify downside")
        except Exception:
            pass

        # ── Fakeout probability check ────────────────────────────────────
        try:
            from arjun.agents.riskmanageragent import RiskManagerAgent
            rm     = RiskManagerAgent()
            fakeout = await rm.get_fakeout_probability(ticker, direction)
            if fakeout > 0.65:
                score -= 12; reasons.append(f"Fakeout risk {fakeout:.0%} — high reversal probability")
            elif fakeout < 0.35:
                score += 5;  reasons.append(f"Fakeout risk {fakeout:.0%} — low, clean setup")
        except Exception:
            pass

        # ── DTE-based regime preference ──────────────────────────────────
        if dte == 0:
            reasons.append("0DTE: needs strong momentum, tight stop")
        elif 1 <= dte <= 5:
            score += 3; reasons.append(f"{dte}DTE gamma-rich zone")
        elif dte > 21:
            score -= 4; reasons.append(f"{dte}DTE flow urgency lower")

        # ── Extreme UOA boost ────────────────────────────────────────────
        if ctx.get("is_extreme"):
            score += 6; reasons.append("EXTREME UOA — institutional commitment")

        # ── Dark pool confirmation ───────────────────────────────────────
        dp = ctx.get("dark_pool_pct", 0)
        if dp >= 40:
            score += 7; reasons.append(f"Dark pool {dp:.0f}% — heavy smart money")
        elif dp >= 25:
            score += 3

        conviction = max(40, min(97, int(round(score))))
        approved   = conviction >= 60

        return {
            "approved":    approved,
            "conviction":  conviction,
            "reasons":     reasons,
            "adjustment":  {},   # Arjun can put preferred_strike / preferred_expiry here
            "source":      "Arjun ML",
        }
