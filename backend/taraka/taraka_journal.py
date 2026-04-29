"""
taraka_journal.py — TARAKA Daily Journal + ARJUN Training Pipeline
Runs at 4pm ET every day.

1. Fetches outcomes for all today's TARAKA alerts
2. Updates analyst win rates via AnalystTracker
3. Writes WIN/LOSS outcomes into ARJUN's performance DB for retraining
4. Posts EOD summary to Discord #taraka channel

Crontab (3:02pm CST = 4:02pm ET):
  2 15 * * 1-5 cd ~/trading-ai && venv/bin/python3 backend/taraka/taraka_journal.py >> logs/taraka/journal.log 2>&1
"""

import json
import logging
import os
import sys
import sqlite3
import requests
from datetime import datetime, date
from pathlib import Path

import pytz
from dotenv import load_dotenv

# ── Path setup ─────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).parent))
load_dotenv(BASE / ".env", override=True)

log = logging.getLogger("taraka.journal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

# ── Config ─────────────────────────────────────────────────────────────
LOG_DIR         = BASE / "logs" / "taraka"
DB_PATH         = BASE / "logs" / "arjun_performance.db"
ET              = pytz.timezone("America/New_York")
ALPACA_KEY      = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET   = os.getenv("ALPACA_API_SECRET", "")
POLYGON_KEY     = os.getenv("POLYGON_API_KEY", "")
DISCORD_TARAKA  = os.getenv("DISCORD_TARAKA_WEBHOOK", "")
DISCORD_ALERTS  = os.getenv("DISCORD_WEBHOOK_URL", "")

ALPACA_HEADERS  = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}


# ══════════════════════════════════════════════════════════════════════
# 1. PRICE FETCHERS
# ══════════════════════════════════════════════════════════════════════

def get_close_price(ticker: str) -> float | None:
    """Get today's closing price from Polygon snapshot."""
    try:
        import httpx
        r = httpx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_KEY},
            timeout=10
        )
        data = r.json().get("ticker", {})
        # Try prevDay close or day close
        close = data.get("day", {}).get("c") or data.get("prevDay", {}).get("c")
        return float(close) if close else None
    except Exception as e:
        log.warning(f"Close price fetch failed {ticker}: {e}")
    return None


def get_option_exit_price(contract: str) -> float | None:
    """Get final option price from Polygon."""
    try:
        import httpx
        r = httpx.get(
            f"https://api.polygon.io/v3/snapshot/options/{contract.split(':')[-1].split('2')[0]}/{contract}",
            params={"apiKey": POLYGON_KEY},
            timeout=8
        )
        result = r.json().get("results", {})
        mark = result.get("day", {}).get("close") or result.get("day", {}).get("vwap")
        return float(mark) if mark else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
# 2. OUTCOME RESOLVER
# ══════════════════════════════════════════════════════════════════════

def resolve_outcome(alert: dict) -> dict:
    """
    Determine WIN/LOSS for an alert based on price movement.
    Returns outcome dict with won, pnl, close_price fields.
    """
    parsed    = alert.get("parsed", {})
    ticker    = parsed.get("ticker", "")
    direction = parsed.get("direction", "").upper()
    entry     = parsed.get("entry") or parsed.get("price")
    target    = parsed.get("target")
    stop      = parsed.get("stop")

    if not ticker or not direction or not entry:
        return {"won": None, "pnl": 0, "reason": "insufficient_data"}

    close = get_close_price(ticker)
    if not close:
        return {"won": None, "pnl": 0, "reason": "price_unavailable", "ticker": ticker}

    entry = float(entry)
    pnl   = 0
    won   = None

    if direction == "CALL":
        won = close > entry
        if target and stop:
            # Use R-multiple for PnL estimate
            risk   = entry - float(stop)
            reward = float(target) - entry
            if won:
                pnl = reward * 100  # 1 contract = 100 shares equivalent
            else:
                pnl = -risk * 100
        else:
            pnl = (close - entry) * 100

    elif direction == "PUT":
        won = close < entry
        if target and stop:
            risk   = float(stop) - entry
            reward = entry - float(target)
            if won:
                pnl = reward * 100
            else:
                pnl = -risk * 100
        else:
            pnl = (entry - close) * 100

    return {
        "won":         won,
        "pnl":         round(pnl, 2),
        "close_price": close,
        "entry":       entry,
        "direction":   direction,
        "ticker":      ticker,
        "reason":      "price_comparison",
    }


# ══════════════════════════════════════════════════════════════════════
# 3. ARJUN TRAINING DB WRITER
# ══════════════════════════════════════════════════════════════════════

