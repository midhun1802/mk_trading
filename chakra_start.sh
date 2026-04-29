#!/bin/bash
echo "CHAKRA Neural Trading OS - Starting..."
BASE=~/trading-ai
pkill -f uvicorn 2>/dev/null; pkill -f arka_engine 2>/dev/null
pkill -f taraka_engine 2>/dev/null; pkill -f market_internals 2>/dev/null
sleep 2
echo "  Starting Dashboard..."
cd $BASE && venv/bin/python3 -m uvicorn backend.dashboard_api:app --host 0.0.0.0 --port 8000 >> logs/dashboard.log 2>&1 &
sleep 3
curl -s http://localhost:8000/ > /dev/null && echo "  OK  Dashboard -> http://localhost:8000" || echo "  FAIL Dashboard"
echo "  Starting ARKA..."
cd $BASE && venv/bin/python3 backend/arka/arka_engine.py >> logs/arka/arka.log 2>&1 &
echo "  Starting TARAKA..."
cd $BASE && venv/bin/python3 backend/taraka/taraka_engine.py >> logs/taraka/taraka.log 2>&1 &
echo "  Starting Internals..."
cd $BASE && venv/bin/python3 backend/internals/market_internals.py >> logs/internals/internals.log 2>&1 &
sleep 2
echo ""
echo "All engines running. Dashboard -> http://localhost:8000"
ps aux | grep -E "arka_engine|taraka_engine|market_internals|uvicorn" | grep -v grep | awk '{print "  * " $11}'
