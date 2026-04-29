"""
CHAKRA — ARJUN Self-Healer
backend/chakra/arjun_healer.py

Reads health monitor issues, uses Claude API to diagnose and propose fixes,
takes backups, posts to #app-health for confirmation, applies approved fixes.

Flow:
  1. health_monitor.py detects issue → writes to logs/chakra/health_issues.json
  2. arjun_healer.py reads issues → Claude API reasons about each one
  3. Takes backup of affected file
  4. Posts to #app-health with proposed fix + APPROVE/SKIP buttons (reply-based)
  5. Watches #app-health for your reply: "fix it" or "skip"
  6. Applies fix + restarts affected engine
  7. Posts confirmation

Usage:
  python3 backend/chakra/arjun_healer.py          # run once (called by health_monitor)
  python3 backend/chakra/arjun_healer.py --watch  # watch Discord for fix/skip replies
  python3 backend/chakra/arjun_healer.py --status # show pending fixes

Cron: add to health_monitor.py to call this after detecting issues.
"""

import os
import sys
import json
import shutil
import logging
import asyncio
import requests
import subprocess

from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# ── Setup ──────────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [HEALER] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('arjun_healer')

# ── Config ─────────────────────────────────────────────────────────────
HEALTH_WEBHOOK   = os.getenv("DISCORD_HEALTH_WEBHOOK", "")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
BACKUPS_DIR      = BASE / "logs" / "backups"
ISSUES_FILE      = BASE / "logs" / "chakra" / "health_issues.json"
PENDING_FILE     = BASE / "logs" / "chakra" / "pending_fixes.json"

# ── Engine restart commands ────────────────────────────────────────────
ENGINE_RESTART = {
    "ARKA":      "pkill -f arka_engine; sleep 1; nohup venv/bin/python3 backend/arka/arka_engine.py >> logs/arka/arka-$(date +%Y-%m-%d).log 2>&1 &",
    "ARJUN":     "pkill -f arjun_live_engine; sleep 1; nohup venv/bin/python3 backend/arjun/arjun_live_engine.py >> logs/arjun.log 2>&1 &",
    "TARAKA":    "pkill -f taraka_engine; sleep 1; nohup venv/bin/python3 backend/taraka/taraka_engine.py >> logs/taraka/taraka-$(date +%Y-%m-%d).log 2>&1 &",
    "Dashboard": "pkill -f uvicorn; sleep 1; nohup venv/bin/python3 -m uvicorn backend.dashboard_api:app --host 0.0.0.0 --port 8000 > logs/dashboard.log 2>&1 &",
    "Internals": "pkill -f market_internals; sleep 1; nohup venv/bin/python3 backend/arjun/market_internals.py >> logs/internals.log 2>&1 &",
}

# ── Engine file map ────────────────────────────────────────────────────
ENGINE_FILES = {
    "ARKA":      BASE / "backend" / "arka" / "arka_engine.py",
    "ARJUN":     BASE / "backend" / "arjun" / "arjun_live_engine.py",
    "TARAKA":    BASE / "backend" / "taraka" / "taraka_engine.py",
    "Dashboard": BASE / "backend" / "dashboard_api.py",
    "discord_notifier": BASE / "backend" / "arka" / "discord_notifier.py",
}


# ══════════════════════════════════════════════════════════════════════
# 1. BACKUP
# ══════════════════════════════════════════════════════════════════════

