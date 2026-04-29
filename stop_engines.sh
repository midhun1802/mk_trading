#!/bin/bash
# stop_engines.sh — Stop CHAKRA engines
ENGINE=${1:-all}

case $ENGINE in
    arka)      pkill -f "arka_engine.py" && echo "ARKA stopped" ;;
    internals) pkill -f "market_internals" && echo "Internals stopped" ;;
    dashboard) pkill -f "uvicorn" && echo "Dashboard stopped" ;;
    all)
        pkill -f "arka_engine.py"
        pkill -f "market_internals"
        echo "Engines stopped (dashboard kept running)"
        ;;
esac
