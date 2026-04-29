"""
CHAKRA Discord Notifier v2
===========================
Posts trade alerts matching George's exact card format.

ARKA  — Indices engine (SPY/QQQ/IWM intraday)
CHAKRA — Stocks engine (AAPL/NVDA/TSLA etc)
Arjun  — Backend brain only, never posts directly

George's card structure (replicated exactly):
  Header:    [Strategy] 🔔 Opening LONG/PUTS Position: TICKER [Strategy]
  Subtext:   "ARKA is entering a trade."
  Row 1:     Account | Strategy
  Row 2:     Contract/Instrument
  Row 3:     Entry | Cost | Stop Loss
  Row 4:     Take Profit
  Row 5:     Analysis (conviction grade)
  Row 6:     Alignment | Session
  Row 7:     Confluence (score + all signal bullets)
  Row 8:     Reasoning
  Footer:    Balance | P&L | open positions
"""

import os
import httpx
import asyncio
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

log = logging.getLogger("CHAKRA.Discord")

# ── Deduplication guard — prevent same alert within 60 seconds ──────────────
import time as _time
_SENT_CACHE: dict = {}
_DEDUP_TTL = 60  # seconds

def _is_duplicate(key: str) -> bool:
    """Return True if this key was sent within the last 60 seconds."""
    now = _time.time()
    if key in _SENT_CACHE and now - _SENT_CACHE[key] < _DEDUP_TTL:
        return True
    _SENT_CACHE[key] = now
    return False
ET  = ZoneInfo("America/New_York")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# ── Brand colors ──────────────────────────────────────────────────────────────
COLOR_ARKA_LONG   = 0x9B59B6   # purple  — ARKA long entry
COLOR_ARKA_EXIT_W = 0x00FF9D   # green   — ARKA win
COLOR_ARKA_EXIT_L = 0xFF2D55   # red     — ARKA loss
COLOR_CHAKRA_BUY  = 0xFF7C2A   # orange  — CHAKRA buy
COLOR_CHAKRA_SELL = 0xFF2D55   # red     — CHAKRA sell
COLOR_SYSTEM      = 0x00D4FF   # cyan    — system / self-correct
COLOR_ARJUN       = 0xFFCC00   # gold    — morning brief
COLOR_WARN        = 0xFFCC00   # yellow  — warnings
COLOR_ERROR       = 0xFF2D55   # red     — errors

# ── Grade system (mimics George's A+ SETUP) ──────────────────────────────────
def conviction_grade(score: float) -> tuple[str, str]:
    """Returns (grade, label) based on conviction score."""
    if score >= 80: return "A+", "🌟 A+ SETUP (elite confluence)"
    if score >= 70: return "A",  "✅ A SETUP (strong confluence)"
    if score >= 60: return "B+", "📊 B+ SETUP (good confluence)"
    if score >= 55: return "B",  "📈 B SETUP (moderate confluence)"
    return "C", "⚠️ C SETUP (minimal confluence)"

def confluence_pct(score: float) -> str:
    """Convert score to George-style confidence %."""
    return f"{min(int(score * 1.14), 100)}% confidence"


def _read_pulse() -> dict:
    """Read Neural Pulse + GEX from latest internals file."""
    import json as _j, glob as _g, os as _o
    try:
        p = "logs/internals/internals_latest.json"
        if _o.path.exists(p):
            with open(p) as f: d = _j.load(f)
            return {
                "pulse":  d.get("neural_pulse", {}).get("score", 50),
                "regime": d.get("gex_regime", "UNKNOWN"),
            }
    except: pass
    return {"pulse": 50, "regime": "UNKNOWN"}

