"""
ARJUN — Weekly Macro Brief Generator
File: backend/arjun/weekly_brief.py

Aggregates current market state into a structured weekly outlook.
Reads from: GEX state, market internals, ARJUN signals, sector snapshot.
Called by: GET /api/weekly/brief in dashboard_api.py
"""

import os
import json
import logging
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("ARJUN.WeeklyBrief")

BASE = Path(__file__).resolve().parents[2]
ET   = ZoneInfo("America/New_York")


def _read_json(path: str | Path) -> dict:
    try:
        p = BASE / path if not Path(path).is_absolute() else Path(path)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


def _read_json_list(path: str | Path) -> list:
    data = _read_json(path)
    if isinstance(data, list):
        return data
    return data.get("results", data.get("signals", []))


def _regime_label(regime: str) -> dict:
    mapping = {
        "POSITIVE_GAMMA": {"label": "Positive GEX", "icon": "🟢", "desc": "Dealers stabilizing — mean reversion favored. Buy dips."},
        "NEGATIVE_GAMMA": {"label": "Negative GEX", "icon": "🔴", "desc": "Dealers amplifying moves — momentum favored. Trend following."},
        "LOW_VOL":        {"label": "Low Vol",       "icon": "🟡", "desc": "Compressed range — wait for catalyst."},
        "FOLLOW_MOMENTUM":{"label": "Follow Momentum","icon":"🔵", "desc": "Strong directional bias — ride the trend."},
        "SHORT_THE_POPS": {"label": "Short the Pops","icon": "🔴", "desc": "Sells rips — bearish momentum regime."},
        "BUY_THE_DIPS":   {"label": "Buy the Dips",  "icon": "🟢", "desc": "Bullish momentum — accumulate on weakness."},
    }
    return mapping.get(regime, {"label": regime or "Unknown", "icon": "⚪", "desc": "No regime data."})


