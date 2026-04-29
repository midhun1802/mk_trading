"""
Heat Seeker → ARKA Bridge
=========================
Caches the latest Heat Seeker scan results in memory and on disk.
Provides conviction boost for ARKA trades without an HTTP round-trip
on every scan cycle.

Used by arka_engine.py to replace the old _get_heatseeker_boost() method.

Also triggers HS→ARJUN intraday deliberation when a strong signal (score >= 72)
is detected. ARJUN writes a trade_request that ARKA picks up in the next scan.
"""
import json
import time
import asyncio
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger("CHAKRA.HSBridge")

BASE = Path(__file__).resolve().parents[2]

# ── In-memory cache ───────────────────────────────────────────────────────────
_hs_cache: dict = {
    "scalp": {"signals": [], "ts": 0.0},
    "swing": {"signals": [], "ts": 0.0},
}

CACHE_TTL          = 300   # 5 min — stale after this
FILE_CACHE_TTL     = 600   # 10 min — max file cache age
ARJUN_TRIGGER_SCORE = 72   # HS score threshold to fire ARJUN deliberation

# Track last ARJUN trigger per ticker to avoid hammering the API
_arjun_last_trigger: dict = {}   # ticker → unix ts
ARJUN_COOLDOWN = 300             # 5 min cooldown per ticker


# ── File cache paths ──────────────────────────────────────────────────────────

def _cache_path(mode: str) -> Path:
    p = BASE / f"logs/heatseeker/latest_{mode}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_from_file(mode: str) -> list:
    """Fall back to on-disk cache when memory cache is stale."""
    try:
        path = _cache_path(mode)
        if not path.exists():
            return []
        d   = json.loads(path.read_text())
        age = time.time() - d.get("ts", 0)
        if age > FILE_CACHE_TTL:
            return []
        return d.get("signals", [])
    except Exception:
        return []


def _run_arjun_pipeline():
    """
    Background thread: write HS pending signals then run ARJUN deliberation.
    Fired when a strong HS signal (score >= ARJUN_TRIGGER_SCORE) is detected.
    NEVER imports heat_seeker.py — reads cache files only.
    """
    try:
        from backend.arka.hs_signal_writer import write_pending_signals
        from backend.arjun.arjun_intraday import run_pipeline
        sigs   = write_pending_signals()
        result = run_pipeline()
        if result and result.get("decision") == "EXECUTE":
            log.info(
                f"  🤖 ARJUN EXECUTE: {result['ticker']} {result['direction']} "
                f"conf={result['confidence']:.0f}%"
            )
        else:
            log.debug(f"  🤖 ARJUN result: {result.get('decision','NONE')}")
    except Exception as e:
        log.error(f"  ARJUN pipeline trigger error: {e}")


def update_cache_from_scan(mode: str, signals: list):
    """
    Called by dashboard_api after every /api/heatseeker/scan.
    Writes both memory and file cache.
    Also triggers ARJUN deliberation when a top signal scores >= ARJUN_TRIGGER_SCORE.
    """
    _hs_cache[mode] = {"signals": signals, "ts": time.time()}
    try:
        _cache_path(mode).write_text(json.dumps({
            "signals":    signals[:20],
            "mode":       mode,
            "ts":         time.time(),
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }, default=str))
    except Exception as e:
        log.debug(f"  HS file cache write failed: {e}")

    # Fire ARJUN if a strong signal is present and cooled down
    now = time.time()
    for sig in signals[:3]:  # check top 3 only
        score  = float(sig.get("score", 0))
        ticker = sig.get("ticker", "")
        if score >= ARJUN_TRIGGER_SCORE and ticker:
            last = _arjun_last_trigger.get(ticker, 0)
            if now - last >= ARJUN_COOLDOWN:
                _arjun_last_trigger[ticker] = now
                log.info(
                    f"  🔥 HS score={score:.0f} on {ticker} — triggering ARJUN deliberation"
                )
                t = threading.Thread(target=_run_arjun_pipeline, daemon=True)
                t.start()
                break  # one ARJUN trigger per scan update


# ── Conviction boost ──────────────────────────────────────────────────────────

def get_hs_conviction_boost(ticker: str, direction: str) -> dict:
    """
    Return a conviction boost dict for a given ticker + direction.
    Called by ARKA before placing every trade — no HTTP call needed.

    Returns:
        boost    (int)  — conviction points to add (0–25)
        reason   (str)  — human-readable explanation
        score    (int)  — best matching signal score
        is_sweep (bool) — confirmed sweep
        premium  (float)
        vol_mult (float)
    """
    result = {"boost": 0, "reason": "", "score": 0, "is_sweep": False,
              "premium": 0, "vol_mult": 0}

    is_call = direction in ("CALL", "LONG", "BULLISH")

    for mode in ("scalp", "swing"):
        cache   = _hs_cache.get(mode, {})
        mem_age = time.time() - cache.get("ts", 0)

        # Use memory cache if fresh, else fall back to file
        if mem_age <= CACHE_TTL:
            signals = cache.get("signals", [])
        else:
            signals = _load_from_file(mode)

        for sig in signals:
            if sig.get("ticker", "").upper() != ticker.upper():
                continue

            bias     = (sig.get("bias") or "").upper()
            sig_bull = "BULL" in bias
            sig_bear = "BEAR" in bias

            aligned = (is_call and sig_bull) or (not is_call and sig_bear)
            if not aligned:
                continue

            score    = sig.get("score", 0)
            is_sweep = sig.get("is_sweep", False)
            premium  = sig.get("premium", 0)
            vol_mult = sig.get("vol_mult", 0)

            # Boost tiers
            if score >= 85:
                boost  = 25
                reason = f"HS:{score} {vol_mult:.0f}x vol"
            elif score >= 75:
                boost  = 18
                reason = f"HS:{score} {vol_mult:.0f}x vol"
            elif score >= 65:
                boost  = 12
                reason = f"HS:{score} confirmed"
            else:
                boost  = 5
                reason = f"HS:{score} weak"

            if is_sweep:
                boost  += 7
                reason += " SWEEP"

            if premium >= 200_000:
                boost  += 5
                reason += f" ${premium/1000:.0f}K"

            boost = min(boost, 25)

            if boost > result["boost"]:
                result = {
                    "boost":    boost,
                    "reason":   reason,
                    "score":    score,
                    "is_sweep": is_sweep,
                    "premium":  premium,
                    "vol_mult": vol_mult,
                }

    return result


# ── Background refresh loop ───────────────────────────────────────────────────

async def refresh_cache(mode: str = "scalp"):
    """Pull fresh scan from the dashboard API and update memory cache."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"http://localhost:5001/api/heatseeker/scan?mode={mode}"
            )
            if r.status_code == 200:
                data    = r.json()
                signals = data.get("signals", [])
                update_cache_from_scan(mode, signals)
                log.info(f"  🔥 HS cache refreshed ({mode}): {len(signals)} signals")
    except Exception as e:
        log.debug(f"  HS cache refresh failed ({mode}): {e}")


async def auto_refresh_loop():
    """
    Background loop: refresh scalp cache every 5 min, swing every 10 min.
    Started once by arka_engine on startup.
    """
    log.info("  🔥 Heat Seeker bridge auto-refresh loop started")
    while True:
        try:
            await refresh_cache("scalp")
            await asyncio.sleep(30)
            await refresh_cache("swing")
        except Exception as e:
            log.error(f"  HS refresh loop error: {e}")
        await asyncio.sleep(270)  # 5 min total cycle
