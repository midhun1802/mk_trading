#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# process_guard.sh — CHAKRA Process Guard
#
# Runs at 9:15 AM ET weekdays (via cron). Also safe to run manually any time.
#
# What it does:
#   1. For every CHAKRA service, count running instances.
#   2. If duplicates found → kill all older PIDs, keep only the newest.
#   3. Checks PID files for staleness and removes them.
#   4. Prints a status table and appends to logs/process_guard.log.
#
# Usage:
#   bash scripts/process_guard.sh           # detect + kill duplicates
#   bash scripts/process_guard.sh --report  # report only, no kills
# ─────────────────────────────────────────────────────────────────────────────

cd "$HOME/trading-ai"

REPORT_ONLY=false
[ "${1:-}" = "--report" ] && REPORT_ONLY=true

LOG="logs/process_guard.log"
mkdir -p "$(dirname "$LOG")"

TS="$(date '+%Y-%m-%d %H:%M:%S')"
KILLED=0

_log() { echo "$TS  $*" | tee -a "$LOG"; }

_log "══════════════════════════════════════════"
_log "CHAKRA Process Guard — started"
[ "$REPORT_ONLY" = "true" ] && _log "(report-only mode — no kills)"

# ─── Helper: get sorted PIDs for a pattern (oldest first) ────────────────────
_pids_for() {
    pgrep -f "$1" 2>/dev/null | sort -n | tr '\n' ' ' | sed 's/ $//'
}

_count_for() {
    pgrep -f "$1" 2>/dev/null | wc -l | tr -d ' '
}

# ─── Check a service ─────────────────────────────────────────────────────────
# Usage: _check "Display Name" "grep_pattern"
_check() {
    local name="$1"
    local pattern="$2"
    local pids
    local count
    local newest
    local old_pid

    pids=$(_pids_for "$pattern")
    count=$(_count_for "$pattern")

    if [ "$count" -eq 0 ]; then
        printf "%-24s  %-9s  %s\n" "$name" "STOPPED" "-"
        _log "WARN  $name — NOT RUNNING"
        return
    fi

    if [ "$count" -eq 1 ]; then
        printf "%-24s  %-9s  %s\n" "$name" "OK" "$pids"
        return
    fi

    # Duplicate — newest PID is the largest number
    newest=$(echo "$pids" | tr ' ' '\n' | sort -n | tail -1)

    if [ "$REPORT_ONLY" = "true" ]; then
        printf "%-24s  %-9s  keep=%s  would_kill=%s\n" \
            "$name" "DUPLICATE" "$newest" \
            "$(echo "$pids" | tr ' ' '\n' | sort -n | head -$(( count - 1 )) | tr '\n' ' ')"
        _log "DUPLICATE  $name — $count instances running (newest=$newest)"
        return
    fi

    # Kill every PID except the newest
    for old_pid in $(echo "$pids" | tr ' ' '\n' | sort -n | head -$(( count - 1 ))); do
        if kill "$old_pid" 2>/dev/null; then
            _log "KILLED  $name  PID=$old_pid  (kept PID=$newest)"
            KILLED=$(( KILLED + 1 ))
        else
            _log "WARN    $name  PID=$old_pid  — already gone"
        fi
    done
    printf "%-24s  %-9s  kept=%s  killed=%d\n" \
        "$name" "FIXED" "$newest" $(( count - 1 ))
}

# ─── Services ────────────────────────────────────────────────────────────────
printf "\n%-24s  %-9s  %s\n" "SERVICE" "STATUS" "PIDs"
printf "%-24s  %-9s  %s\n"  "-------" "------" "----"

# Use Python-only patterns to avoid matching caffeinate/sh wrappers
_check "ARKA Engine"       "Python.*arka_engine"
_check "Flow Scalper"      "Python.*flow_scalper"
_check "Flow Monitor"      "flow_monitor"
_check "WS Stream Engine"  "Python.*ws_stream_engine"
_check "Dashboard API"     "Python.*uvicorn"

# ─── Stale PID file check ─────────────────────────────────────────────────────
echo ""
_log "── PID file audit ──"
for pid_file in logs/arka/*.pid; do
    [ -f "$pid_file" ] || continue
    stored_pid="$(cat "$pid_file" 2>/dev/null || echo 0)"
    if [ -n "$stored_pid" ] && kill -0 "$stored_pid" 2>/dev/null; then
        _log "PID FILE  $pid_file → PID=$stored_pid  alive"
    else
        _log "PID FILE  $pid_file → PID=$stored_pid  STALE — removing"
        [ "$REPORT_ONLY" = "false" ] && rm -f "$pid_file"
    fi
done

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
if [ "$KILLED" -gt 0 ]; then
    _log "Killed $KILLED duplicate process(es)"
else
    _log "Done — no duplicates killed"
fi
echo ""
