#!/bin/bash
# ╔══════════════════════════════════════════════════════╗
# ║          CHAKRA — ONE COMMAND STARTUP v2             ║
# ║  Usage: ./start_chakra.sh                            ║
# ╚══════════════════════════════════════════════════════╝

BASE="$HOME/trading-ai"
VENV="$BASE/venv/bin/python3"
LOG="$BASE/logs"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║        CHAKRA STARTING UP  v2        ║"
echo "╚══════════════════════════════════════╝"
echo ""

export $(grep -v '^#' "$BASE/.env" | grep -v '^$' | xargs) 2>/dev/null

pkill -f "arka_engine.py" 2>/dev/null
pkill -f "arjun_live_engine.py" 2>/dev/null
pkill -f "taraka_engine.py" 2>/dev/null
pkill -f "market_internals.py" 2>/dev/null
pkill -f "dashboard_api" 2>/dev/null
sleep 2

find "$BASE/backend" -name "*.pyc" -delete 2>/dev/null
find "$BASE/backend" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true

echo "🖥  Dashboard API..."
cd "$BASE"
nohup "$VENV" -m uvicorn backend.dashboard_api:app --host 0.0.0.0 --port 8000 >> "$LOG/dashboard.log" 2>&1 &
sleep 3
curl -s http://localhost:8000/ > /dev/null 2>&1 && echo "   ✅ Running on :8000" || echo "   ⚠️  Check $LOG/dashboard.log"

echo "🧠  Market Internals..."
cd "$BASE"
nohup "$VENV" backend/internals/market_internals.py >> "$LOG/internals.log" 2>&1 &
echo "   ✅ Started (PID $!)"
sleep 2

echo "📈  ARKA Scalper..."
cd "$BASE"
nohup "$VENV" backend/arka/arka_engine.py >> "$LOG/arka/arka.log" 2>&1 &
echo "   ✅ Started (PID $!)"
sleep 2

echo "⚡  Arjun Live Engine..."
cd "$BASE"
nohup "$VENV" backend/arjun/arjun_live_engine.py >> "$LOG/arjun.log" 2>&1 &
echo "   ✅ Started (PID $!)"
sleep 2

echo "👁  TARAKA Discord..."
cd "$BASE"
nohup "$VENV" backend/taraka/taraka_engine.py >> "$LOG/taraka.log" 2>&1 &
echo "   ✅ Started (PID $!)"
sleep 3

echo ""
echo "┌─────────────────────────────────────────┐"
echo "│  CHAKRA ENGINE STATUS                   │"
echo "├─────────────────────────────────────────┤"

chk() { pgrep -f "$2" > /dev/null 2>&1 && printf "│  ✅  %-36s│\n" "$1" || printf "│  ❌  %-36s│\n" "$1"; }

chk "Dashboard API        :8000" "dashboard_api"
chk "Market Internals" "market_internals"
chk "ARKA Scalper" "arka_engine"
chk "Arjun Live Engine" "arjun_live_engine"
chk "TARAKA Discord" "taraka_engine"

echo "└─────────────────────────────────────────┘"
echo ""
open "http://localhost:8000" 2>/dev/null || true
echo "✅  CHAKRA is live → http://localhost:8000"
echo "  Stop all: pkill -f 'arka_engine|arjun_live|taraka_engine|market_internals|dashboard_api'"
echo ""

# MOC Engine (starts at 3:40 PM via LaunchAgent — manual start only needed for testing)
# nohup venv/bin/python3 backend/arka/moc_engine.py >> logs/arka/moc-$(date +%Y-%m-%d).log 2>&1 &
# echo "✅ MOC Engine started"
