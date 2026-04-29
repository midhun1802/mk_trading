# ARJUN Training & Calibration Guide
## CHAKRA Neural Trading OS v3

---

## OVERVIEW

ARJUN is CHAKRA's multi-agent AI signal system. It generates daily BUY/SELL/HOLD
signals for 30+ tickers using 4 specialized agents:

| Agent | Role | Model |
|-------|------|-------|
| Bull Agent | Generates bullish thesis + score 0-100 | Claude claude-sonnet-4-6 |
| Bear Agent | Generates bearish thesis + score 0-100 | Claude claude-sonnet-4-6 |
| Risk Manager | APPROVE/BLOCK decision | Claude claude-sonnet-4-6 |
| Coordinator | Synthesizes all agents → final signal | Claude claude-sonnet-4-6 |

ARKA's ML models (fakeout filter, conviction scorer) are separate XGBoost models
trained on historical bar data. Both systems need periodic retraining.

---

## PART 1: ARJUN AGENT PROMPT CALIBRATION

ARJUN doesn't "train" in the traditional ML sense — it uses Claude via API.
Calibration means tuning the prompts, thresholds, and scoring logic.

### 1.1 Check Current Signal Quality

```bash
cd ~/trading-ai

# Review last 7 days of signals
python3 << 'PYEOF'
import json, glob
from pathlib import Path

files = sorted(glob.glob("logs/signals/signals_*.json"), reverse=True)[:7]
for f in files:
    try:
        d = json.loads(Path(f).read_text())
        sigs = d if isinstance(d, list) else d.get("signals", [])
        buys  = sum(1 for s in sigs if s.get("signal") == "BUY")
        sells = sum(1 for s in sigs if s.get("signal") == "SELL")
        holds = sum(1 for s in sigs if s.get("signal") == "HOLD")
        avg_conf = sum(s.get("confidence", 0) for s in sigs) / max(len(sigs), 1)
        print(f"{f[-15:-5]}: {len(sigs)} tickers | BUY:{buys} SELL:{sells} HOLD:{holds} | avg_conf:{avg_conf:.1f}%")
    except:
        pass
PYEOF
```

### 1.2 Review Agent Prompt Files

```bash
# Find all agent prompt files
find backend/arjun -name "*.py" | xargs grep -l "system_prompt\|SYSTEM\|You are" | head -10

# Check bull agent prompt
grep -A 20 "system_prompt\|You are a" backend/arjun/agents/bull_agent.py | head -30

# Check bear agent prompt  
grep -A 20 "system_prompt\|You are a" backend/arjun/agents/bear_agent_v2.py | head -30

# Check risk manager thresholds
grep -n "threshold\|APPROVE\|BLOCK\|min_score\|confidence" backend/arjun/agents/risk_manager_agent.py | head -20
```

### 1.3 Calibrate Signal Thresholds

The key thresholds that control signal quality:

```bash
# Check current thresholds
grep -rn "confidence.*threshold\|min_confidence\|CONVICTION\|threshold.*buy\|threshold.*sell" \
  backend/arjun/ | grep -v ".pyc" | head -20
```

**Target thresholds for production:**
- Minimum confidence to generate BUY: **65%**
- Minimum confidence to generate SELL: **65%**  
- Risk Manager blocks if: bull_score < 40 OR bear overwhelms by >30pts
- Coordinator requires: both agents agree within 25pts for strong signal

### 1.4 Fix Common ARJUN Issues

**Issue: All signals showing HOLD**
```bash
# Check if API calls are timing out
grep -n "timeout\|TimeoutError\|rate_limit" logs/arjun/*.log 2>/dev/null | tail -10

# Check Claude API key
grep "ANTHROPIC\|CLAUDE_API" .env | head -3
```

**Issue: Confidence scores always near 50%**
```bash
# This means agents aren't differentiating — check that market data is fresh
python3 -c "
import json
d = json.load(open('logs/signals/signals_$(date +%Y-%m-%d).json'))
sigs = d if isinstance(d, list) else d.get('signals', [])
for s in sigs[:5]:
    print(s.get('ticker'), s.get('confidence'), s.get('signal'))
    agents = s.get('agents', {})
    print('  Bull:', agents.get('bull', {}).get('score'))
    print('  Bear:', agents.get('bear', {}).get('score'))
"
```

