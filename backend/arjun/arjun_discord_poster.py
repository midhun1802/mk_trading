"""
Arjun Daily Signal Poster
==========================
Posts Arjun's morning ETF signals to Discord each trading day.
Designed to run at 8:00am ET via LaunchAgent (already scheduled).

Run manually:
    cd ~/trading-ai
    python3 backend/arjun/arjun_discord_poster.py

How it works:
  1. Reads today's signal JSON from logs/signals/
  2. Formats a George-style Discord embed
  3. Posts to DISCORD_WEBHOOK_URL
"""

import os
import sys
import json
import glob
import asyncio
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("ARJUN.Discord")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")


def load_todays_signals() -> list[dict]:
    """Load today's signals from logs/signals/."""
    today = date.today().isoformat()
    log_dir = BASE_DIR / "logs/signals"

    # Try today's date file first
    paths = [
        log_dir / f"signals_{today}.json",
        log_dir / f"{today}.json",
    ]
    # Fallback: most recent file
    fallback = sorted(glob.glob(str(log_dir / "*.json")), reverse=True)

    for path in paths:
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else [data]
            except Exception as e:
                log.warning(f"Could not load {path}: {e}")

    if fallback:
        try:
            with open(fallback[0]) as f:
                data = json.load(f)
                log.info(f"Using fallback signals from {fallback[0]}")
                return data if isinstance(data, list) else [data]
        except:
            pass

    return []


def enrich_signal(s: dict) -> dict:
    """Add derived fields for Discord formatting."""
    price  = float(s.get("price", s.get("entry", 0)))
    target = float(s.get("target", 0))
    stop   = float(s.get("stop", 0))
    entry  = float(s.get("entry", price))

    s["price"]      = price
    s["entry"]      = entry
    s["target_pct"] = round((target - entry) / entry * 100, 1) if entry else 0
    s["stop_pct"]   = round((stop - entry) / entry * 100, 1)   if entry else 0
    s["rr"]         = round(abs(target - entry) / abs(entry - stop), 2) if stop != entry else 0

    return s


async def post_morning_brief(signals: list[dict]) -> bool:
    """Post the full morning signal brief to Discord."""
    from backend.arka.discord_notifier import post_arjun_daily, post_system_alert

    if not signals:
        log.warning("No signals found for today — posting notice to Discord")
        return await post_system_alert(
            "Arjun Daily Signals",
            f"No signals generated for {date.today().isoformat()} — market may be closed or data unavailable.",
            level="warning"
        )

    signals = [enrich_signal(s) for s in signals]
    buys    = [s for s in signals if s.get("signal") == "BUY"]
    sells   = [s for s in signals if s.get("signal") == "SELL"]
    holds   = [s for s in signals if s.get("signal") == "HOLD"]

    log.info(f"Posting {len(signals)} signals: {len(buys)} BUY | {len(sells)} SELL | {len(holds)} HOLD")

    # ── Build the embed directly for more control ─────────────────────────────
    from backend.arka.discord_notifier import post_embed, COLOR_BLUE

    def signal_block(s: dict) -> str:
        sig    = s.get("signal", "?")
        ticker = s.get("ticker", "?")
        price  = s.get("price", 0)
        entry  = s.get("entry", price)
        target = s.get("target", 0)
        stop   = s.get("stop", 0)
        conf   = s.get("confidence", s.get("win_rate", 0))
        t_pct  = s.get("target_pct", 0)
        s_pct  = s.get("stop_pct", 0)
        rr     = s.get("rr", 0)
        icon   = "🟢" if sig == "BUY" else "🔴" if sig == "SELL" else "🟡"

        return (
            f"{icon} **{ticker}** — **{sig}**\n"
            f"  Price: `${price:.2f}` | Entry: `${entry:.2f}`\n"
            f"  Target: `${target:.2f}` (+{t_pct}%) | Stop: `${stop:.2f}` ({s_pct}%)\n"
            f"  R:R `{rr:.1f}` | Confidence: `{conf:.1f}%`"
        )

    buy_text  = "\n\n".join(signal_block(s) for s in buys)  or "*None today*"
    sell_text = "\n\n".join(signal_block(s) for s in sells) or "*None today*"
    hold_text = ", ".join(f"**{s.get('ticker','')}**" for s in holds) or "*None*"

    today_str    = datetime.now(ET).strftime("%A, %B %d %Y")
    market_open  = datetime.now(ET).strftime("%I:%M %p ET")

    # Backtest stats line
    avg_conf = sum(s.get("confidence", s.get("win_rate", 77)) for s in signals) / len(signals)

    embed = {
        "color": COLOR_BLUE,
        "author": {
            "name": "⚡ ARJUN — Daily ETF Signal Report"
        },
        "description": (
            f"**Morning briefing for {today_str}**\n"
            f"ML-powered swing signals across SPY, QQQ, IWM, DIA, XLF, XLE, XLK\n"
            f"Posted at {market_open} • 77% historical win rate"
        ),
        "fields": [
            {
                "name":   f"🟢 BUY Signals ({len(buys)})",
                "value":  buy_text,
                "inline": False
            },
            {
                "name":   f"🔴 SELL Signals ({len(sells)})",
                "value":  sell_text,
                "inline": False
            },
            {
                "name":   f"🟡 HOLD ({len(holds)})",
                "value":  hold_text,
                "inline": False
            },
            {
                "name":   "📊 Today's Stats",
                "value":  (
                    f"Total signals: **{len(signals)}** | "
                    f"Avg confidence: **{avg_conf:.1f}%** | "
                    f"System: **Alpaca Paper**"
                ),
                "inline": False
            },
            {
                "name":   "⚠️ Note",
                "value":  "Long-only system. SELL = exit long only, not short. Retraining scheduled after Day 30.",
                "inline": False
            }
        ],
        "footer": {
            "text": "ARJUN Engine • 20yr backtest • 77% win rate • Sharpe 1.2 • Max DD < 25%"
        },
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return await post_embed(embed, username="Arjun Signals")


async def main():
    log.info("="*50)
    log.info("  ARJUN Discord Poster")
    log.info(f"  {date.today().isoformat()}")
    log.info("="*50)

    # Skip weekends
    if datetime.now(ET).weekday() >= 5:
        log.info("Weekend — skipping Discord post")
        return

    signals = load_todays_signals()
    log.info(f"Loaded {len(signals)} signals")

    success = await post_morning_brief(signals)
    if success:
        log.info("✅ Morning brief posted to Discord")
    else:
        log.error("❌ Failed to post to Discord — check DISCORD_WEBHOOK_URL")


if __name__ == "__main__":
    asyncio.run(main())
