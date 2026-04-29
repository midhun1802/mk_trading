"""
ARKA EOD Closer — Hard close all options positions at 3:57 PM ET
Run via crontab: 57 15 * * 1-5

Runs independently of arka_engine.py so a sleeping/hung engine
does not cause 0DTE options to expire unmanaged.
"""
import os, json, logging
from datetime import datetime, date
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

ET = ZoneInfo("America/New_York")
log = logging.getLogger("ARKA.EODCloser")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EODCloser] %(message)s",
    datefmt="%H:%M:%S",
)

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE   = "https://paper-api.alpaca.markets"

WH_ARJUN = os.getenv("DISCORD_ARJUN_ALERTS", "")


def _alpaca(method: str, path: str, **kwargs):
    import httpx
    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    fn = getattr(httpx, method)
    return fn(f"{ALPACA_BASE}{path}", headers=headers, timeout=10, **kwargs)


def _post_discord(msg: str):
    if not WH_ARJUN:
        return
    try:
        import httpx
        httpx.post(WH_ARJUN, json={"content": msg}, timeout=8)
    except Exception as e:
        log.warning(f"Discord failed: {e}")


def close_all_options():
    now = datetime.now(ET)
    log.info(f"EOD Closer running — {now.strftime('%H:%M ET')}")

    # Only run in the 3:58–4:05 PM ET window
    if not ((now.hour == 15 and now.minute >= 58) or (now.hour == 16 and now.minute <= 5)):
        log.info("Outside EOD window — exiting")
        return

    try:
        r = _alpaca("get", "/v2/positions")
        positions = r.json()
    except Exception as e:
        log.error(f"Failed to fetch positions: {e}")
        _post_discord(f"⚠️ **EOD Closer failed** — could not fetch positions: `{e}`")
        return

    if not positions or isinstance(positions, dict):
        log.info("No positions to close")
        return

    options_positions = [
        p for p in positions
        if p.get("asset_class") == "us_option" or
           (len(p.get("symbol", "")) > 10 and any(c.isdigit() for c in p.get("symbol", "")))
    ]

    if not options_positions:
        log.info("No options positions open — nothing to close")
        return

    closed = []
    failed = []
    for pos in options_positions:
        sym    = pos.get("symbol", "")
        qty    = abs(int(float(pos.get("qty", 1))))
        entry  = float(pos.get("avg_entry_price", 0))
        curr   = float(pos.get("current_price") or pos.get("lastday_price") or 0)
        pnl    = float(pos.get("unrealized_pl", 0))
        pnl_pct = float(pos.get("unrealized_plpc", 0)) * 100

        log.info(f"  Closing {sym} | qty={qty} | entry=${entry:.2f} | now=${curr:.2f} | P&L={pnl_pct:+.1f}%")
        try:
            r = _alpaca("delete", f"/v2/positions/{sym}")
            if r.status_code in (200, 204):
                closed.append((sym, pnl, pnl_pct))
                log.info(f"  ✅ Closed {sym}")
            else:
                failed.append((sym, r.status_code, r.text[:100]))
                log.error(f"  ❌ Failed {sym}: {r.status_code} {r.text[:100]}")
        except Exception as e:
            failed.append((sym, 0, str(e)))
            log.error(f"  ❌ Exception closing {sym}: {e}")

    # Discord summary
    total_pnl = sum(p for _, p, _ in closed)
    lines = ["⏰ **EOD Closer — 3:57 PM Auto-Close**\n"]
    for sym, pnl, pct in closed:
        icon = "✅" if pnl >= 0 else "❌"
        lines.append(f"{icon} `{sym}` → **{pct:+.1f}%** (${pnl:+.2f})")
    if failed:
        for sym, code, msg in failed:
            lines.append(f"⚠️ `{sym}` close failed [{code}]: {msg[:50]}")
    lines.append(f"\n**Total P&L: ${total_pnl:+.2f}** | Closed: {len(closed)} | Failed: {len(failed)}")

    _post_discord("\n".join(lines))
    log.info(f"Done — closed {len(closed)}, failed {len(failed)}, P&L ${total_pnl:+.2f}")


if __name__ == "__main__":
    close_all_options()
