#!/usr/bin/env python3
"""
ARKA Vanna Tracker
===================
Monitors intraday vanna exposure at key SPX strike nodes.
Fires alerts when vanna flips sign (POSITIVE→NEGATIVE or vice versa)
which signals a dealer sensitivity regime change.

Negative vanna + rising IV = dealers SELL = amplifies downside
Positive vanna + falling IV = dealers BUY = supports upside

Runs every minute alongside gamma_node_tracker.py.

Usage:
  python3 backend/chakra/vanna_tracker.py          # run once
  python3 backend/chakra/vanna_tracker.py --watch  # continuous

Crontab:
  * 9-16 * * 1-5 cd ~/trading-ai && venv/bin/python3 backend/chakra/vanna_tracker.py >> logs/chakra/vanna.log 2>&1
"""

import os, sys, json, logging, time
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv
load_dotenv(BASE / ".env", override=True)

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("ARKA.Vanna")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VANNA] %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)

POLYGON_KEY     = os.getenv("POLYGON_API_KEY", "")
DISCORD_ALERTS  = os.getenv("DISCORD_ALERTS", os.getenv("DISCORD_WEBHOOK_URL", ""))
DISCORD_GAMMA_FLIP = os.getenv("DISCORD_GAMMA_FLIP_WEBHOOK", "")
DISCORD_EXTREME = os.getenv("DISCORD_ARKA_SCALP_EXTREME", "")

STATE_FILE = BASE / "logs/chakra/vanna_state.json"

# Thresholds
MIN_OI              = 0    # allow zero OI — filter by gamma size instead
MIN_VANNA_USD       = 500_000_000   # $500M vanna to track a node
FLIP_ALERT_COOLDOWN = 3600          # once per hour per strike


def fetch_spx_options() -> list:
    try:
        r = httpx.get(
            "https://api.polygon.io/v3/snapshot/options/SPY",
            params={"apiKey": POLYGON_KEY, "limit": 250},
            timeout=15,
        )
        return r.json().get("results", [])
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return []


def fetch_iv() -> float:
    """Get VIX proxy from SPX ATM IV."""
    try:
        r = httpx.get(
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/UVXY",
            params={"apiKey": POLYGON_KEY},
            timeout=5,
        )
        snap = r.json().get("ticker", {})
        return float(snap.get("day", {}).get("c", 0) or 0)
    except Exception:
        return 0.0


def compute_vanna(contracts: list, spot: float) -> dict:
    """
    Compute dollar vanna at each strike.
    Vanna = dDelta/dIV = sensitivity of delta to changes in implied vol.
    Dollar vanna = vanna × OI × 100 × spot
    Returns: {strike: {vanna_usd, calls, puts, net, oi}}
    """
    nodes = {}
    for c in contracts:
        greeks  = c.get("greeks", {})
        delta   = greeks.get("delta")
        gamma   = greeks.get("gamma")
        vega    = greeks.get("vega")
        iv      = float((c.get("implied_volatility") or c.get("last_quote",{}).get("implied_volatility") or 0))
        oi      = int(c.get("open_interest", 0) or 0)
        details = c.get("details", {})
        strike  = float(details.get("strike_price", 0) or 0)
        ct      = details.get("contract_type", "").lower()

        if not gamma or not delta or not strike or oi < MIN_OI:
            continue

        # Derive vanna from vega and spot: vanna ≈ vega × (1 - delta×delta) / (spot × iv)
        # Simplified: vanna ≈ vega / spot (approximation when iv unavailable)
        if iv and iv > 0 and vega:
            vanna = float(vega) / (spot * iv) if spot > 0 else 0
        elif vega:
            vanna = float(vega) / spot if spot > 0 else 0
        else:
            vanna = float(gamma) * 0.1  # rough proxy

        # Dollar vanna = vanna × OI × 100 × spot
        vanna_usd = vanna * oi * 100 * spot

        if strike not in nodes:
            nodes[strike] = {"vanna_usd": 0, "oi": 0, "calls": 0, "puts": 0}

        nodes[strike]["oi"] += oi
        nodes[strike]["vanna_usd"] += vanna_usd if ct == "call" else -vanna_usd
        if ct == "call":
            nodes[strike]["calls"] += vanna_usd
        else:
            nodes[strike]["puts"] += abs(vanna_usd)

    return nodes


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            s = json.loads(STATE_FILE.read_text())
            if s.get("date") == date.today().isoformat():
                return s
    except Exception:
        pass
    return {
        "date":          date.today().isoformat(),
        "prev_nodes":    {},
        "open_nodes":    {},
        "alerts_sent":   [],
        "total_vanna":   0,
        "scans":         0,
        "last_scan":     "",
        "flip_history":  [],
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def post_discord(webhook: str, embed: dict, content: str = "") -> bool:
    if not webhook:
        return False
    try:
        payload = {"embeds": [embed], "username": "ARKA Vanna Tracker"}
        if content:
            payload["content"] = content
        r = httpx.post(DISCORD_GAMMA_FLIP or webhook, json=payload, timeout=8)
        return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"Discord error: {e}")
        return False


