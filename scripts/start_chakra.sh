#!/bin/bash
# CHAKRA Morning Startup Script
# Run at 8:25 AM ET before market open
# Usage: bash scripts/start_chakra.sh

set -e
BASE="$HOME/trading-ai"
cd "$BASE"

echo "🚀 Starting CHAKRA $(date)"

# Create required log directories
mkdir -p logs/gex logs/arka logs/chakra logs/signals logs/lotto \
          logs/notifications logs/internals logs/options logs/swings \
          logs/premarket logs/taraka logs/trades

# Kill any stale processes
echo "Stopping old processes..."
pkill -f "uvicorn.*dashboard_api" 2>/dev/null && sleep 1 || true
pkill -f "arka_engine"            2>/dev/null && sleep 1 || true
pkill -f "flow_scalper"           2>/dev/null && sleep 1 || true
pkill -f "flow_monitor"           2>/dev/null && sleep 1 || true
pkill -f "ws_stream_engine"       2>/dev/null && sleep 1 || true
pkill -f "watchdog.sh"            2>/dev/null             || true
sleep 2

# Clean up stale PID files
rm -f logs/arka/*.pid

# ── Start Dashboard API ───────────────────────────────────────
echo "Starting Dashboard API..."
nohup venv/bin/uvicorn backend.dashboard_api:app \
    --host 0.0.0.0 --port 5001 \
    > logs/dashboard_api.log 2>&1 &
API_PID=$!
echo "  API PID=$API_PID"

# Wait for API to be ready
sleep 4
if curl -sf http://localhost:5001/api/health > /dev/null 2>&1; then
    echo "  ✅ API healthy"
else
    echo "  ⚠️  API not responding yet (may still be starting)"
fi

# ── Start ARKA Engine ─────────────────────────────────────────
echo "Starting ARKA Engine..."
nohup venv/bin/python3 -m backend.arka.arka_engine \
    > logs/arka/arka_engine.log 2>&1 &
echo "  ARKA PID=$!"

# ── Start Flow Scalper ────────────────────────────────────────
echo "Starting Flow Scalper..."
nohup venv/bin/python3 -m backend.arka.flow_scalper \
    > logs/arka/flow_scalper.log 2>&1 &
echo "  Flow Scalper PID=$!"

# ── Start Flow Monitor ────────────────────────────────────────
echo "Starting Flow Monitor..."
nohup venv/bin/python3 backend/chakra/flow_monitor.py --watch \
    > logs/chakra/flow_monitor.log 2>&1 &
echo "  Flow Monitor PID=$!"

# ── Start WS Stream Engine ────────────────────────────────────
echo "Starting WS Stream Engine..."
nohup venv/bin/python3 backend/arka/ws_stream_engine.py \
    > logs/arka/ws_stream.log 2>&1 &
echo "  WS Stream PID=$!"

# ── Start Watchdog ────────────────────────────────────────────
echo "Starting Watchdog..."
nohup bash scripts/watchdog.sh > logs/watchdog.log 2>&1 &
echo "  Watchdog PID=$!"

sleep 3

# ── Final status ──────────────────────────────────────────────
echo ""
echo "✅ CHAKRA started — system status:"
bash scripts/process_guard.sh --report
echo ""
echo "Logs:"
echo "  API:          tail -f logs/dashboard_api.log"
echo "  ARKA:         tail -f logs/arka/arka_engine.log"
echo "  Flow Scalper: tail -f logs/arka/flow_scalper.log"
echo "  Flow Monitor: tail -f logs/chakra/flow_monitor.log"
echo "  WS Stream:    tail -f logs/arka/ws_stream.log"
echo "  Watchdog:     tail -f logs/watchdog.log"
