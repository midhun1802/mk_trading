"""
ARJUN Intraday Deliberation Engine
===================================
Triggered by Heat Seeker signals. Runs a fast 2-agent deliberation
(Bull Agent + Risk Manager) and writes a trade_request for ARKA.

Faster than the full daily pipeline — no Coordinator needed for
intraday scalp decisions. Just Bull vs Risk Manager.

Pipeline: Heat Seeker → hs_signal_writer → arjun_intraday → trade_request.json → ARKA
"""
import json
import time
import os
import httpx
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo

ET             = ZoneInfo("America/New_York")
HS_INPUT_PATH  = "logs/arjun/hs_pending_signals.json"
TRADE_REQ_PATH = "logs/arjun/trade_request.json"
ARJUN_LOG_PATH = "logs/arjun/intraday.log"

EXECUTE_THRESHOLD = 68   # combined confidence needed to EXECUTE
WATCH_THRESHOLD   = 55   # below this → SKIP


def _get_claude_api_key() -> str:
    from dotenv import load_dotenv
    load_dotenv(override=True)
    return os.getenv("ANTHROPIC_API_KEY", "")


def _call_claude(system: str, user: str, max_tokens: int = 600) -> str:
    """Fast Claude Sonnet call for intraday deliberation."""
    key = _get_claude_api_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "system":     system,
            "messages":   [{"role": "user", "content": user}],
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


def _load_market_context(ticker: str) -> dict:
    """Load supporting data for ARJUN deliberation."""
    ctx = {}

    # GEX state — prefer ticker-specific, fall back to SPY
    for gex_ticker in (ticker, "SPY"):
        gex_path = Path(f"logs/gex/gex_latest_{gex_ticker}.json")
        if gex_path.exists():
            try:
                g = json.loads(gex_path.read_text())
                age = time.time() - g.get("ts", 0)
                if age < 600:  # only use if < 10 min old
                    ctx["gex_regime"]  = g.get("regime", "UNKNOWN")
                    ctx["regime_call"] = g.get("regime_call", "NEUTRAL")
                    ctx["zero_gamma"]  = g.get("zero_gamma", 0)
                    ctx["call_wall"]   = g.get("call_wall", 0)
                    ctx["put_wall"]    = g.get("put_wall", 0)
                    ctx["bias_ratio"]  = g.get("bias_ratio", 1.0)
                    break
            except Exception:
                pass

    # Today's ARJUN daily pipeline output
    pipeline_path = Path("logs/arjun/pipeline_latest.json")
    if pipeline_path.exists():
        try:
            pl = json.loads(pipeline_path.read_text())
            sig_list = pl.get("signals", [])
            ticker_sig = next(
                (s for s in sig_list
                 if s.get("ticker", "").upper() == ticker.upper()), {}
            )
            if ticker_sig:
                ctx["daily_action"]     = ticker_sig.get("action", "HOLD")
                ctx["daily_confidence"] = float(ticker_sig.get("confidence", 0.5)) * 100
                ctx["daily_direction"]  = ticker_sig.get("direction", "NONE")
                ctx["daily_rationale"]  = ticker_sig.get("rationale", "")[:120]
        except Exception:
            pass

    # Market internals
    intern_path = Path("logs/internals/internals_latest.json")
    if intern_path.exists():
        try:
            intern = json.loads(intern_path.read_text())
            ctx["risk_mode"]    = intern.get("risk_mode", "NORMAL")
            ctx["vix_regime"]   = intern.get("vix_regime", "UNKNOWN")
            ctx["neural_pulse"] = intern.get("neural_pulse_score",
                                   intern.get("neural_pulse", {}).get("score", 50))
        except Exception:
            pass

    # Flow signals
    flow_path = Path("logs/chakra/flow_signals_latest.json")
    if flow_path.exists():
        try:
            flow = json.loads(flow_path.read_text())
            ticker_flows = [
                f for f in flow.get("signals", [])
                if f.get("ticker", "").upper() == ticker.upper()
            ]
            ctx["flow_signals"] = ticker_flows[:3]
        except Exception:
            pass

    return ctx


