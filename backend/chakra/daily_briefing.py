"""
CHAKRA Daily Briefing — 7:00 AM Discord Report
Posts structured morning briefing before ARJUN runs at 8AM.
Cron: 0 7 * * 1-5
"""
import os, json, requests, glob
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)
ET              = ZoneInfo("America/New_York")
DISCORD_WEBHOOK = os.getenv("DISCORD_TRADES_WEBHOOK", "")


def _read_latest(pattern: str) -> dict:
    files = sorted(glob.glob(str(BASE / pattern)), reverse=True)
    if not files:
        return {}
    try:
        return json.loads(Path(files[0]).read_text())
    except Exception:
        return {}


def _get_7day_performance() -> dict:
    import sqlite3
    db = BASE / "logs/arjun_performance.db"
    if not db.exists():
        return {"win_rate": 0, "wins": 0, "losses": 0, "total": 0}
    try:
        import pandas as pd
        conn = sqlite3.connect(str(db))
        df   = pd.read_sql_query(
            'SELECT outcome FROM signals WHERE date > date("now","-7 days") AND outcome IS NOT NULL', conn)
        conn.close()
        if df.empty:
            return {"win_rate": 0, "wins": 0, "losses": 0, "total": 0}
        wins   = (df["outcome"] == "WIN").sum()
        total  = len(df)
        return {"win_rate": round(wins/total*100, 1), "wins": int(wins),
                "losses": int(total-wins), "total": total}
    except Exception:
        return {"win_rate": 0, "wins": 0, "losses": 0, "total": 0}


def build_briefing() -> str:
    now      = datetime.now(ET)
    internals = _read_latest("logs/internals/internals_latest.json")
    gex      = _read_latest("logs/arka/gex_heatmap_*.json") or _read_latest("logs/arka/gex-heatmap-*.json")
    perf     = _get_7day_performance()

    # Execution gates
    try:
        from backend.chakra.execution_gates import calculate_execution_gates
        gates = calculate_execution_gates()
        gate_str = f"{gates['overall_icon']} {gates['overall']} ({gates['gates_passed']}/{gates['gates_total']} gates)"
    except Exception:
        gate_str = "⚠️ Gates unavailable"

    # Market regime
    pulse     = internals.get("neural_pulse", {})
    risk      = internals.get("risk", {})
    vix_data  = internals.get("vix", {})

    # ── VRP (Session 1) ──────────────────────────────────────────────
    vrp_line = ""
    try:
        from backend.chakra.modules.vrp_engine import get_vrp_briefing_line, compute_and_cache_vrp
        compute_and_cache_vrp()   # refresh at 7 AM
        vrp_line = get_vrp_briefing_line()
    except Exception:
        pass

    # ── VEX Vanna flow day flag (Session 2) ──────────────────────────
    vex_line = ""
    try:
        from backend.chakra.modules.vex_engine import compute_and_cache_vex, get_vex_briefing_line
        compute_and_cache_vex()
        vex    = get_vex_briefing_line()
        vex_line = f"⚡ **Vanna Flow Day** — {vex}" if "MELTUP" in vex or "SELLOFF" in vex else vex
    except Exception:
        pass
    spy_qqq   = internals.get("spy_qqq_ratio", {})
    bond      = internals.get("bond_stress", {})

    # GEX
    gex_regime   = gex.get("regime", "UNKNOWN")
    call_wall    = gex.get("top_call_wall", gex.get("call_wall", "N/A"))
    put_wall     = gex.get("top_put_wall",  gex.get("put_wall",  "N/A"))

    # Sector leaders from internals
    index_last = internals.get("index_last", {})
    sectors    = {k: v for k, v in index_last.items()
                  if k not in ["SPY","QQQ","IWM","DIA","EWU","EWG","EWJ","EWH","FXI","EEM"]}

    # Macro events
    try:
        from backend.arjun.modules.macro_calendar import fetch_upcoming_events
        events    = fetch_upcoming_events(hours_ahead=24)
        macro_str = ", ".join(e.get("name","?") for e in events[:3]) if events else "None today ✅"
    except Exception:
        macro_str = "Calendar unavailable"

    # ARKA streak
    try:
        from backend.arjun.weekly_retrain import analyze_signal_performance
        streak_str = f"7D: {perf['wins']}W/{perf['losses']}L ({perf['win_rate']}%)"
    except Exception:
        streak_str = "No data yet"

    lines = [
        f"⚡ **CHAKRA DAILY BRIEFING** — {now.strftime('%a %b %-d, %Y')}",
        f"Day {(now.date() - date(2026, 2, 27)).days + 1} of 30-Day Validation | {now.strftime('%-I:%M %p ET')}",
        "",
        f"**EXECUTION GATES**",
        f"{gate_str}",
        "",
        f"**MARKET REGIME**",
        f"GEX: {gex_regime} | Call Wall: {call_wall} | Put Wall: {put_wall}",
        f"Neural Pulse: {pulse.get('score', '?')}/100 ({pulse.get('label','?')}) {pulse.get('color','')}",
        f"Risk Mode: {risk.get('mode','?')} | VIX: {vix_data.get('close','?')}",
        f"Bond Stress: {bond.get('stress','?')} ({bond.get('regime','?')})",
        f"SPY/QQQ: {spy_qqq.get('signal','?')} — {spy_qqq.get('trend','?')}",
        "",
        f"**MACRO EVENTS TODAY**",
        f"{macro_str}",
        "",
        f"**ARJUN SIGNALS** — Generating at 8:00 AM ET",
        f"Last session performance: {streak_str}",
        "",
        f"**PERFORMANCE (7 days)**",
        f"Win Rate: {perf['win_rate']}% | {perf['wins']}W / {perf['losses']}L / {perf['total']} total",
    ]
    return "\n".join(lines)


