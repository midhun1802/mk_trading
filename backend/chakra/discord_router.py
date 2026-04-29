"""
CHAKRA Discord Router
Central routing + deduplication for all Discord notifications.

2 main trading channels  +  4 system channels.
Every message type has a per-ticker cooldown to prevent spam.

Usage:
    from backend.chakra.discord_router import post, CHANNELS
    post("scalper", payload, ticker="SPY", msg_type="uoa")
    post("swings",  payload, ticker="NVDA", msg_type="entry", cooldown=0)
"""
import os, time, logging, requests
from dotenv import load_dotenv
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

log = logging.getLogger("CHAKRA.DiscordRouter")

# ── 2 Trading  +  4 System channels ─────────────────────────────────────────
# Trading:
#   scalper  → ALL index notifications  (SPY/QQQ/SPX/IWM 0DTE)
#   swings   → ALL swing notifications  (stocks ≤21 DTE)
# System:
#   lotto    → lotto plays
#   health   → engine health alerts
#   news     → market news
#   alerts   → general catch-all

CHANNELS: dict = {
    # ── Primary trade alerts (all open/close/watchlist) ───────
    "arjun_alerts": os.getenv("DISCORD_ARJUN_ALERTS", ""),
    # ── Legacy trading channels (kept for flow/internals only) ─
    "scalper": os.getenv("DISCORD_ARKA_SCALP_EXTREME", ""),
    "swings":  os.getenv("DISCORD_ARJUN_ALERTS",          # now points to arjun-alerts
               os.getenv("DISCORD_ARKA_SWINGS_EXTREME", "")),
    # ── System ───────────────────────────────────────────────
    "lotto":   os.getenv("DISCORD_ARKA_LOTTO",
               os.getenv("DISCORD_LOTTO", "")),
    "health":  os.getenv("DISCORD_APP_HEALTH",
               os.getenv("DISCORD_HEALTH_WEBHOOK", "")),
    "news":    os.getenv("DISCORD_ARJUN_ALERTS",
               os.getenv("DISCORD_ALERTS", "")),
    "alerts":  os.getenv("DISCORD_ARJUN_ALERTS",
               os.getenv("DISCORD_ALERTS", "")),
}

# ── All legacy / alias names → canonical channel ──────────────────────────────
CHANNEL_MAP: dict = {
    # arjun-alerts — primary destination for all trade/swing/watchlist alerts
    "arjun-alerts":           "arjun_alerts",
    "arjun_alerts":           "arjun_alerts",
    # scalper — flow/internals only (no longer receives trade open/close)
    "arka-extreme":           "scalper",
    "arka-scalp-extreme":     "scalper",
    "arka-scalp-signals":     "scalper",
    "arka-scalper":           "scalper",
    "arka-spx-only":          "scalper",
    "arka-gama-flips":        "scalper",
    "scalp-extreme":          "scalper",
    "scalp-signals":          "scalper",
    "scalper":                "scalper",
    # swings → now routes to arjun_alerts
    "arka-swings-extreme":    "arjun_alerts",
    "arka-swings-signals":    "arjun_alerts",
    "arka-swing-extreme":     "arjun_alerts",
    "arka-swings":            "arjun_alerts",
    "chakra-signals":         "arjun_alerts",
    "flow-signals":           "arjun_alerts",
    "swings":                 "arjun_alerts",
    # system
    "lotto":                  "lotto",
    "arka-lotto":             "lotto",
    "health":                 "health",
    "app-health":             "health",
    "news":                   "news",
    "alerts":                 "alerts",
}

# ── In-memory dedup cache ─────────────────────────────────────────────────────
_sent_cache: dict = {}   # key → last_sent_epoch_float


def _dedup_key(channel: str, ticker: str, msg_type: str) -> str:
    from datetime import date
    return f"{channel}:{ticker.upper()}:{msg_type}:{date.today()}"


