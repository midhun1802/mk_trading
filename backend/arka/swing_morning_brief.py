"""
CHAKRA Swing Morning Brief
Posts daily swing watchlist to Discord at 8:30 AM.
Shows each candidate with direction, score, and why.

Crontab:
  30 8 * * 1-5 cd ~/trading-ai && venv/bin/python3 backend/arka/swing_morning_brief.py >> logs/arka/swing_brief.log 2>&1
"""
import os, json, logging, requests
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SwingBrief] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("CHAKRA.SwingBrief")
ET  = ZoneInfo("America/New_York")


def post_swing_morning_brief() -> bool:
    """
    Post morning brief of swing candidates to #arka-swings-extreme.
    Reads from logs/chakra/watchlist_latest.json (written by --premarket scan).
    """
    # Resolve webhook via discord_router when available, else direct env
    try:
        from backend.chakra.discord_router import CHANNELS
        webhook = CHANNELS.get("swings", "")
    except Exception:
        webhook = os.getenv("DISCORD_ARKA_SWINGS_EXTREME",
                  os.getenv("DISCORD_ARKA_SWINGS_SIGNALS", ""))

    if not webhook:
        log.warning("No swings webhook configured")
        return False

    wl_path = BASE / "logs/chakra/watchlist_latest.json"
    if not wl_path.exists():
        log.warning("No watchlist file — run --premarket first")
        return False

    try:
        d     = json.loads(wl_path.read_text())
        cands = d.get("candidates", [])
    except Exception as e:
        log.error(f"Watchlist parse error: {e}")
        return False

    if not cands:
        log.info("No swing candidates today — skipping brief")
        return False

    now       = datetime.now(ET)
    date_str  = now.strftime("%A, %B %d")
    scan_time = d.get("scan_time", "")[:16]

    # Sort by score descending
    cands = sorted(cands, key=lambda x: x.get("score", 0), reverse=True)

    calls = [c for c in cands if c.get("direction") in ("LONG", "CALL", "BULLISH")]
    puts  = [c for c in cands if c.get("direction") in ("SHORT", "PUT", "BEARISH")]

    fields = []

    # ── Header ──────────────────────────────────────────────────────────────
    fields.append({
        "name":  f"📊 Today's Scan — {len(cands)} candidates",
        "value": (
            f"**{len(calls)} CALLS** (bullish)  •  "
            f"**{len(puts)} PUTS** (bearish)\n"
            f"Auto-enters score ≥60 during market hours  •  Max 3 positions  •  ≤21 DTE"
        ),
        "inline": False,
    })

    # ── One field per ticker (top 10) ────────────────────────────────────────
    for c in cands[:10]:
        ticker    = c.get("ticker", "?")
        direction = c.get("direction", "?")
        score     = c.get("score", 0)
        price     = c.get("price", 0)
        tp1       = c.get("tp1", 0) or c.get("target", 0)
        stop      = c.get("stop", 0) or c.get("stop_loss", 0)
        rr        = c.get("rr", 0)
        reasons   = c.get("reasons", [])

        is_long    = direction in ("LONG", "CALL", "BULLISH")
        emoji      = "📈" if is_long else "📉"
        dir_label  = "CALLS" if is_long else "PUTS"
        score_icon = "🟢" if score >= 75 else "🟡" if score >= 65 else "⚪"

        val = f"{emoji} **{dir_label}** | Score: {score_icon} **{score}** | Price: **${price:.2f}**\n"
        if tp1 and stop:
            rr_str = f" | R/R: {rr:.1f}" if rr else ""
            val   += f"Target: **${tp1:.2f}** | Stop: **${stop:.2f}**{rr_str}\n"
        if reasons:
            val += f"*{' • '.join(str(r) for r in reasons[:2])}*"

        fields.append({
            "name":   ticker,
            "value":  val,
            "inline": False,
        })

    # ── Rules reminder ───────────────────────────────────────────────────────
    fields.append({
        "name":  "📋 ARKA Swing Rules",
        "value": (
            "• 1 contract per position  •  ≤21 DTE options\n"
            "• Auto-enters score ≥60 during market hours\n"
            "• Target: +20%  |  Stop: -30%  |  Max hold: 15 days\n"
            "• Entry scan every 30 min: 9:30am–3:30pm ET"
        ),
        "inline": False,
    })

    embed = {
        "title":       f"🌊 ARKA SWING WATCHLIST — {date_str}",
        "description": (
            f"Daily swing candidates for **{date_str}**.\n"
            f"ARKA will auto-enter high-conviction setups during market hours."
        ),
        "color":       0x9B59B6,
        "fields":      fields,
        "footer": {
            "text": (
                f"CHAKRA Swing Engine  •  "
                f"Scan: {scan_time}  •  "
                f"Refreshes every 30min"
            )
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        r = requests.post(webhook, json={"embeds": [embed]}, timeout=8)
        success = r.status_code in (200, 204)
        if success:
            log.info(f"✅ Morning brief posted — {len(cands)} candidates ({len(calls)}C/{len(puts)}P)")
        else:
            log.error(f"❌ Brief failed: {r.status_code} {r.text[:80]}")
        return success
    except Exception as e:
        log.error(f"❌ Brief error: {e}")
        return False


if __name__ == "__main__":
    result = post_swing_morning_brief()
    print("✅ Sent" if result else "❌ Failed")