def score_bar(score: float, width: int = 12) -> str:
    filled = int(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


# ── Core webhook poster ───────────────────────────────────────────────────────

async def post_embed(embed: dict, username: str = "CHAKRA", avatar_url: str = None, webhook_url: str = None) -> bool:
    url = webhook_url or WEBHOOK_URL
    if not url:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping")
        return False
    payload = {"username": username, "embeds": [embed]}
    if avatar_url:
        payload["avatar_url"] = avatar_url
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            if r.status_code in (200, 204):
                log.info("  📣 Discord alert posted")
                return True
            log.warning(f"  Discord error {r.status_code}: {r.text[:120]}")
            return False
    except Exception as e:
        log.error(f"  Discord post failed: {e}")
        return False

def post_embed_sync(embed: dict, username: str = "CHAKRA", webhook_url: str = None) -> bool:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(post_embed(embed, username, webhook_url=webhook_url))
            return True
        return loop.run_until_complete(post_embed(embed, username, webhook_url=webhook_url))
    except Exception as e:
        log.error(f"  Sync post failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  ARKA — INDICES ENGINE ALERTS
# ══════════════════════════════════════════════════════════════════════════════

# ── Channel URLs by grade ───────────────────────────────────────────────────
# Set these webhook URLs in your .env or update here directly
WEBHOOK_EXTREME = os.getenv("DISCORD_WEBHOOK_ARKA_EXTREME", WEBHOOK_URL)
WEBHOOK_SIGNALS = os.getenv("DISCORD_WEBHOOK_ARKA_SIGNALS", WEBHOOK_URL)
WEBHOOK_LOG     = os.getenv("DISCORD_WEBHOOK_ARKA_LOG",     WEBHOOK_URL)

def _grade_channel(conviction: float) -> str:
    """Route to correct channel based on conviction grade."""
    if conviction >= 75: return WEBHOOK_EXTREME
    if conviction >= 55: return WEBHOOK_SIGNALS
    return WEBHOOK_LOG

async def post_arka_entry(signal: dict, position: dict) -> bool:
    """
    George-style ARKA entry card.
    Matches: header / account+strategy / instrument / entry+cost+stop /
             take profit / analysis / alignment+session / confluence / reasoning
    """
    ticker   = signal["ticker"]
    price    = signal["price"]
    conv     = signal["conviction"]
    session  = signal["session"]
    fakeout  = signal["fakeout_prob"]
    reasons  = signal.get("reasons", [])
    comp     = signal.get("components", {})
    qty      = position["qty"]
    stop     = position["stop"]
    target   = position["target"]
    risk     = position["risk_dollars"]
    cost     = round(price * qty, 2)

    stop_pct   = round((stop - price) / price * 100, 1)
    target_pct = round((target - price) / price * 100, 1)

    grade, grade_label = conviction_grade(conv)
    conf_pct           = confluence_pct(conv)
    bar                = score_bar(conv)

    # Session display
    session_map = {
        "OPEN":       "🔔 Opening Session (first 30min)",
        "NORMAL":     "📊 Normal Session",
        "POWER_HOUR": "⚡ Power Hour (2:30-4pm ET)",
        "LUNCH":      "🍽️ Lunch Session",
    }
    session_str = session_map.get(session, session)

    # Alignment — based on EMA stack and VWAP
    ema_bull  = comp.get("ema", 0) > 0
    vwap_bull = comp.get("vwap", 0) > 0
    if ema_bull and vwap_bull:
        alignment = "✅ ALIGNED"
        liq_note  = "EMA stack + VWAP confirmed"
    elif ema_bull or vwap_bull:
        alignment = "⚠️ PARTIAL"
        liq_note  = "Partial alignment"
    else:
        alignment = "❌ MIXED"
        liq_note  = "Mixed signals — proceed with caution"

    # Confluence bullet list — all signal reasons + component scores
    comp_bullets = []
    comp_icons   = {"vwap": "💧", "orb": "🔲", "macd": "📈", "rsi": "⚡", "volume": "📊", "ema": "〰️"}
    for k, v in comp.items():
        icon = comp_icons.get(k, "•")
        sign = "+" if v >= 0 else ""
        comp_bullets.append(f"{icon} {k.upper()}:{sign}{v:.0f}")

    signal_bullets = " • ".join(reasons) if reasons else "Multiple technical factors"
    comp_str       = " • ".join(comp_bullets)

    # Fakeout line
    fakeout_str = f"🤖 FakeoutFilter:{fakeout:.0%} (cleared ✅)" if fakeout < 0.55 else f"⚠️ FakeoutFilter:{fakeout:.0%}"

    # Full confluence block
    confluence_score = int(conv)
    confluence_text  = (
        f"{signal_bullets}\n"
        f"{comp_str}\n"
        f"{fakeout_str}"
    )

    # Reasoning
    direction_str = "bullish" if conv >= 55 else "bearish"
    reasoning = (
        f"Conviction score {conv:.1f}/100 — {grade} grade setup. "
        f"Session: {session}. "
        f"Structure: {direction_str.upper()} | "
        f"Fakeout probability: {fakeout:.0%} | "
        f"Risk: ${risk:,.2f}"
    )

    now_et = datetime.now(ET)
    time_str = now_et.strftime("%I:%M %p ET")

    embed = {
        "color":  COLOR_ARKA_LONG,
        "author": {
            "name": f"[ARKA (Indices)] 🔔 Opening LONG Position: {ticker} [ARKA (Indices)]"
        },
        "description": "**ARKA is entering a trade.**",
        "fields": [
            # Row 1 — Account + Strategy
            {"name": "🧳 Account",  "value": "Alpaca Paper",       "inline": True},
            {"name": "🏛️ Strategy", "value": "ARKA — Indices",     "inline": True},
            {"name": "\u200b",      "value": "\u200b",             "inline": True},

            # Row 2 — Instrument
            {"name": "📋 Instrument", "value": f"**{ticker}** Equity", "inline": False},

            # Row 3 — Entry / Cost / Stop
            {"name": "💵 Entry",     "value": f"**${price:.2f}** x {qty} shares", "inline": True},
            {"name": "💰 Cost",      "value": f"**${cost:,.2f}**",                "inline": True},
            {"name": "🛑 Stop Loss", "value": f"**${stop:.2f}** ({stop_pct}%)",   "inline": True},

            # Row 4 — Take Profit
            {"name": "🎯 Take Profit",
             "value": f"**${target:.2f}** (+{target_pct}%) — then trail stop",
             "inline": False},

            # Row 5 — Analysis (conviction grade)
            {"name": "📊 ARKA Analysis",
             "value": f"{grade_label}\n`{bar}` {conv:.1f}/100 — {conf_pct}",
             "inline": False},

            # Row 6 — Alignment + Session
            {"name": "🔗 Alignment", "value": alignment,    "inline": True},
            {"name": "⏰ Session",   "value": session_str,  "inline": True},
            {"name": "\u200b",       "value": "\u200b",     "inline": True},

            # Row 7 — Confluence
            {"name": f"🧩 Confluence ({confluence_score})",
             "value": confluence_text,
             "inline": False},

            # Row 8 — Reasoning
            {"name": "📝 Reasoning",
             "value": reasoning,
             "inline": False},
        ],
        "footer": {
            "text": f"ARKA Engine • {time_str} • Paper Trading • Risk ${risk:,.2f} at stake"
        },
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    # ── LAYMAN PLAIN-TEXT MESSAGE ────────────────────────────────────
    internals  = _read_pulse()
    pulse      = signal.get("neural_pulse", internals["pulse"])
    gex_regime = signal.get("gex_regime",   internals["regime"])
    uoa        = signal.get("uoa_detected", False)
    sec_mod    = signal.get("sector_modifier", 0)

    # Enrich embed with new fields before sending
    embed["fields"].insert(-1, {
        "name": "⚡ Neural Pulse",
        "value": f"{pulse}/100 {'🟢' if int(pulse)>=65 else '🟡' if int(pulse)>=50 else '🔴'}",
        "inline": True
    })
    embed["fields"].insert(-1, {
        "name": "📊 GEX Regime",
        "value": f"{'🔴' if 'NEG' in str(gex_regime) else '🟢'} {gex_regime}",
        "inline": True
    })
    embed["fields"].insert(-1, {
        "name": "🌊 UOA Hit",
        "value": "✅ Confirmed" if uoa else "❌ None",
        "inline": True
    })
    if sec_mod != 0:
        embed["fields"].insert(-1, {
            "name": "🔄 Sector Mod",
            "value": f"{int(sec_mod):+d} pts",
            "inline": True
        })

    ok = await post_embed(embed, username="ARKA (Indices)")

    # ── LAYMAN MESSAGE ────────────────────────────────────────────────
    reasons_list = []
    conv_int = int(conv)
    if conv_int >= 75:   reasons_list.append("very strong setup")
    elif conv_int >= 60: reasons_list.append("solid setup")
    else:                reasons_list.append("conditions met")
    if uoa:              reasons_list.append("big money spotted buying the same direction")
    vwap_comp = comp.get("vwap", 0)
    if vwap_comp > 0:    reasons_list.append("price is above its daily average (VWAP)")
    if "NEGATIVE" in str(gex_regime): reasons_list.append("market makers may amplify this move")
    elif "POSITIVE" in str(gex_regime): reasons_list.append("market makers are stabilizing price")
    pulse_int = int(pulse) if str(pulse).isdigit() else 50
    if pulse_int >= 65:  reasons_list.append("market internals are strong")
    elif pulse_int <= 35: reasons_list.append("market internals are weak — defensive sizing")

    reason_text = ", and ".join(reasons_list) if reasons_list else "multiple technical factors aligned"
    cost_str    = f"${cost:,.2f}"

    layman = (
        f"📈 **ARKA just entered a trade!**\n\n"
        f"🛒 Buying **{qty} {ticker}** shares "
        f"at **${price:.2f}** (total cost ≈ **{cost_str}**).\n\n"
        f"💬 *Why?* Because there's a {reason_text}.\n\n"
        f"🎯 Target: **${target:.2f}** (+{target_pct}%) "
        f"| 🛑 Stop: **${stop:.2f}** ({stop_pct}%)"
    )
    # Dedup check — don't send same ticker+direction within 60s
    _dedup_key = f"arka_entry_{signal.get('ticker','?')}_{signal.get('direction','?')}"
    if _is_duplicate(_dedup_key):
        log.info(f"  🔕 Skipping duplicate Discord alert for {_dedup_key}")
        return True

    # Route to correct channel by conviction grade
    conviction = float(signal.get("conviction", 50))
    _channel   = _grade_channel(conviction)

    await post_embed({"description": layman}, username="ARKA", webhook_url=_channel)

    # ── Also post clean alert to #arjun-alerts ─────────────────────
    try:
        from backend.arka.arka_discord_notifier import post_arka_entry as _arjun_entry
        await _arjun_entry(signal, position)
    except Exception as _ae:
        log.warning(f"  arjun-alerts entry post failed: {_ae}")

    return ok


async def post_arka_exit(ticker: str, entry: float, exit_price: float,
                          qty: int, reason: str, session: str = "") -> bool:
    """George-style ARKA exit card — matches his Position Closed format exactly."""
    pnl     = round((exit_price - entry) * qty, 2)
    pnl_pct = round((exit_price - entry) / entry * 100, 1)
    cost    = round(entry * qty, 2)
    won     = pnl > 0

    reason_clean = reason.replace("_", " ").upper()
    result_icon  = "🎯" if "TARGET" in reason_clean or "PROFIT" in reason_clean else "🛑"
    result_label = "WIN ✅" if won else "LOSS ❌"
    color        = COLOR_ARKA_EXIT_W if won else COLOR_ARKA_EXIT_L
    pnl_str      = f"{'+'if won else ''}{pnl:,.2f} ({pnl_pct:+.1f}%)"

    now_et   = datetime.now(ET)
    time_str = now_et.strftime("%I:%M %p ET")

    embed = {
        "color":  color,
        "author": {
            "name": f"[ARKA (Indices)] {'▲' if won else '▼'} {ticker}: Position Closed"
        },
        "description": f"{'Profit secured. On to the next. 🚀' if won else 'Stopping out. On to the next. 📉'}",
        "fields": [
            # Row 1
            {"name": "🧳 Account",  "value": "Alpaca Paper",   "inline": True},
            {"name": "🏛️ Strategy", "value": "ARKA — Indices", "inline": True},
            {"name": "\u200b",      "value": "\u200b",         "inline": True},

            # Row 2 — Instrument
            {"name": "📋 Instrument", "value": f"**{ticker}** Equity", "inline": False},

            # Row 3 — Size / Entry / Exit
            {"name": "📦 Size",   "value": f"**{qty} shares**",     "inline": True},
            {"name": "📥 Entry",  "value": f"**${entry:.2f}**",     "inline": True},
            {"name": "📤 Exit",   "value": f"**${exit_price:.2f}**","inline": True},

            # Row 4 — P&L
            {"name": "💰 P&L",
             "value": f"**{pnl_str}**",
             "inline": False},

            # Row 5 — Reason
            {"name": "📝 Reason",
             "value": reason_clean,
             "inline": False},
        ],
        "footer": {
            "text": f"ARKA Engine • {time_str} • {result_label} • Cost basis ${cost:,.2f}"
        },
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    # Route exit to the same signals channel as entry (not the default webhook)
    _exit_channel = WEBHOOK_SIGNALS if WEBHOOK_SIGNALS else WEBHOOK_URL
    ok = await post_embed(embed, username="ARKA (Indices)", webhook_url=_exit_channel)

    # ── Also post clean close alert to #arjun-alerts ───────────────
    try:
        from backend.arka.arka_discord_notifier import post_arka_exit as _arjun_exit
        _arjun_exit(ticker, entry, exit_price, qty, reason)
    except Exception as _ae:
        log.warning(f"  arjun-alerts exit post failed: {_ae}")

    return ok


async def post_arka_daily_summary(summary: dict) -> bool:
    """Post ARKA's 4pm end-of-day summary."""
    trades    = summary.get("trades", 0)
    pnl       = summary.get("daily_pnl", 0)
    log_      = summary.get("trade_log", [])
    wins      = len([t for t in log_ if (t.get("pnl") or 0) > 0])
    losses    = len([t for t in log_ if (t.get("pnl") or 0) <= 0 and t.get("pnl") is not None])
    win_rate  = round(wins / trades * 100) if trades > 0 else 0
    pnl_str   = f"{'+'if pnl>=0 else ''}{pnl:,.2f}"
    color     = COLOR_ARKA_EXIT_W if pnl >= 0 else COLOR_ARKA_EXIT_L
    today     = datetime.now(ET).strftime("%A, %B %d %Y")

    # Trade log table
    if log_:
        log_lines = []
        for t in log_:
            side  = t.get("side", "?")
            sym   = t.get("ticker", "?")
            price = t.get("price", 0)
            tpnl  = t.get("pnl")
            pnl_tag = f"{'+'if (tpnl or 0)>0 else ''}{tpnl:,.2f}" if tpnl is not None else "open"
            icon  = "✅" if (tpnl or 0) > 0 else "❌" if tpnl is not None else "⏳"
            log_lines.append(f"{icon} `{t.get('time','')}` {side} {sym} @ ${price:.2f} → {pnl_tag}")
        trade_log_text = "\n".join(log_lines)
    else:
        trade_log_text = "*No trades today*"

    embed = {
        "color": color,
        "author": {"name": f"📊 ARKA (Indices) — Daily Summary"},
        "description": f"**End of day report — {today}**",
        "fields": [
            {"name": "📈 Trades",    "value": str(trades),          "inline": True},
            {"name": "✅ Wins",      "value": str(wins),            "inline": True},
            {"name": "❌ Losses",    "value": str(losses),          "inline": True},
            {"name": "🏆 Win Rate",  "value": f"{win_rate}%",       "inline": True},
            {"name": "💰 Daily P&L", "value": f"**${pnl_str}**",   "inline": True},
            {"name": "\u200b",       "value": "\u200b",            "inline": True},
            {"name": "📋 Trade Log", "value": trade_log_text,       "inline": False},
        ],
        "footer": {"text": f"ARKA Engine • Market closed • See you tomorrow 🚀"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed(embed, username="ARKA (Indices)")


async def post_arka_self_correct(old_config: dict, new_config: dict, reason: str) -> bool:
    """Post self-correction notification when thresholds auto-adjust."""
    old_thr = old_config.get("thresholds", {})
    new_thr = new_config.get("thresholds", {})

    changes = []
    for k in ("conviction_normal", "conviction_power_hour", "fakeout_block"):
        ov = old_thr.get(k)
        nv = new_thr.get(k)
        if ov != nv:
            arrow = "🔼" if nv > ov else "🔽"
            changes.append(f"{arrow} **{k.replace('_',' ').title()}**: `{ov}` → `{nv}`")

    if not changes:
        return False

    embed = {
        "color": COLOR_SYSTEM,
        "author": {"name": "🧠 ARKA Self-Correction Engine — Thresholds Updated"},
        "description": f"**Trigger:** {reason}",
        "fields": [
            {"name": "⚙️ Changes Applied", "value": "\n".join(changes),                      "inline": False},
            {"name": "📋 Effect",           "value": "New thresholds active immediately — no restart needed", "inline": False},
            {"name": "🔒 Safety Rails",     "value": "Conviction: 40–70 | Fakeout: 0.35–0.75", "inline": False},
        ],
        "footer": {"text": f"ARKA Self-Correct • {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed(embed, username="ARKA (Indices)")


# ══════════════════════════════════════════════════════════════════════════════
#  CHAKRA — STOCKS ENGINE ALERTS
# ══════════════════════════════════════════════════════════════════════════════

async def post_chakra_entry(signal: dict) -> bool:
    """George-style CHAKRA stock entry card."""
    ticker   = signal["ticker"]
    sig      = signal.get("signal", "BUY")
    price    = signal.get("price", 0)
    entry    = signal.get("entry", price)
    target   = signal.get("target", 0)
    stop     = signal.get("stop", 0)
    conf     = signal.get("confidence", 0)
    reasons  = signal.get("reasons", [])
    comp     = signal.get("components", {})
    sector   = signal.get("sector", "Unknown")
    name     = signal.get("name", ticker)
    rsi      = signal.get("rsi", 0)
    vol_ratio = signal.get("volume_ratio", 1.0)

    target_pct = round((target - entry) / entry * 100, 1) if entry else 0
    stop_pct   = round((stop - entry) / entry * 100, 1)   if entry else 0
    rr         = round(abs(target - entry) / abs(entry - stop), 2) if stop != entry else 0

    grade, grade_label = conviction_grade(conf)
    conf_pct           = confluence_pct(conf)
    bar                = score_bar(conf)
    color              = COLOR_CHAKRA_BUY if sig == "BUY" else COLOR_CHAKRA_SELL
    direction_icon     = "🟢" if sig == "BUY" else "🔴"
    action             = "LONG" if sig == "BUY" else "SHORT"

    # Confluence
    comp_bullets = []
    for k, v in comp.items():
        sign = "+" if v >= 0 else ""
        comp_bullets.append(f"📊 {k.upper()}:{sign}{v:.0f}")

    signal_bullets  = " • ".join(reasons) if reasons else "Technical confluence"
    comp_str        = " • ".join(comp_bullets)
    vol_str         = f"📦 VOL:{vol_ratio:.1f}x avg"
    rsi_str         = f"⚡ RSI:{rsi:.0f}"
    confluence_text = f"{signal_bullets}\n{comp_str}\n{rsi_str} • {vol_str}"

    reasoning = (
        f"{name} ({sector}) showing {sig} setup. "
        f"Confidence {conf:.1f}% — {grade} grade. "
        f"R:R {rr:.1f} | Stop {stop_pct}% | Target +{target_pct}%"
    )

    now_et   = datetime.now(ET)
    time_str = now_et.strftime("%I:%M %p ET")

    embed = {
        "color": color,
        "author": {
            "name": f"[CHAKRA (Stocks)] 🔔 Opening {action} Position: {ticker} [CHAKRA (Stocks)]"
        },
        "description": "**CHAKRA has identified a trade setup.**",
        "fields": [
            {"name": "🧳 Account",  "value": "Alpaca Paper",      "inline": True},
            {"name": "🏛️ Strategy", "value": "CHAKRA — Stocks",   "inline": True},
            {"name": "\u200b",      "value": "\u200b",            "inline": True},

            {"name": "📋 Instrument", "value": f"**{ticker}** — {name} ({sector})", "inline": False},

            {"name": "💵 Entry",     "value": f"**${entry:.2f}**",                "inline": True},
            {"name": "💰 Price",     "value": f"**${price:.2f}**",                "inline": True},
            {"name": "🛑 Stop Loss", "value": f"**${stop:.2f}** ({stop_pct}%)",   "inline": True},

            {"name": "🎯 Take Profit",
             "value": f"**${target:.2f}** (+{target_pct}%) | R:R **{rr:.1f}**",
             "inline": False},

            {"name": "📊 CHAKRA Analysis",
             "value": f"{grade_label}\n`{bar}` {conf:.1f}% — {conf_pct}",
             "inline": False},

            {"name": "🔗 Alignment",  "value": "✅ ALIGNED" if conf >= 65 else "⚠️ PARTIAL", "inline": True},
            {"name": "📦 Volume",     "value": f"**{vol_ratio:.1f}x** average",                "inline": True},
            {"name": "\u200b",        "value": "\u200b",                                       "inline": True},

            {"name": f"🧩 Confluence ({int(conf)})",
             "value": confluence_text,
             "inline": False},

            {"name": "📝 Reasoning",
             "value": reasoning,
             "inline": False},
        ],
        "footer": {
            "text": f"CHAKRA Stocks • {time_str} • Paper Trading • Powered by Arjun ML"
        },
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed(embed, username="CHAKRA (Stocks)")


async def post_chakra_exit(ticker: str, name: str, entry: float, exit_price: float,
                            qty: int, reason: str) -> bool:
    """George-style CHAKRA exit card."""
    pnl     = round((exit_price - entry) * qty, 2)
    pnl_pct = round((exit_price - entry) / entry * 100, 1)
    won     = pnl > 0
    color   = COLOR_ARKA_EXIT_W if won else COLOR_ARKA_EXIT_L
    pnl_str = f"{'+'if won else ''}{pnl:,.2f} ({pnl_pct:+.1f}%)"
    result  = "WIN ✅" if won else "LOSS ❌"

    embed = {
        "color": color,
        "author": {
            "name": f"[CHAKRA (Stocks)] {'▲' if won else '▼'} {ticker}: Position Closed"
        },
        "description": f"{'Profit secured. 🚀' if won else 'Stopping out. 📉'}",
        "fields": [
            {"name": "🧳 Account",    "value": "Alpaca Paper",          "inline": True},
            {"name": "🏛️ Strategy",  "value": "CHAKRA — Stocks",       "inline": True},
            {"name": "\u200b",        "value": "\u200b",                "inline": True},

            {"name": "📋 Instrument", "value": f"**{ticker}** — {name}", "inline": False},

            {"name": "📦 Size",  "value": f"**{qty} shares**",      "inline": True},
            {"name": "📥 Entry", "value": f"**${entry:.2f}**",      "inline": True},
            {"name": "📤 Exit",  "value": f"**${exit_price:.2f}**", "inline": True},

            {"name": "💰 P&L",    "value": f"**{pnl_str}**",                      "inline": True},
            {"name": "📝 Reason", "value": reason.replace("_"," ").upper(),        "inline": True},
            {"name": "\u200b",    "value": "\u200b",                               "inline": True},
        ],
        "footer": {
            "text": f"CHAKRA Stocks • {datetime.now(ET).strftime('%I:%M %p ET')} • {result}"
        },
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed(embed, username="CHAKRA (Stocks)")


async def post_chakra_daily_summary(signals: list[dict], date_str: str = "") -> bool:
    """Post CHAKRA's end-of-day stock signal summary."""
    if not date_str:
        date_str = datetime.now(ET).strftime("%A, %B %d %Y")

    buys  = [s for s in signals if s.get("signal") == "BUY"]
    sells = [s for s in signals if s.get("signal") == "SELL"]
    total = len(signals)

    signal_lines = []
    for s in signals:
        icon = "🟢" if s.get("signal") == "BUY" else "🔴"
        signal_lines.append(
            f"{icon} **{s['ticker']}** {s.get('signal')} @ ${s.get('price',0):.2f} "
            f"→ T:${s.get('target',0):.2f} S:${s.get('stop',0):.2f} | {s.get('confidence',0):.0f}%"
        )

    embed = {
        "color": COLOR_CHAKRA_BUY,
        "author": {"name": "📊 CHAKRA (Stocks) — Daily Signal Summary"},
        "description": f"**{date_str}** — {total} setups identified today",
        "fields": [
            {"name": "🟢 Buys",  "value": str(len(buys)),  "inline": True},
            {"name": "🔴 Sells", "value": str(len(sells)), "inline": True},
            {"name": "📋 Total", "value": str(total),      "inline": True},
            {"name": "📈 Signals",
             "value": "\n".join(signal_lines) if signal_lines else "*No signals today*",
             "inline": False},
        ],
        "footer": {"text": f"CHAKRA Stocks • Powered by Arjun ML • Paper Trading"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed(embed, username="CHAKRA (Stocks)")


# ══════════════════════════════════════════════════════════════════════════════
#  ARJUN — MORNING BRIEF (backend brain, posts as CHAKRA)
# ══════════════════════════════════════════════════════════════════════════════

async def post_arjun_morning_brief(signals: list[dict]) -> bool:
    """
    Arjun's morning ETF signal brief — posts under CHAKRA brand.
    Arjun is the brain; CHAKRA is the face.
    """
    if not signals:
        return False

    buys  = [s for s in signals if s.get("signal") == "BUY"]
    sells = [s for s in signals if s.get("signal") == "SELL"]
    holds = [s for s in signals if s.get("signal") == "HOLD"]
    today = datetime.now(ET).strftime("%A, %B %d %Y")

    def sig_line(s):
        ticker = s.get("ticker","?")
        price  = s.get("price", s.get("entry", 0))
        target = s.get("target", 0)
        stop   = s.get("stop", 0)
        conf   = s.get("confidence", s.get("win_rate", 0))
        entry  = s.get("entry", price)
        rr     = round(abs(target-entry)/abs(entry-stop),1) if stop != entry else 0
        t_pct  = round((target-entry)/entry*100,1) if entry else 0
        s_pct  = round((stop-entry)/entry*100,1)   if entry else 0
        return (
            f"**{ticker}** @ `${price:.2f}`\n"
            f"  Entry `${entry:.2f}` | Target `${target:.2f}` (+{t_pct}%) | Stop `${stop:.2f}` ({s_pct}%)\n"
            f"  R:R `{rr}` | Confidence `{conf:.1f}%`"
        )

    buy_text  = "\n\n".join(sig_line(s) for s in buys)  or "*None today*"
    sell_text = "\n\n".join(sig_line(s) for s in sells) or "*None today*"
    hold_text = " • ".join(f"**{s.get('ticker','')}**" for s in holds) or "*None*"
    avg_conf  = sum(s.get("confidence", 77) for s in signals) / len(signals) if signals else 0

    embed = {
        "color": COLOR_ARJUN,
        "author": {"name": "⚡ CHAKRA — Morning ETF Signal Brief"},
        "description": (
            f"**{today}**\n"
            f"Arjun ML-powered swing signals across SPY, QQQ, IWM, DIA, XLF, XLE, XLK\n"
            f"Avg confidence: **{avg_conf:.1f}%** | Historical win rate: **77%**"
        ),
        "fields": [
            {"name": f"🟢 BUY Signals ({len(buys)})",  "value": buy_text,  "inline": False},
            {"name": f"🔴 SELL Signals ({len(sells)})", "value": sell_text, "inline": False},
            {"name": f"🟡 HOLD ({len(holds)})",         "value": hold_text, "inline": False},
            {"name": "⚠️ Note",
             "value": "Long-only paper system. SELL = informational only. Retraining after Day 30.",
             "inline": False},
        ],
        "footer": {
            "text": "CHAKRA System • Arjun Engine • 20yr backtest • 77% win rate • Sharpe 1.2"
        },
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed(embed, username="CHAKRA (Morning Brief)")


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM ALERTS
# ══════════════════════════════════════════════════════════════════════════════

async def post_system_alert(title: str, message: str, level: str = "info",
                             engine: str = "CHAKRA") -> bool:
    color = {
        "info":    COLOR_SYSTEM,
        "warning": COLOR_WARN,
        "error":   COLOR_ERROR,
        "success": 0x00FF9D,
    }.get(level, COLOR_SYSTEM)

    icon = {"info":"ℹ️","warning":"⚠️","error":"❌","success":"✅"}.get(level,"•")

    embed = {
        "color": color,
        "author": {"name": f"{icon} {engine} System — {title}"},
        "description": message,
        "footer": {"text": f"{engine} • {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed(embed, username=f"{engine} System")


# ══════════════════════════════════════════════════════════════════════════════
#  TEST
# ══════════════════════════════════════════════════════════════════════════════

async def test_webhook():
    return await post_system_alert(
        title="Webhook Connected ✅",
        message=(
            "**CHAKRA Discord system is live!**\n\n"
            "You'll receive alerts from:\n"
            "🟣 **ARKA (Indices)** — SPY/QQQ/IWM intraday entries, exits, daily summary\n"
            "🟠 **CHAKRA (Stocks)** — Individual stock setups, exits, daily summary\n"
            "⚡ **CHAKRA Morning Brief** — Daily ETF signals at 8am (powered by Arjun)\n"
            "🧠 **ARKA Self-Correct** — Threshold auto-adjustments\n"
            "🔧 **System** — Engine status, errors, daily loss limit hits\n\n"
            "*Arjun runs silently as the ML brain powering all signals.*"
        ),
        level="success",
        engine="CHAKRA"
    )


if __name__ == "__main__":
    print("Testing Discord webhook...")
    result = asyncio.run(test_webhook())
    print("✅ Success! Check your Discord channel." if result else "❌ Failed — check DISCORD_WEBHOOK_URL")


# ══════════════════════════════════════════════════════════════════════════════
#  PRE-MARKET BRIEF
# ══════════════════════════════════════════════════════════════════════════════

async def post_premarket_brief(data: dict) -> bool:
    """
    Post George-style pre-market game plan to Discord.
    One embed per ticker showing bias, levels, and conditional plans.
    """
    today = data.get("date", date.today().isoformat())
    gen   = data.get("generated", "8:00 AM ET")

    # Header card first
    header_embed = {
        "color": 0xFFCC00,
        "author": {"name": "☀️ CHAKRA — Pre-Market Game Plan"},
        "description": (
            f"**Game Plan for {today}**\n"
            f"Generated at {gen} • Arjun ML-powered bias analysis\n"
            f"*CHAKRA is calibrating. Calculating maximum confluence...*"
        ),
        "footer": {"text": "CHAKRA System • Pre-Market Phase • All levels are key decision zones"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    await post_embed(header_embed, username="CHAKRA (Pre-Market)")
    await asyncio.sleep(1)

    # One card per ticker
    for ticker, result in data.get("tickers", {}).items():
        if result.get("error"):
            continue

        bias   = result.get("bias", {})
        levels = result.get("levels", {})
        plans  = result.get("game_plans", [])

        bias_str     = bias.get("bias", "NEUTRAL")
        bias_strength = bias.get("strength", "")
        score        = bias.get("score", 50)
        factors      = bias.get("factors", [])
        arjun_sig    = bias.get("arjun_signal")
        arjun_conf   = bias.get("arjun_conf", 0)

        color = (0x00FF9D if bias_str == "BULLISH" else
                 0xFF2D55 if bias_str == "BEARISH" else 0xFFCC00)

        bias_icon = "📈" if bias_str == "BULLISH" else "📉" if bias_str == "BEARISH" else "↔️"
        bar_filled = int(score / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)

        # Key levels text
        watch = result.get("watch_list", [])
        high_items = [w for w in watch if w["importance"] == "high"]
        levels_text = "\n".join(
            f"`${w['price']:.2f}` — {w['label'].split(' ',1)[1] if ' ' in w['label'] else w['label']}"
            for w in high_items[:6]
        ) or "Loading..."

        # Game plans text
        plans_text = ""
        for p in plans:
            conf_tag = "✅" if p["confidence"] == "HIGH" else "⚠️"
            plans_text += (
                f"{p['icon']} **{p['condition']}**\n"
                f"  → {conf_tag} {p['action']} | Entry `${p['entry']:.2f}` "
                f"Target `${p['target']:.2f}` Stop `${p['stop']:.2f}`\n"
                f"  *{p['note']}*\n\n"
            )

        # Arjun line
        arjun_text = (
            f"**{arjun_sig}** ({arjun_conf:.0f}% confidence)"
            if arjun_sig else "Signal not yet generated"
        )

        embed = {
            "color": color,
            "author": {
                "name": f"{bias_icon} {ticker} [{result.get('name',ticker)}] — Pre-market: {bias_str}"
            },
            "fields": [
                # Bias score
                {"name": f"🎯 Bias Score ({score:.0f}/100)",
                 "value": f"`{bar}` {bias_strength} {bias_str}",
                 "inline": False},

                # Factors
                {"name": "🧠 Arjun ML",
                 "value": arjun_text,
                 "inline": True},
                {"name": "📊 Factors",
                 "value": " • ".join(factors[:3]) if factors else "N/A",
                 "inline": True},
                {"name": "\u200b", "value": "\u200b", "inline": True},

                # Levels
                {"name": "📍 Levels to Watch",
                 "value": levels_text,
                 "inline": False},

                # Game plans
                {"name": "🎮 Conditional Game Plans",
                 "value": plans_text.strip() if plans_text else "No plans generated",
                 "inline": False},

                # Expected range
                {"name": "📊 Expected Range",
                 "value": (
                     f"High: `${levels.get('expected_high',0):.2f}` | "
                     f"Low: `${levels.get('expected_low',0):.2f}` | "
                     f"ATR: `${levels.get('atr',0):.2f}`"
                 ),
                 "inline": False},
            ],
            "footer": {
                "text": f"CHAKRA Pre-Market • {gen} • Confirm direction after 9:45am ET"
            },
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }

        await post_embed(embed, username="CHAKRA (Pre-Market)")
        await asyncio.sleep(1)  # avoid rate limiting between cards

    return True


# ══════════════════════════════════════════════════════════════════════════════
#  OPTIONS ENGINE ALERTS
# ══════════════════════════════════════════════════════════════════════════════

async def post_ticker_licker(data: dict) -> bool:
    """Post 0DTE Ticker Licker top plays to Discord — matches George's format."""
    plays = data.get("plays", [])[:9]
    if not plays:
        return False

    time_str = data.get("time", "")
    total    = data.get("total", 0)
    calls    = [p for p in plays if p["type"] == "CALL"]
    puts     = [p for p in plays if p["type"] == "PUT"]

    def play_line(p):
        conf_bar = "█" * int(p["confidence"]/10) + "░" * (10 - int(p["confidence"]/10))
        return (
            f"**{p['ticker']} ${p['strike']} {p['type']}** "
            f"{'🟢' if p['type']=='CALL' else '🔴'} "
            f"`{date.today().strftime('%b %d')}` 0DTE\n"
            f"`${p['spot']:.2f}` → {p['pct_from_strike']:+.1f}% from strike\n"
            f"Entry `${p['entry']:.2f}` | Conf `{p['confidence']:.0f}%` | "
            f"Gamma `{p['gamma']:.3f}`\n"
            f"`{conf_bar}`"
        )

    fields = []

    # Calls block
    if calls:
        fields.append({
            "name":   f"🚀 BULLISH SETUPS ({len(calls)})",
            "value":  "\n\n".join(play_line(p) for p in calls[:4]),
            "inline": False
        })

    # Puts block
    if puts:
        fields.append({
            "name":   f"🐻 BEARISH SETUPS ({len(puts)})",
            "value":  "\n\n".join(play_line(p) for p in puts[:4]),
            "inline": False
        })

    embed = {
        "color": 0xFFCC00,
        "author": {"name": f"🎯 CHAKRA — 0DTE Ticker Licker | High Confidence First"},
        "description": (
            f"Same-day expiration plays • Updated {time_str}\n"
            f"Scanned {total} contracts • Showing top {len(plays)} by confidence"
        ),
        "fields": fields,
        "footer": {
            "text": f"CHAKRA Options Engine • 0DTE • Powered by Polygon Options Advanced • Updates every 5min"
        },
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed(embed, username="CHAKRA (0DTE)")


async def post_opening_bell_prep(data: dict) -> bool:
    """Post Opening Bell Prep card at 9:25am — matches George's exact format."""
    bullish = data.get("bullish", [])
    bearish = data.get("bearish", [])
    flips   = data.get("flips", [])
    tickers = data.get("tickers", {})
    time_str = data.get("time", "9:25 AM ET")

    # Header
    header_embed = {
        "color": 0xFFCC00,
        "author": {"name": f"🔔 CHAKRA — OPENING BELL PREP (5 min to open)"},
        "description": (
            f"**CHAKRA's game plan UPDATED based on live price action.**\n"
            f"{len(flips)} direction(s) refreshed.\n\n"
            + (f"⚠️ **FLIPS DETECTED:**\n" + "\n".join(f"• {f}" for f in flips) if flips else "✅ No direction changes from morning plan")
        ),
        "footer": {"text": f"CHAKRA Options Engine • {time_str} • Confirm after 9:45am ET"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    await post_embed(header_embed, username="CHAKRA (Bell Prep)")
    await asyncio.sleep(0.5)

    # Bullish setups
    if bullish:
        bull_lines = []
        for ticker in bullish:
            td   = tickers.get(ticker, {})
            plan = td.get("plans", {}).get("lows_swept", {})
            gap  = td.get("gap_pct", 0)
            gap_str = f"Gapping {'UP' if gap>0 else 'DOWN'} {abs(gap):.2f}%" if gap else "Flat open expected"
            cwall = td.get("call_wall")
            pwall = td.get("put_wall")
            walls_str = f"Walls: C${cwall:.0f} P${pwall:.0f}" if cwall and pwall else ""

            bull_lines.append(
                f"🚀 **{ticker}** BUY CALLS{' *(was BEARISH)*' if td.get('flipped') else ''}\n"
                f"{gap_str}\n"
                f"Strike: `${plan.get('entry',0):.2f}` | Exp: {plan.get('expiry','0DTE')}\n"
                f"Target: `${plan.get('target',0):.2f}`"
                + (f"\n{walls_str}" if walls_str else "")
            )

        bull_embed = {
            "color": 0x00FF9D,
            "author": {"name": f"🚀 BULLISH SETUPS ({len(bullish)})"},
            "description": "\n\n".join(bull_lines),
            "footer": {"text": f"Direction confirmed by sweep + reversal after 9:45 AM"},
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        await post_embed(bull_embed, username="CHAKRA (Bell Prep)")
        await asyncio.sleep(0.5)

    # Bearish setups
    if bearish:
        bear_lines = []
        for ticker in bearish:
            td   = tickers.get(ticker, {})
            plan = td.get("plans", {}).get("highs_swept", {})
            gap  = td.get("gap_pct", 0)
            gap_str = f"Gapping {'UP' if gap>0 else 'DOWN'} {abs(gap):.2f}%" if gap else "Flat open expected"
            cwall = td.get("call_wall")
            pwall = td.get("put_wall")
            walls_str = f"Walls: C${cwall:.0f} P${pwall:.0f}" if cwall and pwall else ""

            bear_lines.append(
                f"🔻 **{ticker}** BUY PUTS{' *(was BULLISH)*' if td.get('flipped') else ''}\n"
                f"{gap_str}\n"
                f"Strike: `${plan.get('entry',0):.2f}` | Exp: {plan.get('expiry','0DTE')}\n"
                f"Target: `${plan.get('target',0):.2f}`"
                + (f"\n{walls_str}" if walls_str else "")
            )

        bear_embed = {
            "color": 0xFF2D55,
            "author": {"name": f"🐻 BEARISH SETUPS ({len(bearish)})"},
            "description": "\n\n".join(bear_lines),
            "footer": {"text": f"Direction confirmed by sweep + reversal after 9:45 AM"},
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        await post_embed(bear_embed, username="CHAKRA (Bell Prep)")

    return True


async def post_gex_update(data: dict) -> bool:
    """Post GEX walls update to Discord."""
    tickers = data.get("tickers", {})
    if not tickers:
        return False

    fields = []
    for ticker, td in tickers.items():
        gex     = td.get("gex", {})
        magnets = td.get("magnets", {})
        spot    = td.get("spot", 0)
        regime  = gex.get("regime", "unknown")
        regime_icon = "📌" if regime == "pinned" else "💥"

        bsl = magnets.get("bsl", [])
        ssl = magnets.get("ssl", [])
        max_pain = magnets.get("max_pain")

        fields.append({
            "name": f"{regime_icon} {ticker} — ${spot:.2f}",
            "value": (
                f"Call Wall: **${gex.get('call_wall', 'N/A')}** | "
                f"Put Wall: **${gex.get('put_wall', 'N/A')}**\n"
                f"Zero Gamma: **${gex.get('zero_gamma', 'N/A')}** | "
                f"Regime: **{regime.upper()}**\n"
                f"BSL: {', '.join(f'${x}' for x in bsl[:2]) or 'N/A'} | "
                f"SSL: {', '.join(f'${x}' for x in ssl[:2]) or 'N/A'}\n"
                f"Max Pain: **${max_pain or 'N/A'}**"
            ),
            "inline": False
        })

    embed = {
        "color": 0x00D4FF,
        "author": {"name": "📊 CHAKRA — GEX Walls & Magnet Levels"},
        "description": f"Gamma exposure analysis • {data.get('generated','')}",
        "fields": fields,
        "footer": {"text": "CHAKRA Options Engine • Polygon Options Advanced • Updated at market open"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed(embed, username="CHAKRA (GEX)")


async def post_market_internals(data: dict) -> bool:
    """Post Market Internals update to Discord — George-style."""
    risk    = data.get("risk", {})
    vix     = data.get("vix", {})
    tlt     = data.get("tlt", {})
    gld     = data.get("gld", {})
    herding = data.get("herding", {})
    arka    = data.get("arka_mod", {})
    vc      = vix.get("classification", {})

    color = (0x00FF9D if risk.get("mode") == "RISK ON"
             else 0xFF2D55 if risk.get("mode") == "RISK OFF"
             else 0xFFCC00)

    pairs_text = "\n".join(
        f"`{p['pair']}` {p['direction']} **{p['corr_pct']}%**"
        for p in herding.get("pairs", [])[:5]
    ) or "Calculating..."

    reasons_text = "\n".join(f"• {r}" for r in arka.get("reasons", []))

    embed = {
        "color": color,
        "author": {"name": f"{risk.get('icon','📊')} CHAKRA — Market Internals | {data.get('time','')}"},
        "fields": [
            {"name": f"{risk.get('icon','')} Risk Mode",
             "value": f"**{risk.get('mode','N/A')}**\n{risk.get('description','')}",
             "inline": True},
            {"name": f"{vc.get('icon','📊')} VIX {vix.get('close',0):.1f}",
             "value": f"**{vc.get('regime','N/A')}**\nTLT {tlt.get('chg_pct',0):+.2f}% | GLD {gld.get('chg_pct',0):+.2f}%",
             "inline": True},
            {"name": "\u200b", "value": "\u200b", "inline": True},
            {"name": f"📡 Herding Score {herding.get('score_pct',0)}% — {herding.get('regime','N/A')}",
             "value": f"{herding.get('regime_note','')}\n\n{pairs_text}",
             "inline": False},
            {"name": f"⚡ ARKA Conviction Modifier: {arka.get('modifier',0):+d} pts",
             "value": reasons_text or "No adjustments",
             "inline": False},
        ],
        "footer": {"text": "CHAKRA Market Internals • Updates every 30min • Feeds into ARKA conviction scorer"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    return await post_embed(embed, username="CHAKRA (Internals)")


# ══════════════════════════════════════════════════════════════════════════════
#  EOD SUMMARY v2 — Deep analysis matching George's admin report format
#  Posts to CHAKRA Trades channel
# ══════════════════════════════════════════════════════════════════════════════

TRADES_WEBHOOK_URL = os.getenv("DISCORD_TRADES_WEBHOOK", "")

async def post_embed_trades(embed: dict, username: str = "CHAKRA") -> bool:
    """Post to CHAKRA Trades channel."""
    if not TRADES_WEBHOOK_URL:
        log.warning("DISCORD_TRADES_WEBHOOK not set — skipping")
        return False
    payload = {"embeds": [embed], "username": username, "avatar_url": ""}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(TRADES_WEBHOOK_URL, json=payload)
        if r.status_code in (200, 204):
            log.info("  📣 Trades channel alert posted")
            return True
        log.error(f"  Discord trades error: {r.status_code} {r.text[:100]}")
        return False
    except Exception as e:
        log.error(f"  Discord trades exception: {e}")
        return False


async def post_arka_eod_summary(summary: dict) -> bool:
    """
    Post comprehensive EOD summary to CHAKRA Trades channel.
    Includes: P&L, win rate, trade log, what went wrong, fakeout stats,
    self-correction analysis, and tomorrow's watchlist.
    """
    trades    = summary.get("trades", 0)
    pnl       = summary.get("daily_pnl", 0)
    log_      = summary.get("trade_log", [])
    scan_hist = summary.get("scan_history", [])
    config    = summary.get("config", {})
    today     = datetime.now(ET).strftime("%A, %B %d %Y")

    # ── Stats ────────────────────────────────────────────────────────────────
    closed = [t for t in log_ if t.get("pnl") is not None]
    wins   = [t for t in closed if t.get("pnl", 0) > 0]
    losses = [t for t in closed if t.get("pnl", 0) <= 0]
    win_rate  = round(len(wins) / len(closed) * 100) if closed else 0
    avg_win   = round(sum(t["pnl"] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss  = round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0
    pf        = round(abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)), 2) if losses and sum(t["pnl"] for t in losses) != 0 else 0

    # ── Scan analysis ────────────────────────────────────────────────────────
    total_scans    = len(scan_hist)
    blocked_fakeout = len([s for s in scan_hist if "FAKEOUT" in s.get("decision", "")])
    blocked_streak  = len([s for s in scan_hist if "losing streak" in s.get("decision", "")])
    blocked_max     = len([s for s in scan_hist if "max concurrent" in s.get("decision", "")])
    actual_trades   = len([s for s in scan_hist if s.get("decision") == "TRADE"])

    # Best missed opportunity (blocked but high conviction)
    blocked_high = [s for s in scan_hist if s.get("decision","").startswith("BLOCKED") and s.get("score",0) >= 75]
    best_missed  = max(blocked_high, key=lambda x: x["score"]) if blocked_high else None

    # ── Color ────────────────────────────────────────────────────────────────
    color = 0x00FF9D if pnl > 0 else 0xFF2D55
    pnl_str = f"{'+'if pnl>=0 else ''}{pnl:,.2f}"
    pnl_icon = "📈" if pnl > 0 else "📉"

    # ── Trade log ────────────────────────────────────────────────────────────
    trade_lines = []
    for t in log_:
        side = t.get("side","?"); sym = t.get("ticker","?")
        price = t.get("price",0); tpnl = t.get("pnl")
        if tpnl is not None:
            pnl_tag = f"{'+'if tpnl>0 else ''}{tpnl:,.2f}"
            icon = "✅" if tpnl > 0 else "❌"
        else:
            pnl_tag = "open"; icon = "⏳"
        trade_lines.append(f"{icon} `{t.get('time','')}` **{sym}** {side} @${price:.2f} → `{pnl_tag}`")
    trade_log_text = "\n".join(trade_lines) if trade_lines else "*No trades today*"

    # ── What went wrong analysis ──────────────────────────────────────────────
    issues = []
    if losses:
        early_losses = [t for t in losses if t.get("time","") < "14:30"]
        if early_losses:
            issues.append(f"• {len(early_losses)} early stop(s) — market was trending against entry bias")
        if avg_loss and avg_win and abs(avg_loss) > avg_win:
            issues.append(f"• Avg loss (${abs(avg_loss):.2f}) > avg win (${avg_win:.2f}) — stops too tight")
    if blocked_streak > 20:
        issues.append(f"• Lost {blocked_streak} scan opportunities to losing streak pause")
    if blocked_fakeout > 30:
        issues.append(f"• Fakeout detector blocked {blocked_fakeout} scans — choppy/reversing market")
    if not issues:
        issues.append("• No major issues — clean execution day")
    issues_text = "\n".join(issues)

    # ── What worked ──────────────────────────────────────────────────────────
    wins_text = []
    if wins:
        wins_text.append(f"• {len(wins)} winning trade(s) with avg ${avg_win:.2f} profit")
    if blocked_fakeout > 0:
        wins_text.append(f"• Fakeout detector blocked {blocked_fakeout} potential bad entries ✅")
    if pf > 1:
        wins_text.append(f"• Profit factor {pf} > 1.0 — system is net positive")
    wins_summary = "\n".join(wins_text) if wins_text else "• First trading day — building baseline"

    # ── Fakeout summary ──────────────────────────────────────────────────────
    fakeout_text = (
        f"Blocked `{blocked_fakeout}` entries (fakeout >{config.get('fakeout_block_threshold',0.55)})\n"
        f"Streak pause: `{blocked_streak}` scans | Max positions: `{blocked_max}` scans\n"
        f"Net trade rate: `{actual_trades}/{total_scans}` scans → trades"
    )

    # ── Best missed ──────────────────────────────────────────────────────────
    missed_text = (
        f"**{best_missed['ticker']}** conv={best_missed['score']} @ {best_missed['time']}\n"
        f"Blocked by: {best_missed['decision']}"
    ) if best_missed else "None today"

    # ── Build embeds ─────────────────────────────────────────────────────────

    # Header card
    header = {
        "color": color,
        "author": {"name": f"📊 CHAKRA — End of Day Summary | {today}"},
        "description": (
            f"## {pnl_icon} Daily P&L: **${pnl_str}**\n"
            f"ARKA completed its first full trading session.\n"
            f"Market closed at 4:00pm ET."
        ),
        "fields": [
            {"name": "📈 Trades",     "value": str(len(closed)),      "inline": True},
            {"name": "✅ Wins",       "value": str(len(wins)),         "inline": True},
            {"name": "❌ Losses",     "value": str(len(losses)),       "inline": True},
            {"name": "🏆 Win Rate",   "value": f"**{win_rate}%**",     "inline": True},
            {"name": "💰 Avg Win",    "value": f"${avg_win:+.2f}",     "inline": True},
            {"name": "📉 Avg Loss",   "value": f"${avg_loss:.2f}",     "inline": True},
            {"name": "⚡ Profit Factor", "value": f"**{pf}x**",        "inline": True},
            {"name": "🎯 Conviction", "value": str(config.get("conviction_threshold_normal", 55)), "inline": True},
            {"name": "🛡 Fakeout",    "value": str(config.get("fakeout_block_threshold", 0.55)),   "inline": True},
        ],
        "footer": {"text": f"CHAKRA ARKA Engine • Paper Trading • Day 1"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    await post_embed_trades(header, username="CHAKRA (EOD)")
    await asyncio.sleep(0.5)

    # Trade log card
    log_embed = {
        "color": color,
        "author": {"name": "📋 Trade Log — All Executions Today"},
        "description": trade_log_text,
        "footer": {"text": "Times shown in ET"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    await post_embed_trades(log_embed, username="CHAKRA (EOD)")
    await asyncio.sleep(0.5)

    # Analysis card
    analysis = {
        "color": 0x00D4FF,
        "author": {"name": "🧠 ARKA Self-Analysis — What Happened Today"},
        "fields": [
            {"name": "✅ What Worked",        "value": wins_summary,    "inline": False},
            {"name": "⚠️ What Went Wrong",    "value": issues_text,     "inline": False},
            {"name": "🛡 Risk Filter Stats",  "value": fakeout_text,    "inline": False},
            {"name": "🎯 Best Missed Setup",  "value": missed_text,     "inline": False},
        ],
        "footer": {"text": "CHAKRA ARKA Engine • Self-correcting thresholds active"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    await post_embed_trades(analysis, username="CHAKRA (EOD)")

    return True


async def post_position_update(ticker: str, action: str, details: dict) -> bool:
    """
    Post open position update to CHAKRA Trades channel.
    Called on: entry, stop move, partial exit, target hit, manual close.
    """
    color_map = {
        "ENTRY":   0x9B59B6,
        "STOP_HIT": 0xFF2D55,
        "TARGET":  0x00FF9D,
        "UPDATE":  0x00D4FF,
        "CLOSE":   0xFFCC00,
    }
    color = color_map.get(action, 0x00D4FF)

    entry  = details.get("entry", 0)
    price  = details.get("price", 0)
    stop   = details.get("stop", 0)
    target = details.get("target", 0)
    qty    = details.get("qty", 0)
    conv   = details.get("conviction", 0)
    pnl    = details.get("pnl")
    side   = details.get("side", "LONG")

    action_labels = {
        "ENTRY":    f"🟢 Opening {side} Position",
        "STOP_HIT": "🔴 Stop Loss Hit",
        "TARGET":   "✅ Target Reached",
        "UPDATE":   "📊 Position Update",
        "CLOSE":    "🏁 Position Closed",
    }

    pnl_text = f"\n**P&L: {'+'if (pnl or 0)>0 else ''}{pnl:.2f}**" if pnl is not None else ""

    embed = {
        "color": color,
        "author": {"name": f"{action_labels.get(action, action)} — {ticker}"},
        "description": f"**{ticker}** | {details.get('session','NORMAL')} session | Conv {conv:.0f}/100{pnl_text}",
        "fields": [
            {"name": "Entry",   "value": f"${entry:.2f}",  "inline": True},
            {"name": "Stop",    "value": f"${stop:.2f}",   "inline": True},
            {"name": "Target",  "value": f"${target:.2f}", "inline": True},
            {"name": "Price",   "value": f"${price:.2f}",  "inline": True},
            {"name": "Qty",     "value": str(qty),         "inline": True},
            {"name": "Side",    "value": side,             "inline": True},
        ],
        "footer": {"text": f"CHAKRA Trades • {datetime.now(ET).strftime('%I:%M %p ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed_trades(embed, username="CHAKRA (Trades)")


# ══════════════════════════════════════════════════════════════════════════════
#  NOTIFICATION ENGINE v2 — Channel routing + new notification types
# ══════════════════════════════════════════════════════════════════════════════

CH_ARKA_LOTTO     = os.getenv("DISCORD_ARKA_LOTTO",          WEBHOOK_URL)  # #arka-lotto
CH_FLOW_EXTREME   = os.getenv("DISCORD_FLOW_EXTREME",          WEBHOOK_URL)  # #flow-extreme
CH_FLOW_SIGNALS   = os.getenv("DISCORD_FLOW_SIGNALS",          WEBHOOK_URL)  # #flow-signals
CH_ALERTS         = os.getenv("DISCORD_ALERTS",                WEBHOOK_URL)  # #alerts
CH_APP_HEALTH     = os.getenv("DISCORD_APP_HEALTH",            WEBHOOK_URL)  # #app-health
CH_ARKA_EXTREME   = os.getenv("DISCORD_ARKA_SCALP_EXTREME", os.getenv("DISCORD_WEBHOOK_ARKA_EXTREME", WEBHOOK_URL))
CH_ARKA_SIGNALS   = os.getenv("DISCORD_ARKA_SCALP_SIGNALS", os.getenv("DISCORD_WEBHOOK_ARKA_SIGNALS", WEBHOOK_URL))
CH_ARKA_LOG       = os.getenv("DISCORD_ARKA_SCALP_LOG", os.getenv("DISCORD_WEBHOOK_ARKA_LOG", WEBHOOK_URL))
CH_CHAKRA_SIGNALS = os.getenv("DISCORD_ARKA_SWINGS_SIGNALS", os.getenv("DISCORD_WEBHOOK_CHAKRA_SIGNALS", WEBHOOK_URL))
CH_CHAKRA_LOG     = os.getenv("DISCORD_ARKA_SWINGS_EXTREME", os.getenv("DISCORD_WEBHOOK_CHAKRA_LOG", WEBHOOK_URL))
CH_CHAKRA_EXTREME = os.getenv("DISCORD_FLOW_EXTREME", os.getenv("DISCORD_HIGHSTAKES_WEBHOOK", WEBHOOK_URL))
HEALTH_WEBHOOK_V2 = os.getenv("DISCORD_HEALTH_WEBHOOK",         WEBHOOK_URL)


def _route_arka(conviction: float) -> str:
    if conviction >= 75: return CH_ARKA_EXTREME
    if conviction >= 55: return CH_ARKA_SIGNALS
    return CH_ARKA_LOG


def _route_chakra(uoa_ratio: float = 0, premium: float = 0, score: float = 0) -> str:
    if uoa_ratio >= 50 or premium >= 500000 or score >= 80: return CH_CHAKRA_SIGNALS
    if uoa_ratio >= 2  or premium >= 50000 or score >= 60:  return CH_CHAKRA_SIGNALS
    return CH_CHAKRA_LOG


async def _send(webhook_url: str, embed: dict, username: str = 'CHAKRA') -> bool:
    if not webhook_url: return False
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(webhook_url, json={"username": username, "embeds": [embed]})
            return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"Discord send error: {e}")
        return False


def _now_et() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M %p ET")


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


# ── Notification 2: GEX Regime Flip ──────────────────────────────────────────
async def notify_gex_flip(ticker: str, old_regime: str, new_regime: str,
                           gex_before: float, gex_after: float,
                           key_strike: float = 0) -> bool:
    key = f"gex_flip_{ticker}_{new_regime}"
    if _is_duplicate(key): return True
    is_bearish = "NEG" in new_regime or "SHORT" in new_regime
    color = 0xFF4444 if is_bearish else 0x00FF88
    flip_label = "SHORT GAMMA (trending/volatile)" if is_bearish else "LONG GAMMA (mean-reverting)"
    impl = "Expect larger moves, dealers amplify direction." if is_bearish else "Market makers dampen moves. Fade extremes."
    embed = {
        "color": color,
        "author": {"name": f"GEX REGIME FLIP — {ticker}"},
        "description": f"**{old_regime} -> {new_regime}**",
        "fields": [
            {"name": "Flip",        "value": flip_label,             "inline": False},
            {"name": "GEX Before",  "value": f"{gex_before:.3f}B",   "inline": True},
            {"name": "GEX After",   "value": f"{gex_after:.3f}B",    "inline": True},
            {"name": "Key Strike",  "value": f"${key_strike:,.0f}" if key_strike else "---", "inline": True},
            {"name": "Implication", "value": impl,                   "inline": False},
        ],
        "footer": {"text": f"CHAKRA GEX Monitor -- {_now_et()}"}
    }
    return await _send(CH_ARKA_EXTREME, embed, "GEX Alert")


def notify_gex_flip_sync(*a, **kw):
    import asyncio; return asyncio.run(notify_gex_flip(*a, **kw))


# ── Notification 3: VWAP/ORB Break ───────────────────────────────────────────
async def notify_vwap_orb_break(ticker: str, break_type: str, break_price: float,
                                  vwap_level: float, vol_ratio: float,
                                  gex_regime: str, session: str) -> bool:
    key = f"vwap_orb_{ticker}_{break_type}_{round(break_price)}"
    if _is_duplicate(key): return True
    is_bull = "ABOVE" in break_type or "LONG" in break_type
    color   = 0x00FF88 if is_bull else 0xFF4444
    gex_ok  = (is_bull and "POS" in gex_regime) or (not is_bull and "NEG" in gex_regime)
    gex_lbl = f"Confirms ({gex_regime})" if gex_ok else f"Contradicts ({gex_regime})"
    embed = {
        "color": color,
        "author": {"name": f"{break_type} -- {ticker}"},
        "fields": [
            {"name": "Break Price", "value": f"${break_price:.2f}",    "inline": True},
            {"name": "VWAP",        "value": f"${vwap_level:.2f}",     "inline": True},
            {"name": "Volume",      "value": f"{vol_ratio:.1f}x avg",  "inline": True},
            {"name": "GEX",         "value": gex_lbl,                  "inline": True},
            {"name": "Session",     "value": session,                  "inline": True},
            {"name": "Bias",        "value": "LONG" if is_bull else "SHORT", "inline": True},
        ],
        "footer": {"text": f"CHAKRA VWAP/ORB -- {_now_et()}"}
    }
    return await _send(CH_ARKA_SIGNALS, embed, "Level Break")


# ── Notification 4: Index UOA ─────────────────────────────────────────────────
async def notify_index_uoa(ticker: str, strike: float, expiry: str,
                            contract_type: str, vol_oi_ratio: float,
                            premium: float, iv: float, iv_avg: float,
                            dte: int, delta: float) -> bool:
    key = f"uoa_{ticker}_{strike}_{contract_type}_{expiry}"
    if _is_duplicate(key): return True
    is_extreme = vol_oi_ratio >= 50
    color      = 0xFF0000 if is_extreme else 0xFF8C00
    tier       = f"EXTREME (>{vol_oi_ratio:.0f}x)" if is_extreme else f"WHALE ({vol_oi_ratio:.0f}x)"
    bias       = "BULLISH" if contract_type.upper() == "CALL" else "BEARISH"
    iv_mult    = iv / iv_avg if iv_avg > 0 else 1.0
    channel    = CH_ARKA_EXTREME if is_extreme else CH_ARKA_SIGNALS
    scalp      = " -- 0DTE SCALP" if dte < 3 else ""
    embed = {
        "color": color,
        "author": {"name": f"INDEX UOA {tier} -- {ticker} {contract_type.upper()}"},
        "fields": [
            {"name": "Contract", "value": f"{ticker} {strike} {contract_type.upper()} {expiry}", "inline": False},
            {"name": "Vol/OI",   "value": f"{vol_oi_ratio:.0f}x",           "inline": True},
            {"name": "Premium",  "value": f"${premium:,.0f}",                "inline": True},
            {"name": "IV",       "value": f"{iv:.0f}% ({iv_mult:.1f}x avg)", "inline": True},
            {"name": "Bias",     "value": bias,                              "inline": True},
            {"name": "DTE",      "value": f"{dte} days{scalp}",              "inline": True},
            {"name": "Delta",    "value": f"{delta:.3f}",                    "inline": True},
        ],
        "footer": {"text": f"CHAKRA UOA Scanner -- {_now_et()}"}
    }
    return await _send(channel, embed, "UOA Alert")


# ── Notification 5: Dark Pool ─────────────────────────────────────────────────
# DARK_POOL_PRINT_DISABLED = True — user disabled, too noisy
async def notify_dark_pool(ticker: str, shares: int, notional: float,
                            price: float, bid: float, ask: float,
                            cumul_bull_pct: float) -> bool:
    return False  # Dark Pool Print alerts disabled by user preference
    key = f"darkpool_{ticker}_{round(notional/1e6)}M"
    if _is_duplicate(key): return True
    mid = (bid + ask) / 2 if bid and ask else price
    vs  = price - mid
    if price > ask:    dirn = "Aggressive Buy (above ask)"
    elif price < bid:  dirn = "Aggressive Sell (below bid)"
    else:              dirn = "Neutral (at mid)"
    embed = {
        "color": 0x9B59B6,
        "author": {"name": f"DARK POOL PRINT -- {ticker}"},
        "fields": [
            {"name": "Block Size",       "value": f"{shares:,} shares",       "inline": True},
            {"name": "Notional",         "value": f"${notional/1e6:.1f}M",    "inline": True},
            {"name": "Price",            "value": f"${price:.2f}",            "inline": True},
            {"name": "vs Mid",           "value": f"{vs:+.3f}",               "inline": True},
            {"name": "Direction",        "value": dirn,                       "inline": False},
            {"name": "Cumulative Flow",  "value": f"{cumul_bull_pct:.0f}% Bullish today", "inline": True},
        ],
        "footer": {"text": f"CHAKRA Dark Pool -- {_now_et()} -- FINRA TRF"}
    }
    return await _send(CH_CHAKRA_EXTREME, embed, "Dark Pool")


# ── Notification 6: CHAKRA Swing Entry ───────────────────────────────────────
async def notify_chakra_swing_entry(signal: dict) -> bool:
    ticker = signal.get("ticker", "?")
    key    = f"chakra_entry_{ticker}"
    if _is_duplicate(key): return True
    score      = float(signal.get("score", 0))
    entry      = float(signal.get("entry_price", 0))
    stop       = float(signal.get("stop_loss", 0))
    target     = float(signal.get("target_price", 0))
    rr         = abs(target - entry) / abs(entry - stop) if entry != stop else 0
    stop_pct   = abs(entry - stop)   / entry * 100 if entry else 0
    target_pct = abs(target - entry) / entry * 100 if entry else 0
    uoa        = signal.get("uoa_detected", False)
    regime     = signal.get("gex_regime", "UNKNOWN")
    channel    = _route_chakra(uoa_ratio=signal.get("uoa_ratio", 0), premium=signal.get("premium", 0), score=signal.get("score", signal.get("conviction", 0)))
    contract   = signal.get("contract", {})
    cstr       = "{} {} C {}".format(ticker, contract.get("strike","?"), contract.get("expiry","?")) if contract else "Equity"
    hc         = " -- HIGH CONVICTION" if score >= 75 else ""
    embed = {
        "color": 0x00FF88,
        "author": {"name": f"CHAKRA SWING ENTRY -- {ticker}"},
        "description": f"Score: {score:.0f}/100{hc}",
        "fields": [
            {"name": "Contract", "value": cstr,                            "inline": False},
            {"name": "Entry",    "value": f"${entry:.2f}",                 "inline": True},
            {"name": "Target",   "value": f"${target:.2f} (+{target_pct:.1f}%)", "inline": True},
            {"name": "Stop",     "value": f"${stop:.2f} (-{stop_pct:.1f}%)",     "inline": True},
            {"name": "R/R",      "value": f"1:{rr:.2f}",                   "inline": True},
            {"name": "GEX",      "value": regime,                          "inline": True},
            {"name": "UOA",      "value": "Detected" if uoa else "None",   "inline": True},
            {"name": "Catalyst", "value": signal.get("catalyst", "Technical setup"), "inline": False},
        ],
        "footer": {"text": f"CHAKRA Swings -- {_now_et()} -- Paper Trading"}
    }
    return await _send(channel, embed, "CHAKRA")


def notify_chakra_swing_entry_sync(*a, **kw):
    import asyncio; return asyncio.run(notify_chakra_swing_entry(*a, **kw))


# ── Notification 8: ARKA Trade v2 ────────────────────────────────────────────
async def notify_arka_trade_v2(signal: dict, position: dict) -> bool:
    ticker     = signal.get("ticker", "?")
    conviction = float(signal.get("conviction", 0))
    direction  = signal.get("direction", "LONG")
    key        = f"arka_v2_{ticker}_{direction}_{round(conviction)}"
    if _is_duplicate(key): return True
    is_short   = direction in ("SHORT", "STRONG_SHORT")
    trade_sym  = position.get("trade_sym", ticker)
    entry      = float(position.get("entry", signal.get("price", 0)))
    stop       = float(position.get("stop", 0))
    target     = float(position.get("target", 0))
    qty        = int(position.get("qty", 0))
    atr        = float(signal.get("atr", 0))
    session    = signal.get("session", "NORMAL")
    fakeout    = float(signal.get("fakeout_prob", 0))
    gex_regime = signal.get("gex_regime", "UNKNOWN")
    grade, gemoji = conviction_grade(conviction)
    stop_pct   = abs(entry - stop)   / entry * 100 if entry else 0
    target_pct = abs(target - entry) / entry * 100 if entry else 0
    rr         = abs(target - entry) / abs(entry - stop) if entry != stop else 0
    sym_note   = f" via {trade_sym}" if trade_sym != ticker else ""
    color      = 0xFF0000 if conviction >= 75 else 0xFF8C00 if conviction >= 55 else 0x808080
    lbl        = "SHORT" if is_short else "LONG"
    channel    = _route_arka(conviction)
    reasons    = ", ".join(signal.get("reasons", [])[:3]) or "---"
    embed = {
        "color": color,
        "author": {"name": f"ARKA {lbl} -- {ticker}{sym_note} | Grade {grade} {gemoji}"},
        "description": f"Conviction {conviction:.0f}/100",
        "fields": [
            {"name": "Entry",   "value": f"${entry:.2f}",                      "inline": True},
            {"name": "Stop",    "value": f"${stop:.2f} (-{stop_pct:.1f}%)",    "inline": True},
            {"name": "Target",  "value": f"${target:.2f} (+{target_pct:.1f}%)","inline": True},
            {"name": "R/R",     "value": f"1:{rr:.2f}",                        "inline": True},
            {"name": "Size",    "value": f"{qty} sh (${entry*qty:,.0f})",       "inline": True},
            {"name": "Fakeout", "value": f"{fakeout:.0%}",                     "inline": True},
            {"name": "GEX",     "value": gex_regime,                           "inline": True},
            {"name": "Session", "value": session,                              "inline": True},
            {"name": "ATR",     "value": f"${atr:.2f}",                        "inline": True},
            {"name": "Reason",  "value": reasons,                              "inline": False},
        ],
        "footer": {"text": f"CHAKRA ARKA -- {_now_et()} -- Paper Trading"}
    }
    return await _send(channel, embed, f"ARKA {grade}")


# ── Notification 9: ARKA Exit v2 ─────────────────────────────────────────────
async def notify_arka_exit_v2(ticker: str, entry: float, exit_price: float,
                               qty: int, reason: str, direction: str = 'LONG',
                               trade_sym: str = None) -> bool:
    key = f"arka_exit_{ticker}_{reason[:20]}"
    if _is_duplicate(key): return True
    is_short = direction in ("SHORT", "STRONG_SHORT")
    pnl      = (entry - exit_price if is_short else exit_price - entry) * qty
    pnl_pct  = pnl / (entry * qty) * 100 if entry and qty else 0
    won      = pnl > 0
    color    = 0x00FF88 if won else 0xFF4444
    result   = "WIN" if won else "LOSS"
    sym_note = f" (via {trade_sym})" if trade_sym and trade_sym != ticker else ""
    embed = {
        "color": color,
        "author": {"name": f"ARKA EXIT {result} -- {ticker}{sym_note}"},
        "fields": [
            {"name": "Entry",     "value": f"${entry:.2f}",                            "inline": True},
            {"name": "Exit",      "value": f"${exit_price:.2f}",                       "inline": True},
            {"name": "P&L",       "value": f"{pnl_pct:+.1f}% (${pnl:+.2f})",          "inline": True},
            {"name": "Qty",       "value": f"{qty} shares",                            "inline": True},
            {"name": "Reason",    "value": reason.replace("_", " "),                   "inline": True},
            {"name": "Direction", "value": direction,                                  "inline": True},
        ],
        "footer": {"text": f"CHAKRA ARKA -- {_now_et()} -- Paper Trading"}
    }
    return await _send(CH_ARKA_SIGNALS, embed, "ARKA Exit")


# ── Notification 10: System Health ───────────────────────────────────────────
async def notify_system_health(module: str, status: str, detail: str,
                                level: str = 'warning') -> bool:
    key = f"health_{module}_{status}"
    if _is_duplicate(key): return True
    clr = {"ok":0x00FF88,"warning":0xFF8C00,"error":0xFF4444,"critical":0xFF0000}.get(level,0x808080)
    embed = {
        "color": clr,
        "author": {"name": f"SYSTEM HEALTH -- {module}"},
        "description": f"Status: {status} -- {detail}",
        "footer": {"text": f"CHAKRA Health -- {_now_et()}"}
    }
    return await _send(HEALTH_WEBHOOK_V2, embed, "CHAKRA Health")


def notify_system_health_sync(*a, **kw):
    import asyncio; return asyncio.run(notify_system_health(*a, **kw))


# ── Notification 11: Lotto Entry ─────────────────────────────────────────────
async def notify_lotto_entry(ticker: str, strike: float, contract_type: str,
                              premium: float, conviction: int, gex_regime: str) -> bool:
    key = f"lotto_{ticker}_{_today()}"
    if _is_duplicate(key): return True
    is_call = contract_type.upper() == "CALL"
    color   = 0x00FF88 if is_call else 0xFF4444
    embed = {
        "color": color,
        "author": {"name": f"POWER HOUR LOTTO -- {ticker} 0DTE {contract_type.upper()}"},
        "description": "3:30 PM ET lotto position opened",
        "fields": [
            {"name": "Contract",   "value": f"{ticker} {strike} {contract_type.upper()} 0DTE", "inline": True},
            {"name": "Premium",    "value": f"${premium:.2f} (${premium*100:.0f} total)",       "inline": True},
            {"name": "Conviction", "value": f"{conviction}/100",    "inline": True},
            {"name": "GEX",        "value": gex_regime,             "inline": True},
            {"name": "Target",     "value": f"+100% -> ${premium*2:.2f}",  "inline": True},
            {"name": "Stop",       "value": f"-50% -> ${premium*0.5:.2f}", "inline": True},
            {"name": "Hard Close", "value": "3:58 PM ET auto-exit", "inline": True},
            {"name": "Rules",      "value": "Max 1 contract -- Hero or Zero", "inline": False},
        ],
        "footer": {"text": f"CHAKRA Lotto -- {_now_et()} -- Paper Trading"}
    }
    return await _send(CH_ARKA_EXTREME, embed, "ARKA Lotto")


def notify_lotto_entry_sync(*a, **kw):
    import asyncio; return asyncio.run(notify_lotto_entry(*a, **kw))

