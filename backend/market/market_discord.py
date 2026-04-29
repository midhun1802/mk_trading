"""
market_discord.py — Posts market briefings to Discord.
Pre-market  → 9:00 AM ET (Mon-Fri)
Post-market → 4:00 PM ET (Mon-Thu only, Friday = last post of week)
Weekend     → silent (Fri 4PM through Mon 9AM)
"""

import os
import httpx
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("market.discord")
ET  = ZoneInfo("America/New_York")

WEBHOOK_URL = "https://discord.com/api/webhooks/1472327129432588470/9NgfYy2zRBVZhUjjaKxMKE575_RuHghoyE-ZcQkVPGxSKbLF2FlGOie37DtIUytey9xp"

COLOR_PRE  = 0xF59E0B   # amber  — morning
COLOR_POST = 0x6366F1   # indigo — evening


def _conviction_bar(markets: dict) -> str:
    """Build a quick snapshot line from market data for the embed description."""
    lines = []
    us = markets.get("US", [])
    macro = markets.get("Macro", [])
    for item in us:
        sym = item.get("symbol", "")
        chg = item.get("change_pct", 0) or 0
        arrow = "▲" if chg > 0 else "▼" if chg < 0 else "—"
        sign  = "+" if chg > 0 else ""
        lines.append(f"**{sym}** {arrow} {sign}{chg:.2f}%")
    for item in macro:
        if item.get("symbol") in ("VIXY", "GLD"):
            sym = item.get("symbol", "")
            chg = item.get("change_pct", 0) or 0
            arrow = "▲" if chg > 0 else "▼" if chg < 0 else "—"
            sign  = "+" if chg > 0 else ""
            lines.append(f"**{sym}** {arrow} {sign}{chg:.2f}%")
    return "  ·  ".join(lines)


def _build_embed(briefing: dict) -> dict:
    mode      = briefing.get("mode", "pre")
    narrative = briefing.get("narrative", "Unavailable")
    date_str  = briefing.get("date", "")
    gen_time  = briefing.get("generated", "")
    markets   = briefing.get("markets", {})

    is_pre    = mode == "pre"
    color     = COLOR_PRE if is_pre else COLOR_POST
    title     = "🌅 Pre-Market Briefing" if is_pre else "🌆 Post-Market Debrief"

    # Snapshot line
    snapshot = _conviction_bar(markets)

    # Split narrative into paragraphs → fields (Discord limit: 1024 chars/field)
    paras = [p.strip() for p in narrative.split("\n\n") if p.strip()]

    fields = []
    para_labels = (
        ["📊 Overnight Setup", "🎯 Bias & Key Levels", "📋 Trade Plan"]
        if is_pre else
        ["📊 What Happened", "💡 Key Takeaways", "🔭 Tomorrow's Setup"]
    )
    for i, para in enumerate(paras[:3]):
        label = para_labels[i] if i < len(para_labels) else f"Note {i+1}"
        fields.append({"name": label, "value": para[:1024], "inline": False})

    # Sector tiles as a compact field
    sentiment = markets.get("Sentiment", [])
    if sentiment:
        tqqq = next((x for x in sentiment if x["symbol"] == "TQQQ"), None)
        sqqq = next((x for x in sentiment if x["symbol"] == "SQQQ"), None)
        if tqqq and sqqq:
            tc = tqqq.get("change_pct", 0) or 0
            sc = sqqq.get("change_pct", 0) or 0
            ratio_note = "🚀 Bulls in control" if tc > abs(sc) else "🐻 Bears in control" if sc > abs(tc) else "⚖️ Balanced"
            fields.append({
                "name": "🧠 Leverage Sentiment",
                "value": f"TQQQ {'+' if tc>0 else ''}{tc:.2f}%  ·  SQQQ {'+' if sc>0 else ''}{sc:.2f}%  →  {ratio_note}",
                "inline": False
            })

    now_et = datetime.now(ET)
    embed = {
        "color":       color,
        "author":      {"name": f"CHAKRA — {title}"},
        "description": snapshot,
        "fields":      fields,
        "footer":      {"text": f"CHAKRA Neural Trading OS  ·  {date_str}  ·  Generated {gen_time}"},
        "timestamp":   now_et.isoformat(),
    }
    return embed


async def post_briefing_to_discord(briefing: dict) -> bool:
    if not WEBHOOK_URL:
        log.warning("No Discord webhook URL set")
        return False
    if "error" in briefing:
        log.error(f"Briefing has error, skipping Discord: {briefing['error']}")
        return False

    embed   = _build_embed(briefing)
    payload = {"username": "CHAKRA Markets", "embeds": [embed]}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(WEBHOOK_URL, json=payload)
            if r.status_code in (200, 204):
                log.info(f"Discord market briefing posted ({briefing.get('mode')})")
                return True
            log.warning(f"Discord error {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        log.error(f"Discord post failed: {e}")
        return False
