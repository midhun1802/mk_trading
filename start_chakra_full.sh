#!/bin/bash
echo ""
echo "╔══════════════════════════════════════╗"
echo "║   CHAKRA — Full System Startup       ║"
echo "╚══════════════════════════════════════╝"
echo ""

cd ~/trading-ai
source ~/.zshrc 2>/dev/null

# ── Kill everything cleanly ──────────────────────────────────
echo "Stopping existing processes..."
pkill -f uvicorn 2>/dev/null
pkill -f arka_engine 2>/dev/null
pkill -f taraka_engine 2>/dev/null
pkill -f market_internals 2>/dev/null
pkill -f flow_monitor 2>/dev/null
pkill -f price_broadcaster 2>/dev/null
sleep 3  # give port 8000 time to release
lsof -ti:8000 | xargs kill -9 2>/dev/null
sleep 1

mkdir -p logs/arka logs/taraka logs/chakra logs/internals

start_daemon() {
  local name=$1
  local cmd=$2
  local pidfile=$3
  local logfile=$4
  eval "nohup $cmd >> $logfile 2>&1 &"
  echo $! > $pidfile
  sleep 2
  if kill -0 $(cat $pidfile) 2>/dev/null; then
    echo "   ✅  $name (pid $(cat $pidfile))"
  else
    echo "   ❌  $name — check $logfile"
  fi
}

# ── Persistent daemons ───────────────────────────────────────
echo "▶  Dashboard API (port 8000)..."
start_daemon "Dashboard API" \
  "venv/bin/uvicorn backend.dashboard_api:app --host 0.0.0.0 --port 8000" \
  "logs/api.pid" "logs/dashboard.log"

echo "▶  ARKA Engine..."
LOG_DATE=$(date +%Y-%m-%d)
start_daemon "ARKA Engine" \
  "venv/bin/python3 backend/arka/arka_engine.py" \
  "logs/arka/arka.pid" "logs/arka/arka_${LOG_DATE}.log"

echo "▶  TARAKA Engine..."

echo "▶  Market Internals..."
nohup venv/bin/python3 backend/internals/market_internals.py >> logs/internals/internals.log 2>&1 &
echo "   ✅  Market Internals (pid $!)"


echo "▶  Market Internals..."
nohup venv/bin/python3 backend/internals/market_internals.py >> logs/internals/internals.log 2>&1 &
echo "   ✅  Market Internals (pid $!)"

start_daemon "TARAKA Engine" \

echo "▶  Market Internals..."
nohup venv/bin/python3 backend/internals/market_internals.py >> logs/internals/internals.log 2>&1 &
echo "   ✅  Market Internals (pid $!)"


echo "▶  Market Internals..."
nohup venv/bin/python3 backend/internals/market_internals.py >> logs/internals/internals.log 2>&1 &
echo "   ✅  Market Internals (pid $!)"

  "venv/bin/python3 backend/taraka/taraka_engine.py" \
  "logs/taraka/taraka.pid" "logs/taraka/taraka.log"

echo "▶  Market Internals..."
start_daemon "Market Internals" \
  "venv/bin/python3 backend/internals/market_internals.py" \
  "logs/internals.pid" "logs/internals/internals.log"

echo "▶  Flow Monitor..."
echo "▶  Flow Monitor (one-shot)..."
  "venv/bin/python3 backend/chakra/flow_monitor.py" \
  "logs/flow_monitor.pid" "logs/chakra/flow_monitor.log"

# ── One-shot scripts (run once, managed by cron after) ───────
echo "▶  Health Monitor (one-shot)..."
venv/bin/python3 backend/chakra/health_monitor.py >> logs/chakra/health_monitor.log 2>&1 &
echo "   ✅  Health Monitor (cron-managed every 5min)"

echo "▶  ARJUN Healer (one-shot)..."
venv/bin/python3 backend/chakra/arjun_healer.py >> logs/chakra/healer.log 2>&1 &
echo "   ✅  ARJUN Healer (cron-managed every 30min)"

echo ""
echo "══════════════════════════════════════════════"
echo "  ✅  Dashboard  → http://localhost:8000"
echo "  ✅  API Health → http://localhost:8000/api/system/health"
echo ""
echo "  Logs:"
echo "    tail -f logs/dashboard.log"
echo "    tail -f logs/arka/arka_$(date +%Y-%m-%d).log"
echo "    tail -f logs/taraka/taraka.log"
echo "══════════════════════════════════════════════"
echo ""
