"""
ARKA Self-Correction Engine
============================
Monitors ARKA's performance and automatically adjusts thresholds
in arka_config.json after every 5 trades or when triggers fire.

Triggers (all active):
  - Win rate < 50%       → tighten conviction (raise threshold)
  - No trades in 3 days  → loosen conviction (lower threshold)
  - Fakeout blocking >60% of signals → loosen fakeout threshold
  - Win rate > 75%       → can afford to loosen slightly

Run modes:
  - Called by arka_engine.py after each trade (automatic)
  - Run manually: python3 backend/arka/arka_self_correct.py
  - Run via cron: after market close daily

Author: CHAKRA system
"""

import json
import os
import glob
import logging
import shutil
from datetime import datetime, date, timedelta
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent.parent   # ~/trading-ai
CONFIG_FILE = BASE_DIR / "backend/arka/arka_config.json"
LOG_DIR     = BASE_DIR / "logs/arka"
CORRECT_LOG = LOG_DIR / "self_correct.log"

os.makedirs(LOG_DIR, exist_ok=True)

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(CORRECT_LOG)),
    ]
)
log = logging.getLogger("ARKA-CORRECT")


# ── Config I/O ────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    raise FileNotFoundError(f"arka_config.json not found at {CONFIG_FILE}")


def save_config(config: dict, reason: str, changes: dict):
    """Save config with full audit trail."""
    # Backup current config
    backup_dir = LOG_DIR / "config_backups"
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy(CONFIG_FILE, backup_dir / f"arka_config_{ts}.json")

    # Record this change in history
    config.setdefault("history", []).append({
        "timestamp":  datetime.now().isoformat(),
        "reason":     reason,
        "changes":    changes,
        "version":    config.get("version", "v2"),
    })

    # Keep only last 50 history entries
    config["history"] = config["history"][-50:]
    config["updated_at"] = datetime.now().isoformat()
    config["updated_by"] = "self_correct"

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    log.info(f"  💾 Config saved — reason: {reason}")
    log.info(f"     Changes: {changes}")


# ── Data collection ───────────────────────────────────────────────────────────

def load_recent_summaries(days: int = 14) -> list[dict]:
    """Load daily summary JSONs from the last N days."""
    summaries = []
    for i in range(days):
        d = (date.today() - timedelta(days=i)).isoformat()
        path = LOG_DIR / f"summary_{d}.json"
        if path.exists():
            try:
                with open(path) as f:
                    summaries.append(json.load(f))
            except Exception as e:
                log.warning(f"  Could not load {path}: {e}")
    return summaries


def collect_all_trades(summaries: list[dict]) -> list[dict]:
    """Extract all trades with outcomes from summaries."""
    trades = []
    for s in summaries:
        for t in s.get("trade_log", []):
            if t.get("pnl") is not None:   # only completed trades
                trades.append({
                    **t,
                    "date": s.get("date"),
                })
    return trades


def collect_scan_history(summaries: list[dict]) -> list[dict]:
    """Extract all scan records (for fakeout block rate analysis)."""
    scans = []
    for s in summaries:
        scans.extend(s.get("scan_history", []))
    return scans


def count_no_trade_days(summaries: list[dict]) -> int:
    """Count consecutive days with zero trades (most recent first)."""
    count = 0
    for s in sorted(summaries, key=lambda x: x.get("date",""), reverse=True):
        if s.get("trades", 0) == 0:
            count += 1
        else:
            break
    return count


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_performance(trades: list[dict], scans: list[dict]) -> dict:
    """Compute all performance metrics needed for self-correction decisions."""

    total = len(trades)
    if total == 0:
        return {
            "total_trades":    0,
            "win_rate":        None,
            "avg_pnl":         None,
            "wins":            0,
            "losses":          0,
            "fakeout_block_rate": None,
            "total_scans":     len(scans),
        }

    wins   = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / total
    avg_pnl  = sum(t.get("pnl", 0) for t in trades) / total

    # Fakeout block rate — what % of scans were blocked by fakeout filter
    total_scans   = len(scans)
    fakeout_blocks = len([s for s in scans if "FAKEOUT" in s.get("decision", "")])
    fakeout_block_rate = fakeout_blocks / total_scans if total_scans > 0 else 0

    return {
        "total_trades":       total,
        "wins":               len(wins),
        "losses":             len(losses),
        "win_rate":           round(win_rate, 3),
        "avg_pnl":            round(avg_pnl, 2),
        "fakeout_block_rate": round(fakeout_block_rate, 3),
        "total_scans":        total_scans,
        "fakeout_blocks":     fakeout_blocks,
    }


