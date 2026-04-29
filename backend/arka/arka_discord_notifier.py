"""
CHAKRA ARKA Discord Notifier — v4
All trade alerts route to a single channel: #arjun-alerts
Swings alerts also use the same channel.

Message types:
  - Scalp OPEN  : contract, conviction, reasons, stop/target
  - Scalp CLOSE : P&L, entry vs exit, reason
  - Swing OPEN  : contract, expiry, conviction, full reasons, levels
  - Swing CLOSE : P&L, how long held, reason
  - Watchlist   : top candidates with reasons
"""
import os, re, requests
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

# Trade alerts → #arjun-alerts
ARJUN_ALERTS_WH = os.getenv("DISCORD_ARJUN_ALERTS", "")

# Flow signal alerts → dedicated flow channels (not arjun-alerts)
_FLOW_EXTREME_WH = os.getenv("DISCORD_FLOW_EXTREME",  os.getenv("DISCORD_HIGHSTAKES_WEBHOOK", ""))
_FLOW_SIGNALS_WH = os.getenv("DISCORD_FLOW_SIGNALS",  os.getenv("DISCORD_WEBHOOK_URL", ""))

# GEX regime change alerts → #gamma-flips
_GAMMA_FLIP_WH = os.getenv("DISCORD_GAMMA_FLIP_WEBHOOK", "")

# Keep old webhook as silent fallback
_FALLBACK_WH = os.getenv("DISCORD_TRADES_WEBHOOK", "")


def _wh() -> str:
    """Return the active webhook — arjun-alerts or fallback."""
    return ARJUN_ALERTS_WH or _FALLBACK_WH


def _post(payload: dict) -> bool:
    """Send payload to #arjun-alerts. Returns True on success."""
    url = _wh()
    if not url:
        print("[ARKA Notifier] No webhook — skipping")
        return False
    try:
        r = requests.post(url, json=payload, timeout=8)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"[ARKA Notifier] Send failed: {e}")
        return False


