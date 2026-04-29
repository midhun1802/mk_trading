"""
CHAKRA Pipeline Scheduler
Runs the agent pipeline every 15 minutes during market hours.
Replaces scattered crontab entries with in-process APScheduler.
"""
import asyncio, logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("CHAKRA.Scheduler")
ET  = ZoneInfo("America/New_York")

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False
    log.warning("APScheduler not available — pipeline won't auto-run")

PIPELINE_WATCHLIST = [
    "SPY","QQQ","IWM",
    "NVDA","TSLA","AAPL","MSFT","AMZN","META"
]


async def run_pipeline_job():
    """Async job executed each cycle."""
    from backend.arjun.pipeline.chakra_pipeline import run_cycle, is_market_hours

    if not is_market_hours():
        log.debug("Market closed — skipping pipeline cycle")
        return

    et = datetime.now(ET)
    log.info(f"⚡ Pipeline cycle at {et.strftime('%I:%M %p ET')}")

    try:
        result = await run_cycle(PIPELINE_WATCHLIST)
        sigs   = len(result.get("all_signals") or [])
        placed = len([r for r in (result.get("execution_results") or [])
                     if r.get("status")=="placed"])
        log.info(f"✅ Cycle done: {sigs} signals, {placed} orders placed")
    except Exception as e:
        log.error(f"❌ Pipeline cycle failed: {e}")


def start_scheduler():
    """Start APScheduler for market-hours pipeline cycles."""
    if not SCHEDULER_AVAILABLE:
        log.warning("APScheduler not installed — install: pip install apscheduler")
        return None

    scheduler = AsyncIOScheduler(timezone=ET)

    # Every 15 min during market hours Mon-Fri (9:30, 9:45, 10:00, 10:15 ...)
    scheduler.add_job(
        func             = run_pipeline_job,
        trigger          = CronTrigger(
            day_of_week  = "mon-fri",
            hour         = "9-15",
            minute       = "30,45,0,15",
            timezone     = ET,
        ),
        id               = "chakra_pipeline",
        name             = "CHAKRA Agent Pipeline",
        replace_existing = True,
        max_instances    = 1,
        coalesce         = True,
    )

    # Morning prep at 8:00 AM ET
    scheduler.add_job(
        func             = run_pipeline_job,
        trigger          = CronTrigger(
            day_of_week  = "mon-fri",
            hour         = "8",
            minute       = "0",
            timezone     = ET,
        ),
        id               = "chakra_morning",
        name             = "CHAKRA Morning Prep",
        replace_existing = True,
        max_instances    = 1,
        coalesce         = True,
    )

    scheduler.start()
    log.info("✅ CHAKRA scheduler started — pipeline runs every 15min 9:30-4pm ET")
    return scheduler
