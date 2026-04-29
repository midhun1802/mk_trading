#!/bin/bash
# start_engines.sh — Start all CHAKRA engines
# Usage: bash start_engines.sh [engine_name]
# Engines: arka, internals, all

cd ~/trading-ai
export PYTHONPATH=~/trading-ai
source venv/bin/activate

ENGINE=${1:-all}
TODAY=$(date +%Y-%m-%d)

start_arka() {
    if pgrep -f "arka_engine.py" > /dev/null; then
        echo "ARKA already running (PID $(pgrep -f arka_engine.py))"
    else
        nohup /Users/midhunkrothapalli/trading-ai/venv/bin/python3 backend/arka/arka_engine.py \
            >> logs/arka/arka_${TODAY}.log 2>&1 &
        echo "ARKA started (PID $!)"
    fi
}

start_internals() {
    if pgrep -f "market_internals" > /dev/null; then
        echo "Market Internals already running"
    else
        nohup /Users/midhunkrothapalli/trading-ai/venv/bin/python3 -c "
import sys, time
sys.path.insert(0, '.')
from backend.internals.market_internals import run_continuous
run_continuous()
" >> logs/internals/internals.log 2>&1 &
        echo "Market Internals started (PID $!)"
    fi
}

start_dashboard() {
    if pgrep -f "uvicorn" > /dev/null; then
        echo "Dashboard API already running"
    else
        nohup /Users/midhunkrothapalli/trading-ai/venv/bin/python3 -m uvicorn backend.dashboard_api:app \
            --host 0.0.0.0 --port 5001 \
            >> logs/dashboard.log 2>&1 &
        echo "Dashboard API started (PID $!)"
    fi
}

case $ENGINE in
    arka)      start_arka ;;
    internals) start_internals ;;
    dashboard) start_dashboard ;;
    all)
        start_arka
        start_internals
        start_dashboard
        echo "All engines started"
        ;;
    *)
        echo "Unknown engine: $ENGINE"
        echo "Usage: bash start_engines.sh [arka|internals|dashboard|all]"
        ;;
esac