def deliberate(hs_signal: dict) -> dict:
    """
    Run ARJUN 2-agent deliberation on a Heat Seeker signal.
    Returns a trade_request dict with EXECUTE/WATCH/SKIP decision.
    """
    ticker    = hs_signal["ticker"]
    direction = hs_signal["direction"]
    context   = _load_market_context(ticker)
    now_et    = datetime.now(ET).strftime("%H:%M ET")

    # ── Build context block ────────────────────────────────────────
    ctx_lines = [f"Time: {now_et}"]
    if context.get("gex_regime"):
        ctx_lines.append(
            f"GEX Regime: {context['gex_regime']} | Regime call: {context.get('regime_call','?')} | "
            f"Bias ratio: {context.get('bias_ratio',1):.2f}x"
        )
        ctx_lines.append(
            f"Walls: Call=${context.get('call_wall',0):.2f}  "
            f"Put=${context.get('put_wall',0):.2f}  "
            f"ZeroGamma=${context.get('zero_gamma',0):.2f}"
        )
    if context.get("daily_action"):
        ctx_lines.append(
            f"ARJUN daily signal: {context['daily_action']} "
            f"{context.get('daily_confidence',50):.0f}% ({context.get('daily_direction','?')})"
        )
        if context.get("daily_rationale"):
            ctx_lines.append(f"Rationale: {context['daily_rationale']}")
    if context.get("risk_mode"):
        ctx_lines.append(
            f"Market: {context['risk_mode']} | VIX: {context.get('vix_regime','?')} | "
            f"Neural Pulse: {context.get('neural_pulse',50):.0f}/100"
        )
    for f in context.get("flow_signals", []):
        ctx_lines.append(
            f"Flow: {f.get('direction','?')} {f.get('tier','?')} conf={f.get('confidence',0):.0f}%"
        )

    context_block = "\n".join(ctx_lines)

    # ── Bull Agent prompt ──────────────────────────────────────────
    bull_prompt = f"""You are ARJUN Bull Agent. Evaluate this Heat Seeker signal for a FAST 0DTE scalp trade.

HEAT SEEKER SIGNAL:
{hs_signal['context']}

MARKET CONTEXT:
{context_block}

Score the BULL case for entering a {direction} options trade on {ticker} RIGHT NOW (0DTE scalp).
Consider: institutional flow strength, market regime alignment, risk/reward for a 10-15 min hold.

Respond in exactly this JSON format:
{{
  "bull_score": <0-100>,
  "key_reason": "<one sentence why this trade makes sense>",
  "concerns": "<one sentence main risk>",
  "recommended_action": "ENTER" or "WAIT" or "SKIP"
}}
Only JSON. No markdown."""

    # ── Risk Manager prompt ────────────────────────────────────────
    risk_prompt = f"""You are ARJUN Risk Manager. Review this 0DTE scalp trade request.

HEAT SEEKER SIGNAL:
{hs_signal['context']}

MARKET CONTEXT:
{context_block}

Rules you enforce:
- BLOCK if VIX regime is PANIC
- BLOCK if risk_mode is RISK_OFF
- BLOCK if GEX regime_call opposes direction (SHORT_THE_POPS + BULLISH entry, or BUY_THE_DIPS + BEARISH)
- BLOCK if HS score < 65 AND no sweep confirmation
- APPROVE if HS score >= 75 OR (sweep confirmed AND score >= 65)
- WATCH if borderline (score 60-74, no sweep)

Respond in exactly this JSON format:
{{
  "decision": "APPROVE" or "WATCH" or "BLOCK",
  "confidence": <0-100>,
  "reason": "<one sentence>",
  "max_contracts": <1, 2, or 3>
}}
Only JSON. No markdown."""

    try:
        _sys = "You are a focused trading AI agent. Respond only in valid JSON."
        bull_raw = _call_claude(_sys, bull_prompt)
        risk_raw = _call_claude(_sys, risk_prompt)

        bull_resp = json.loads(bull_raw.strip().replace("```json", "").replace("```", ""))
        risk_resp = json.loads(risk_raw.strip().replace("```json", "").replace("```", ""))

    except Exception as e:
        print(f"❌ ARJUN deliberation error: {e}")
        return {
            "ticker":     ticker,
            "decision":   "SKIP",
            "reason":     f"Deliberation error: {e}",
            "confidence": 0,
        }

    bull_score    = float(bull_resp.get("bull_score", 50))
    risk_conf     = float(risk_resp.get("confidence", 50))
    risk_dec      = risk_resp.get("decision", "BLOCK")
    max_contracts = int(risk_resp.get("max_contracts", 1))

    # ── Final decision ─────────────────────────────────────────────
    combined_conf = (bull_score * 0.6) + (risk_conf * 0.4)

    if risk_dec == "BLOCK":
        final_decision = "SKIP"
        combined_conf  = min(combined_conf, 30)
    elif risk_dec == "APPROVE" and combined_conf >= EXECUTE_THRESHOLD:
        final_decision = "EXECUTE"
    elif combined_conf >= WATCH_THRESHOLD:
        final_decision = "WATCH"
    else:
        final_decision = "SKIP"

    trade_request = {
        "ticker":          ticker,
        "direction":       direction,
        "decision":        final_decision,
        "confidence":      round(combined_conf, 1),
        "max_contracts":   max_contracts if final_decision == "EXECUTE" else 0,
        "bull_score":      round(bull_score, 1),
        "risk_decision":   risk_dec,
        "risk_confidence": round(risk_conf, 1),
        "hs_score":        hs_signal.get("hs_score", 0),
        "is_sweep":        hs_signal.get("is_sweep", False),
        "gex_aligned":     hs_signal.get("gex_aligned", False),
        "mode":            hs_signal.get("mode", "scalp"),
        "key_reason":      bull_resp.get("key_reason", ""),
        "concerns":        bull_resp.get("concerns", ""),
        "risk_reason":     risk_resp.get("reason", ""),
        "context":         context,
        "timestamp":       time.time(),
        "datetime":        datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        "expires_at":      time.time() + 600,  # ARKA must act within 10 min
    }

    # Append to intraday log
    os.makedirs("logs/arjun", exist_ok=True)
    with open(ARJUN_LOG_PATH, "a") as f:
        f.write(json.dumps({
            "time":      trade_request["datetime"],
            "ticker":    ticker,
            "decision":  final_decision,
            "conf":      round(combined_conf, 1),
            "hs_score":  hs_signal.get("hs_score", 0),
            "sweep":     hs_signal.get("is_sweep", False),
        }) + "\n")

    print(
        f"🤖 ARJUN deliberation: {ticker} → {final_decision} "
        f"(conf={combined_conf:.1f}% | bull={bull_score:.0f} | risk={risk_dec} {risk_conf:.0f}%)"
    )
    return trade_request


