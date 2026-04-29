#!/bin/bash
# CHAKRA Watchdog — auto-restarts crashed engines every 60s
# Also deduplicates processes every 10 minutes.
# Start with: nohup bash scripts/watchdog.sh > logs/watchdog.log 2>&1 &

BASE="$HOME/trading-ai"
LOG="$BASE/logs/watchdog.log"
DEDUP_INTERVAL=600   # run process_guard every 10 min
LAST_DEDUP=0

echo "$(date): Watchdog started (PID=$$)" >> "$LOG"

_restart() {
    local name="$1"; local cmd="$2"; local log="$3"
    echo "$(date): $name crashed — restarting" >> "$LOG"
    cd "$BASE"
    eval "nohup $cmd > $log 2>&1 &"
    echo "$(date): $name restarted PID=$!" >> "$LOG"
}

_count() { pgrep -fc "$1" 2>/dev/null || echo 0; }

while true; do
    cd "$BASE"

    # ── Crash detection (every 60s) ──────────────────────────────────────────
    pgrep -f "arka_engine" > /dev/null || \
        _restart "ARKA Engine" \
            "venv/bin/python3 -m backend.arka.arka_engine" \
            "logs/arka/arka_engine.log"

    pgrep -f "flow_scalper" > /dev/null || \
        _restart "Flow Scalper" \
            "venv/bin/python3 -m backend.arka.flow_scalper" \
            "logs/arka/flow_scalper.log"

    pgrep -f "flow_monitor" > /dev/null || \
        _restart "Flow Monitor" \
            "venv/bin/python3 backend/chakra/flow_monitor.py --watch" \
            "logs/chakra/flow_monitor.log"

    pgrep -f "ws_stream_engine" > /dev/null || \
        _restart "WS Stream Engine" \
            "venv/bin/python3 backend/arka/ws_stream_engine.py" \
            "logs/arka/ws_stream.log"

    pgrep -f "uvicorn.*dashboard_api" > /dev/null || \
        _restart "Dashboard API" \
            "venv/bin/uvicorn backend.dashboard_api:app --host 0.0.0.0 --port 5001" \
            "logs/dashboard_api.log"

    # ── Duplicate detection (every 10 min) ──────────────────────────────────
    NOW=$(date +%s)
    if (( NOW - LAST_DEDUP >= DEDUP_INTERVAL )); then
        bash "$BASE/scripts/process_guard.sh" >> "$LOG" 2>&1
        LAST_DEDUP=$NOW
    fi

    sleep 60
done