**Issue: Risk Manager blocking too many signals**
```bash
grep -n "BLOCK\|block_reason\|risk.*threshold" backend/arjun/agents/risk_manager_agent.py
```

---

## PART 2: ARKA ML MODEL RETRAINING

ARKA uses two XGBoost models:
1. **Fakeout Filter** — blocks low-quality entries (AUC 0.98 after last retrain)
2. **Conviction Scorer** — scores 0-100 based on technical features

### 2.1 Check Current Model Status

```bash
# Check model files and ages
ls -la backend/arka/models/arka/ 2>/dev/null || ls -la models/arka/ 2>/dev/null

# Check last training date
python3 << 'PYEOF'
import os, time
model_paths = [
    "backend/arka/models/arka/arka_fakeout_spy.pkl",
    "backend/arka/models/arka/arka_fakeout_qqq.pkl", 
    "backend/arka/models/arka/arka_conviction_spy.pkl",
]
for p in model_paths:
    if os.path.exists(p):
        age_days = (time.time() - os.path.getmtime(p)) / 86400
        print(f"{p}: {age_days:.0f} days old")
    else:
        print(f"{p}: NOT FOUND")
PYEOF
```

### 2.2 Retrain Fakeout Model

Retrain weekly (Sunday nights) or after 200+ new trades:

```bash
cd ~/trading-ai

# Check training script
ls backend/arka/train_arka.py 2>/dev/null || ls backend/arka/retrain*.py 2>/dev/null

# Run retraining on 90-day data
python3 backend/arka/train_arka.py --days 90 --ticker SPY 2>&1 | tail -20
python3 backend/arka/train_arka.py --days 90 --ticker QQQ 2>&1 | tail -20
```

**Expected output after good retrain:**
```
✅ SPY fakeout model: AUC=0.97 (train) / AUC=0.94 (test)
✅ SPY conviction model: accuracy=0.82
Models saved to backend/arka/models/arka/
```

**If AUC drops below 0.85:** The model needs more data or feature engineering review.

### 2.3 Validate Model After Retraining

```bash
python3 << 'PYEOF'
import pickle, numpy as np

# Load and spot-check fakeout model
try:
    with open("backend/arka/models/arka/arka_fakeout_spy.pkl", "rb") as f:
        model = pickle.load(f)
    print("✅ SPY fakeout model loaded")
    print(f"   Features expected: {model.n_features_in_}")
    print(f"   Classes: {model.classes_}")
except Exception as e:
    print(f"❌ Model load failed: {e}")
PYEOF
```

---

## PART 3: SWING SCREENER CALIBRATION

### 3.1 Verify Score Distribution

After market hours Monday, run the screener and check score spread:

```bash
cd ~/trading-ai && python3 << 'PYEOF'
import sys
sys.path.insert(0, ".")

# Import and run screener directly
from backend.arka.arka_swings import score_ticker
import httpx, os
from dotenv import load_dotenv
load_dotenv()

# Test a few tickers
test_tickers = ["AAPL", "NVDA", "TSLA", "SPY", "QQQ"]
key = os.getenv("POLYGON_API_KEY", "")

for ticker in test_tickers:
    try:
        result = score_ticker(ticker, key)
        if result:
            print(f"{ticker}: score={result['score']} dir={result['direction']} rsi={result['rsi']:.0f} vol={result['vol_ratio']:.1f}x")
        else:
            print(f"{ticker}: no result")
    except Exception as e:
        print(f"{ticker}: error - {e}")
PYEOF
```

**Expected:** Scores ranging from 45-90, not all 60.

### 3.2 Tune Swing Thresholds

```bash
grep -n "MIN_SCORE\|MAX_POSITIONS\|MIN_SCORE_DISCORD\|MAX_DISCORD" backend/arka/arka_swings.py | head -10
```

**Current production settings:**
- `MIN_SCORE = 60` — minimum to appear in watchlist
- `MIN_SCORE_DISCORD = 75` — minimum to fire Discord alert
- `MAX_POSITIONS = 3` — max concurrent swing positions
- `MAX_DISCORD_ALERTS = 5` — max Discord alerts per day
- Discord gate: vol ≥ 1.5x AND rr ≥ 1.5 AND score ≥ 75

---

## PART 4: WEEKLY RETRAIN SCHEDULE

Add this to crontab for automatic Sunday night retraining:

