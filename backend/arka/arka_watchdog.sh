#!/bin/bash
# ARKA Engine Watchdog — restart if dead during market hours
# Only runs Mon-Fri 8:30am-4:30pm ET

cd /Users/midhunkrothapalli/trading-ai

# Check if market hours (ET) — simple hour check
ET_HOUR=$(TZ="America/New_York" date +%H)
ET_MIN=$(TZ="America/New_York" date +%M)
ET_DOW=$(TZ="America/New_York" date +%u)  # 1=Mon, 7=Sun

# Skip weekends
if [ "$ET_DOW" -ge 6 ]; then exit 0; fi

# Only 8:25am-4:35pm ET
if [ "$ET_HOUR" -lt 8 ] || [ "$ET_HOUR" -gt 16 ]; then exit 0; fi
if [ "$ET_HOUR" -eq 8 ] && [ "$ET_MIN" -lt 25 ]; then exit 0; fi
if [ "$ET_HOUR" -eq 16 ] && [ "$ET_MIN" -gt 35 ]; then exit 0; fi

# Check and restart ARKA engine if dead
if ! pgrep -f "backend.arka.arka_engine" > /dev/null 2>&1; then
    echo "[$(date)] ARKA watchdog: engine dead, restarting..." >> logs/arka/watchdog.log
    nohup caffeinate -i venv/bin/python3 -m backend.arka.arka_engine >> logs/arka/arka_engine.log 2>&1 &
    echo "[$(date)] ARKA watchdog: engine restarted PID $!" >> logs/arka/watchdog.log
fi

# Check and restart Flow Scalper if dead
if ! pgrep -f "backend.arka.flow_scalper" > /dev/null 2>&1; then
    echo "[$(date)] ARKA watchdog: flow_scalper dead, restarting..." >> logs/arka/watchdog.log
    nohup venv/bin/python3 -m backend.arka.flow_scalper >> logs/arka/flow_scalper.log 2>&1 &
    echo "[$(date)] ARKA watchdog: flow_scalper restarted PID $!" >> logs/arka/watchdog.log
fi
