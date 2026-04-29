#!/usr/bin/env python3
"""
eod_summary.py — CHAKRA End-of-Day Intelligence Summary
Runs at 4:15 PM ET (after market close + journal writes settle)

Posts to #arka Discord channel:
  - Today's trade performance
  - What went wrong (missed signals, bad entries, module failures)
  - What ARJUN learned (conviction adjustments, regime detection)
  - Postmarket watchlist scan results
  - Module health check
"""

import os
import json
import sqlite3
import datetime
import requests
import argparse
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
BASE = Path.home() / "trading-ai"
LOGS = BASE / "logs"
ARKA_LOG_DIR = LOGS / "arka"
CHAKRA_LOG_DIR = LOGS / "chakra"
SWINGS_DB = LOGS / "swings" / "swings.db"
INTERNALS_FILE = LOGS / "internals" / "internals_latest.json"
WATCHLIST_FILE = CHAKRA_LOG_DIR / "watchlist_latest.json"

TODAY = datetime.date.today().strftime("%Y-%m-%d")
ARKA_SUMMARY_FILE = ARKA_LOG_DIR / f"summary_{TODAY}.json"


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def load_env():
    env_path = BASE / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


load_env()
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")


# ── Data Collectors ───────────────────────────────────────────────────────────

def get_arka_session():
    """Load today's ARKA session summary JSON."""
    return load_json(ARKA_SUMMARY_FILE)


def get_todays_trades_from_db():
    """Pull today's completed trades from swings DB."""
    trades = []
    if not SWINGS_DB.exists():
        return trades
    try:
        conn = sqlite3.connect(str(SWINGS_DB))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Detect actual table name
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        table = next((t for t in tables if "swing" in t.lower() or "position" in t.lower() or "trade" in t.lower()), None)
        if not table:
            print(f"  [DB] Tables found: {tables} — none matched, skipping")
            conn.close()
            return trades
        print(f"  [DB] Using table: {table}")
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        print(f"  [DB] Columns: {cols}")

        # Detect date/time column
        date_col = next((c for c in cols if c in ("entry_time", "open_time", "created_at", "date", "timestamp", "entry_date")), None)
        # Detect pnl column
        pnl_col = next((c for c in cols if c in ("pnl", "profit_loss", "realized_pnl", "pl", "gain_loss")), None)
        # Detect symbol column
        sym_col = next((c for c in cols if c in ("symbol", "ticker", "sym")), None)

        if not date_col:
            print(f"  [DB] Could not find date column in {cols} — fetching all rows")
            cur.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 50")
        else:
            cur.execute(f"""
                SELECT * FROM {table}
                WHERE date({date_col}) = ?
                ORDER BY rowid DESC
            """, (TODAY,))
        rows = cur.fetchall()
        for r in rows:
            d = dict(r)
            # Normalise to expected keys
            if pnl_col and pnl_col != "pnl":
                d["pnl"] = d.get(pnl_col, 0)
            if sym_col and sym_col != "symbol":
                d["symbol"] = d.get(sym_col, "?")
            trades.append(d)
        conn.close()
    except Exception as e:
        print(f"  [DB] Error reading trades: {e}")
    return trades


def get_module_health():
    """Check which module cache files are fresh (< 2 hours old)."""
    modules = {
        "DEX": "dex_latest.json",
        "Hurst": "hurst_latest.json",
        "VRP": "vrp_latest.json",
        "VEX": "vex_latest.json",
        "Charm": "charm_latest.json",
        "Entropy": "entropy_latest.json",
        "HMM": "hmm_latest.json",
        "IVSkew": "ivskew_latest.json",
        "Iceberg": "iceberg_latest.json",
        "Lambda": "lambda_latest.json",
        "COT": "cot_latest.json",
        "ProbDist": "probdist_latest.json",
    }
    health = {}
    today_midnight = datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp()
    for name, fname in modules.items():
        fpath = CHAKRA_LOG_DIR / fname
        if not fpath.exists():
            health[name] = "❌ missing — cron not writing cache"
        elif fpath.stat().st_mtime < today_midnight:
            health[name] = "⚠️ not updated today — check S1/S2/S3/S4 crons"
        else:
            health[name] = "✅"
    return health


def get_internals():
    return load_json(INTERNALS_FILE)


def get_watchlist():
    return load_json(WATCHLIST_FILE)


# ── Learning Engine ───────────────────────────────────────────────────────────

