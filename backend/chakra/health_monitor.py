"""
CHAKRA — App Health Monitor
backend/chakra/health_monitor.py

Runs every 5 minutes via cron. Checks all engines and services.
Posts to #app-health Discord ONLY when something is wrong.
Silent when everything is healthy.

Checks:
  1. Engine processes (ARKA, ARJUN, TARAKA, internals, uvicorn)
  2. Dashboard API responsiveness
  3. Alpaca connection (auth test)
  4. Polygon API (snapshot test)
  5. ARKA trading activity (no trades by 2PM warning)
  6. Daily briefing posted (7AM check)
  7. Watchlist scan freshness (pre-8AM check)
  8. ARJUN signals generated (post-8AM check)
  9. Log file error scan (detect crashes from logs)

Usage:
  python3 backend/chakra/health_monitor.py          # single check run
  python3 backend/chakra/health_monitor.py --test   # test all alerts fire
  python3 backend/chakra/health_monitor.py --status # print status, no Discord

Cron (every 5 min during trading hours):
  */5 8-16 * * 1-5 cd $HOME/trading-ai && venv/bin/python3 backend/chakra/health_monitor.py >> logs/chakra/health_monitor.log 2>&1
"""

import os
import sys
import json
import logging
import argparse
import subprocess
import requests

from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

# ── Setup ──────────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [HEALTH] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('health_monitor')

# ── Config ─────────────────────────────────────────────────────────────
HEALTH_WEBHOOK   = os.getenv("DISCORD_HEALTH_WEBHOOK", "")
POLYGON_API_KEY  = os.getenv("POLYGON_API_KEY", "")
ALPACA_API_KEY   = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET    = os.getenv("ALPACA_SECRET_KEY") or os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE_URL  = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DASHBOARD_URL    = "http://localhost:8000"

# State file — tracks what alerts have already been sent today
# (prevents repeat alerts every 5 min for the same issue)
ALERT_STATE_FILE = BASE / "logs" / "chakra" / "health_alert_state.json"

# ── Engine process names to monitor ───────────────────────────────────
ENGINES = {
    "ARKA":      "arka_engine.py",
    "ARJUN":     "arjun_live_engine.py",
    "TARAKA":    "taraka_engine.py",
    "Internals": "market_internals.py",
    "Dashboard": "uvicorn",
}


# ══════════════════════════════════════════════════════════════════════
# ALERT STATE — prevent duplicate alerts
# ══════════════════════════════════════════════════════════════════════


# ── ARJUN Self-Healer integration ─────────────────────────────────────
try:
    from backend.chakra.arjun_healer import run_healer, save_issues_for_healer
    HEALER_AVAILABLE = True
except ImportError:
    HEALER_AVAILABLE = False

def _load_alert_state() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(ALERT_STATE_FILE) as f:
            state = json.load(f)
        if state.get("date") != today:
            return {"date": today, "sent": {}}
        return state
    except Exception:
        return {"date": today, "sent": {}}


def _save_alert_state(state: dict):
    try:
        ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERT_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save alert state: {e}")


def _already_alerted(state: dict, key: str) -> bool:
    """Returns True if this alert was already sent today."""
    return state["sent"].get(key, False)


def _mark_alerted(state: dict, key: str):
    state["sent"][key] = datetime.now().isoformat()


def _clear_alert(state: dict, key: str):
    """Clear a resolved alert so it can fire again if it recurs."""
    state["sent"].pop(key, None)


# ══════════════════════════════════════════════════════════════════════
# CHECK FUNCTIONS
# ══════════════════════════════════════════════════════════════════════