def _parse_contract(sym: str) -> dict:
    """Parse options symbol → human readable parts."""
    if not sym:
        return {}
    m = re.match(r'^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d+)$', sym.upper())
    if not m:
        return {}
    underlying = m.group(1)
    expiry     = f"20{m.group(2)}-{m.group(3)}-{m.group(4)}"
    is_call    = m.group(5) == 'C'
    strike     = int(m.group(6)) / 1000
    from datetime import date as _date
    today      = _date.today()
    try:
        exp_date = _date.fromisoformat(expiry)
        dte      = max(0, (exp_date - today).days)
        exp_fmt  = exp_date.strftime("%b %d, %Y")
    except Exception:
        dte, exp_fmt = 0, expiry
    dte_label  = "0DTE" if dte == 0 else f"{dte}DTE"
    type_str   = "Call" if is_call else "Put"
    strike_fmt = f"${strike:.0f}" if strike == int(strike) else f"${strike:.2f}"
    return {
        "underlying": underlying,
        "expiry": expiry,
        "exp_fmt": exp_fmt,
        "is_call": is_call,
        "type": type_str,
        "strike": strike,
        "strike_fmt": strike_fmt,
        "dte": dte,
        "dte_label": dte_label,
        "label": f"{underlying} {strike_fmt} {type_str} · {dte_label}",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  SCALP ALERTS  (called from arka_engine.py)
# ══════════════════════════════════════════════════════════════════════════════

async def post_arka_entry(signal: dict, pos: dict) -> None:
    """Clean scalp-open alert → #arjun-alerts."""
    contract_sym = signal.get("contract_sym", "")
    ct           = _parse_contract(contract_sym)

    is_call    = signal.get("direction", "") not in ("SHORT", "STRONG_SHORT")
    ticker     = signal.get("ticker", "?")
    conviction = int(signal.get("conviction", 50))
    premium    = float(pos.get("est_premium", 0) or 0)
    qty        = int(pos.get("qty", 1))
    total_risk = round(premium * qty * 100, 2)

    # Contract label
    if ct:
        contract_label = f"{ct['underlying']} {ct['strike_fmt']} {ct['type']} · {ct['dte_label']}"
        expiry_line    = ct['exp_fmt']
        target_px      = round(premium * 1.50, 2)
        stop_px        = round(premium * 0.70, 2)
    else:
        contract_label = contract_sym or ticker
        expiry_line    = "0DTE"
        target_px      = round(premium * 1.50, 2)
        stop_px        = round(premium * 0.70, 2)

    # Reasons
    reasons    = signal.get("reasons", [])[:5]
    reason_txt = "\n".join([f"• {r}" for r in reasons]) if reasons else "• Conviction threshold met"

    # GEX + internals context
    gex_regime   = signal.get("gex_regime", "")
    neural_pulse = signal.get("neural_pulse", "")
    uoa          = signal.get("uoa_detected", False)
    vwap         = str(signal.get("vwap_bias", ""))
    rsi          = signal.get("rsi", "")
    extra_lines  = []
    if gex_regime:
        extra_lines.append(f"• GEX regime: {gex_regime}")
    if neural_pulse:
        extra_lines.append(f"• Market internals (Neural Pulse): {neural_pulse}")
    if uoa:
        extra_lines.append("• Unusual options activity detected — big money same direction")
    if "ABOVE" in vwap.upper():
        extra_lines.append("• Price above VWAP (bullish structure)")
    elif "BELOW" in vwap.upper():
        extra_lines.append("• Price below VWAP (bearish structure)")

    color = 0x00E676 if is_call else 0xFF4444
    emoji = "📈" if is_call else "📉"
    direction_word = "CALL" if is_call else "PUT"
    conviction_badge = " ⭐ HIGH CONVICTION" if conviction >= 75 else ""

    embed = {
        "title": f"{emoji}  ARKA SCALP OPENED — {ticker} {direction_word}",
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "fields": [
            {
                "name": "Contract",
                "value": f"`{contract_sym}`\n{contract_label}",
                "inline": False,
            },
            {
                "name": "Expires",
                "value": expiry_line,
                "inline": True,
            },
            {
                "name": "Premium",
                "value": f"**${premium:.2f}**/share · ${premium*100:.0f}/contract",
                "inline": True,
            },
            {
                "name": "Size",
                "value": f"{qty} contract{'s' if qty > 1 else ''} · **${total_risk:,.0f}** total risk",
                "inline": True,
            },
            {
                "name": f"Conviction{conviction_badge}",
                "value": f"**{conviction}/100**",
                "inline": True,
            },
            {
                "name": "Target / Stop",
                "value": f"🎯 +50% → **${target_px:.2f}**  ·  🛑 -30% → **${stop_px:.2f}**",
                "inline": False,
            },
            {
                "name": "Why this trade",
                "value": reason_txt + ("\n" + "\n".join(extra_lines) if extra_lines else ""),
                "inline": False,
            },
        ],
        "footer": {"text": f"ARKA Scalper · {datetime.now().strftime('%I:%M %p ET')}"},
    }
    _post({"embeds": [embed]})


def post_arka_exit(ticker: str, entry: float, exit_price: float,
                   qty: int, reason: str, contract: str = "") -> None:
    """Clean scalp-close alert → #arjun-alerts."""
    pnl        = round((exit_price - entry) * qty * 100, 2)
    pnl_pct    = round((exit_price - entry) / entry * 100, 1) if entry else 0
    is_win     = pnl >= 0
    emoji      = "✅" if is_win else "❌"
    result_lbl = "WIN" if is_win else "LOSS"
    color      = 0x00E676 if is_win else 0xFF4444

    ct = _parse_contract(contract)
    contract_label = ct.get("label", contract or ticker)

    # Classify exit
    r_lower = reason.lower()
    if "stop" in r_lower:
        exit_type = "Stop Loss Hit"
    elif "target" in r_lower or "profit" in r_lower or "tp" in r_lower:
        exit_type = "Take Profit Hit ✨"
    elif "runner" in r_lower:
        exit_type = "Runner Closed"
    elif "eod" in r_lower or "close" in r_lower:
        exit_type = "EOD Forced Close"
    else:
        exit_type = reason[:40]

    pnl_str  = f"+${abs(pnl):,.2f}" if is_win else f"-${abs(pnl):,.2f}"
    pnl_line = f"**{pnl_str}** ({pnl_pct:+.1f}%)"

    embed = {
        "title": f"{emoji}  ARKA SCALP CLOSED — {ticker} · {result_lbl}",
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "fields": [
            {
                "name": "Contract",
                "value": f"`{contract or ticker}`\n{contract_label}",
                "inline": False,
            },
            {
                "name": "Entry → Exit",
                "value": f"**${entry:.2f}** → **${exit_price:.2f}**",
                "inline": True,
            },
            {
                "name": "P&L",
                "value": pnl_line,
                "inline": True,
            },
            {
                "name": "Size",
                "value": f"{qty} contract{'s' if qty > 1 else ''}",
                "inline": True,
            },
            {
                "name": "Reason",
                "value": exit_type,
                "inline": False,
            },
        ],
        "footer": {"text": f"ARKA Scalper · {datetime.now().strftime('%I:%M %p ET')}"},
    }
    _post({"embeds": [embed]})


# ══════════════════════════════════════════════════════════════════════════════
#  SWING ALERTS  (called from arka_swings.py)
# ══════════════════════════════════════════════════════════════════════════════

def post_swing_entry(candidate: dict, contract_sym: str, qty: int,
                     premium: float, dte: int) -> None:
    """
    Swing-open alert → #arjun-alerts.
    candidate: from screen_universe() — has ticker, price, score, direction,
               reasons, rsi, vol_ratio, mom5, stop, tp1, tp2, rr, atr_pct
    """
    ticker    = candidate.get("ticker", "?")
    price     = float(candidate.get("price", 0))
    score     = int(candidate.get("score", 60))
    direction = candidate.get("direction", "LONG")
    is_call   = direction == "LONG"
    reasons   = candidate.get("reasons", [])
    rsi       = candidate.get("rsi", 0)
    vol_ratio = float(candidate.get("vol_ratio", 0))
    mom5      = float(candidate.get("mom5", 0))
    stop_px   = float(candidate.get("stop", 0))
    tp1_px    = float(candidate.get("tp1", 0))
    tp2_px    = float(candidate.get("tp2", 0))
    rr        = float(candidate.get("rr", 0))

    ct = _parse_contract(contract_sym)
    if ct:
        contract_label = f"{ct['underlying']} {ct['strike_fmt']} {ct['type']} · {ct['dte_label']}"
        exp_fmt        = ct["exp_fmt"]
    else:
        contract_label = contract_sym or f"{ticker} Options"
        exp_fmt        = f"{dte}DTE"

    total_risk   = round(premium * qty * 100, 2)
    target_prem  = round(premium * 1.50, 2)
    stop_prem    = round(premium * 0.75, 2)
    direction_wd = "CALLS" if is_call else "PUTS"
    color        = 0x00E676 if is_call else 0xFF4444
    emoji        = "🌀"
    conviction_badge = " ⭐ HIGH CONVICTION" if score >= 80 else ""

    # Reason bullets
    reason_lines = "\n".join([f"• {r}" for r in reasons[:5]]) if reasons else "• Score threshold met"

    # Extra context
    extras = []
    if rsi > 0:
        rsi_note = "oversold — bounce setup" if rsi < 35 else ("overbought — puts setup" if rsi > 70 else "neutral")
        extras.append(f"• RSI {rsi:.0f} — {rsi_note}")
    if vol_ratio >= 1.5:
        extras.append(f"• Volume: {vol_ratio:.1f}x above average — elevated interest")
    if abs(mom5) >= 1:
        dir_word = "bullish" if mom5 > 0 else "bearish"
        extras.append(f"• 5-day momentum: {mom5:+.1f}% ({dir_word})")

    from datetime import date as _d
    from datetime import timedelta as _td
    max_close = (_d.today() + _td(days=28)).strftime("%b %d")

    embed = {
        "title": f"{emoji}  SWING TRADE OPENED — {ticker} {direction_wd}",
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "fields": [
            {
                "name": "Contract",
                "value": f"`{contract_sym}`\n{contract_label}",
                "inline": False,
            },
            {
                "name": "Expiry",
                "value": exp_fmt,
                "inline": True,
            },
            {
                "name": "Premium",
                "value": f"**${premium:.2f}**/share · ${premium*100:.0f}/contract",
                "inline": True,
            },
            {
                "name": "Size / Risk",
                "value": f"{qty} contract · **${total_risk:,.0f}** at risk",
                "inline": True,
            },
            {
                "name": f"Conviction{conviction_badge}",
                "value": f"**{score}/100**  ·  R/R 1:{rr:.1f}",
                "inline": True,
            },
            {
                "name": "Max hold until",
                "value": f"ARKA will close by **{max_close}** if not stopped/targeted",
                "inline": False,
            },
            {
                "name": "Options levels",
                "value": (
                    f"🎯 Option target +50% → **${target_prem:.2f}**  ·  "
                    f"🛑 Option stop -25% → **${stop_prem:.2f}**"
                ),
                "inline": False,
            },
            {
                "name": "Underlying stock levels",
                "value": (
                    f"Entry **${price:.2f}**  ·  "
                    f"Stop **${stop_px:.2f}**  ·  "
                    f"TP1 **${tp1_px:.2f}**  ·  "
                    f"TP2 **${tp2_px:.2f}**"
                ),
                "inline": False,
            },
            {
                "name": "Why ARKA is swinging this",
                "value": (reason_lines + ("\n" + "\n".join(extras) if extras else "")),
                "inline": False,
            },
        ],
        "footer": {"text": f"ARKA Swings · {datetime.now().strftime('%I:%M %p ET')} · Paper Trading"},
    }
    _post({"embeds": [embed]})


def post_swing_exit(ticker: str, contract_sym: str, entry: float, exit_px: float,
                    qty: int, hold_days: int, reason: str, pnl: float, pnl_pct: float) -> None:
    """Swing-close alert → #arjun-alerts."""
    is_win     = pnl >= 0
    emoji      = "✅" if is_win else "❌"
    result_lbl = "WIN" if is_win else "LOSS"
    color      = 0x00E676 if is_win else 0xFF4444

    ct = _parse_contract(contract_sym)
    contract_label = ct.get("label", contract_sym or ticker)

    r_lower = reason.lower()
    if "stop" in r_lower:
        exit_type = "Stop Loss Hit"
    elif "tp2" in r_lower or "target 2" in r_lower or "full" in r_lower:
        exit_type = "Full Target Hit (TP2) ✨"
    elif "tp1" in r_lower or "target 1" in r_lower:
        exit_type = "First Target Hit (TP1) — runner remains"
    elif "timeout" in r_lower or "max hold" in r_lower or "28d" in r_lower:
        exit_type = "Max Hold Period Reached (28 days)"
    elif "eod" in r_lower:
        exit_type = "EOD Forced Close"
    else:
        exit_type = reason[:60]

    pnl_str  = f"+${abs(pnl):,.2f}" if is_win else f"-${abs(pnl):,.2f}"
    pnl_line = f"**{pnl_str}** ({pnl_pct:+.1f}%) on {qty} contract"

    embed = {
        "title": f"{emoji}  SWING CLOSED — {ticker} · {result_lbl}",
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "fields": [
            {
                "name": "Contract",
                "value": f"`{contract_sym}`\n{contract_label}",
                "inline": False,
            },
            {
                "name": "Entry → Exit",
                "value": f"**${entry:.2f}** → **${exit_px:.2f}**",
                "inline": True,
            },
            {
                "name": "P&L",
                "value": pnl_line,
                "inline": True,
            },
            {
                "name": "Held",
                "value": f"{hold_days} day{'s' if hold_days != 1 else ''}",
                "inline": True,
            },
            {
                "name": "Reason",
                "value": exit_type,
                "inline": False,
            },
        ],
        "footer": {"text": f"ARKA Swings · {datetime.now().strftime('%I:%M %p ET')}"},
    }
    _post({"embeds": [embed]})


def post_swing_watchlist(candidates: list, mode: str = "scan") -> None:
    """
    Watchlist update → #arjun-alerts.
    Shows all candidates with score, direction, RSI, volume, momentum and top reasons.
    Fires on premarket, entry scan, and postmarket.
    """
    if not candidates:
        return

    total   = len(candidates)
    top     = candidates[:8]  # show top 8 in detail
    emoji   = "🌅" if mode == "premarket" else ("🌙" if mode == "postmarket" else "📋")
    mode_lbl = {"premarket": "Pre-Market Watchlist", "postmarket": "Post-Market Watchlist",
                 "entry_scan": "Watchlist Updated", "entry_scan_closed": "Watchlist (Market Closed)"
                }.get(mode, "Swing Watchlist")

    lines = []
    for i, c in enumerate(top, 1):
        t       = c.get("ticker", "?")
        sc      = c.get("score", 0)
        direct  = c.get("direction", "LONG")
        rsi     = c.get("rsi", 0)
        vol     = float(c.get("vol_ratio", 0))
        mom5    = float(c.get("mom5", 0))
        px      = float(c.get("price", 0))
        reasons = c.get("reasons", [])

        dir_emoji = "📈" if direct == "LONG" else "📉"
        dir_word  = "BULLISH" if direct == "LONG" else "BEARISH"
        vol_txt   = f"{vol:.1f}x vol" if vol >= 1.5 else ""
        mom_txt   = f"{mom5:+.1f}%" if abs(mom5) >= 0.5 else ""
        rsi_txt   = f"RSI {rsi:.0f}" if rsi else ""

        stats = " · ".join(filter(None, [rsi_txt, vol_txt, mom_txt]))
        top_reason = reasons[0] if reasons else ""

        line = (
            f"**{i}. {t}** `{sc}/100` {dir_emoji} {dir_word}"
            f"\n    ${px:.2f}  {stats}"
        )
        if top_reason:
            line += f"\n    _{top_reason}_"
        lines.append(line)

    remainder = total - len(top)
    if remainder > 0:
        rest_tickers = ", ".join([c.get("ticker","?") for c in candidates[8:12]])
        lines.append(f"\n_...and **{remainder} more**: {rest_tickers}{'...' if total > 12 else ''}_")

    color = 0x3498DB if mode == "premarket" else (0x9B59B6 if mode == "postmarket" else 0x2ECC71)

    embed = {
        "title": f"{emoji}  ARKA SWINGS — {mode_lbl.upper()}",
        "color": color,
        "description": (
            f"**{total} candidates** ready  ·  "
            f"Min conviction 60 · Max DTE 28 · Options only\n\n"
            + "\n\n".join(lines)
        ),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "footer": {"text": f"ARKA Swings · {datetime.now().strftime('%I:%M %p ET')}"},
    }
    _post({"embeds": [embed]})


def post_eod_summary(wins: int, losses: int, total_pnl: float, candidates: list) -> None:
    """EOD P&L + tomorrow watchlist summary → #arjun-alerts."""
    is_green = total_pnl >= 0
    emoji    = "🟢" if is_green else "🔴"
    color    = 0x00E676 if is_green else 0xFF4444
    pnl_str  = f"+${abs(total_pnl):,.2f}" if is_green else f"-${abs(total_pnl):,.2f}"

    top3 = ", ".join([c.get("ticker","?") for c in candidates[:3]])
    desc = (
        f"**Today:** {wins} win{'s' if wins!=1 else ''} · {losses} loss{'es' if losses!=1 else ''}  |  "
        f"P&L **{pnl_str}**\n"
        f"**Tomorrow's top picks:** {top3}"
        + (f" + {len(candidates)-3} more" if len(candidates) > 3 else "")
    )

    embed = {
        "title": f"{emoji}  ARKA SWINGS — END OF DAY SUMMARY",
        "color": color,
        "description": desc,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "footer": {"text": f"ARKA Swings · {datetime.now().strftime('%I:%M %p ET')}"},
    }
    _post({"embeds": [embed]})


# ══════════════════════════════════════════════════════════════════════════════
#  LEGACY WRAPPERS  — kept for backward compat, route to #arjun-alerts
# ══════════════════════════════════════════════════════════════════════════════

def send_trade_alert(trade: dict):
    """Legacy entry point — wraps post_arka_entry style."""
    is_call   = str(trade.get("direction", "CALL")).upper() == "CALL"
    ticker    = trade.get("ticker", "?")
    premium   = float(trade.get("premium", 0))
    qty       = int(trade.get("qty", 1))
    conviction = int(trade.get("conviction", 50))
    contract_sym = trade.get("contract_sym", "")
    reasons   = [trade.get("reason", "")] if trade.get("reason") else []

    ct = _parse_contract(contract_sym)
    direction_wd = "CALL" if is_call else "PUT"
    contract_label = ct.get("label", contract_sym or f"{ticker} {direction_wd}")

    total_risk   = round(premium * qty * 100, 2)
    target_px    = round(premium * 1.50, 2)
    stop_px      = round(premium * 0.70, 2)
    color        = 0x00E676 if is_call else 0xFF4444
    emoji        = "📈" if is_call else "📉"
    conviction_badge = " ⭐" if conviction >= 75 else ""

    reason_txt = trade.get("reason", "")
    extras = []
    if trade.get("uoa_detected"):
        extras.append("• Unusual options activity — big money same direction")
    vwap = str(trade.get("vwap", ""))
    if "ABOVE" in vwap.upper():
        extras.append("• Price above VWAP (bullish)")
    elif "BELOW" in vwap.upper():
        extras.append("• Price below VWAP (bearish)")
    gex = trade.get("gex_regime", "")
    if gex:
        extras.append(f"• GEX regime: {gex}")

    why_txt = (f"• {reason_txt}\n" if reason_txt else "") + "\n".join(extras) or "• Conviction threshold met"

    embed = {
        "title": f"{emoji}  ARKA SCALP OPENED — {ticker} {direction_wd}",
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "fields": [
            {"name": "Contract", "value": f"`{contract_sym or ticker}`\n{contract_label}", "inline": False},
            {"name": "Premium",  "value": f"**${premium:.2f}**/share · ${premium*100:.0f}/contract", "inline": True},
            {"name": "Size",     "value": f"{qty} contract{'s' if qty>1 else ''} · **${total_risk:,.0f}** at risk", "inline": True},
            {"name": f"Conviction{conviction_badge}", "value": f"**{conviction}/100**", "inline": True},
            {"name": "Target / Stop", "value": f"🎯 +50% → **${target_px:.2f}**  ·  🛑 -30% → **${stop_px:.2f}**", "inline": False},
            {"name": "Why this trade", "value": why_txt, "inline": False},
        ],
        "footer": {"text": f"ARKA Scalper · {datetime.now().strftime('%I:%M %p ET')}"},
    }
    _post({"embeds": [embed]})


def send_exit_alert(trade: dict, exit_price: float, reason: str = "AUTO"):
    """Legacy exit entry point."""
    post_arka_exit(
        ticker     = trade.get("ticker", "?"),
        entry      = float(trade.get("premium", 0)),
        exit_price = exit_price,
        qty        = int(trade.get("qty", 1)),
        reason     = reason,
        contract   = trade.get("contract_sym", trade.get("ticker", "")),
    )


def send_arjun_signal(signal: dict):
    """ARJUN daily signal → #arjun-alerts."""
    sig    = signal.get("signal", "HOLD").upper()
    ticker = signal.get("ticker", "?")
    score  = signal.get("score", 50)
    emoji  = "🟢" if sig == "BUY" else "🔴" if sig == "SELL" else "⚪"
    color  = 0x00E676 if sig == "BUY" else 0xFF4444 if sig == "SELL" else 0x888888

    embed = {
        "title": f"{emoji}  ARJUN SIGNAL — {ticker} {sig}",
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "fields": [
            {"name": "Score",   "value": f"{score}/100",                           "inline": True},
            {"name": "Entry",   "value": f"${signal.get('entry',0):.2f}",          "inline": True},
            {"name": "Stop",    "value": f"${signal.get('stop',0):.2f}",           "inline": True},
            {"name": "Target",  "value": f"${signal.get('target',0):.2f}",         "inline": True},
            {"name": "R/R",     "value": str(signal.get("risk_reward","—")),        "inline": True},
            {"name": "Summary", "value": signal.get("summary","—")[:200],          "inline": False},
        ],
        "footer": {"text": "ARKA Engine · ARJUN Signal"},
    }
    _post({"embeds": [embed]})


def post_institutional_flow(signal: dict, account: dict = None) -> bool:
    """Institutional flow alert — route to #arjun-alerts."""
    from zoneinfo import ZoneInfo
    ET     = ZoneInfo("America/New_York")
    now    = datetime.now(ET)
    ticker = (signal.get("ticker") or "?").upper()

    _INDEX_TICKERS = {"SPY", "QQQ", "SPX", "IWM", "DIA", "RUT"}

    # Market hours gate for non-index
    _market_open = (
        now.weekday() < 5 and
        ((now.hour == 9 and now.minute >= 30) or now.hour > 9) and
        now.hour < 16
    )
    if not _market_open and ticker not in _INDEX_TICKERS:
        return False

    direction = (signal.get("direction") or "").upper()
    is_call   = "BULL" in direction or "CALL" in direction
    direction_wd = "CALLS" if is_call else "PUTS"
    strike    = signal.get("strike", 0)
    dte       = signal.get("dte", 0)
    premium   = float(signal.get("premium", 0) or 0)
    vol_ratio = float(signal.get("vol_ratio", 0) or 0)
    score     = float(signal.get("score", 0) or 0)
    execution = (signal.get("execution") or "SWEEP").upper()

    premium_str = (f"${premium/1_000_000:.1f}M" if premium >= 1_000_000
                   else f"${premium/1_000:.0f}K" if premium >= 1_000 else f"${premium:.0f}")
    dte_label = f"{dte}DTE" if dte else "0DTE"
    strike_str = f"${strike:.0f}" if strike else "ATM"

    flow_size = ("MEGA BLOCK" if premium >= 500_000 else
                 "INSTITUTIONAL" if premium >= 200_000 else
                 "LARGE BLOCK" if premium >= 100_000 else
                 "BLOCK TRADE" if premium >= 50_000 else "ELEVATED")

    # "Entering..." was misleading — ARKA evaluates separately and may not enter.
    # Use neutral labels so users don't think a trade was placed from this alert.
    arka_action = ("Strong signal 🎯" if score >= 85 else
                   "Signal active 📡" if score >= 75 else "Monitoring 🔍")

    color = 0x00E676 if is_call else 0xFF4444

    embed = {
        "title": "🏛️  INSTITUTIONAL FLOW DETECTED",
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "description": (
            f"**{ticker} {direction_wd}** · {strike_str} · {dte_label}\n"
            f"{now.strftime('%I:%M:%S %p ET')}"
        ),
        "fields": [
            {"name": "Premium",     "value": f"**{premium_str}**",    "inline": True},
            {"name": "Execution",   "value": f"**{execution}**",       "inline": True},
            {"name": "Flow Size",   "value": f"**{flow_size}**",       "inline": True},
            {"name": "Volume",      "value": f"{vol_ratio:.1f}x avg",  "inline": True},
            {"name": "Score",       "value": f"{score:.0f}/100",       "inline": True},
            {"name": "Flow Signal", "value": arka_action,               "inline": True},
        ],
        "footer": {"text": f"CHAKRA Flow Monitor · {now.strftime('%I:%M %p ET')} · ARKA evaluates separately"},
    }

    # Route to flow channels, NOT arjun-alerts
    # Mega block / institutional tier → extreme channel; everything else → signals channel
    _is_big = flow_size in ("MEGA BLOCK", "INSTITUTIONAL")
    _dest   = (_FLOW_EXTREME_WH if _is_big else _FLOW_SIGNALS_WH) or _FALLBACK_WH
    if not _dest:
        return False
    try:
        import requests as _req
        r = _req.post(_dest, json={"embeds": [embed]}, timeout=8)
        return r.status_code in (200, 204)
    except Exception:
        return False


async def post_position_update(ticker: str, action: str, data: dict) -> None:
    """Position update — handled by post_arka_entry/exit."""
    pass


def post_gex_regime_change(flip: dict) -> bool:
    """
    Post a GEX regime change alert to #gamma-flips Discord channel.

    Args:
        flip: dict from gex_state.check_regime_change() — must have changed=True

    Returns True if posted successfully.

    Embed format matches the George-style card in the image:
    ──────────────────────────────────
    🔄 SPY — GEX Regime Change
    Regime: Positive (Pin) → Negative (Explosive)

    Dealers now amplify moves. Expect larger swings and faster breakouts.

    🏦 Dealer Bias   ⚡ Accelerator   💰 Total GEX
    Bullish          0 → +43          -$686.4M

    📌 Current Price   🔥 Regime           ⚡ Severity
    $708.84            Negative (Explosive)  STRONG

    7:00:45 PM ET | STRONG | After Hours
    ──────────────────────────────────
    """
    if not _GAMMA_FLIP_WH:
        return False
    if not flip or not flip.get("changed"):
        return False

    from zoneinfo import ZoneInfo as _ZI
    _now_et = datetime.now(_ZI("America/New_York"))
    _market_open = (
        _now_et.weekday() < 5 and
        ((_now_et.hour == 9 and _now_et.minute >= 30) or _now_et.hour > 9) and
        _now_et.hour < 16
    )
    _session = "Market Hours" if _market_open else "After Hours"
    _time_str = _now_et.strftime("%-I:%M:%S %p ET")

    ticker     = flip.get("ticker", "?")
    old_label  = flip.get("old_label", flip.get("old_regime", "?"))
    new_label  = flip.get("new_label", flip.get("new_regime", "?"))
    desc       = flip.get("description", "")
    severity   = flip.get("severity", "MODERATE")
    bias       = flip.get("dealer_bias", "Neutral")
    accel_old  = flip.get("accel_old", 0)
    accel_new  = flip.get("accel_new", 0)
    net_gex_m  = flip.get("net_gex_m", 0)
    spot       = flip.get("spot", 0)
    call_wall  = flip.get("call_wall", 0)
    put_wall   = flip.get("put_wall", 0)
    regime_call = flip.get("regime_call", "NEUTRAL")

    # Severity → color (Discord embed color as int)
    _colors = {"STRONG": 0xFF4444, "MODERATE": 0xFF8C00, "MILD": 0xFFD700}
    _color  = _colors.get(severity, 0xAAAAAA)

    # Direction icon
    _is_explosive = "NEGATIVE" in flip.get("new_regime", "")
    _icon = "🔴" if _is_explosive else "🟢"

    # Accelerator string
    _accel_sign = "+" if accel_new >= 0 else ""
    _accel_str  = f"{int(accel_old)} → {_accel_sign}{int(accel_new)}"

    # GEX dollar string
    _gex_str = (f"-${abs(net_gex_m):.1f}M" if net_gex_m < 0 else f"+${net_gex_m:.1f}M")

    # Regime call label
    _rc_map = {
        "FOLLOW_MOMENTUM": "Follow Momentum 🚀",
        "SHORT_THE_POPS":  "Short the Pops 📉",
        "BUY_THE_DIPS":    "Buy the Dips 📈",
        "NEUTRAL":         "Neutral —",
    }
    _rc_label = _rc_map.get(regime_call, regime_call)

    embed = {
        "title":       f"{_icon} {ticker} — GEX Regime Change",
        "description": f"**Regime: {old_label} → {new_label}**\n\n{desc}",
        "color":       _color,
        "fields": [
            {"name": "🏦 Dealer Bias",    "value": bias,         "inline": True},
            {"name": "⚡ Accelerator",    "value": _accel_str,   "inline": True},
            {"name": "💰 Total GEX",      "value": _gex_str,     "inline": True},
            {"name": "📌 Current Price",  "value": f"${spot:.2f}" if spot else "—", "inline": True},
            {"name": "🔥 Regime",         "value": new_label,    "inline": True},
            {"name": "⚡ Severity",       "value": severity,     "inline": True},
        ],
        "footer": {
            "text": f"{_time_str} | {severity} | {_session} | Call Wall ${call_wall:.0f} | Put Wall ${put_wall:.0f} | {_rc_label}"
        },
    }

    # Forwarded tag for STRONG flips
    content = "@here" if severity == "STRONG" else ""
    if call_wall and put_wall and spot:
        content += f"\n> **Walls:** Call ${call_wall:.0f} ↑ | Put ${put_wall:.0f} ↓ | Spot ${spot:.2f}"

    try:
        r = requests.post(
            _GAMMA_FLIP_WH,
            json={"content": content.strip(), "embeds": [embed]},
            timeout=8,
        )
        return r.status_code in (200, 204)
    except Exception as _e:
        print(f"[GEX Regime Alert] Discord error: {_e}")
        return False


if __name__ == "__main__":
    print("Testing #arjun-alerts notifier...")
    import asyncio
    # post_gex_regime_change test
    # post_gex_regime_change({...})
    asyncio.run(post_arka_entry(
        signal={
            "ticker": "SPY", "direction": "LONG", "conviction": 72,
            "contract_sym": "SPY260414C00693000",
            "reasons": ["RSI bullish", "VWAP hold", "UOA detected"],
            "uoa_detected": True, "vwap_bias": "ABOVE VWAP",
            "gex_regime": "POSITIVE_GAMMA", "neural_pulse": 68,
        },
        pos={"est_premium": 2.45, "qty": 2},
    ))
    print("✅ Test sent — check #arjun-alerts")
