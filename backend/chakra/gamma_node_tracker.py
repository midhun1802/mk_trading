#!/usr/bin/env python3
"""
ARKA Gamma Node Tracker
========================
Monitors intraday gamma buildup at key SPX/SPY strike levels.
Runs every minute during market hours, detects when nodes go "live"
(significant gamma addition at a strike while price is testing it).

Posts to #alerts Discord channel when:
  - Gamma at a node increases 3x+ from open
  - Largest single-minute gamma addition detected
  - Node goes live (price within 0.3% of strike + gamma spike)

Usage:
  python3 backend/chakra/gamma_node_tracker.py          # run once
  python3 backend/chakra/gamma_node_tracker.py --watch  # run every minute

Crontab:
  * 9-16 * * 1-5 cd ~/trading-ai && venv/bin/python3 backend/chakra/gamma_node_tracker.py >> logs/chakra/gamma_nodes.log 2>&1
"""

import os, sys, json, logging, time
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx

# ── Path setup ────────────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv
load_dotenv(BASE / ".env", override=True)

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("ARKA.GammaNodes")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GAMMA] %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)

# ── Config ────────────────────────────────────────────────────────────────────
POLYGON_KEY    = os.getenv("POLYGON_API_KEY", "")
DISCORD_ALERTS = os.getenv("DISCORD_ALERTS", os.getenv("DISCORD_WEBHOOK_URL", ""))
DISCORD_EXTREME = os.getenv("DISCORD_ARKA_SCALP_EXTREME", "")
DISCORD_GAMMA_FLIP = os.getenv("DISCORD_GAMMA_FLIP_WEBHOOK", "")

STATE_FILE = BASE / "logs/chakra/gamma_nodes_state.json"
BASE / "logs/chakra"

# Thresholds
MIN_OI           = 500      # minimum OI to track a node
GAMMA_SPIKE_MULT = 3.0      # 3x gamma from open = node going live
MIN_DELTA_USD    = 500_000  # minimum $500K gamma addition to alert
NODE_PROXIMITY   = 0.003    # price within 0.3% of strike = "testing"
LOOKBACK_STRIKES = 50       # top N strikes by OI to monitor


def fetch_spx_options() -> list:
    """Fetch SPX options snapshot with Greeks."""
    try:
        r = httpx.get(
            "https://api.polygon.io/v3/snapshot/options/SPY",
            params={
                "apiKey": POLYGON_KEY,
                "limit":  250,
            },
            timeout=15,
        )
        return r.json().get("results", [])
    except Exception as e:
        log.error(f"SPX options fetch failed: {e}")
        return []


def fetch_spx_price() -> float:
    """Get current SPX price."""
    try:
        r = httpx.get(
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/SPY",
            params={"apiKey": POLYGON_KEY},
            timeout=5,
        )
        snap = r.json().get("ticker", {})
        spy_price = float(snap.get("day", {}).get("c", 0) or snap.get("min", {}).get("c", 0))
        return spy_price * 10  # SPY * 10 ≈ SPX
    except Exception:
        return 0.0


def compute_node_gamma(contracts: list, spot: float) -> dict:
    """
    Compute dollar gamma at each strike node.
    Returns dict: strike → {gamma_usd, oi, calls, puts, net}
    """
    nodes = {}

    for c in contracts:
        greeks  = c.get("greeks", {})
        gamma   = greeks.get("gamma")
        oi      = int(c.get("open_interest", 0) or 0)
        details = c.get("details", {})
        strike  = float(details.get("strike_price", 0) or 0)
        ct      = details.get("contract_type", "").lower()

        if not gamma or not strike or abs(float(gamma)) < 1e-8:
            continue

        # Dollar gamma = gamma × OI × 100 × spot²/ 100
        # (standard dealer gamma exposure formula)
        gamma_usd = gamma * oi * 100 * (spot ** 2) / 100

        if strike not in nodes:
            nodes[strike] = {"gamma_usd": 0, "oi": 0, "calls": 0, "puts": 0}

        nodes[strike]["oi"]       += oi
        nodes[strike]["gamma_usd"] += gamma_usd if ct == "call" else -gamma_usd

        if ct == "call":
            nodes[strike]["calls"] += gamma_usd
        else:
            nodes[strike]["puts"]  += abs(gamma_usd)

    return nodes