```bash
# View current crontab
crontab -l

# Add Sunday retraining (run at 11pm Sunday)
# crontab -e and add:
# 0 23 * * 0 cd ~/trading-ai && venv/bin/python3 backend/arka/train_arka.py --days 90 > logs/arka/retrain.log 2>&1
```

**Full weekly maintenance checklist (Sunday evening):**
1. Retrain ARKA fakeout models
2. Check signal quality from past week
3. Review ARKA win rate from performance page
4. Clear expired options positions from Alpaca
5. Check disk space: `df -h ~/trading-ai/logs/`
6. Archive old logs: files older than 30 days

---

## PART 5: PRODUCTION READINESS CHECKLIST

Run this full health check Monday morning before market open:

```bash
cd ~/trading-ai && python3 << 'PYEOF'
import os, json, time
from pathlib import Path
from datetime import date, datetime

print("=" * 60)
print("CHAKRA PRODUCTION HEALTH CHECK")
print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

checks = []

# 1. Environment variables
env_keys = ["POLYGON_API_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET", 
            "ANTHROPIC_API_KEY", "DISCORD_TRADES_WEBHOOK"]
for key in env_keys:
    val = os.getenv(key, "")
    status = "✅" if val and len(val) > 5 else "❌"
    checks.append((status, f"ENV: {key}", "OK" if val else "MISSING"))

# 2. Model files
models = [
    "backend/arka/models/arka/arka_fakeout_spy.pkl",
    "backend/arka/models/arka/arka_fakeout_qqq.pkl",
]
for m in models:
    if Path(m).exists():
        age = (time.time() - Path(m).stat().st_mtime) / 86400
        status = "✅" if age < 14 else "⚠️"
        checks.append((status, f"MODEL: {m.split('/')[-1]}", f"{age:.0f} days old"))
    else:
        checks.append(("❌", f"MODEL: {m.split('/')[-1]}", "MISSING"))

# 3. Log directories
log_dirs = ["logs/arka", "logs/chakra", "logs/signals", "logs/gex", 
            "logs/internals", "logs/swings"]
for d in log_dirs:
    status = "✅" if Path(d).exists() else "❌"
    checks.append((status, f"DIR: {d}", "exists" if Path(d).exists() else "MISSING — run mkdir -p"))

# 4. Latest signal file
sig_path = f"logs/signals/signals_{date.today()}.json"
if Path(sig_path).exists():
    sigs = json.loads(Path(sig_path).read_text())
    sig_list = sigs if isinstance(sigs, list) else sigs.get("signals", [])
    checks.append(("✅", "SIGNALS: today", f"{len(sig_list)} tickers"))
else:
    checks.append(("⚠️", "SIGNALS: today", "Not yet generated — ARJUN runs at 8am ET"))

# 5. Watchlist file
wl_path = "logs/chakra/watchlist_latest.json"
if Path(wl_path).exists():
    wl = json.loads(Path(wl_path).read_text())
    count = len(wl.get("candidates", []))
    checks.append(("✅", "WATCHLIST: swings", f"{count} candidates"))
else:
    checks.append(("❌", "WATCHLIST: swings", "MISSING — run --premarket scan"))

# 6. Flow signals
flow_path = "logs/chakra/flow_signals_latest.json"
if Path(flow_path).exists():
    age = (time.time() - Path(flow_path).stat().st_mtime) / 3600
    status = "✅" if age < 2 else "⚠️"
    checks.append((status, "FLOW SIGNALS", f"{age:.1f}h old"))
else:
    checks.append(("⚠️", "FLOW SIGNALS", "No file yet — starts when flow monitor runs"))

# Print results
for status, name, detail in checks:
    print(f"  {status}  {name:<45} {detail}")

print("=" * 60)
failures = [c for c in checks if c[0] == "❌"]
warnings = [c for c in checks if c[0] == "⚠️"]
print(f"RESULT: {len(checks)-len(failures)-len(warnings)} OK | {len(warnings)} warnings | {len(failures)} failures")
if failures:
    print("\nCRITICAL — Fix before trading:")
    for f in failures:
        print(f"  {f[1]}: {f[2]}")
print("=" * 60)
PYEOF
```

---

## PART 6: MONDAY MORNING STARTUP SEQUENCE

**Run in this exact order:**