def _can_send(channel: str, ticker: str, msg_type: str,
              cooldown: int = 300) -> bool:
    """
    Global dedup — same channel:ticker:msg_type can't fire within `cooldown` seconds.
    Default 5-minute window. Pass cooldown=0 to bypass.
    """
    if cooldown <= 0:
        return True
    key  = _dedup_key(channel, ticker, msg_type)
    last = _sent_cache.get(key, 0)
    now  = time.time()
    if now - last < cooldown:
        log.debug(f"  🔇 DEDUP {key} — sent {int(now-last)}s ago")
        return False
    _sent_cache[key] = now
    return True


def _is_market_hours() -> bool:
    from zoneinfo import ZoneInfo
    from datetime import datetime
    et = datetime.now(ZoneInfo("America/New_York"))
    return (et.weekday() < 5 and
            ((et.hour == 9 and et.minute >= 30) or et.hour > 9) and
            et.hour < 16)


_ALWAYS_ON = {"SPY", "QQQ", "SPX", "IWM"}


def post(channel: str, payload: dict,
         ticker:   str = "",
         msg_type: str = "generic",
         force:    bool = False,
         cooldown: int = 300) -> bool:
    """
    Route a Discord payload to the correct channel with dedup.

    Args:
        channel:  Canonical name or alias (resolved via CHANNEL_MAP)
        payload:  Discord API payload dict ({"embeds": [...]} or {"content": "..."})
        ticker:   Ticker symbol for dedup key (empty = no ticker-based dedup)
        msg_type: Message type for dedup key ("entry", "exit", "uoa", "flow", etc.)
        force:    Skip dedup + market-hours gate (for system/health messages)
        cooldown: Seconds between identical messages (0 = no cooldown)
    """
    # Resolve alias
    resolved = CHANNEL_MAP.get(channel.lower(), channel.lower())
    webhook  = CHANNELS.get(resolved, "")

    if not webhook:
        log.warning(f"  🔇 No webhook configured: '{channel}' → '{resolved}'")
        return False

    # Market hours gate (trading channels only, non-always-on tickers)
    if not force and resolved in ("scalper", "swings"):
        if not _is_market_hours() and ticker.upper() not in _ALWAYS_ON:
            log.debug(f"  🔇 {ticker} blocked — outside market hours ({resolved})")
            return False

    # Dedup check
    if not force and ticker and not _can_send(resolved, ticker, msg_type, cooldown):
        return False

    try:
        r = requests.post(webhook, json=payload, timeout=8)
        success = r.status_code in (200, 204)
        if not success:
            log.error(f"  ❌ Discord {r.status_code}: {r.text[:120]}")
        return success
    except Exception as e:
        log.error(f"  ❌ Discord router error: {e}")
        return False


# ── Convenience helpers ───────────────────────────────────────────────────────

def post_scalp(payload: dict, ticker: str = "", msg_type: str = "scalp",
               cooldown: int = 300, force: bool = False) -> bool:
    """Post to #arka-scalp-extreme."""
    return post("scalper", payload, ticker=ticker, msg_type=msg_type,
                cooldown=cooldown, force=force)


def post_swing(payload: dict, ticker: str = "", msg_type: str = "swing",
               cooldown: int = 300, force: bool = False) -> bool:
    """Post to #arka-swings-extreme."""
    return post("swings", payload, ticker=ticker, msg_type=msg_type,
                cooldown=cooldown, force=force)


def post_health(payload: dict) -> bool:
    """Post health/system alert (no dedup, no hours gate)."""
    return post("health", payload, force=True)


def post_news(payload: dict, ticker: str = "") -> bool:
    """Post news/ARJUN alert."""
    return post("news", payload, ticker=ticker, msg_type="news",
                cooldown=600, force=False)


if __name__ == "__main__":
    print("CHANNELS:")
    for name, url in CHANNELS.items():
        status = "✅" if url else "❌ EMPTY"
        print(f"  {status} {name}")