def analyze_what_went_wrong(session, trades):
    """
    Compare ARJUN signals vs actual trade outcomes.
    Returns list of learning observations.
    """
    lessons = []

    # Check if any signals fired but no trades taken (missed entries)
    signals_fired = session.get("signals_total", 0)
    trades_taken = session.get("trades_executed", len(trades))
    missed = signals_fired - trades_taken
    if missed > 0:
        lessons.append(f"📭 **{missed} signal(s) fired but no trade taken** — check threshold/gate logs")

    # Check for losing trades and correlate with module state
    losing_trades = [t for t in trades if t.get("pnl", 0) < 0]
    winning_trades = [t for t in trades if t.get("pnl", 0) > 0]

    if losing_trades:
        total_loss = sum(t.get("pnl", 0) for t in losing_trades)
        for t in losing_trades:
            symbol = t.get("symbol", "?")
            pnl = t.get("pnl", 0)
            direction = t.get("direction", "?")
            entry = t.get("entry_price", "?")
            lessons.append(
                f"🔴 **{symbol}** {direction} @ {entry} → PnL: ${pnl:+.2f}"
            )
        lessons.append(f"   Total loss: **${total_loss:+.2f}** across {len(losing_trades)} trade(s)")

    # Check conviction score accuracy
    avg_conviction = session.get("avg_conviction", None)
    win_rate = session.get("win_rate_today", None)
    if avg_conviction and win_rate:
        if avg_conviction > 70 and win_rate < 0.5:
            lessons.append("⚠️ **High conviction but low win rate** — modules may be over-scoring; review HMM/DEX alignment")
        elif avg_conviction < 50 and win_rate > 0.7:
            lessons.append("💡 **Low conviction but good win rate** — threshold may be too conservative; consider lowering by 5 pts")

    # Fakeout gating performance
    fakeouts_blocked = session.get("fakeouts_blocked", 0)
    if fakeouts_blocked > 3:
        lessons.append(f"🛡️ Fakeout gate blocked **{fakeouts_blocked}** entries — performing well")
    elif fakeouts_blocked == 0:
        lessons.append("🤔 Fakeout gate blocked 0 entries today — market was clean or gate too loose")

    return lessons, winning_trades, losing_trades


def arjun_learnings(session, internals):
    """
    Synthesize what ARJUN's modules observed and what should adjust tomorrow.
    """
    learnings = []

    # HMM regime
    hmm_data = load_json(CHAKRA_LOG_DIR / "hmm_latest.json")
    regime = hmm_data.get("regime", "UNKNOWN")
    if regime == "CHOPPY_RANGE":
        learnings.append("🔄 **HMM: CHOPPY_RANGE all day** — threshold was +15 pts; mean-reversion setups only")
    elif regime == "HIGH_VOL_TREND":
        learnings.append("📈 **HMM: HIGH_VOL_TREND** — momentum plays favored; threshold +5 pts applied")
    elif regime == "CRISIS":
        learnings.append("🚨 **HMM: CRISIS regime** — 0.25x size enforcement active; minimal exposure correct")
    elif regime != "UNKNOWN":
        learnings.append(f"📊 HMM regime today: **{regime}**")

    # VRP gate performance
    vrp_data = load_json(CHAKRA_LOG_DIR / "vrp_latest.json")
    vrp_state = vrp_data.get("state", "UNKNOWN")
    if vrp_state == "EXPENSIVE":
        learnings.append("💸 **VRP: IV EXPENSIVE** — Lotto/MOC gates correctly blocked premium selling")
    elif vrp_state == "CHEAP":
        learnings.append("💎 **VRP: IV CHEAP** — favorable for buying options; Lotto size multiplier applied")

    # DEX dealer positioning — skip if no cache (cron gap)
    dex_data = load_json(CHAKRA_LOG_DIR / "dex_latest.json")
    dex_signal = dex_data.get("signal", None)
    if dex_signal:
        learnings.append(f"🎯 DEX dealer position: **{dex_signal}** — {'ARKA got +10 pt boost' if dex_signal == 'DEALER_SHORT' else 'neutral/bearish dealer flow'}")
    else:
        learnings.append("🎯 DEX: **no cache today** — S1 morning cron (7:30 AM) may not have run")

    # Entropy
    entropy_data = load_json(CHAKRA_LOG_DIR / "entropy_latest.json")
    entropy_score = entropy_data.get("score", None)
    if entropy_score is not None:
        if entropy_score < 1.0:
            learnings.append(f"📉 **Entropy LOW ({entropy_score:.2f})** — choppy, size was reduced 0.5x")
        elif entropy_score > 2.0:
            learnings.append(f"📈 **Entropy HIGH ({entropy_score:.2f})** — directional consistency; size at 1.2x")

    # Neural pulse / risk mode
    # Neural pulse can be a nested dict
    pulse_raw = internals.get("neural_pulse", {})
    if isinstance(pulse_raw, dict):
        pulse = f"{pulse_raw.get('color', '')} {pulse_raw.get('label', 'UNKNOWN')} ({pulse_raw.get('score', '?')})"
    else:
        pulse = str(pulse_raw) if pulse_raw else "?"

    # VIX can be a nested dict
    vix_raw = internals.get("vix", {})
    if isinstance(vix_raw, dict):
        vix_class = vix_raw.get("classification", {})
        vix = f"{vix_class.get('icon', '')} {vix_class.get('regime', 'UNKNOWN')}"
    else:
        vix = str(vix_raw) if vix_raw else "?"

    risk_mode = internals.get('risk', {}).get('mode')
    if not risk_mode:
        # Fallback chain
        risk_mode = (
            internals.get("risk_mode")
            or internals.get("arka_boost", {}).get("risk_mode")
            or internals.get("arka_boost", {}).get("mode")
            or internals.get("market_regime")
            or "UNKNOWN"
        )
    if isinstance(risk_mode, dict):
        risk_mode = risk_mode.get("mode") or risk_mode.get("label") or risk_mode.get("regime") or "UNKNOWN"
    risk_mode = str(risk_mode) if risk_mode else "UNKNOWN"
    learnings.append(f"🧠 Neural Pulse: **{pulse}** | VIX: **{vix}** | Risk Mode: **{risk_mode}**")

    return learnings


