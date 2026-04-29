"""
taraka_engine.py — TARAKA Discord Signal Engine
Listens to Discord channels, parses analyst alerts,
scores them with Arjun's brain, and executes 0DTE options via Alpaca.

Start:
  python3 backend/taraka/taraka_engine.py

Add to crontab (starts with market, 8:28am CST):
  28 8 * * 1-5 cd ~/trading-ai && venv/bin/python3 backend/taraka/taraka_engine.py >> logs/taraka/taraka.log 2>&1 &
"""

import sys, os as _os
sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import discord
from dotenv import load_dotenv
from pathlib import Path as _Path
load_dotenv(_Path(__file__).resolve().parents[2] / ".env", override=True)
import asyncio
import logging
import os
import json
from datetime import datetime
from pathlib import Path
import pytz

from alert_parser   import AlertParser
from taraka_scorer    import TarakaScorer
from options_executor import OptionsExecutor
from analyst_tracker  import AnalystTracker

# ── CONFIG ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN   = os.getenv("DISCORD_BOT_TOKEN", "")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
TARAKA_WEBHOOK  = os.getenv("DISCORD_TARAKA_WEBHOOK", "")

# Fill these in after setup — channel IDs (right-click channel → Copy ID)
WATCH_CHANNELS = [
    1477297456545796128,   # arka-signals
    1483969942935044267,   # arka-extreme
    1478124867713765468,   # chakra-signals
    1480690163637026876,   # high_stakes
]

# ── HOT-RELOAD: re-read channels from JSON every 60s ─────────────────
import time as _time
_channels_last_loaded = 0.0

def _reload_channels() -> list[int]:
    """Load active channel IDs from taraka_channels.json."""
    global _channels_last_loaded
    now = _time.time()
    if now - _channels_last_loaded < 60:
        return WATCH_CHANNELS  # use cached
    try:
        import json as _json
        cfg_path = _Path(__file__).resolve().parents[2] / "backend/taraka/taraka_channels.json"
        with open(cfg_path) as _f:
            cfg = _json.load(_f)
        ids = [int(i) for i in cfg.get("active_ids", []) if str(i).isdigit()]
        _channels_last_loaded = now
        if ids != WATCH_CHANNELS:
            log.info(f"  🔄 Channel config reloaded — {len(ids)} active channels")
        return ids
    except Exception as _e:
        log.warning(f"  ⚠️  Could not reload channels: {_e}")
        return WATCH_CHANNELS



LOG_DIR = Path("logs/taraka")
ET      = pytz.timezone("America/New_York")

# ── SESSIONS ────────────────────────────────────────────────────────────────
def get_session():
    now = datetime.now(ET)
    h, m = now.hour, now.minute
    mins = (h - 9) * 60 + m - 30   # minutes since market open

    if h < 9 or (h == 9 and m < 30):   return "PRE",        False, 0, 0
    if h >= 16:                          return "CLOSED",     False, 0, 0
    if h == 15 and m >= 58:              return "AUTO_CLOSE", False, 0, 0
    if h == 15 or (h == 14 and m >= 30): return "POWER_HOUR", True,  10, 100
    if (h == 11 and m >= 30) or h == 12 or (h == 13 and m < 30):
                                          return "LUNCH",     False, 0, 0
    if mins <= 30:                        return "OPEN",      True,  10, 250
    return                                       "NORMAL",    True,  10, 250


# ── LOGGING ─────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / f"taraka_{datetime.now().strftime('%Y-%m-%d')}.log"),
    ]
)
log = logging.getLogger("taraka")


# ── DISCORD CLIENT ───────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client  = discord.Client(intents=intents)

# Initialise components
parser   = AlertParser(anthropic_key=ANTHROPIC_KEY)
scorer   = TarakaScorer()
executor = OptionsExecutor()
tracker  = AnalystTracker()