def generate_brief() -> dict:
    now  = datetime.now(ET)
    today = date.today().isoformat()
    week_start = (now.date() - __import__("datetime").timedelta(days=now.weekday())).isoformat()

    # ── GEX State ──────────────────────────────────────────────────────
    gex_spy = _read_json(f"logs/gex/gex_latest_SPY.json")
    gex_spx = _read_json(f"logs/gex/gex_latest_SPX.json")
    gex_qqq = _read_json(f"logs/gex/gex_latest_QQQ.json")

    regime     = (gex_spy or gex_spx or {}).get("regime", "UNKNOWN")
    zero_gamma = (gex_spy or gex_spx or {}).get("zero_gamma", 0)
    call_wall  = (gex_spy or gex_spx or {}).get("call_wall", 0)
    put_wall   = (gex_spy or gex_spx or {}).get("put_wall", 0)
    spot_spy   = gex_spy.get("spot", 0)
    regime_call = gex_spy.get("regime_call", "") or gex_spx.get("regime_call", "")
    cliff_today = gex_spy.get("cliff_today", False) or gex_spx.get("cliff_today", False)

    regime_info = _regime_label(regime_call or regime)

    # ── Market Internals ───────────────────────────────────────────────
    internals = _read_json("logs/internals/internals_latest.json")
    pulse      = int(internals.get("neural_pulse", {}).get("score", 0) or 0)
    vix        = float(internals.get("vix", {}).get("close", 0) or 0)
    bond_stress= internals.get("bond_stress", {})
    risk_level = internals.get("risk", {}).get("level", "UNKNOWN")

    # ── Flow Signals ───────────────────────────────────────────────────
    flow = _read_json("logs/chakra/flow_signals_latest.json")
    flow_lines = []
    for ticker in ["SPY", "QQQ", "SPX"]:
        sig = flow.get(ticker, {})
        if isinstance(sig, dict) and sig.get("bias") not in (None, "NEUTRAL"):
            flow_lines.append({
                "ticker":     ticker,
                "bias":       sig.get("bias", "NEUTRAL"),
                "confidence": sig.get("confidence", 0),
                "is_extreme": sig.get("is_extreme", False),
            })

    # ── Sector Performance ─────────────────────────────────────────────
    sector_data = _read_json("logs/chakra/sector_snapshot.json").get("data", {})
    sectors_sorted = sorted(sector_data.items(), key=lambda x: x[1].get("chg_pct", 0), reverse=True)
    top_sectors = [{"etf": k, "chg_pct": v["chg_pct"], "direction": v["direction"]}
                   for k, v in sectors_sorted[:4]]
    bot_sectors = [{"etf": k, "chg_pct": v["chg_pct"], "direction": v["direction"]}
                   for k, v in sectors_sorted[-3:] if v.get("chg_pct", 0) < 0]

    # ── ARJUN Signals (latest) ─────────────────────────────────────────
    pipeline = _read_json("logs/arjun/pipeline_latest.json")
    arjun_signals = []
    for ticker, sig in (pipeline.items() if isinstance(pipeline, dict) else {}):
        if not isinstance(sig, dict):
            continue
        conf = float(sig.get("confidence", 0) or 0)
        if conf >= 0.65:
            arjun_signals.append({
                "ticker":     ticker,
                "signal":     sig.get("signal", "HOLD"),
                "confidence": conf,
                "regime":     sig.get("regime", ""),
            })
    arjun_signals.sort(key=lambda x: x["confidence"], reverse=True)

    # ── OI Delta ──────────────────────────────────────────────────────
    oi_delta = _read_json("logs/chakra/oi_delta_latest.json")
    oi_lines = []
    for ticker, d in oi_delta.items():
        if isinstance(d, dict) and d.get("signal") not in ("NEUTRAL", "UNKNOWN", None):
            oi_lines.append({
                "ticker":   ticker,
                "signal":   d.get("signal"),
                "call_chg": d.get("call_delta_pct", 0),
                "put_chg":  d.get("put_delta_pct", 0),
            })

    # ── ARKA Today Summary ─────────────────────────────────────────────
    arka_summary = _read_json(f"logs/arka/summary_{today}.json")
    scan_history = arka_summary.get("scan_history", [])
    trades_today = [s for s in scan_history if s.get("direction") in
                    ("LONG", "STRONG_LONG", "SHORT", "STRONG_SHORT")]
    wins  = arka_summary.get("wins", 0)
    losses= arka_summary.get("losses", 0)

    # ── Key levels text ───────────────────────────────────────────────
    levels = []
    if call_wall: levels.append({"label": "Call Wall", "value": call_wall, "color": "green"})
    if zero_gamma: levels.append({"label": "Zero Gamma", "value": zero_gamma, "color": "yellow"})
    if put_wall:  levels.append({"label": "Put Wall",  "value": put_wall,  "color": "red"})

    # ── Market bias summary ───────────────────────────────────────────
    if pulse >= 70:
        bias_text = "Broadly bullish internals. High neural pulse confirms institutional buying."
    elif pulse >= 50:
        bias_text = "Neutral internals. Mixed signals — trade selectively."
    else:
        bias_text = "Weak internals. Risk-off tone. Prefer SHORT setups or stand aside."

    if vix > 25:
        bias_text += f" VIX elevated at {vix:.1f} — expect wide swings."
    elif vix < 15:
        bias_text += f" VIX compressed at {vix:.1f} — low volatility environment."

    return {
        "generated_at":   now.isoformat(),
        "week_start":     week_start,
        "today":          today,
        "regime": {
            "name":       regime_call or regime,
            "label":      regime_info["label"],
            "icon":       regime_info["icon"],
            "description":regime_info["desc"],
            "cliff_today":cliff_today,
        },
        "levels":         levels,
        "spot_spy":       round(spot_spy, 2) if spot_spy else 0,
        "neural_pulse":   pulse,
        "vix":            round(vix, 2) if vix else 0,
        "risk_level":     risk_level,
        "bias_text":      bias_text,
        "flow_signals":   flow_lines,
        "top_sectors":    top_sectors,
        "weak_sectors":   bot_sectors,
        "arjun_signals":  arjun_signals[:8],
        "oi_signals":     oi_lines,
        "arka_today": {
            "scans":  len(scan_history),
            "trades": len(trades_today),
            "wins":   wins,
            "losses": losses,
        },
        "schedule": _build_schedule(now),
    }


def _build_schedule(now: datetime) -> list:
    """Build today's key schedule items."""
    entries = [
        {"time": "07:15",  "label": "Swing Pre-Market Scan",      "tag": "ARKA"},
        {"time": "08:00",  "label": "ARJUN Daily Signals",         "tag": "ARJUN"},
        {"time": "08:30",  "label": "ARKA Engine Start",           "tag": "ARKA"},
        {"time": "09:30",  "label": "Market Open — ORB window",    "tag": "MARKET"},
        {"time": "10:30",  "label": "TARAKA Entry Scan",           "tag": "TARAKA"},
        {"time": "12:00",  "label": "Lunch — reduced size",        "tag": "RISK"},
        {"time": "14:00",  "label": "TARAKA Monitor",              "tag": "TARAKA"},
        {"time": "15:00",  "label": "Power Hour begins",           "tag": "LOTTO"},
        {"time": "15:30",  "label": "Lotto window open",           "tag": "LOTTO"},
        {"time": "15:58",  "label": "EOD Closer — all positions",  "tag": "EOD"},
        {"time": "16:00",  "label": "Market Close",                "tag": "MARKET"},
        {"time": "17:00",  "label": "Swing Post-Market + EOD Summary","tag": "ARKA"},
    ]
    current_hhmm = now.strftime("%H:%M")
    for e in entries:
        e["past"] = e["time"] < current_hhmm
    return entries


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [WeeklyBrief] %(message)s")
    brief = generate_brief()
    print(json.dumps(brief, indent=2, default=str))