# ── Decision engine ───────────────────────────────────────────────────────────

def decide_adjustments(config: dict, metrics: dict, no_trade_days: int) -> list[dict]:
    """
    Returns a list of adjustments to make.
    Each adjustment: {param, old_value, new_value, reason}
    """
    adjustments = []
    sc   = config["self_correct"]
    thr  = config["thresholds"]

    conv_now    = thr["conviction_normal"]
    fakeout_now = thr["fakeout_block"]
    conv_step   = sc["conviction_step"]        # default 2
    fakeout_step = sc["fakeout_step"]          # default 0.05
    conv_min    = sc["conviction_min"]         # 40
    conv_max    = sc["conviction_max"]         # 70
    fakeout_min = sc["fakeout_min"]            # 0.35
    fakeout_max = sc["fakeout_max"]            # 0.75

    win_rate       = metrics.get("win_rate")
    fakeout_rate   = metrics.get("fakeout_block_rate")
    total_trades   = metrics.get("total_trades", 0)

    log.info(f"\n  📊 PERFORMANCE METRICS:")
    log.info(f"     Trades: {total_trades}  |  Win rate: {win_rate*100:.1f}% " if win_rate else f"     Trades: {total_trades}  |  Win rate: N/A")
    log.info(f"     Fakeout block rate: {fakeout_rate*100:.1f}%" if fakeout_rate else "     Fakeout block rate: N/A")
    log.info(f"     No-trade days (consecutive): {no_trade_days}")
    log.info(f"  ⚙️  CURRENT CONFIG: conviction={conv_now} | fakeout={fakeout_now}")

    # ── TRIGGER 1: No trades firing for 3+ days ──────────────────────────────
    if no_trade_days >= sc["no_trade_days_trigger"]:
        new_conv = max(conv_min, conv_now - conv_step * 2)   # bigger step for this trigger
        if new_conv != conv_now:
            adjustments.append({
                "param":     "conviction_normal",
                "old_value": conv_now,
                "new_value": new_conv,
                "reason":    f"No trades fired for {no_trade_days} consecutive days — lowering threshold",
                "trigger":   "no_trade_days",
            })
            # Also lower power hour proportionally
            ph_now = thr["conviction_power_hour"]
            new_ph = max(conv_min - 5, ph_now - conv_step * 2)
            adjustments.append({
                "param":     "conviction_power_hour",
                "old_value": ph_now,
                "new_value": new_ph,
                "reason":    f"Proportional power hour adjustment",
                "trigger":   "no_trade_days",
            })
            log.info(f"  🔽 TRIGGER: no_trade_days={no_trade_days} → lower conviction {conv_now}→{new_conv}")

    # ── TRIGGER 2: Win rate too low — tighten up ─────────────────────────────
    elif win_rate is not None and win_rate < sc["win_rate_low_trigger"] and total_trades >= 5:
        new_conv = min(conv_max, conv_now + conv_step)
        if new_conv != conv_now:
            adjustments.append({
                "param":     "conviction_normal",
                "old_value": conv_now,
                "new_value": new_conv,
                "reason":    f"Win rate {win_rate*100:.1f}% below {sc['win_rate_low_trigger']*100:.0f}% — raising threshold",
                "trigger":   "low_win_rate",
            })
            log.info(f"  🔼 TRIGGER: win_rate={win_rate*100:.1f}% → raise conviction {conv_now}→{new_conv}")

    # ── TRIGGER 3: Win rate very high — can loosen ───────────────────────────
    elif win_rate is not None and win_rate > sc["win_rate_high_trigger"] and total_trades >= 10:
        new_conv = max(conv_min, conv_now - conv_step)
        if new_conv != conv_now:
            adjustments.append({
                "param":     "conviction_normal",
                "old_value": conv_now,
                "new_value": new_conv,
                "reason":    f"Win rate {win_rate*100:.1f}% above {sc['win_rate_high_trigger']*100:.0f}% — can take more trades",
                "trigger":   "high_win_rate",
            })
            log.info(f"  🔽 TRIGGER: win_rate={win_rate*100:.1f}% → lower conviction {conv_now}→{new_conv}")

    # ── TRIGGER 4: Fakeout blocking too many signals ──────────────────────────
    if fakeout_rate is not None and fakeout_rate > sc["fakeout_block_rate_trigger"]:
        new_fakeout = min(fakeout_max, fakeout_now + fakeout_step)
        if new_fakeout != fakeout_now:
            adjustments.append({
                "param":     "fakeout_block",
                "old_value": fakeout_now,
                "new_value": round(new_fakeout, 3),
                "reason":    f"Fakeout blocking {fakeout_rate*100:.1f}% of signals (>{sc['fakeout_block_rate_trigger']*100:.0f}%) — loosening filter",
                "trigger":   "fakeout_over_blocking",
            })
            log.info(f"  🔽 TRIGGER: fakeout_rate={fakeout_rate*100:.1f}% → loosen fakeout {fakeout_now}→{new_fakeout:.3f}")

    if not adjustments:
        log.info("  ✅ No adjustments needed — performance within acceptable range")

    return adjustments