def write_to_arjun_db(alert: dict, outcome: dict):
    """
    Write TARAKA alert outcome into ARJUN's signals table for retraining.
    Maps TARAKA alert fields to ARJUN's signal schema.
    """
    if not DB_PATH.exists():
        log.warning(f"ARJUN DB not found at {DB_PATH}")
        return False

    parsed  = alert.get("parsed", {})
    ticker  = parsed.get("ticker", "")
    score   = alert.get("score", 50)
    analyst = alert.get("analyst", "unknown")

    if not ticker or outcome.get("won") is None:
        return False  # skip unknowns

    won_str = "WIN" if outcome["won"] else "LOSS"

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            INSERT INTO signals (
                date, ticker, signal, confidence,
                entry_price, target_price, stop_price, exit_price,
                pnl, outcome,
                analyst_bias, analyst_score,
                bull_score, bear_score,
                risk_decision, gex_regime,
                agent_json, created_at, indicators_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date.today().isoformat(),
            ticker,
            f"TARAKA_{parsed.get('direction','').upper()}",   # signal type
            score,                                             # TARAKA score as confidence
            parsed.get("entry") or parsed.get("price", 0),
            parsed.get("target", 0),
            parsed.get("stop", 0),
            outcome.get("close_price", 0),
            outcome.get("pnl", 0),
            won_str,
            parsed.get("direction", "NEUTRAL"),               # analyst_bias
            score,                                             # analyst_score
            score if parsed.get("direction") == "CALL" else 100 - score,  # bull_score
            score if parsed.get("direction") == "PUT"  else 100 - score,  # bear_score
            "TARAKA_APPROVED" if score >= 65 else "TARAKA_MONITOR",
            "TARAKA",                                          # gex_regime placeholder
            json.dumps({                                       # agent_json
                "source":   "taraka",
                "analyst":  analyst,
                "channel":  alert.get("channel", ""),
                "score":    score,
                "raw":      (alert.get("raw", ""))[:200],
            }),
            datetime.now().isoformat(),
            json.dumps({                                       # indicators_json
                "taraka_score":    score,
                "analyst":         analyst,
                "direction":       parsed.get("direction", ""),
                "channel":         alert.get("channel", ""),
                "dark_pool_conviction": 0,
                "news_score":      0,
            }),
        ))
        conn.commit()
        conn.close()
        log.info(f"  ✅ ARJUN DB: {ticker} {won_str} score={score} analyst={analyst}")
        return True
    except Exception as e:
        log.error(f"  ❌ ARJUN DB write failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════
# 4. ANALYST TRACKER
# ══════════════════════════════════════════════════════════════════════

def update_analyst_tracker(alert: dict, outcome: dict):
    """Update analyst win/loss record."""
    try:
        from backend.taraka.analyst_tracker import AnalystTracker
        tracker = AnalystTracker()
        tracker.record_outcome(
            alert.get("analyst", "unknown"),
            alert.get("id", ""),
            outcome.get("won", False),
            outcome.get("pnl", 0),
        )
    except Exception as e:
        log.warning(f"AnalystTracker update failed: {e}")


# ══════════════════════════════════════════════════════════════════════
# 5. DISCORD EOD SUMMARY
# ══════════════════════════════════════════════════════════════════════

def post_eod_summary(today: str, stats: dict, leaderboard: list):
    """Post EOD journal summary to #taraka Discord channel."""
    webhook = DISCORD_TARAKA or DISCORD_ALERTS
    if not webhook:
        return

    wins      = stats["wins"]
    losses    = stats["losses"]
    papers    = stats["papers"]
    win_rate  = stats["win_rate"]
    total_pnl = stats["total_pnl"]
    arjun_written = stats["arjun_written"]

    wr_color  = 0x00FF9D if win_rate >= 60 else 0xFFCC00 if win_rate >= 45 else 0xFF2D55
    pnl_sign  = "+" if total_pnl >= 0 else ""

    # Leaderboard text
    lb_lines = []
    for i, analyst in enumerate(leaderboard[:5], 1):
        medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i - 1]
        wr_a  = analyst.get("win_rate", 0)
        lb_lines.append(f"{medal} **{analyst['name']}** — {wr_a:.0f}% WR | {analyst.get('total_alerts', 0)} alerts")

    fields = [
        {"name": "📊 Today's Alerts",
         "value": f"**{wins+losses}** real | **{papers}** paper",
         "inline": True},
        {"name": "🎯 Win Rate",
         "value": f"**{win_rate:.1f}%** ({wins}W / {losses}L)",
         "inline": True},
        {"name": "💵 Est. P&L",
         "value": f"**{pnl_sign}${total_pnl:.2f}**",
         "inline": True},
        {"name": "🧠 ARJUN Training",
         "value": f"**{arjun_written}** outcomes written to training DB",
         "inline": False},
    ]

    if lb_lines:
        fields.append({
            "name":   "🏆 Analyst Leaderboard",
            "value":  "\n".join(lb_lines),
            "inline": False,
        })

    embed = {
        "title":       f"📓 TARAKA Journal — {today}",
        "color":       wr_color,
        "description": f"Daily signal review complete. ARJUN training data updated.",
        "fields":      fields,
        "footer":      {"text": "CHAKRA TARAKA • 4:00 PM EOD Journal"},
        "timestamp":   datetime.utcnow().isoformat() + "Z",
    }

    try:
        requests.post(webhook, json={"embeds": [embed]}, timeout=8)
        log.info("EOD summary posted to Discord")
    except Exception as e:
        log.warning(f"Discord post failed: {e}")