def post_briefing():
    if not DISCORD_WEBHOOK:
        print("No Discord webhook configured")
        return

    now_str = datetime.now(ET).strftime("%A, %B %d %Y • %I:%M %p ET")

    # ── Read all data sources ─────────────────────────────────────────
    internals = _read_latest("logs/internals/internals_latest.json")
    gates_raw = _read_latest("logs/chakra/execution_gates_latest.json")
    perf      = _get_7day_performance()

    # Execution gates
    try:
        from backend.chakra.execution_gates import check_all_gates
        gates = check_all_gates()
    except Exception:
        gates = gates_raw or {}

    overall   = gates.get("overall", "UNKNOWN")
    gate_list = gates.get("gates", [])
    gate_col  = 0x00FF88 if overall == "GO" else 0xFFCC00 if overall == "CAUTION" else 0xFF2D55

    # Neural Pulse
    pulse_data = internals.get("neural_pulse", {})
    pulse      = pulse_data.get("score", 50)
    pulse_label= pulse_data.get("label", "NEUTRAL")
    pulse_emoji= "🟢" if pulse >= 65 else "🟡" if pulse >= 50 else "🟠" if pulse >= 35 else "🔴"

    # GEX
    gex_regime = internals.get("gex_regime", "UNKNOWN")
    gex_emoji  = "🔴" if "NEGATIVE" in str(gex_regime) else "🟢"

    # Bond stress
    bond = internals.get("bond_stress", {})
    bond_val   = bond.get("velocity", 0) if isinstance(bond, dict) else 0
    bond_str   = f"{bond_val:+.2f}% {'⚠️ stress' if bond_val < -0.5 else '✅ calm'}"

    # SPY/QQQ prices
    spy_price  = internals.get("spy_price", 0)
    qqq_price  = internals.get("qqq_price", 0)
    spy_str    = f"${spy_price:.2f}" if spy_price else "—"
    qqq_str    = f"${qqq_price:.2f}" if qqq_price else "—"

    # Macro events
    try:
        from backend.arjun.modules.macro_calendar import fetch_upcoming_events
        events = fetch_upcoming_events(hours_ahead=8)
        macro_str = ", ".join([e.get("name", "?") for e in events[:3]]) if events else "✅ Clear"
    except Exception:
        macro_str = "—"

    # 7-day performance
    wins  = perf.get("wins", 0)
    total = perf.get("total", 0)
    wr    = perf.get("win_rate", 0)
    pnl   = perf.get("total_pnl", 0)
    perf_str = f"{wins}W / {total-wins}L ({wr:.0f}% WR) | P&L: ${pnl:+,.2f}" if total else "No trades yet"

    # Gate summary
    gate_lines = []
    for g in gate_list:
        icon  = g.get("icon", "•")
        name  = g.get("name", "?")
        status= g.get("status", "?")
        gate_lines.append(f"{icon} {name}: **{status}**")
    gate_str = "\n".join(gate_lines) if gate_lines else "—"


    # Overall status line
    status_emoji = "✅" if overall == "GO" else "⚠️" if overall == "CAUTION" else "🚫"

    embed = {
        "color": gate_col,
        "author": {"name": "☀️ CHAKRA Morning Briefing — Pre-Market Report"},
        "title": f"{status_emoji} System Status: **{overall}** — {now_str}",
        "fields": [
            # Row 1 — Execution Gates
            {"name": "🚦 Execution Gates",
             "value": gate_str or "—",
             "inline": False},

            # Row 2 — Market State
            {"name": f"{pulse_emoji} Neural Pulse",
             "value": f"**{pulse}/100** — {pulse_label}",
             "inline": True},
            {"name": f"{gex_emoji} GEX Regime",
             "value": f"**{gex_regime}**",
             "inline": True},
            {"name": "📉 Bond Stress",
             "value": bond_str,
             "inline": True},

            # Row 3 — Prices
            {"name": "📊 SPY",
             "value": spy_str,
             "inline": True},
            {"name": "📊 QQQ",
             "value": qqq_str,
             "inline": True},
            {"name": "📅 Macro Events",
             "value": macro_str,
             "inline": True},

            # Row 4 — 7-day performance
            {"name": "📈 7-Day Performance",
             "value": perf_str,
             "inline": False},

            # Row 5 — Action
            {"name": "⚙️ ARJUN runs at",
             "value": "8:00 AM ET — signals fire automatically",
             "inline": True},
            {"name": "🎯 Today's Focus",
             "value": "POWER_HOUR lotto armed • ARKA scanning" if overall == "GO" else "Reduced sizing — CAUTION mode" if overall == "CAUTION" else "🚫 All gates blocked — NO TRADES",
             "inline": True},
        ],
        "footer": {"text": "CHAKRA Neural Trading OS • Daily Briefing • Powered by ARJUN + ARKA"}
    }

    # Send rich embed
    r = requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)
    if r.status_code in (200, 204):
        print(f"✅ Daily briefing embed posted to Discord")
    else:
        print(f"❌ Discord error: {r.status_code}")

    # Also send plain-text summary as second message
    plain = build_briefing()
    requests.post(DISCORD_WEBHOOK, json={"content": "```\n" + plain + "\n```"}, timeout=10)
    print("✅ Plain-text backup posted")

if __name__ == "__main__":
    import sys
    msg = build_briefing()
    if "--post" in sys.argv:
        post_briefing()
    else:
        print(msg)
