#!/bin/bash
VENV="/Users/midhunkrothapalli/trading-ai/venv/bin/python3"
BASE="/Users/midhunkrothapalli/trading-ai"
export PYTHONPATH="$BASE"
cd $BASE

echo "=== CHAKRA Full System Startup $(date) ==="

# 1. Dashboard API
lsof -ti:5001 | xargs kill -9 2>/dev/null; sleep 1
nohup $VENV -m uvicorn backend.dashboard_api:app \
  --host 0.0.0.0 --port 5001 >> logs/dashboard.log 2>&1 &
echo "✅  Dashboard API        → port 5001"
sleep 3

# 2. Market Internals (Neural Pulse, VIX)
pkill -f "runloop.py" 2>/dev/null; sleep 1
nohup $VENV backend/internals/runloop.py \
  >> logs/internals/internals.log 2>&1 &
echo "✅  Market Internals     → running"
sleep 1

# 3. Flow Monitor (UOA + dark pool)
pkill -f "flow_monitor.py" 2>/dev/null; sleep 1
nohup $VENV backend/chakra/flow_monitor.py \
  >> logs/chakra/flow_monitor.log 2>&1 &
echo "✅  Flow Monitor         → running"
sleep 1

# 4. Health Monitor
pkill -f "health_monitor.py" 2>/dev/null; sleep 1
nohup $VENV backend/chakra/health_monitor.py \
  >> logs/chakra/health_monitor.log 2>&1 &
echo "✅  Health Monitor       → running"
sleep 1

# 5. ARJUN Healer
pkill -f "arjun_healer.py" 2>/dev/null; sleep 1
nohup $VENV backend/chakra/arjun_healer.py --watch \
  >> logs/chakra/healer.log 2>&1 &
echo "✅  ARJUN Healer         → watching"
sleep 1

# 6. CHAKRA Swings Engine
pkill -f "chakra_swings_engine.py" 2>/dev/null; sleep 1
nohup $VENV backend/chakra/chakra_swings_engine.py --monitor \
  >> logs/swings/swings.log 2>&1 &
echo "✅  CHAKRA Swings        → monitoring"
sleep 1

# 7. Prime module caches (run once at startup)
echo "   Priming module caches..."
$VENV backend/chakra/modules/hurst_engine.py   >> logs/chakra/session1_modules.log 2>&1 &
$VENV backend/chakra/modules/vrp_engine.py     >> logs/chakra/session1_modules.log 2>&1 &
$VENV backend/chakra/modules/hmm_regime.py     >> logs/chakra/session3_modules.log 2>&1 &
$VENV backend/chakra/modules/iv_skew.py        >> logs/chakra/session3_modules.log 2>&1 &
echo "✅  Module caches        → priming (background)"
sleep 2

# 8. API health check
STATUS=$(curl -s http://localhost:5001/api/engine/status 2>/dev/null)
if echo "$STATUS" | grep -q "engines"; then
    echo ""
    echo "✅  API healthy — all systems GO"
else
    echo ""
    echo "❌  API not responding — check: tail -20 logs/dashboard.log"
fi

echo ""
echo "────────────────────────────────────────────"
echo "  Dashboard  → http://localhost:8000"
echo "  API        → http://localhost:5001"
echo "  ARKA       → auto-fires at 8:30 AM (cron)"
echo "────────────────────────────────────────────"
echo ""
echo "Manual ARKA start (if needed before 8:30):"
echo "  PYTHONPATH=\$HOME/trading-ai $VENV backend/arka/arka_engine.py &"