def run_pipeline() -> dict:
    """
    Full HS→ARJUN pipeline. Called by ARKA or bridge trigger.
    1. Read pending HS signals
    2. Deliberate on top signal
    3. Write trade_request.json
    Returns the trade_request or {} if nothing actionable.
    """
    if not Path(HS_INPUT_PATH).exists():
        print("⚠️ No HS pending signals file")
        return {}

    data = json.loads(Path(HS_INPUT_PATH).read_text())
    sigs = data.get("signals", [])

    if not sigs:
        print("⚠️ No HS signals to deliberate on")
        return {}

    # Market hours check
    now_et = datetime.now(ET)
    is_market = (
        now_et.weekday() < 5
        and ((now_et.hour == 9 and now_et.minute >= 30) or now_et.hour > 9)
        and now_et.hour < 16
    )
    if not is_market:
        print("⚠️ Market closed — ARJUN skipping deliberation")
        return {}

    # Deliberate on top signal (highest HS score)
    top_signal = sigs[0]
    trade_req  = deliberate(top_signal)

    Path(TRADE_REQ_PATH).write_text(json.dumps(trade_req, indent=2))
    print(f"📋 Trade request written → {TRADE_REQ_PATH}")

    return trade_req


if __name__ == "__main__":
    result = run_pipeline()
    if result:
        print(f"\n{'='*50}")
        print(f"ARJUN DECISION: {result.get('decision')} | {result.get('ticker')} {result.get('direction')}")
        print(f"Confidence: {result.get('confidence')}%")
        print(f"Bull reason: {result.get('key_reason')}")
        print(f"Risk: {result.get('risk_reason')}")