```bash
cd ~/trading-ai

# Step 0: Health check
python3 -c "
import os
keys = ['POLYGON_API_KEY','ALPACA_API_KEY','ALPACA_API_SECRET','ANTHROPIC_API_KEY']
[print(f'✅ {k}') if os.getenv(k) else print(f'❌ MISSING: {k}') for k in keys]
" && source .env 2>/dev/null || true

# Step 1: Create required directories
mkdir -p logs/gex logs/arka logs/chakra logs/signals logs/internals logs/swings

# Step 2: Start Dashboard API
pkill -f "uvicorn" 2>/dev/null; sleep 1
nohup venv/bin/uvicorn backend.dashboard_api:app --host 0.0.0.0 --port 5001 \
  > logs/dashboard_api.log 2>&1 &
sleep 3 && curl -s http://localhost:5001/api/account | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(f'✅ API OK — equity: \${float(d.get(\"equity\",0)):,.2f}')"

# Step 3: Start ARKA Engine
pkill -f "arka_engine" 2>/dev/null; sleep 1
nohup python3 -m backend.arka.arka_engine > logs/arka/arka_engine.log 2>&1 &
sleep 3 && tail -5 logs/arka/arka_engine.log

# Step 4: Start Flow Monitor
pkill -f "flow_monitor" 2>/dev/null; sleep 1
nohup python3 backend/chakra/flow_monitor.py --watch \
  > logs/chakra/flow_monitor.log 2>&1 &
sleep 2 && echo "✅ Flow monitor started"

# Step 5: Premarket swings scan (run at 8:15am ET)
python3 backend/arka/arka_swings.py --premarket 2>&1 | tail -10

# Step 6: Verify all running
echo ""
echo "=== PROCESS STATUS ==="
ps aux | grep -E "uvicorn|arka_engine|flow_monitor" | grep -v grep | \
  awk '{print "✅ Running:", $11, $12}'

echo ""
echo "=== READY FOR MARKET OPEN ==="
```

---

## PART 7: ARJUN DAILY SIGNAL GENERATION

ARJUN runs automatically via crontab at 8:00 AM ET. To run manually:

```bash
cd ~/trading-ai

# Run full signal generation
python3 -m backend.arjun.run_daily 2>&1 | tail -20

# Or if run_daily doesn't exist:
python3 backend/arjun/coordinator.py --daily 2>&1 | tail -20

# Check results
python3 << 'PYEOF'
import json
from pathlib import Path
from datetime import date

path = f"logs/signals/signals_{date.today()}.json"
if Path(path).exists():
    d = json.loads(Path(path).read_text())
    sigs = d if isinstance(d, list) else d.get("signals", [])
    print(f"✅ {len(sigs)} signals generated today")
    for s in sorted(sigs, key=lambda x: x.get("confidence",0), reverse=True)[:5]:
        print(f"  {s.get('ticker'):<6} {s.get('signal'):<5} {s.get('confidence',0):.0f}%")
else:
    print(f"❌ No signals file found at {path}")
PYEOF
```

---

## PART 8: MONITORING DURING MARKET HOURS

### Live Log Tails (open in separate terminals)
```bash
# Terminal 1: ARKA engine activity
tail -f ~/trading-ai/logs/arka/arka_engine.log | grep -E "TRADE|ENTRY|EXIT|BLOCKED|GEX|conviction"

# Terminal 2: Flow monitor signals  
tail -f ~/trading-ai/logs/chakra/flow_monitor.log | grep -E "ALERT|FIRE|BLOCKED|DROPPED"

# Terminal 3: Dashboard API requests
tail -f ~/trading-ai/logs/dashboard_api.log | grep -v "Waiting\|OPTIONS"
```

### Key Metrics to Watch
```bash
# Check ARKA session state
curl -s http://localhost:5001/api/arka/summary | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'Trades: {d.get(\"trades\",0)} | PnL: \${d.get(\"daily_pnl\",0):.2f}')
print(f'Open positions: {list(d.get(\"open_positions\",{}).keys())}')
"

# Check live P&L
curl -s http://localhost:5001/api/account/live-pnl | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'Daily P&L: \${d.get(\"daily_pnl\",0):.2f}')
print(f'Unrealized: \${d.get(\"unrealized_pl\",0):.2f}')
print(f'Equity: \${d.get(\"equity\",0):.2f}')
"
```

---

*ARJUN Training & Production Guide v1.0*
*CHAKRA Neural Trading OS — March 28, 2026*