def run_scan():
    now   = datetime.now(ET)
    state = load_state()
    state["scans"] += 1
    state["last_scan"] = now.strftime("%H:%M")

    if not (9 <= now.hour < 16):
        log.info(f"  Market closed — {now.strftime('%H:%M ET')}")
        save_state(state)
        return

    log.info(f"  Vanna scan #{state['scans']} — {now.strftime('%H:%M ET')}")

    # Fetch data
    contracts = fetch_spx_options()
    if not contracts:
        log.warning("  No SPX options data")
        return

    # Get SPX spot
    try:
        r = httpx.get(
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/SPY",
            params={"apiKey": POLYGON_KEY}, timeout=5
        )
        spy = float(r.json().get("ticker",{}).get("day",{}).get("c", 0) or 0)
        spot = spy * 10
    except Exception:
        spot = 5800.0

    if not spot:
        return

    # Get current IV
    iv = fetch_iv()

    # Compute vanna nodes
    current = compute_vanna(contracts, spot)
    if not current:
        log.warning("  No vanna computed (Greeks unavailable)")
        return

    # Total market vanna
    total_vanna = sum(n["vanna_usd"] for n in current.values())
    state["total_vanna"] = round(total_vanna, 0)
    log.info(f"  SPX spot: {spot:.0f} | Total vanna: ${total_vanna/1e9:.2f}B | IV proxy: {iv:.2f}")

    # Set open baseline on first scan
    if not state["open_nodes"]:
        state["open_nodes"] = {str(s): n["vanna_usd"] for s, n in current.items()}
        log.info(f"  Vanna baseline set for {len(current)} nodes")
        state["prev_nodes"] = {str(s): n["vanna_usd"] for s, n in current.items()}
        save_state(state)
        return

    prev  = state["prev_nodes"]
    alerts_fired = 0
    node_summaries = []

    for strike, node in sorted(current.items()):
        key      = str(strike)
        curr_v   = node["vanna_usd"]
        prev_v   = float(prev.get(key, curr_v))
        open_v   = float(state["open_nodes"].get(key, curr_v))

        if abs(curr_v) < MIN_VANNA_USD:
            continue

        node_summaries.append({
            "strike":     strike,
            "vanna_usd":  round(curr_v, 0),
            "open_vanna": round(open_v, 0),
            "change":     round(curr_v - prev_v, 0),
            "sign":       "POS" if curr_v > 0 else "NEG",
        })

        # ── Vanna flip detection ──────────────────────────────────────────────
        flipped = (prev_v > 0 > curr_v) or (prev_v < 0 < curr_v)
        if not flipped:
            continue

        dedup_key = f"vanna_flip_{strike}_{now.strftime('%H')}"
        if dedup_key in state["alerts_sent"]:
            continue
        state["alerts_sent"].append(dedup_key)
        alerts_fired += 1

        direction    = "NEGATIVE → POSITIVE ↑" if curr_v > 0 else "POSITIVE → NEGATIVE ↓"
        consequence  = (
            "IV relief rally possible — dealers buying as vol drops" if curr_v > 0
            else "IV rising = dealers SELL into move — amplifies downside"
        )
        is_bearish   = curr_v < 0
        color        = 0xFF4444 if is_bearish else 0x00FF88
        is_extreme   = abs(curr_v) >= 2_000_000_000  # $2B+

        log.info(f"  ⚡ VANNA FLIP: ${strike:.0f} {direction} | ${curr_v/1e9:.2f}B")

        # Record flip in history
        state["flip_history"].append({
            "strike":    strike,
            "direction": direction,
            "vanna_usd": curr_v,
            "time":      now.strftime("%H:%M"),
            "iv":        iv,
        })
        state["flip_history"] = state["flip_history"][-20:]

        embed = {
            "color":  color,
            "author": {"name": f"⚡ VANNA FLIP — SPX ${strike:.0f} | {direction}"},
            "description": consequence,
            "fields": [
                {"name": "💲 SPX Spot",       "value": f"${spot:.0f}",                    "inline": True},
                {"name": "📊 Strike",          "value": f"${strike:.0f}",                  "inline": True},
                {"name": "⚡ Vanna Now",       "value": f"${curr_v/1e9:.2f}B",            "inline": True},
                {"name": "📈 Was",             "value": f"${prev_v/1e9:.2f}B",            "inline": True},
                {"name": "🌡️ IV Proxy",        "value": f"{iv:.2f}",                      "inline": True},
                {"name": "🌍 Total Mkt Vanna", "value": f"${total_vanna/1e9:.2f}B",       "inline": True},
                {"name": "💡 Implication",
                 "value": (
                     f"**{'Bearish regime confirmed' if is_bearish else 'Bullish relief incoming'}**\n"
                     f"Dealers now {'add selling pressure' if is_bearish else 'provide buying support'} "
                     f"as IV {'rises' if is_bearish else 'falls'}."
                 ),
                 "inline": False},
            ],
            "footer": {"text": f"ARKA Vanna Tracker • {now.strftime('%I:%M %p ET')}"}
        }

        content = f"⚡ **VANNA FLIP** — SPX ${strike:.0f} | {direction}" if is_extreme else ""
        webhook = DISCORD_EXTREME if is_extreme else DISCORD_ALERTS
        post_discord(webhook, embed, content)

    # Also post total vanna summary every 30 min
    if state["scans"] % 30 == 0:
        top_nodes = sorted(node_summaries, key=lambda x: abs(x["vanna_usd"]), reverse=True)[:5]
        if top_nodes and DISCORD_ALERTS:
            lines = "\n".join([
                f"**${n['strike']:.0f}** — ${n['vanna_usd']/1e9:.2f}B "
                f"({'🟢 POS' if n['sign']=='POS' else '🔴 NEG'})"
                for n in top_nodes
            ])
            embed = {
                "color": 0x3498DB,
                "author": {"name": f"📊 Vanna Summary — {now.strftime('%I:%M %p ET')}"},
                "description": f"**Total market vanna: ${total_vanna/1e9:.2f}B**\n\n{lines}",
                "fields": [
                    {"name": "🌡️ IV Proxy", "value": str(round(iv, 2)), "inline": True},
                    {"name": "💲 SPX",      "value": f"${spot:.0f}",    "inline": True},
                    {"name": "⚡ Flips today", "value": str(len(state["flip_history"])), "inline": True},
                ],
                "footer": {"text": "ARKA Vanna Tracker"}
            }
            post_discord(DISCORD_ALERTS, embed)

    # Update prev state
    state["prev_nodes"] = {str(s): n["vanna_usd"] for s, n in current.items()}
    save_state(state)
    log.info(f"  Scan complete — {alerts_fired} flip alert(s) | {len(node_summaries)} nodes tracked")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch",  action="store_true")
    parser.add_argument("--reset",  action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.reset:
        STATE_FILE.unlink(missing_ok=True)
        print("Vanna baseline reset")
        sys.exit(0)

    if args.status:
        if STATE_FILE.exists():
            s = json.loads(STATE_FILE.read_text())
            print(f"Date: {s.get('date')} | Scans: {s.get('scans')} | "
                  f"Total vanna: ${s.get('total_vanna',0)/1e9:.2f}B")
            print(f"Flips today: {len(s.get('flip_history',[]))}")
            for f in s.get('flip_history', []):
                print(f"  {f['time']} ${f['strike']:.0f} {f['direction']}")
        sys.exit(0)

    if args.watch:
        log.info("Vanna Tracker starting — watching every 60s")
        while True:
            try:
                run_scan()
            except Exception as e:
                log.error(f"Scan error: {e}")
            time.sleep(60)
    else:
        run_scan()