# ── Discord Formatter ─────────────────────────────────────────────────────────

def build_discord_message(session, trades, internals):
    lessons, winners, losers = analyze_what_went_wrong(session, trades)
    learnings = arjun_learnings(session, internals)
    module_health = get_module_health()

    total_pnl = sum(t.get("pnl", 0) for t in trades if t.get("pnl") is not None)
    total_trades = len(trades)
    win_count = len(winners)
    loss_count = len(losers)
    win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0

    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    now_str = datetime.datetime.now().strftime("%I:%M %p ET")

    lines = []
    lines.append(f"## 🌆 CHAKRA EOD Intelligence Summary — {TODAY}")
    lines.append(f"*Generated at {now_str}*")
    lines.append("")

    # ── Performance ──
    lines.append("### 📊 Today's Performance")
    lines.append(f"{pnl_emoji} **Total PnL:** ${total_pnl:+.2f}")
    lines.append(f"📈 Trades: **{total_trades}** total | ✅ {win_count} wins | ❌ {loss_count} losses | Win rate: **{win_rate:.0f}%**")
    if session.get("avg_conviction"):
        lines.append(f"🎯 Avg conviction score: **{session.get('avg_conviction'):.1f}**")
    lines.append("")

    # ── What Went Wrong ──
    if lessons:
        lines.append("### 🔍 What Went Wrong / Flags")
        for l in lessons:
            lines.append(f"> {l}")
        lines.append("")

    # ── ARJUN Learnings ──
    lines.append("### 🤖 What ARJUN Observed Today")
    for l in learnings:
        lines.append(f"> {l}")
    lines.append("")

    # ── Module Health ──
    lines.append("### 🔧 Module Health")
    ok_modules = [k for k, v in module_health.items() if v == "✅"]
    warn_modules = {k: v for k, v in module_health.items() if v != "✅"}
    lines.append(f"✅ **OK:** {', '.join(ok_modules) if ok_modules else 'none'}")
    if warn_modules:
        for k, v in warn_modules.items():
            lines.append(f"  {v} **{k}**")
    lines.append("")

    # ── Watchlist for Tomorrow ──
    watchlist = get_watchlist()
    swing_candidates = watchlist.get("postmarket_candidates", watchlist.get("candidates", []))
    if swing_candidates:
        lines.append("### 👀 Swing Watchlist for Tomorrow")
        for c in swing_candidates[:6]:
            sym = c.get("symbol", c) if isinstance(c, dict) else c
            reason = c.get("reason", "") if isinstance(c, dict) else ""
            lines.append(f"  • **{sym}** {reason}")
        lines.append("")

    lines.append("─────────────────────────────")
    lines.append("*CHAKRA ARJUN · Automated EOD Report*")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print to console, don't send Discord")
    parser.add_argument("--date", default=TODAY, help="Override date (YYYY-MM-DD)")
    args = parser.parse_args()

    print(f"[EOD Summary] Running for {args.date}")

    session = get_arka_session()
    trades = get_todays_trades_from_db()
    internals = get_internals()

    if not session and not trades:
        print("  [WARN] No ARKA session data or trades found for today. Sending minimal summary.")

    msg = build_discord_message(session, trades, internals)

    if args.dry_run:
        print("\n" + "="*60)
        print(msg)
        print("="*60)
        return

    if not DISCORD_WEBHOOK:
        print("  [ERROR] DISCORD_WEBHOOK_URL not set in .env — cannot send")
        return

    resp = requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=10)
    if resp.status_code in (200, 204):
        print(f"  [OK] EOD summary posted to Discord ({len(msg)} chars)")
    else:
        print(f"  [ERROR] Discord post failed: {resp.status_code} — {resp.text[:200]}")


if __name__ == "__main__":
    main()