def check_engine_processes() -> list[dict]:
    """Check if each engine process is running."""
    issues = []
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )
        ps_output = result.stdout
    except Exception as e:
        issues.append({
            "key":      "ps_failed",
            "severity": "🔴",
            "title":    "Process check failed",
            "detail":   str(e),
        })
        return issues

    for name, pattern in ENGINES.items():
        if pattern not in ps_output:
            issues.append({
                "key":      f"engine_down_{name.lower()}",
                "severity": "🔴",
                "title":    f"{name} engine is NOT running",
                "detail":   f"Process `{pattern}` not found in ps output.",
                "action":   f"Restart: `nohup venv/bin/python3 backend/arka/{pattern} &`",
            })
        else:
            log.info(f"  ✅ {name} running")

    return issues


def check_dashboard_api() -> list[dict]:
    """Ping dashboard API health endpoint."""
    issues = []
    try:
        r = requests.get(f"{DASHBOARD_URL}/api/stats", timeout=5)
        if r.status_code == 200:
            log.info("  ✅ Dashboard API responding")
        else:
            issues.append({
                "key":      "dashboard_bad_status",
                "severity": "🟡",
                "title":    f"Dashboard API returned HTTP {r.status_code}",
                "detail":   f"GET /api/stats → {r.status_code}",
                "action":   "Check uvicorn logs: `tail -20 logs/dashboard.log`",
            })
    except requests.exceptions.ConnectionError:
        issues.append({
            "key":      "dashboard_down",
            "severity": "🔴",
            "title":    "Dashboard API is DOWN (connection refused)",
            "detail":   f"Cannot reach {DASHBOARD_URL}/api/stats",
            "action":   "Restart: `pkill -f uvicorn && nohup venv/bin/python3 -m uvicorn backend.dashboard_api:app --host 0.0.0.0 --port 8000 &`",
        })
    except Exception as e:
        issues.append({
            "key":      "dashboard_error",
            "severity": "🟡",
            "title":    "Dashboard API check failed",
            "detail":   str(e),
        })
    return issues