# ══════════════════════════════════════════════════════════════════════
# 6. MAIN
# ══════════════════════════════════════════════════════════════════════

def run_journal():
    today = datetime.now(ET).strftime("%Y-%m-%d")
    log.info(f"══ TARAKA Journal: {today} ══")

    alerts_file = LOG_DIR / f"alerts_{today}.json"
    if not alerts_file.exists():
        log.info("  No alert log for today")
        # Still post a "quiet day" summary
        post_eod_summary(today, {
            "wins": 0, "losses": 0, "papers": 0,
            "win_rate": 0, "total_pnl": 0, "arjun_written": 0
        }, [])
        return

    with open(alerts_file) as f:
        alerts = json.load(f)

    log.info(f"  Processing {len(alerts)} alerts...")

    wins = losses = papers = arjun_written = 0
    total_pnl = 0.0

    for alert in alerts:
        mode   = alert.get("mode", "PAPER")
        parsed = alert.get("parsed", {})

        # Skip unparseable alerts
        if not parsed or not parsed.get("ticker"):
            log.info(f"  ⏭  Skipping unparseable alert from @{alert.get('analyst','?')}")
            continue

        # Resolve outcome
        outcome = resolve_outcome(alert)
        alert["outcome"] = outcome

        won = outcome.get("won")
        pnl = outcome.get("pnl", 0)

        if mode == "PAPER":
            papers += 1
            status = "WON ✅" if won else "LOST ❌" if won is False else "UNKNOWN ❓"
            log.info(f"  PAPER @{alert.get('analyst','?')} "
                     f"{parsed.get('ticker','')} {parsed.get('direction','')} — {status}")
        else:
            if won is True:
                wins += 1
                total_pnl += pnl
            elif won is False:
                losses += 1
                total_pnl += pnl

            status = "WIN ✅" if won else "LOSS ❌" if won is False else "UNKNOWN ❓"
            log.info(f"  REAL  @{alert.get('analyst','?')} "
                     f"{parsed.get('ticker','')} {parsed.get('direction','')} — "
                     f"{status} P&L=${pnl:.2f}")

        # Write to ARJUN training DB
        if won is not None:
            written = write_to_arjun_db(alert, outcome)
            if written:
                arjun_written += 1

        # Update analyst tracker
        if won is not None:
            update_analyst_tracker(alert, outcome)

    # Save updated alerts with outcomes filled in
    with open(alerts_file, "w") as f:
        json.dump(alerts, f, indent=2)
    log.info(f"  Outcomes saved → {alerts_file}")

    # Load leaderboard
    leaderboard = []
    try:
        from backend.taraka.analyst_tracker import AnalystTracker
        tracker    = AnalystTracker()
        leaderboard = tracker.get_leaderboard()
    except Exception as e:
        log.warning(f"Leaderboard load failed: {e}")

    # Build stats
    total_real = wins + losses
    win_rate   = round(wins / total_real * 100, 1) if total_real > 0 else 0.0

    stats = {
        "date":          today,
        "alerts":        len(alerts),
        "real":          total_real,
        "paper":         papers,
        "wins":          wins,
        "losses":        losses,
        "win_rate":      win_rate,
        "total_pnl":     round(total_pnl, 2),
        "arjun_written": arjun_written,
        "leaderboard":   leaderboard,
    }

    # Save daily summary
    summary_file = LOG_DIR / f"summary_{today}.json"
    with open(summary_file, "w") as f:
        json.dump(stats, f, indent=2)

    # Print summary
    log.info(f"\n  ── EOD Summary ─────────────────")
    log.info(f"  Alerts today:    {len(alerts)}")
    log.info(f"  Real trades:     {total_real} ({wins}W / {losses}L)")
    log.info(f"  Paper trades:    {papers}")
    log.info(f"  Win rate:        {win_rate}%")
    log.info(f"  Est. P&L:        ${total_pnl:.2f}")
    log.info(f"  ARJUN training:  {arjun_written} records written")
    log.info(f"  Summary saved:   {summary_file}")

    # Post to Discord
    post_eod_summary(today, stats, leaderboard)
    log.info("  ✅ Journal complete")


if __name__ == "__main__":
    run_journal()