def take_backup(file_path: Path) -> Path | None:
    """Take a timestamped backup of a file before modifying it."""
    try:
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup   = BACKUPS_DIR / f"{file_path.stem}_{ts}{file_path.suffix}"
        shutil.copy2(file_path, backup)
        log.info(f"Backup created: {backup.name}")
        return backup
    except Exception as e:
        log.error(f"Backup failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
# 2. CLAUDE API — DIAGNOSE AND PROPOSE FIX
# ══════════════════════════════════════════════════════════════════════

def ask_claude_for_fix(issue: dict, file_content: str = "") -> dict:
    """
    Ask Claude to diagnose an issue and propose a specific fix.
    Returns structured fix proposal.
    """
    if not ANTHROPIC_KEY:
        log.error("ANTHROPIC_API_KEY not set — cannot call Claude")
        return {"can_fix": False, "reason": "No API key"}

    issue_text = f"""
Issue Title: {issue.get('title', '?')}
Issue Detail: {issue.get('detail', '?')}
Severity: {issue.get('severity', '?')}
Suggested Action: {issue.get('action', 'None provided')}
Auto-fix requested: {issue.get('auto_fix', False)}
"""

    file_section = ""
    if file_content:
        # Send first 4000 chars (imports + top-level code most relevant for NameError/ImportError)
        file_section = f"\n\nRelevant file content (first 4000 chars):\n```python\n{file_content[:4000]}\n```"

    prompt = f"""You are ARJUN, the self-healing AI for the CHAKRA trading system.
A health monitor has detected the following issue:

{issue_text}{file_section}

This is a PRODUCTION trading system. The issue is causing every scan cycle to fail.
Analyze the error and respond with ONLY a JSON object (no markdown, no explanation):
{{
  "can_fix": true/false,
  "confidence": 0-100,
  "diagnosis": "one sentence explaining root cause",
  "fix_type": "restart" | "code_patch" | "config_fix" | "env_fix" | "manual_only",
  "fix_description": "plain English description of exactly what will be done",
  "affected_engine": "ARKA" | "ARJUN" | "TARAKA" | "Dashboard" | "Internals" | "none",
  "needs_restart": true/false,
  "safe_to_auto": true/false,
  "patch": {{
    "file": "relative/path/to/file.py or null",
    "find": "exact string to find in the file (null if restart only)",
    "replace": "exact replacement string (null if restart only)"
  }},
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "estimated_time": "seconds to apply fix"
}}

Rules:
- can_fix=false if the fix requires human judgment or external access
- safe_to_auto=true for NameError, ImportError, AttributeError (missing import fixes are LOW risk)
- safe_to_auto=true for engine restart only fixes
- safe_to_auto=false for logic changes, threshold changes, or anything touching P&L
- fix_type=code_patch for NameError, ImportError, AttributeError, SyntaxError
- fix_type=restart for engine crashes / process not running
- For NameError "name 'X' is not defined": the fix is almost always adding "import X" near the top of the file
- patch.find and patch.replace must be EXACT strings that appear verbatim in the file
- If NameError: find the import block and add the missing import to patch.replace
"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 800,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30
        )
        if resp.status_code == 200:
            raw  = resp.json()["content"][0]["text"].strip()
            # Strip any accidental markdown
            raw  = raw.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            log.info(f"Claude diagnosis: {data.get('diagnosis', '?')} (confidence={data.get('confidence')}%)")
            return data
        else:
            log.error(f"Claude API error: {resp.status_code}")
            return {"can_fix": False, "reason": f"API error {resp.status_code}"}
    except json.JSONDecodeError as e:
        log.error(f"Claude response not valid JSON: {e}")
        return {"can_fix": False, "reason": "Invalid JSON from Claude"}
    except Exception as e:
        log.error(f"Claude API exception: {e}")
        return {"can_fix": False, "reason": str(e)}


# ══════════════════════════════════════════════════════════════════════
# 3. APPLY FIX
# ══════════════════════════════════════════════════════════════════════

def apply_fix(fix: dict, backup_path: Path | None) -> tuple[bool, str]:
    """Apply the proposed fix. Returns (success, message)."""
    fix_type = fix.get("fix_type", "manual_only")
    engine   = fix.get("affected_engine", "none")

    # ── Restart only ──────────────────────────────────────────────────
    if fix_type == "restart":
        if engine in ENGINE_RESTART:
            cmd = f"cd {BASE} && {ENGINE_RESTART[engine]}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0 or engine in ("ARKA", "TARAKA", "ARJUN"):
                return True, f"{engine} restarted successfully"
            return False, f"Restart failed: {result.stderr}"
        return False, f"Unknown engine: {engine}"

    # ── Code patch ────────────────────────────────────────────────────
    if fix_type == "code_patch":
        patch     = fix.get("patch", {})
        file_rel  = patch.get("file")
        find_str  = patch.get("find")
        repl_str  = patch.get("replace")

        if not file_rel or not find_str or repl_str is None:
            return False, "Incomplete patch spec from Claude"

        file_path = BASE / file_rel
        if not file_path.exists():
            return False, f"File not found: {file_rel}"

        with open(file_path) as f:
            content = f.read()

        if find_str not in content:
            return False, f"Pattern not found in {file_rel} — may already be fixed"

        content = content.replace(find_str, repl_str, 1)

        # Syntax check before writing
        try:
            import ast
            ast.parse(content)
        except SyntaxError as e:
            return False, f"Patch would introduce syntax error: {e}"

        with open(file_path, "w") as f:
            f.write(content)

        log.info(f"Code patch applied to {file_rel}")

        # Restart engine if needed
        if fix.get("needs_restart") and engine in ENGINE_RESTART:
            cmd = f"cd {BASE} && {ENGINE_RESTART[engine]}"
            subprocess.run(cmd, shell=True, capture_output=True)
            return True, f"Patched {file_rel} + restarted {engine}"

        return True, f"Patched {file_rel}"

    # ── Config/env fix ────────────────────────────────────────────────
    if fix_type in ("config_fix", "env_fix"):
        return False, "Config/env fixes require manual intervention — see action above"

    return False, "Fix type requires manual intervention"


# ══════════════════════════════════════════════════════════════════════
# 4. DISCORD NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════

def post_fix_proposal(issue: dict, fix: dict, backup_path: Path | None,
                       fix_id: str) -> bool:
    """Post fix proposal to #app-health with approve/skip instructions."""
    if not HEALTH_WEBHOOK:
        return False

    can_fix    = fix.get("can_fix", False)
    confidence = fix.get("confidence", 0)
    risk       = fix.get("risk_level", "UNKNOWN")
    fix_type   = fix.get("fix_type", "manual_only")

    risk_color = {"LOW": 0x00875A, "MEDIUM": 0xFF8B00, "HIGH": 0xDE350B}.get(risk, 0x6C757D)
    conf_emoji = "🟢" if confidence >= 70 else "🟡" if confidence >= 40 else "🔴"

    backup_text = f"`{backup_path.name}`" if backup_path else "No backup needed"

    if can_fix:
        action_text = (
            f"Reply **`fix it`** to approve\n"
            f"Reply **`skip`** to ignore\n\n"
            f"Fix ID: `{fix_id}`"
        )
        title = f"🔧 ARJUN Fix Proposal — {issue.get('title', '?')[:50]}"
    else:
        action_text = f"**Manual fix required:**\n{issue.get('action', 'See detail above')}"
        title = f"⚠️ ARJUN Cannot Auto-Fix — {issue.get('title', '?')[:50]}"

    fields = [
        {"name": f"{issue['severity']} Issue",
         "value": issue.get("detail", "?")[:300],
         "inline": False},
        {"name": f"{conf_emoji} Diagnosis",
         "value": fix.get("diagnosis", "Unknown")[:200],
         "inline": False},
        {"name": "🛠️ Proposed Fix",
         "value": fix.get("fix_description", "No fix available")[:300],
         "inline": False},
        {"name": "📋 Fix Type",
         "value": fix_type,
         "inline": True},
        {"name": "⚡ Risk Level",
         "value": risk,
         "inline": True},
        {"name": "🎯 Confidence",
         "value": f"{confidence}%",
         "inline": True},
        {"name": "💾 Backup",
         "value": backup_text,
         "inline": False},
        {"name": "✅ Action Required",
         "value": action_text,
         "inline": False},
    ]

    embed = {
        "title":     title,
        "color":     risk_color,
        "fields":    fields,
        "footer":    {"text": f"CHAKRA ARJUN Healer • {datetime.now().strftime('%H:%M ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    try:
        r = requests.post(HEALTH_WEBHOOK, json={"embeds": [embed]}, timeout=8)
        return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"Discord post error: {e}")
        return False


def post_fix_result(fix_id: str, issue_title: str, success: bool,
                     message: str, backup_path: Path | None):
    """Post fix result to #app-health."""
    if not HEALTH_WEBHOOK:
        return

    color  = 0x00875A if success else 0xDE350B
    emoji  = "✅" if success else "❌"
    backup = f"\n💾 Backup preserved at `{backup_path.name}`" if backup_path else ""

    try:
        requests.post(HEALTH_WEBHOOK, json={
            "embeds": [{
                "title":       f"{emoji} Fix {'Applied' if success else 'Failed'} — {issue_title[:50]}",
                "color":       color,
                "description": f"{message}{backup}",
                "footer":      {"text": f"CHAKRA ARJUN Healer • Fix ID: {fix_id}"},
                "timestamp":   datetime.utcnow().isoformat() + "Z",
            }]
        }, timeout=8)
    except Exception:
        pass


def post_fix_skipped(fix_id: str, issue_title: str):
    """Post skip confirmation to #app-health."""
    if not HEALTH_WEBHOOK:
        return
    try:
        requests.post(HEALTH_WEBHOOK, json={
            "content": f"⏭️ Fix skipped for: **{issue_title[:60]}** (ID: `{fix_id}`)"
        }, timeout=8)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# 5. PENDING FIX STATE
# ══════════════════════════════════════════════════════════════════════

def load_pending() -> list:
    try:
        with open(PENDING_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_pending(pending: list):
    try:
        PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PENDING_FILE, "w") as f:
            json.dump(pending, f, indent=2)
    except Exception as e:
        log.error(f"Could not save pending fixes: {e}")


def add_pending(fix_id: str, issue: dict, fix: dict, backup_path: Path | None):
    pending = load_pending()
    pending.append({
        "fix_id":      fix_id,
        "issue":       issue,
        "fix":         fix,
        "backup_path": str(backup_path) if backup_path else None,
        "proposed_at": datetime.now().isoformat(),
        "status":      "pending",
    })
    save_pending(pending)


def get_pending_by_id(fix_id: str) -> dict | None:
    for p in load_pending():
        if p["fix_id"] == fix_id and p["status"] == "pending":
            return p
    return None


def mark_pending(fix_id: str, status: str):
    pending = load_pending()
    for p in pending:
        if p["fix_id"] == fix_id:
            p["status"]     = status
            p["resolved_at"]= datetime.now().isoformat()
    save_pending(pending)


# ══════════════════════════════════════════════════════════════════════
# 6. DISCORD REPLY WATCHER
# ══════════════════════════════════════════════════════════════════════

async def watch_for_replies(timeout_minutes: int = 30):
    """
    Watch #app-health for 'fix it' or 'skip' replies using Discord bot.
    Runs for timeout_minutes then exits.
    """
    try:
        import discord
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env", override=True)
        BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
    except ImportError:
        log.error("discord.py not installed — cannot watch for replies")
        return

    if not BOT_TOKEN:
        log.error("DISCORD_BOT_TOKEN not set — cannot watch for replies")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    client  = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        log.info(f"Reply watcher connected as {client.user}")

    @client.event
    async def on_message(message):
        if message.author.bot:
            return

        content = message.content.strip().lower()

        # Check for fix approval
        if content.startswith("fix it") or content.startswith("approve"):
            # Extract fix_id if provided: "fix it FIX_ID"
            parts  = content.split()
            fix_id = parts[-1] if len(parts) > 1 else None

            # If no ID given, apply most recent pending fix
            if not fix_id or fix_id in ("it", "approve"):
                pending = [p for p in load_pending() if p["status"] == "pending"]
                if not pending:
                    await message.channel.send("⚠️ No pending fixes found.")
                    return
                pending_item = pending[-1]  # most recent
            else:
                pending_item = get_pending_by_id(fix_id)
                if not pending_item:
                    await message.channel.send(f"⚠️ Fix ID `{fix_id}` not found or already resolved.")
                    return

            # Apply the fix
            fix         = pending_item["fix"]
            issue       = pending_item["issue"]
            backup_path = Path(pending_item["backup_path"]) if pending_item.get("backup_path") else None
            fix_id_use  = pending_item["fix_id"]

            await message.channel.send(f"🔧 Applying fix `{fix_id_use}`...")
            success, msg = apply_fix(fix, backup_path)
            mark_pending(fix_id_use, "applied" if success else "failed")
            post_fix_result(fix_id_use, issue.get("title", "?"), success, msg, backup_path)

        elif content.startswith("skip"):
            parts  = content.split()
            fix_id = parts[-1] if len(parts) > 1 else None

            pending = [p for p in load_pending() if p["status"] == "pending"]
            if not pending:
                await message.channel.send("⚠️ No pending fixes to skip.")
                return

            pending_item = pending[-1] if not fix_id or fix_id == "skip" else get_pending_by_id(fix_id)
            if pending_item:
                mark_pending(pending_item["fix_id"], "skipped")
                post_fix_skipped(pending_item["fix_id"], pending_item["issue"].get("title", "?"))

        elif content == "pending fixes":
            pending = [p for p in load_pending() if p["status"] == "pending"]
            if not pending:
                await message.channel.send("✅ No pending fixes.")
            else:
                lines = [f"**Pending fixes ({len(pending)}):**"]
                for p in pending:
                    lines.append(f"• `{p['fix_id']}` — {p['issue'].get('title','?')[:60]}")
                await message.channel.send("\n".join(lines))

    # Auto-disconnect after timeout
    async def auto_disconnect():
        await asyncio.sleep(timeout_minutes * 60)
        log.info(f"Reply watcher timeout after {timeout_minutes} min — disconnecting")
        await client.close()

    asyncio.ensure_future(auto_disconnect())
    await client.start(BOT_TOKEN)


# ══════════════════════════════════════════════════════════════════════
# 7. MAIN HEALER LOOP
# ══════════════════════════════════════════════════════════════════════

def run_healer(issues: list[dict] | None = None):
    """
    Main healer function. Called by health_monitor after detecting issues.
    For each issue: diagnose → backup → propose → wait for confirmation.
    """
    if issues is None:
        # Load from health_issues.json if not passed directly
        try:
            with open(ISSUES_FILE) as f:
                issues = json.load(f)
        except Exception:
            log.info("No issues file found — nothing to heal")
            return

    if not issues:
        log.info("No issues to heal")
        return

    log.info(f"ARJUN Healer starting — {len(issues)} issue(s) to analyze")

    for issue in issues:
        fix_id = f"FIX_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{issue.get('key','?')[:10].upper()}"
        log.info(f"Analyzing: {issue.get('title', '?')}")

        # ── Load affected file for context ─────────────────────────────
        file_content = ""
        engine       = issue.get("engine") or _guess_engine(issue)
        if engine in ENGINE_FILES and ENGINE_FILES[engine].exists():
            try:
                with open(ENGINE_FILES[engine]) as f:
                    file_content = f.read()
            except Exception:
                pass

        # ── Ask Claude to diagnose ─────────────────────────────────────
        fix = ask_claude_for_fix(issue, file_content)

        # ── Take backup if we have a file to patch ─────────────────────
        backup_path = None
        patch_file  = fix.get("patch", {}).get("file")
        if patch_file and fix.get("can_fix") and fix.get("fix_type") == "code_patch":
            fp = BASE / patch_file
            if fp.exists():
                backup_path = take_backup(fp)

        # ── AUTO-FIX: apply immediately if issue flagged auto_fix=True
        #             AND Claude says safe_to_auto=True AND confidence >= 75 ──
        _auto_requested = issue.get("auto_fix", False)
        _claude_safe    = fix.get("safe_to_auto", False)
        _confidence     = fix.get("confidence", 0)
        _can_fix        = fix.get("can_fix", False)

        if _auto_requested and _claude_safe and _can_fix and _confidence >= 75:
            log.info(f"🤖 AUTO-FIX: {fix.get('diagnosis')} (confidence={_confidence}%)")
            success, msg = apply_fix(fix, backup_path)
            if success:
                log.info(f"✅ Auto-fix applied: {msg}")
                _post_auto_fix_result(issue, fix, success=True, msg=msg)
            else:
                log.error(f"❌ Auto-fix failed: {msg} — falling back to Discord proposal")
                _post_auto_fix_result(issue, fix, success=False, msg=msg)
                # Fall through to Discord proposal on failure
                post_fix_proposal(issue, fix, backup_path, fix_id)
                add_pending(fix_id, issue, fix, backup_path)
        else:
            # ── Post to Discord for approval ───────────────────────────
            if not _can_fix:
                log.info(f"Claude says cannot auto-fix: {fix.get('reason', fix.get('diagnosis', '?'))}")
            posted = post_fix_proposal(issue, fix, backup_path, fix_id)
            if posted:
                log.info(f"Fix proposal posted — ID: {fix_id}")
                add_pending(fix_id, issue, fix, backup_path)
            else:
                log.error("Could not post to Discord — check DISCORD_HEALTH_WEBHOOK")


def _post_auto_fix_result(issue: dict, fix: dict, success: bool, msg: str):
    """Post a Discord notification after an auto-fix is applied (no approval needed)."""
    if not HEALTH_WEBHOOK:
        return
    color  = 0x00D084 if success else 0xFF3D5A
    status = "✅ Auto-fixed" if success else "❌ Auto-fix failed"
    engine = fix.get("affected_engine", issue.get("engine", "ARKA"))
    payload = {
        "embeds": [{
            "color":  color,
            "author": {"name": f"🤖 ARJUN Self-Healer — {status}"},
            "fields": [
                {"name": "Issue",      "value": issue.get("title", "?"),        "inline": False},
                {"name": "Diagnosis",  "value": fix.get("diagnosis", "?"),       "inline": False},
                {"name": "Fix Applied","value": fix.get("fix_description", msg), "inline": False},
                {"name": "Result",     "value": msg,                             "inline": False},
            ],
            "footer": {"text": f"CHAKRA Auto-Healer • {datetime.now().strftime('%H:%M ET')} • Engine restarted: {fix.get('needs_restart',False)}"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    }
    try:
        requests.post(HEALTH_WEBHOOK, json=payload, timeout=5)
        log.info(f"Auto-fix result posted to Discord")
    except Exception as e:
        log.warning(f"Discord post failed: {e}")


def _guess_engine(issue: dict) -> str:
    """Guess which engine is affected from issue title/detail."""
    text = (issue.get("title", "") + " " + issue.get("detail", "")).lower()
    if "arka" in text:      return "ARKA"
    if "arjun" in text:     return "ARJUN"
    if "taraka" in text:    return "TARAKA"
    if "dashboard" in text: return "Dashboard"
    if "uvicorn" in text:   return "Dashboard"
    if "discord" in text:   return "discord_notifier"
    return "ARKA"  # default


# ══════════════════════════════════════════════════════════════════════
# 8. HEALTH MONITOR INTEGRATION
# ══════════════════════════════════════════════════════════════════════

def save_issues_for_healer(issues: list[dict]):
    """Called by health_monitor.py to pass issues to healer."""
    try:
        ISSUES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ISSUES_FILE, "w") as f:
            json.dump(issues, f, indent=2)
    except Exception as e:
        log.error(f"Could not save issues: {e}")


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ARJUN Self-Healer")
    parser.add_argument("--watch",  action="store_true", help="Watch Discord for fix/skip replies")
    parser.add_argument("--status", action="store_true", help="Show pending fixes")
    parser.add_argument("--test",   action="store_true", help="Test with a fake issue")
    args = parser.parse_args()

    if args.status:
        pending = [p for p in load_pending() if p["status"] == "pending"]
        if not pending:
            print("✅ No pending fixes")
        else:
            print(f"\n⏳ {len(pending)} pending fix(es):")
            for p in pending:
                print(f"  {p['fix_id']} — {p['issue'].get('title','?')}")
                print(f"    Proposed: {p['proposed_at']}")
        sys.exit(0)

    if args.watch:
        log.info("Starting Discord reply watcher (30 min timeout)...")
        asyncio.run(watch_for_replies(timeout_minutes=30))
        sys.exit(0)

    if args.test:
        test_issue = [{
            "key":      "test_arka_crash",
            "severity": "🔴",
            "title":    "ARKA engine is NOT running",
            "detail":   "Process arka_engine.py not found in ps output.",
            "action":   "Restart ARKA engine",
        }]
        run_healer(test_issue)
        sys.exit(0)

    # Default: run healer on current issues
    run_healer()
