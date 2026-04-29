#!/bin/bash
# ARKA Engine + Flow Scalper starter
# Kills any stale processes, then starts fresh with caffeinate to prevent Mac sleep
cd /Users/midhunkrothapalli/trading-ai

VENV="/Users/midhunkrothapalli/trading-ai/venv/bin/python3"

# Kill stale engine and flow scalper before starting fresh
pkill -f "backend.arka.arka_engine"  2>/dev/null; sleep 2
pkill -f "backend.arka.flow_scalper" 2>/dev/null; sleep 1

# Start ARKA engine
nohup caffeinate -i $VENV -m backend.arka.arka_engine \
  >> /Users/midhunkrothapalli/trading-ai/logs/arka/arka_engine.log 2>&1 &
echo "[$(date)] ARKA engine started PID $!" >> /Users/midhunkrothapalli/trading-ai/logs/arka/watchdog.log

sleep 3

# Start Flow Scalper (pure institutional flow execution)
nohup $VENV -m backend.arka.flow_scalper \
  >> /Users/midhunkrothapalli/trading-ai/logs/arka/flow_scalper.log 2>&1 &
echo "[$(date)] Flow Scalper started PID $!" >> /Users/midhunkrothapalli/trading-ai/logs/arka/watchdog.log