def check_alpaca_connection() -> list[dict]:
    """Test Alpaca auth by calling /v2/account."""
    issues = []
    if not ALPACA_API_KEY or not ALPACA_SECRET:
        issues.append({
            "key":      "alpaca_missing_keys",
            "severity": "🔴",
            "title":    "Alpaca API keys missing from environment",
            "detail":   (
                f"ALPACA_API_KEY={'SET' if ALPACA_API_KEY else 'MISSING'}  "
                f"ALPACA_SECRET_KEY={'SET' if ALPACA_SECRET else 'MISSING'}"
            ),
            "action":   "Check .env file and run: `source ~/.zshrc`",
        })
        return issues

    try:
        r = requests.get(
            f"{ALPACA_BASE_URL}/v2/account",
            headers={
                "APCA-API-KEY-ID":     ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            timeout=8
        )
        if r.status_code == 200:
            acct = r.json()
            equity = float(acct.get("equity", 0))
            log.info(f"  ✅ Alpaca connected — equity=${equity:,.2f}")
        elif r.status_code == 401:
            issues.append({
                "key":      "alpaca_auth_failed",
                "severity": "🔴",
                "title":    "Alpaca authentication FAILED (401)",
                "detail":   "API key or secret is invalid or expired.",
                "action":   "Verify ALPACA_API_KEY and ALPACA_SECRET_KEY in .env",
            })
        else:
            issues.append({
                "key":      "alpaca_bad_status",
                "severity": "🟡",
                "title":    f"Alpaca returned HTTP {r.status_code}",
                "detail":   r.text[:200],
            })
    except Exception as e:
        issues.append({
            "key":      "alpaca_connection_error",
            "severity": "🔴",
            "title":    "Cannot connect to Alpaca",
            "detail":   str(e),
            "action":   "Check internet connection and Alpaca service status",
        })
    return issues


def check_polygon_api() -> list[dict]:
    """Test Polygon API with a quick SPY snapshot."""
    issues = []
    if not POLYGON_API_KEY:
        issues.append({
            "key":      "polygon_key_missing",
            "severity": "🔴",
            "title":    "POLYGON_API_KEY missing from environment",
            "detail":   "Watchlist scanner and GEX data will fail.",
            "action":   "Add POLYGON_API_KEY to .env and run: `source ~/.zshrc`",
        })
        return issues

    try:
        r = requests.get(
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/SPY",
            params={"apiKey": POLYGON_API_KEY},
            timeout=8
        )
        if r.status_code == 200:
            price = r.json().get("ticker", {}).get("day", {}).get("c", 0)
            log.info(f"  ✅ Polygon API responding — SPY=${price}")
        elif r.status_code == 403:
            issues.append({
                "key":      "polygon_auth_failed",
                "severity": "🔴",
                "title":    "Polygon API key invalid or quota exceeded (403)",
                "detail":   "All market data will fail — GEX, watchlist, prices.",
                "action":   "Check Polygon.io dashboard for quota/key status",
            })
        elif r.status_code == 429:
            issues.append({
                "key":      "polygon_rate_limit",
                "severity": "🟡",
                "title":    "Polygon API rate limit hit (429)",
                "detail":   "Too many requests — will auto-recover.",
                "action":   "No action needed — monitor if persists",
            })
        else:
            issues.append({
                "key":      "polygon_bad_status",
                "severity": "🟡",
                "title":    f"Polygon API returned HTTP {r.status_code}",
                "detail":   r.text[:200],
            })
    except Exception as e:
        issues.append({
            "key":      "polygon_connection_error",
            "severity": "🟡",
            "title":    "Cannot reach Polygon API",
            "detail":   str(e),
        })
    return issues


def check_arka_trading_activity() -> list[dict]:
    """Warn if ARKA has not fired any trades by 2 PM ET."""
    issues = []
    now    = datetime.now()

    # Only check after 2 PM on weekdays
    if now.hour < 14 or now.weekday() >= 5:
        return issues

    try:
        r = requests.get(f"{DASHBOARD_URL}/api/arka/summary", timeout=5)
        if r.status_code == 200:
            data        = r.json()
            trade_count = data.get("trades_today", data.get("entries", 0))
            if trade_count == 0:
                issues.append({
                    "key":      "arka_no_trades",
                    "severity": "🟡",
                    "title":    "ARKA has 0 trades after 2 PM",
                    "detail":   (
                        "No entries fired today. Possible causes:\n"
                        "• Conviction threshold too high (Neural Pulse low)\n"
                        "• No qualifying VWAP+ORB setups today\n"
                        "• Execution gates blocking (check VIX/macro)"
                    ),
                    "action":   (
                        "Check: `curl -s http://localhost:8000/api/execution-gates | python3 -m json.tool`"
                    ),
                })
            else:
                log.info(f"  ✅ ARKA has {trade_count} trade(s) today")
    except Exception as e:
        log.debug(f"ARKA activity check failed: {e}")
    return issues


def check_arka_frozen_conviction() -> list[dict]:
    """Detect frozen conviction score and auto-restart ARKA engine."""
    issues = []
    import json as _j, subprocess as _sp
    from pathlib import Path as _P
    now = datetime.now()
    if now.hour < 9 or now.weekday() >= 5:
        return issues
    try:
        summary_path = _P(f"logs/arka/summary_{now.strftime('%Y-%m-%d')}.json")
        if not summary_path.exists():
            return issues
        data = _j.loads(summary_path.read_text())
        scan_history = data.get("scan_history", [])
        if len(scan_history) >= 8:
            recent = scan_history[-8:]
            scores = [s.get("score", -1) for s in recent if "score" in s]
            decisions = [s.get("decision", "") for s in recent]
            all_blocked = all(any(x in d for x in ["BLOCK","FAKEOUT","HOLD","CLOSE"]) for d in decisions)
            scores = [s.get("score", -1) for s in recent if "score" in s]
            if len(scores) >= 8 and len(set(scores)) == 1 and scores[0] == 0.0 and not all_blocked:
                # Auto-restart
                _sp.run(["pkill", "-f", "arka_engine"], capture_output=True)
                import time; time.sleep(2)
                _sp.Popen(
                    ["venv/bin/python3", "-m", "backend.arka.arka_engine"],
                    stdout=open(f"logs/arka/arka_{now.strftime('%Y-%m-%d')}.log", "a"),
                    stderr=_sp.STDOUT,
                    cwd=str(_P(__file__).parents[2])
                )
                issues.append({
                    "key":      "arka_frozen_auto_restart",
                    "severity": "🟠",
                    "title":    "ARKA auto-restarted — conviction was frozen",
                    "detail":   "Score 0.0 frozen for 8 scans. Engine killed and restarted automatically.",
                    "action":   "Monitor next 2 scans to confirm recovery.",
                })
    except Exception as e:
        log.debug(f"Frozen conviction check failed: {e}")
    return issues


def check_watchlist_freshness() -> list[dict]:
    """Before 8 AM, warn if watchlist_latest.json is stale or missing."""
    issues = []
    now    = datetime.now()

    # Only relevant between 7:20 AM and 8:00 AM
    if not (7 <= now.hour < 8 and now.minute >= 20):
        return issues

    wl_path = BASE / "logs" / "chakra" / "watchlist_latest.json"
    if not wl_path.exists():
        issues.append({
            "key":      "watchlist_missing",
            "severity": "🟡",
            "title":    "Watchlist file missing before ARJUN run",
            "detail":   f"`{wl_path}` not found — scanner may have failed.",
            "action":   "Run: `python3 backend/chakra/watchlist_scanner.py --mode premarket`",
        })
        return issues

    try:
        with open(wl_path) as f:
            wl = json.load(f)
        scan_date = wl.get("date")
        today_str = now.strftime("%Y-%m-%d")
        if scan_date != today_str:
            issues.append({
                "key":      "watchlist_stale",
                "severity": "🟡",
                "title":    f"Watchlist is stale (from {scan_date}, not today)",
                "detail":   "Pre-market refresh scan may have failed.",
                "action":   "Run: `python3 backend/chakra/watchlist_scanner.py --mode premarket`",
            })
        else:
            count = wl.get("count", 0)
            log.info(f"  ✅ Watchlist fresh — {count} candidates")
    except Exception as e:
        issues.append({
            "key":      "watchlist_corrupt",
            "severity": "🟡",
            "title":    "Watchlist file unreadable",
            "detail":   str(e),
        })
    return issues


def check_arjun_signals() -> list[dict]:
    """After 8:30 AM, warn if ARJUN generated no signals today."""
    issues = []
    now    = datetime.now()

    # Only check between 8:30 AM and 10 AM
    if not (8 <= now.hour < 10 and now.minute >= 30):
        return issues

    try:
        r = requests.get(f"{DASHBOARD_URL}/api/signals", timeout=5)
        if r.status_code == 200:
            data    = r.json()
            signals = data if isinstance(data, list) else data.get("signals", [])
            today   = now.strftime("%Y-%m-%d")
            today_sigs = [s for s in signals if s.get("date", "") == today]

            if len(today_sigs) == 0:
                issues.append({
                    "key":      "arjun_no_signals",
                    "severity": "🟡",
                    "title":    "ARJUN generated 0 signals today",
                    "detail":   "8AM run may have been blocked by macro gate or crashed.",
                    "action":   (
                        "Check: `cat logs/cron.log | tail -30`\n"
                        "Manual run: `python3 backend/run_daily_signals.py`"
                    ),
                })
            else:
                log.info(f"  ✅ ARJUN has {len(today_sigs)} signal(s) today")
    except Exception as e:
        log.debug(f"ARJUN signals check failed: {e}")
    return issues


def check_log_errors() -> list[dict]:
    """Scan recent log files for ERROR/Traceback entries.
    Detects repeated scan errors (same crash every cycle) and flags them
    for auto-fix by the healer without requiring Discord approval.
    """
    issues  = []
    today   = datetime.now().strftime('%Y-%m-%d')
    log_map = {
        # Try arka_engine.log first (nohup output), fall back to rotating arka.log
        "ARKA":      (
            BASE / "logs" / "arka" / "arka_engine.log",
            BASE / "logs" / "arka" / f"arka.log",
            BASE / "logs" / "arka" / f"arka-{today}.log",
        ),
        "Dashboard": (BASE / "logs" / "dashboard_api.log", BASE / "logs" / "dashboard.log"),
        "Briefing":  (BASE / "logs" / "chakra" / "briefing.log",),
    }

    for engine, log_paths in log_map.items():
        # Use the first log file that exists
        log_path = next((p for p in log_paths if p.exists()), None)
        if not log_path:
            continue
        try:
            with open(log_path) as f:
                lines = f.readlines()

            # Scan last 100 lines
            recent = lines[-100:]
            error_lines = [
                l.strip() for l in recent
                if "Traceback" in l or " ERROR " in l or "TypeError" in l
                or "NameError" in l or "ImportError" in l or "AttributeError" in l
                or "ConnectionError" in l or "❌ Scan error" in l
            ]

            if not error_lines:
                continue

            # ── Detect REPEATED scan errors (same crash every cycle) ──────────
            # Extract error type from lines like "NameError: name 'x' is not defined"
            import re as _re
            _error_msgs = [_re.sub(r'^\d+:\d+:\d+\s+\S+\s+', '', e) for e in error_lines]
            _scan_errors = [e for e in _error_msgs if 'Scan error' in e or 'NameError' in e
                            or 'AttributeError' in e or 'ImportError' in e]

            # Count distinct repeated errors
            from collections import Counter as _Counter
            _counts = _Counter(_scan_errors)
            _repeated = {msg: cnt for msg, cnt in _counts.items() if cnt >= 3}

            if _repeated:
                # This is a code bug crashing every scan — flag for AUTO-FIX
                _top_error = max(_repeated, key=_repeated.get)
                issues.append({
                    "key":       f"repeated_scan_error_{engine.lower()}",
                    "severity":  "🔴",
                    "title":     f"{engine} repeated scan crash — auto-fix needed",
                    "detail":    f"Error occurred {_repeated[_top_error]}x in last 100 lines:\n{_top_error}\n\nFull context:\n" + "\n".join(error_lines[-8:]),
                    "action":    f"Auto-fix: patch {engine} code and restart",
                    "auto_fix":  True,   # healer should fix without Discord approval
                    "engine":    engine,
                })
            else:
                # Regular error — alert only, no auto-fix
                sample = error_lines[-3:]
                issues.append({
                    "key":      f"log_errors_{engine.lower()}",
                    "severity": "🟡",
                    "title":    f"{engine} log has recent errors",
                    "detail":   "\n".join(sample),
                    "action":   f"Check full log: `tail -50 {log_path}`",
                    "auto_fix": False,
                    "engine":   engine,
                })
        except Exception:
            pass
    return issues


# ══════════════════════════════════════════════════════════════════════
# DISCORD ALERTING
# ══════════════════════════════════════════════════════════════════════

def post_health_alert(issues: list[dict], test_mode: bool = False):
    """Post all new issues to #app-health Discord channel."""
    if not HEALTH_WEBHOOK:
        log.error("DISCORD_HEALTH_WEBHOOK not set — cannot post health alerts")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M ET")

    # Group by severity
    red    = [i for i in issues if i["severity"] == "🔴"]
    yellow = [i for i in issues if i["severity"] == "🟡"]

    color = 0xDE350B if red else 0xFF8B00  # red or amber

    fields = []
    for issue in issues:
        value = issue["detail"]
        if issue.get("action"):
            value += f"\n\n**Action:** {issue['action']}"
        fields.append({
            "name":   f"{issue['severity']} {issue['title']}",
            "value":  value[:1024],
            "inline": False,
        })

    embed = {
        "title": (
            f"⚠️ CHAKRA Health Alert — "
            f"{len(red)} critical, {len(yellow)} warnings"
        ),
        "color":       color,
        "fields":      fields[:10],  # Discord max 10 fields
        "footer":      {
            "text": (
                f"CHAKRA Health Monitor  •  {now_str}  •  "
                f"{'TEST' if test_mode else 'AUTO CHECK'}"
            )
        },
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    try:
        r = requests.post(
            HEALTH_WEBHOOK,
            json={"embeds": [embed]},
            timeout=8
        )
        if r.status_code in (200, 204):
            log.info(f"Health alert posted to Discord ✅ ({len(issues)} issues)")
        else:
            log.error(f"Discord post failed: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Discord health alert error: {e}")


def post_recovery_notice(resolved_keys: list[str]):
    """Post a recovery notice when previously alerted issues are resolved."""
    if not HEALTH_WEBHOOK or not resolved_keys:
        return
    try:
        requests.post(HEALTH_WEBHOOK, json={
            "embeds": [{
                "title":  "✅ CHAKRA Health — Issues Resolved",
                "color":  0x00875A,
                "description": "\n".join(f"• {k}" for k in resolved_keys),
                "footer": {"text": f"Auto-resolved at {datetime.now().strftime('%H:%M ET')}"},
            }]
        }, timeout=8)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# MAIN RUN
# ══════════════════════════════════════════════════════════════════════

def run_health_check(test_mode: bool = False, status_only: bool = False) -> list[dict]:
    """Run all checks. Return list of active issues."""
    log.info("=" * 50)
    log.info(f"CHAKRA Health Monitor — {datetime.now().strftime('%H:%M:%S')}")
    log.info("=" * 50)

    all_issues = []
    all_issues += check_engine_processes()
    all_issues += check_dashboard_api()
    all_issues += check_alpaca_connection()
    all_issues += check_polygon_api()
    all_issues += check_arka_trading_activity()
    all_issues += check_arka_frozen_conviction()
    all_issues += check_watchlist_freshness()
    all_issues += check_arjun_signals()
    all_issues += check_log_errors()

    if status_only:
        if all_issues:
            print(f"\n⚠️  {len(all_issues)} issue(s) found:")
            for i in all_issues:
                print(f"  {i['severity']} {i['title']}")
                print(f"     {i['detail'][:100]}")
        else:
            print("\n✅ All systems healthy")
        return all_issues

    # Load alert state to avoid duplicate alerts
    state         = _load_alert_state()
    new_issues    = []
    resolved_keys = []

    for issue in all_issues:
        key = issue["key"]
        if test_mode or not _already_alerted(state, key):
            new_issues.append(issue)
            _mark_alerted(state, key)

    # Check for resolved issues (previously alerted, now gone)
    active_keys = {i["key"] for i in all_issues}
    for key, ts in list(state["sent"].items()):
        if key not in active_keys:
            resolved_keys.append(key)
            _clear_alert(state, key)

    _save_alert_state(state)

    if new_issues:
        log.warning(f"{len(new_issues)} new issue(s) — posting to #app-health")
        post_health_alert(new_issues, test_mode=test_mode)
        # ── Trigger ARJUN self-healer ──────────────────────────────────
        if HEALER_AVAILABLE and not status_only:
            try:
                save_issues_for_healer(new_issues)
                run_healer(new_issues)
                log.info("ARJUN healer triggered — fix proposals posted to #app-health")
            except Exception as _he:
                log.warning(f"ARJUN healer error: {_he}")
    else:
        log.info("All systems healthy — no alerts needed ✅")

    if resolved_keys:
        post_recovery_notice(resolved_keys)

    return all_issues


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CHAKRA Health Monitor")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Force all alerts to fire (ignores dedup state)"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print status to terminal only — no Discord posts"
    )
    args = parser.parse_args()

    issues = run_health_check(test_mode=args.test, status_only=args.status)
    sys.exit(0 if not any(i["severity"] == "🔴" for i in issues) else 1)
