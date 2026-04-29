"""
market_scheduler.py — Runs market briefings on schedule.

Schedule (ET):
  Mon–Fri  09:00  → pre-market briefing + Discord
  Mon–Fri  16:00  → post-market briefing + Discord
  Fri 16:00       → last post of week, then silent until Mon 09:00
  Sat–Sun         → no posts

Start this alongside uvicorn:
  python3 -m backend.market.market_scheduler
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.market.market_briefing import generate_briefing
from backend.market.market_discord  import post_briefing_to_discord

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("market.scheduler")
ET  = ZoneInfo("America/New_York")

# Schedule: (weekday 0=Mon…6=Sun, hour, minute, mode)
SCHEDULE = [
    (0, 9,  0, "pre"),   # Monday    09:00 pre
    (0, 16, 0, "post"),  # Monday    16:00 post
    (1, 9,  0, "pre"),   # Tuesday   09:00 pre
    (1, 16, 0, "post"),  # Tuesday   16:00 post
    (2, 9,  0, "pre"),   # Wednesday 09:00 pre
    (2, 16, 0, "post"),  # Wednesday 16:00 post
    (3, 9,  0, "pre"),   # Thursday  09:00 pre
    (3, 16, 0, "post"),  # Thursday  16:00 post
    (4, 9,  0, "pre"),   # Friday    09:00 pre
    (4, 16, 0, "post"),  # Friday    16:00 post  ← last of week
]


def _next_run(now: datetime) -> tuple[datetime, str]:
    """Return the next scheduled datetime and mode."""
    for day_offset in range(8):   # look up to a week ahead
        check = now + timedelta(days=day_offset)
        for (wday, hour, minute, mode) in SCHEDULE:
            if check.weekday() != wday:
                continue
            candidate = check.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now:
                return candidate, mode
    raise RuntimeError("No next run found — check SCHEDULE")


async def run_briefing(mode: str):
    log.info(f"Running {mode}-market briefing...")
    try:
        briefing = await generate_briefing(mode)
        ok       = await post_briefing_to_discord(briefing)
        log.info(f"Briefing done — Discord: {'✓' if ok else '✗'}")
    except Exception as e:
        log.error(f"Briefing failed: {e}")


async def scheduler_loop():
    log.info("Market scheduler started")
    while True:
        now           = datetime.now(ET)
        next_dt, mode = _next_run(now)
        wait_secs     = (next_dt - now).total_seconds()

        day_name = next_dt.strftime("%A")
        log.info(f"Next briefing: {day_name} {next_dt.strftime('%I:%M %p ET')} ({mode}) "
                 f"— in {wait_secs/3600:.1f}h")

        await asyncio.sleep(wait_secs)
        await run_briefing(mode)
        await asyncio.sleep(60)  # avoid double-fire within same minute


if __name__ == "__main__":
    asyncio.run(scheduler_loop())