def _post_taraka_alert(parsed: dict, score: int, breakdown: dict, mode: str, trade=None):
    """Post parsed alert result to TARAKA Discord channel."""
    if not TARAKA_WEBHOOK:
        return
    import requests
    is_call  = parsed.get("direction") == "CALL"
    color    = 0x00C8AA if is_call else 0xFF4444
    emoji    = "📈" if is_call else "📉"
    mode_icon = "✅ REAL" if mode == "REAL" else "📋 PAPER"

    fields = [
        {"name": "📌 Ticker",    "value": parsed.get("ticker","?"),         "inline": True},
        {"name": "Direction",    "value": f"{emoji} {parsed.get('direction','?')}", "inline": True},
        {"name": "🎯 Score",     "value": f"{score}/100",                   "inline": True},
        {"name": "👤 Analyst",   "value": str(parsed.get("author","?")),    "inline": True},
        {"name": "📊 Mode",      "value": mode_icon,                        "inline": True},
        {"name": "🔍 Parse Conf","value": f"{parsed.get('parse_conf',0)}%", "inline": True},
    ]
    if parsed.get("entry"):
        fields.append({"name": "Entry",  "value": f"${parsed['entry']:.2f}", "inline": True})
    if parsed.get("target"):
        fields.append({"name": "Target", "value": f"${parsed['target']:.2f}","inline": True})
    if parsed.get("stop"):
        fields.append({"name": "Stop",   "value": f"${parsed['stop']:.2f}",  "inline": True})

    breakdown_text = (
        f"Analyst: {breakdown.get('analyst',0)}pts  |  "
        f"Arjun: {breakdown.get('arjun',0)}pts  |  "
        f"Quality: {breakdown.get('quality',0)}pts"
    )
    fields.append({"name": "📐 Breakdown", "value": breakdown_text, "inline": False})

    if parsed.get("notes"):
        fields.append({"name": "📝 Notes", "value": parsed["notes"][:200], "inline": False})

    embed = {
        "title": f"{emoji} TARAKA Alert — {parsed.get('ticker')} {parsed.get('direction')} | Score {score}/100",
        "color": color,
        "fields": fields,
        "footer": {"text": f"CHAKRA TARAKA Engine  •  {mode_icon}  •  {'EXECUTE' if mode=='REAL' else 'PAPER ONLY'}"},
        "timestamp": datetime.now(ET).isoformat(),
    }

    # Layman message
    direction_word = "up" if is_call else "down"
    action = f"🛒 Executing real trade!" if mode == "REAL" else f"📋 Paper trade only (score {score} < 65)"
    simple = (
        f"{emoji} **TARAKA spotted an analyst alert!**\n\n"
        f"📊 **{parsed.get('ticker')} {parsed.get('direction')}** — Score: **{score}/100**\n"
        f"👤 Analyst: {parsed.get('author','?')}\n"
        f"💬 Expecting {parsed.get('ticker')} to move **{direction_word}**\n\n"
        f"{action}"
    )

    try:
        requests.post(TARAKA_WEBHOOK, json={"embeds": [embed]}, timeout=8)
        requests.post(TARAKA_WEBHOOK, json={"content": simple}, timeout=8)
    except Exception as e:
        log.warning(f"TARAKA webhook error: {e}")

@client.event
async def on_ready():
    log.info(f"══════════════════════════════════════")
    log.info(f"  TARAKA — Discord Signal Engine LIVE")
    log.info(f"  Logged in as: {client.user}")
    log.info(f"  Watching {len(WATCH_CHANNELS)} channel(s)")
    log.info(f"══════════════════════════════════════")


