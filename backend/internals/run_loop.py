import sys, asyncio, time, logging
sys.path.insert(0, '.')
from backend.internals.market_internals import run_internals

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("internals_loop")

async def loop():
    while True:
        try:
            log.info("Running market internals...")
            await run_internals()
            log.info("Done. Sleeping 5 minutes...")
        except Exception as e:
            log.error(f"Internals error: {e}")
        await asyncio.sleep(300)  # 5 minutes

asyncio.run(loop())
