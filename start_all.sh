#!/bin/bash
# CHAKRA — Start All Engines Manually
# Usage: bash ~/trading-ai/start_all.sh

cd ~/trading-ai
source ~/.zshrc 2>/dev/null

echo ""
echo "╔═══════════════════════════════╗"
echo "║   CHAKRA — Starting Engines   ║"
echo "╚═══════════════════════════════╝"
echo ""

mkdir -p logs/arka logs/taraka logs/signals

# ── Kill any existing processes ──────────────────────────────
echo "Stopping existing processes..."
if [ -f logs/arka/arka.pid ]; then
  PID=$(cat logs/arka/arka.pid)
  kill "$PID" 2>/dev/null && echo "  Stopped ARKA (pid $PID)" || true
  rm logs/arka/arka.pid
fi
pkill -f "taraka_engine.py" 2>/dev/null && echo "  Stopped TARAKA" || true
pkill -f "dashboard_api" 2>/dev/null && echo "  Stopped API server" || true
sleep 1

echo ""

# ── Start API server ─────────────────────────────────────────
echo "▶  Starting API server (port 8000)..."
nohup venv/bin/uvicorn backend.dashboard_api:app --port 8000 \
  > logs/api.log 2>&1 &
echo $! > logs/api.pid
sleep 2

if kill -0 $(cat logs/api.pid) 2>/dev/null; then
  echo "   ✅  API server running (pid $(cat logs/api.pid))"
else
  echo "   ❌  API server failed to start — check logs/api.log"
fi

# ── Start ARKA scalper ───────────────────────────────────────
echo "▶  Starting ARKA engine..."
LOG_DATE=$(date +%Y-%m-%d)
nohup venv/bin/python3 backend/arka/arka_engine.py \
  > logs/arka/arka_${LOG_DATE}.log 2>&1 &
echo $! > logs/arka/arka.pid
sleep 2

if kill -0 $(cat logs/arka/arka.pid) 2>/dev/null; then
  echo "   ✅  ARKA running (pid $(cat logs/arka/arka.pid))"
  echo "   📋  Logs: logs/arka/arka_${LOG_DATE}.log"
else
  echo "   ❌  ARKA failed to start"
fi

# ── Start TARAKA Discord bot ─────────────────────────────────
echo "▶  Starting TARAKA engine..."
nohup venv/bin/python3 backend/taraka/taraka_engine.py \
  > logs/taraka/taraka.log 2>&1 &
echo $! > logs/taraka/taraka.pid
sleep 2

if kill -0 $(cat logs/taraka/taraka.pid) 2>/dev/null; then
  echo "   ✅  TARAKA running (pid $(cat logs/taraka/taraka.pid))"
  echo "   📋  Logs: logs/taraka/taraka.log"
else
  echo "   ❌  TARAKA failed to start — check logs/taraka/taraka.log"
fi

echo ""
echo "══════════════════════════════════════════════"
echo "  STATUS SUMMARY"
echo "══════════════════════════════════════════════"
echo ""

for engine in api arka taraka; do
  PID_FILE="logs/${engine}.pid"
  if [ "$engine" = "arka" ]; then PID_FILE="logs/arka/arka.pid"; fi
  if [ "$engine" = "taraka" ]; then PID_FILE="logs/taraka/taraka.pid"; fi

  if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
    echo "  ✅  $engine"
  else
    echo "  ❌  $engine (not running)"
  fi
done

echo ""
echo "Dashboard: open frontend/dashboard.html in your browser"
echo "ARKA log:  tail -f logs/arka/arka_$(date +%Y-%m-%d).log"
echo "TARAKA:    tail -f logs/taraka/taraka.log"
echo ""