# ── Apply adjustments ─────────────────────────────────────────────────────────

def apply_adjustments(config: dict, adjustments: list[dict]) -> dict:
    """Apply a list of adjustments to the config and save."""
    if not adjustments:
        return config

    changes = {}
    reasons = []

    for adj in adjustments:
        param     = adj["param"]
        new_value = adj["new_value"]
        old_value = adj["old_value"]

        config["thresholds"][param] = new_value
        changes[param] = {"from": old_value, "to": new_value}
        reasons.append(adj["reason"])

        log.info(f"\n  ✅ ADJUSTING {param}: {old_value} → {new_value}")
        log.info(f"     Reason: {adj['reason']}")

    # Combine all reasons into one save
    reason_str = " | ".join(set(adj["trigger"] for adj in adjustments))
    save_config(config, reason_str, changes)

    # Print summary
    log.info(f"\n{'='*50}")
    log.info(f"  ARKA CONFIG UPDATED")
    log.info(f"  conviction_normal:     {config['thresholds']['conviction_normal']}")
    log.info(f"  conviction_power_hour: {config['thresholds']['conviction_power_hour']}")
    log.info(f"  fakeout_block:         {config['thresholds']['fakeout_block']}")
    log.info(f"{'='*50}\n")

    return config


# ── Check if adjustment is due ────────────────────────────────────────────────

def should_run_correction(summaries: list[dict], trades: list[dict], config: dict) -> tuple[bool, str]:
    """
    Decide if self-correction should run now.
    Returns (should_run, reason)
    """
    sc = config["self_correct"]

    if not sc.get("enabled", True):
        return False, "self-correction disabled in config"

    no_trade_days = count_no_trade_days(summaries)

    # Always run if no-trade-days trigger hit
    if no_trade_days >= sc["no_trade_days_trigger"]:
        return True, f"no trades for {no_trade_days} days"

    # Run after every N completed trades
    total = len(trades)
    min_trades = sc["min_trades_to_adjust"]
    check_interval = sc["check_interval_trades"]

    if total >= min_trades and total % check_interval == 0:
        return True, f"reached {total} trades (check every {check_interval})"

    return False, f"only {total} trades so far (need {min_trades} min, check every {check_interval})"


# ── Main ──────────────────────────────────────────────────────────────────────

def run_self_correction(force: bool = False) -> dict:
    """
    Main entry point. Called by arka_engine.py after each trade,
    or manually from command line.

    Returns the (possibly updated) config.
    """
    log.info(f"\n{'='*50}")
    log.info(f"  ARKA SELF-CORRECTION ENGINE")
    log.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"{'='*50}")

    config    = load_config()
    summaries = load_recent_summaries(days=14)
    trades    = collect_all_trades(summaries)
    scans     = collect_scan_history(summaries)
    no_trade_days = count_no_trade_days(summaries)

    log.info(f"  Loaded {len(summaries)} days of data | {len(trades)} trades | {len(scans)} scans")

    # Check if correction should run
    should_run, reason = should_run_correction(summaries, trades, config)

    if not should_run and not force:
        log.info(f"  ⏭  Skipping — {reason}")
        return config

    log.info(f"  🔍 Running correction — {reason}")

    # Analyze and decide
    metrics     = analyze_performance(trades, scans)
    adjustments = decide_adjustments(config, metrics, no_trade_days)

    # Apply
    config = apply_adjustments(config, adjustments)

    return config


# ══════════════════════════════════════════════════════════════════════════════
#  ALPACA CIRCUIT BREAKER
#  Detects repeated Alpaca connectivity failures, runs self-diagnosis,
#  and opens a circuit to skip options lookups until Alpaca recovers.
# ══════════════════════════════════════════════════════════════════════════════

import time as _time

_CIRCUIT_FILE = LOG_DIR / "alpaca_circuit.json"
_CIRCUIT_OPEN_SECONDS = 300      # block requests for 5 min after trip
_TRIP_THRESHOLD       = 3        # consecutive failures before opening circuit
_DIAG_TIMEOUT         = 6        # seconds per diagnostic probe


