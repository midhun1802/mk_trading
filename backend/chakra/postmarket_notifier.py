#!/usr/bin/env python3
"""
postmarket_notifier.py — Post postmarket scan results to Discord
Cron: 0 17 * * 1-5  cd ~/trading-ai && source venv/bin/activate && python3 backend/chakra/postmarket_notifier.py >> logs/chakra/postmarket_notifier.log 2>&1
"""

import os
import json
import datetime
import requests
from pathlib import Path

BASE = Path.home() / "trading-ai"
WATCHLIST_FILE = BASE / "logs" / "chakra" / "watchlist_latest.json"
ENV_FILE = BASE / ".env"
TODAY = datetime.date.today().strftime("%Y-%m-%d")


def load_env():
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def post_to_discord(msg, webhook_url):
    resp = requests.post(webhook_url, json={"content": msg}, timeout=10)
    return resp.status_code in (200, 204)


def main():
    load_env()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        print("[ERROR] DISCORD_WEBHOOK_URL not set")
        return

    if not WATCHLIST_FILE.exists():
        print(f"[WARN] {WATCHLIST_FILE} not found — did watchlist_scanner.py run?")
        # Post a minimal notice
        msg = f"⚠️ **CHAKRA Post-Market Scan** — {TODAY}\n> No watchlist cache found. Check watchlist_scanner.py --mode postmarket."
        post_to_discord(msg, webhook)
        return

    with open(WATCHLIST_FILE) as f:
        data = json.load(f)

    scan_time = data.get("scan_time", "unknown")
    mode = data.get("mode", "postmarket")
    candidates = data.get("candidates", data.get("postmarket_candidates", []))
    alerts = data.get("alerts", [])
    divergences = data.get("rsi_divergences", [])
    ema_setups = data.get("ema_stack_setups", [])

    lines = []
    lines.append(f"## 🌇 CHAKRA Post-Market Scan — {TODAY}")
    lines.append(f"*Scan completed: {scan_time}*")
    lines.append("")

    if not candidates and not alerts and not divergences:
        lines.append("> 🔇 No high-conviction setups found in post-market scan.")
        lines.append("> Market closed with no swing candidates meeting threshold.")
    else:
        # RSI divergences
        if divergences:
            lines.append(f"### 📐 RSI Divergences ({len(divergences)} found)")
            for d in divergences[:5]:
                sym = d.get("symbol", "?")
                div_type = d.get("type", "?")
                rsi = d.get("rsi", "?")
                lines.append(f"  • **{sym}** — {div_type} divergence | RSI: {rsi}")
            lines.append("")

        # EMA stack setups
        if ema_setups:
            lines.append(f"### 📈 EMA Stack Setups ({len(ema_setups)} found)")
            for e in ema_setups[:5]:
                sym = e.get("symbol", "?")
                direction = e.get("direction", "?")
                lines.append(f"  • **{sym}** — {direction} EMA alignment")
            lines.append("")

        # General candidates
        if candidates:
            lines.append(f"### 👀 Swing Watchlist for Tomorrow ({len(candidates)} setups)")
            for c in candidates[:8]:
                if isinstance(c, dict):
                    sym = c.get("symbol", "?")
                    reason = c.get("reason", "")
                    score = c.get("score", "")
                    score_str = f" | Score: {score}" if score else ""
                    lines.append(f"  • **{sym}** — {reason}{score_str}")
                else:
                    lines.append(f"  • **{c}**")
            lines.append("")

        # Alerts
        if alerts:
            lines.append(f"### 🔔 Alerts ({len(alerts)})")
            for a in alerts[:5]:
                lines.append(f"  > {a}")
            lines.append("")

    lines.append("─────────────────────────────")
    lines.append("*CHAKRA Watchlist Scanner · Post-Market Report*")

    msg = "\n".join(lines)
    
    ok = post_to_discord(msg, webhook)
    if ok:
        print(f"[OK] Post-market scan posted to Discord ({len(msg)} chars)")
    else:
        print("[ERROR] Discord post failed")


if __name__ == "__main__":
    main()