def load_state() -> dict:
    """Load today's gamma node state."""
    try:
        if STATE_FILE.exists():
            s = json.loads(STATE_FILE.read_text())
            if s.get("date") == date.today().isoformat():
                return s
    except Exception:
        pass
    return {
        "date":         date.today().isoformat(),
        "open_nodes":   {},   # strike → open gamma_usd
        "peak_nodes":   {},   # strike → peak gamma_usd
        "alerts_sent":  [],   # list of "strike_HH:MM" keys (dedup)
        "scans":        0,
        "last_scan":    "",
        "top_nodes":    [],   # sorted list of top nodes this session
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def post_discord(webhook: str, embed: dict, content: str = "") -> bool:
    if not webhook:
        return False
    try:
        payload = {"embeds": [embed], "username": "ARKA Gamma Tracker"}
        if content:
            payload["content"] = content
        r = httpx.post(webhook, json=payload, timeout=8)
        return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"Discord post failed: {e}")
        return False


def run_scan():
    """Main gamma node scan — runs once per minute."""
    now    = datetime.now(ET)
    state  = load_state()
    state["scans"] += 1
    state["last_scan"] = now.strftime("%H:%M")

    # Market hours check
    if not (9 <= now.hour < 16):
        log.info(f"  Market closed — {now.strftime('%H:%M ET')}")
        save_state(state)
        return

    log.info(f"  Gamma node scan #{state['scans']} — {now.strftime('%H:%M ET')}")

    # Fetch data
    spot = fetch_spx_price()
    if not spot:
        log.warning("  Could not get SPX price")
        return

    contracts = fetch_spx_options()
    if not contracts:
        log.warning("  No SPX options data")
        return

    log.info(f"  SPX spot: {spot:.0f} | Contracts: {len(contracts)}")

    # Compute current nodes
    current_nodes = compute_node_gamma(contracts, spot)
    if not current_nodes:
        log.warning("  No nodes computed (Greeks may be unavailable)")
        return

    # Sort by abs gamma — top LOOKBACK_STRIKES
    top = sorted(current_nodes.items(),
                 key=lambda x: abs(x[1]["gamma_usd"]),
                 reverse=True)[:LOOKBACK_STRIKES]

    log.info(f"  Top nodes computed: {len(top)}")

    # Store open baseline on first scan
    is_first_scan = len(state["open_nodes"]) == 0
    if is_first_scan:
        state["open_nodes"] = {str(s): n["gamma_usd"] for s, n in top}
        log.info(f"  Baseline set for {len(top)} nodes")
        save_state(state)
        return

    # Check each top node for significant changes
    alerts_fired = 0
    node_summaries = []

    for strike, node in top:
        key         = str(strike)
        open_gamma  = state["open_nodes"].get(key, node["gamma_usd"])
        prev_peak   = state["peak_nodes"].get(key, open_gamma)
        curr_gamma  = node["gamma_usd"]
        gamma_delta = curr_gamma - open_gamma   # change from open
        gamma_delta_usd = abs(gamma_delta)

        # Update peak
        if abs(curr_gamma) > abs(prev_peak):
            state["peak_nodes"][key] = curr_gamma

        # Check proximity to spot
        pct_from_spot = abs(strike - spot) / spot
        is_near_spot  = pct_from_spot <= NODE_PROXIMITY

        # Check for buildup multiplier
        if open_gamma != 0:
            buildup_mult = abs(curr_gamma) / abs(open_gamma)
        else:
            buildup_mult = 1.0

        node_summaries.append({
            "strike":       strike,
            "gamma_usd":    round(curr_gamma, 0),
            "delta_usd":    round(gamma_delta, 0),
            "buildup_mult": round(buildup_mult, 2),
            "near_spot":    is_near_spot,
            "pct_from_spot": round(pct_from_spot * 100, 2),
        })

        # Alert conditions
        dedup_key = f"{strike}_{now.strftime('%H')}"  # once per hour per strike
        already_alerted = dedup_key in state["alerts_sent"]

        should_alert = (
            not already_alerted and
            gamma_delta_usd >= MIN_DELTA_USD and
            (buildup_mult >= GAMMA_SPIKE_MULT or (is_near_spot and gamma_delta_usd >= MIN_DELTA_USD))
        )

        if should_alert:
            state["alerts_sent"].append(dedup_key)
            alerts_fired += 1

            direction  = "ADDING" if gamma_delta > 0 else "UNWINDING"
            is_extreme = buildup_mult >= 5.0 or gamma_delta_usd >= 1_000_000
            delta_m    = gamma_delta_usd / 1_000_000

            log.info(f"  🔥 NODE ALERT: ${strike} {direction} +${delta_m:.1f}M gamma | "
                     f"{buildup_mult:.1f}x from open | near_spot={is_near_spot}")

            # Scale SPY strike to SPX equivalent for display
            spx_strike = round(strike * 10.04)
            spx_spot   = round(spot * 10.04)
            is_above   = strike > spot
            suggestion = "BUY CALLS" if (direction == "ADDING" and is_above) or (direction == "FADING" and not is_above) else "BUY PUTS"
            sug_col    = 0x00D084 if "CALLS" in suggestion else 0xFF2D55
            sug_emoji  = "🟢" if "CALLS" in suggestion else "🔴"

            embed = {
                "color":  0xFF4444 if is_extreme else 0xFF8C00,
                "author": {"name": f"⚡ GAMMA NODE {'🔥 LIVE' if is_near_spot else 'BUILDING'} — SPX ${spx_strike:,}"},
                "description": (
                    f"**{direction}** at the **${spx_strike:,}** node (SPY ${strike:.0f})\n"
                    f"Price {'is testing this level' if is_near_spot else f'is {pct_from_spot*100:.1f}% away'}"
                ),
                "fields": [
                    {"name": "📊 Current Gamma",   "value": f"${abs(curr_gamma)/1e6:.2f}M",    "inline": True},
                    {"name": "📈 Delta from Open", "value": f"+${gamma_delta_usd/1e6:.2f}M",   "inline": True},
                    {"name": "🔥 Buildup",         "value": f"{buildup_mult:.1f}x from open",  "inline": True},
                    {"name": "💲 SPX Spot",        "value": f"${spx_spot:,} (SPY ${spot:.2f})", "inline": True},
                    {"name": "📍 Strike Distance", "value": f"{pct_from_spot*100:.2f}% away",  "inline": True},
                    {"name": "⏰ Time",             "value": now.strftime("%I:%M %p ET"),       "inline": True},
                    {"name": f"{sug_emoji} ARKA Suggests",
                     "value": f"**{suggestion}** — gamma node {direction.lower()} at ${spx_strike:,}\n"
                              f"{'Strike is a resistance level — puts if rejected' if is_above else 'Strike is a support level — calls if it holds'}",
                     "inline": False},
                    {"name": "💡 Implication",
                     "value": (
                         f"SPX ${spx_strike:,} gamma {'spike' if buildup_mult>=3 else 'buildup'}. "
                         f"{'MM MUST hedge — expect amplified move!' if is_near_spot else 'Node building — watch for price approach.'}"
                     ),
                     "inline": False},
                ],
                "footer": {"text": f"ARKA Gamma Node Tracker • {now.strftime('%I:%M %p ET')}"}
            }

            content = f"🔥 **GAMMA NODE LIVE** — SPX ${spx_strike:,} | +${gamma_delta_usd/1e6:.1f}M gamma | {suggestion}" if is_near_spot else ""
            webhook = DISCORD_GAMMA_FLIP or DISCORD_EXTREME if is_extreme else DISCORD_GAMMA_FLIP or DISCORD_ALERTS
            post_discord(webhook, embed, content)

    # Save top nodes summary
    state["top_nodes"] = sorted(node_summaries, key=lambda x: abs(x["gamma_usd"]), reverse=True)[:10]

    # Log top 5 nodes
    for n in state["top_nodes"][:5]:
        proximity = f"⚡ NEAR SPOT" if n["near_spot"] else f"{n['pct_from_spot']:.1f}% away"
        log.info(f"    ${n['strike']:.0f}: ${n['gamma_usd']/1e6:.2f}M ({n['buildup_mult']}x) {proximity}")

    save_state(state)
    log.info(f"  Scan complete — {alerts_fired} alert(s) fired")


# ── API endpoint helper (for dashboard) ───────────────────────────────────────
def get_gamma_nodes_for_api() -> dict:
    """Return current gamma node state for dashboard API."""
    try:
        if STATE_FILE.exists():
            s = json.loads(STATE_FILE.read_text())
            if s.get("date") == date.today().isoformat():
                return {
                    "nodes":      s.get("top_nodes", []),
                    "scans":      s.get("scans", 0),
                    "last_scan":  s.get("last_scan", ""),
                    "alerts":     len(s.get("alerts_sent", [])),
                    "date":       s.get("date"),
                }
    except Exception:
        pass
    return {"nodes": [], "scans": 0, "last_scan": "", "alerts": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="Run every 60 seconds")
    parser.add_argument("--reset", action="store_true", help="Reset today's baseline")
    args = parser.parse_args()

    if args.reset:
        STATE_FILE.unlink(missing_ok=True)
        print("Gamma node baseline reset")
        sys.exit(0)

    if args.watch:
        log.info("Gamma Node Tracker starting — watching every 60s")
        while True:
            try:
                run_scan()
            except Exception as e:
                log.error(f"Scan error: {e}")
            time.sleep(60)
    else:
        run_scan()