class AlpacaCircuitBreaker:
    """
    Tracks consecutive Alpaca failures and opens a circuit when the same
    error repeats. Runs live diagnostics to explain WHY it failed, then
    posts a Discord health alert. Auto-resets after CIRCUIT_OPEN_SECONDS.
    """

    def __init__(self):
        self._state = self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if _CIRCUIT_FILE.exists():
            try:
                return json.loads(_CIRCUIT_FILE.read_text())
            except Exception:
                pass
        return {
            "open":            False,
            "opened_at":       0.0,
            "consecutive":     0,
            "last_error_type": "",
            "last_error_msg":  "",
            "total_failures":  0,
            "total_recoveries": 0,
        }

    def _save(self):
        try:
            _CIRCUIT_FILE.write_text(json.dumps(self._state, indent=2))
        except Exception as e:
            log.warning(f"  circuit: save failed — {e}")

    # ── Public interface ─────────────────────────────────────────────────────

    def is_open(self) -> bool:
        """True = skip Alpaca call; circuit is tripped."""
        if not self._state["open"]:
            return False
        elapsed = _time.time() - self._state["opened_at"]
        if elapsed >= _CIRCUIT_OPEN_SECONDS:
            # Auto-reset: enough time has passed, try again
            log.info(f"  🔄 Alpaca circuit auto-reset after {elapsed:.0f}s — retrying")
            self._state["open"] = False
            self._state["consecutive"] = 0
            self._save()
            return False
        remaining = int(_CIRCUIT_OPEN_SECONDS - elapsed)
        log.warning(f"  ⚡ Alpaca circuit OPEN — skipping lookup ({remaining}s remaining)")
        return True

    def record_success(self, ticker: str = ""):
        """Call on successful Alpaca response to reset failure counter."""
        if self._state["consecutive"] > 0:
            self._state["consecutive"] = 0
            self._state["open"] = False
            self._state["total_recoveries"] += 1
            self._save()
            if ticker:
                log.info(f"  ✅ Alpaca circuit RESET after success ({ticker})")

    def record_failure(self, error_type: str, error_msg: str):
        """
        Call on each failed Alpaca attempt.
        When consecutive failures reach threshold, trips the circuit,
        runs diagnostics, and fires a Discord health alert.
        """
        self._state["consecutive"]     += 1
        self._state["total_failures"]  += 1
        self._state["last_error_type"]  = error_type
        self._state["last_error_msg"]   = str(error_msg)[:200]
        self._save()

        log.warning(
            f"  ⚡ Alpaca failure #{self._state['consecutive']}/{_TRIP_THRESHOLD} "
            f"— {error_type}: {str(error_msg)[:80]}"
        )

        if self._state["consecutive"] >= _TRIP_THRESHOLD and not self._state["open"]:
            self._trip(error_type, error_msg)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _trip(self, error_type: str, error_msg: str):
        """Open the circuit, run diagnostics, alert Discord."""
        self._state["open"]      = True
        self._state["opened_at"] = _time.time()
        self._save()

        log.error(
            f"  🚨 ALPACA CIRCUIT BREAKER TRIPPED — "
            f"{_TRIP_THRESHOLD} consecutive failures ({error_type})"
        )

        diag = self._run_diagnostics()
        self._send_discord_alert(error_type, error_msg, diag)

    def _run_diagnostics(self) -> dict:
        """
        Probe Alpaca endpoints to pinpoint the failure.
        Returns a dict describing what's up/down.
        """
        try:
            import httpx as _httpx
            from pathlib import Path as _Path
            from dotenv import load_dotenv as _lde
            _lde(_Path(__file__).resolve().parents[2] / ".env", override=True)
            key    = os.getenv("ALPACA_API_KEY", "")
            secret = os.getenv("ALPACA_API_SECRET", "")
            base   = "https://paper-api.alpaca.markets"
            headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        except Exception as e:
            return {"error": f"diag setup failed: {e}"}

        diag: dict = {
            "creds_present":   bool(key and secret),
            "account":         None,
            "options_endpoint": None,
            "internet":        None,
        }

        # 1 — Internet reachability (polygon, no auth needed)
        try:
            r = _httpx.get("https://api.polygon.io/v1/marketstatus/now", timeout=_DIAG_TIMEOUT)
            diag["internet"] = f"✅ reachable (HTTP {r.status_code})"
        except Exception as e:
            diag["internet"] = f"❌ {e}"

        if not key or not secret:
            diag["account"] = "❌ credentials missing"
            diag["options_endpoint"] = "❌ credentials missing"
            return diag

        # 2 — Account endpoint
        try:
            r = _httpx.get(f"{base}/v2/account", headers=headers, timeout=_DIAG_TIMEOUT)
            if r.status_code == 200:
                acc = r.json()
                diag["account"] = f"✅ HTTP 200 — equity=${float(acc.get('equity',0)):.0f}"
            else:
                diag["account"] = f"❌ HTTP {r.status_code}: {r.text[:80]}"
        except Exception as e:
            diag["account"] = f"❌ {e}"

        # 3 — Options contracts endpoint (bare call, no params)
        try:
            r = _httpx.get(
                f"{base}/v2/options/contracts",
                headers=headers,
                params={"underlying_symbols": "SPY", "limit": 1},
                timeout=_DIAG_TIMEOUT,
            )
            if r.status_code in (200, 201):
                diag["options_endpoint"] = f"✅ HTTP {r.status_code}"
            elif r.status_code == 403:
                diag["options_endpoint"] = "❌ HTTP 403 — options not enabled on this account"
            else:
                diag["options_endpoint"] = f"❌ HTTP {r.status_code}: {r.text[:80]}"
        except Exception as e:
            diag["options_endpoint"] = f"❌ {e}"

        log.info(f"  🔬 Alpaca diagnostics: {diag}")
        return diag

    def _send_discord_alert(self, error_type: str, error_msg: str, diag: dict):
        """Post a health embed to the #health channel."""
        try:
            from backend.chakra.discord_router import post_health
            from datetime import datetime, timezone
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

            # Build diagnosis summary lines
            lines = [
                f"**Internet:** {diag.get('internet', '?')}",
                f"**Account API:** {diag.get('account', '?')}",
                f"**Options API:** {diag.get('options_endpoint', '?')}",
                f"**Creds present:** {'✅' if diag.get('creds_present') else '❌'}",
            ]
            diag_block = "\n".join(lines)

            # Determine human-readable cause
            if "61" in str(error_msg) or "Connection refused" in str(error_msg):
                cause = "Alpaca paper-api refused the TCP connection (Errno 61). Likely a brief outage or local network block."
                fix   = "Circuit will auto-retry in 5 min. If persistent, check `paper-api.alpaca.markets` status."
            elif "401" in str(error_type) or "401" in str(error_msg):
                cause = "401 Unauthorized — API credentials rejected."
                fix   = "Verify ALPACA_API_KEY + ALPACA_API_SECRET in .env match paper account."
            elif "403" in str(error_type) or "403" in str(error_msg):
                cause = "403 Forbidden — options trading not enabled."
                fix   = "Enable options on the paper account at alpaca.markets."
            elif "Timeout" in str(error_type) or "timeout" in str(error_msg).lower():
                cause = "Request timed out — Alpaca API slow to respond."
                fix   = "Circuit will auto-retry in 5 min. May self-resolve."
            else:
                cause = f"{error_type}: {str(error_msg)[:120]}"
                fix   = "Circuit will auto-retry in 5 min."

            embed = {
                "title":       "🚨 ARKA — Alpaca Circuit Breaker Tripped",
                "description": (
                    f"**{_TRIP_THRESHOLD} consecutive options lookup failures** detected.\n"
                    f"Swing entries paused for **5 minutes** while auto-recovery runs.\n\n"
                    f"**Cause:** {cause}\n"
                    f"**Fix:** {fix}"
                ),
                "color":  0xE74C3C,
                "fields": [
                    {
                        "name":   "Live Diagnostics",
                        "value":  diag_block,
                        "inline": False,
                    },
                    {
                        "name":   "Error",
                        "value":  f"`{error_type}: {str(error_msg)[:100]}`",
                        "inline": False,
                    },
                ],
                "footer":    {"text": f"ARKA Self-Correct  •  {now_str}  •  Auto-resets in 5 min"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            ok = post_health({"embeds": [embed]})
            if ok:
                log.info("  📣 Circuit breaker alert posted to #health")
            else:
                log.warning("  ⚠️  Circuit breaker alert failed to post")
        except Exception as e:
            log.warning(f"  circuit: discord alert failed — {e}")


# Singleton — imported by arka_swings.py
alpaca_circuit = AlpacaCircuitBreaker()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    if force:
        log.info("  🔧 Force mode — running regardless of trade count")
    config = run_self_correction(force=force)
    print(f"\nFinal thresholds:")
    print(f"  conviction_normal:     {config['thresholds']['conviction_normal']}")
    print(f"  conviction_power_hour: {config['thresholds']['conviction_power_hour']}")
    print(f"  fakeout_block:         {config['thresholds']['fakeout_block']}")