@client.event
async def on_message(message):
    log.info(f"  📨 RAW MESSAGE in #{getattr(message.channel,'name','?')} from @{message.author} — channel_id={message.channel.id}")
    # Ignore own messages and bots
    if message.author.bot:
        return

    # Only watch configured channels — hot-reloads every 60s from JSON
    active_channels = _reload_channels()
    if active_channels and message.channel.id not in active_channels:
        return

    # Check session
    session, tradeable, min_cap, max_cap = get_session()
    if not tradeable:
        log.info(f"  📵 [{session}] Ignored alert from @{message.author.name} — session blocked")
        return

    log.info(f"\n{'─'*50}")
    log.info(f"  📨 Alert from @{message.author.name} in #{message.channel.name}")
    log.info(f"  Message: {message.content[:120]}")
    log.info(f"  Session: {session}  Capital: ${min_cap}-${max_cap}")

    # ── Step 1: Parse the alert ──────────────────────────────
    parsed = await parser.parse(message)
    if not parsed:
        log.info(f"  ⚠️  Could not parse as trading alert — skipping")
        return

    log.info(f"  Parsed: {parsed['ticker']} {parsed['direction']} | "
             f"Entry: {parsed.get('entry','?')} | "
             f"Target: {parsed.get('target','?')} | "
             f"Stop: {parsed.get('stop','?')}")

    # ── Step 2: Score with Arjun's brain ────────────────────
    analyst_weight = tracker.get_weight(str(message.author))
    score, breakdown = scorer.score(parsed, str(message.author), analyst_weight, session)

    log.info(f"  TARAKA Score: {score}/100  (analyst={breakdown['analyst']}, "
             f"arjun={breakdown['arjun']}, session={breakdown['session_bonus']})")
    log.info(f"  Breakdown: {breakdown}")

    # ── Step 3: Decide ───────────────────────────────────────
    alert_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{parsed['ticker']}"
    is_real_trade = score >= 65

    if not is_real_trade:
        log.info(f"  ⏸️  Score {score} < 65 — PAPER TRADE only")
        _log_alert(alert_id, message, parsed, score, breakdown, "PAPER", None)
        tracker.log_alert(str(message.author), alert_id, parsed, score, real=False)
        _post_taraka_alert(parsed, score, breakdown, "PAPER")
        await message.add_reaction("📋")   # paper trade emoji reaction
        return

    # ── Step 4: Execute 0DTE option ─────────────────────────
    log.info(f"  🟢 Score {score} ≥ 65 — EXECUTING real trade")

    # Budget based on session + score (higher score = closer to max)
    score_pct  = (score - 65) / 35          # 0.0 at score=65, 1.0 at score=100
    budget     = min_cap + (max_cap - min_cap) * score_pct
    budget     = round(min(budget, max_cap), 0)

    log.info(f"  Budget: ${budget} (score {score} → {score_pct:.0%} of ${min_cap}-${max_cap})")

    trade = await executor.execute(
        ticker    = parsed["ticker"],
        direction = parsed["direction"],
        budget    = budget,
        session   = session,
    )

    if trade and not trade.get("error"):
        log.info(f"  ✅ Executed: {trade['contract']} × {trade['contracts']} @ ${trade['premium']:.2f}")
        await message.add_reaction("✅")
    else:
        err = trade.get("error","unknown") if trade else "execution failed"
        log.info(f"  ❌ Execution failed: {err}")
        log.info(f"  📋 Falling back to paper trade")
        await message.add_reaction("❌")

    _log_alert(alert_id, message, parsed, score, breakdown,
               "REAL" if trade and not trade.get("error") else "PAPER", trade)
    tracker.log_alert(str(message.author), alert_id, parsed, score,
                      real=bool(trade and not trade.get("error")))
    _post_taraka_alert(parsed, score, breakdown, "REAL" if trade and not trade.get("error") else "PAPER", trade)


def _log_alert(alert_id, message, parsed, score, breakdown, mode, trade):
    """Write full alert record to daily log JSON."""
    record = {
        "id":        alert_id,
        "timestamp": datetime.now(ET).isoformat(),
        "analyst":   str(message.author),
        "channel":   str(message.channel.name),
        "raw":       message.content,
        "parsed":    parsed,
        "score":     score,
        "breakdown": breakdown,
        "mode":      mode,
        "trade":     trade,
        "outcome":   None,   # filled in by taraka_journal.py at 4pm
    }
    today = datetime.now(ET).strftime("%Y-%m-%d")
    path  = LOG_DIR / f"alerts_{today}.json"
    alerts = []
    if path.exists():
        with open(path) as f:
            try: alerts = json.load(f)
            except: alerts = []
    alerts.append(record)
    with open(path, "w") as f:
        json.dump(alerts, f, indent=2)


# ── RUN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "your_token_here":
        log.error("Set DISCORD_BOT_TOKEN environment variable first!")
        log.error("export DISCORD_BOT_TOKEN=your_token_here")
        exit(1)
    client.run(DISCORD_TOKEN)
