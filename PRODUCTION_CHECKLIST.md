# CHAKRA Production Readiness Checklist
## Monday Morning — Market Open Preparation

---

## PRE-MARKET CHECKLIST (Complete by 9:00 AM ET)

### Infrastructure
- [ ] Dashboard API running: `curl -s http://localhost:5001/api/account | python3 -c "import json,sys; print(json.load(sys.stdin).get('equity'))"`
- [ ] Frontend accessible: `curl -s http://localhost:8000 | head -3`
- [ ] ARKA engine running: `ps aux | grep arka_engine | grep -v grep`
- [ ] Flow monitor running: `ps aux | grep flow_monitor | grep -v grep`
- [ ] No stale equity positions: `curl -s http://localhost:5001/api/arka/positions | python3 -c "import json,sys; p=json.load(sys.stdin)['positions']; [print('⚠️ EQUITY:',x['ticker']) for x in p if x.get('asset_class')=='us_equity']"`

### Data
- [ ] ARJUN signals generated: `ls -la logs/signals/signals_$(date +%Y-%m-%d).json`
- [ ] Swings watchlist fresh: `ls -la logs/chakra/watchlist_latest.json`
- [ ] GEX logs directory exists: `ls logs/gex/`
- [ ] Flow signals cache exists: `ls -la logs/chakra/flow_signals_latest.json`

### Account
- [ ] Alpaca paper account accessible
- [ ] Options buying power > $5,000: check sidebar in dashboard
- [ ] No stuck open orders from Friday

### Models
- [ ] Fakeout model loaded (check ARKA log): `grep -i "model.*loaded\|fakeout" logs/arka/arka_engine.log | tail -3`
- [ ] Conviction threshold correct (55 normal, 45 power hour)

---

## KNOWN ISSUES — STATUS BOARD

| Issue | Status | Fix Location |
|-------|--------|-------------|
| Swing screener scores all 60 | ⏳ Fix after first Monday scan | `arka_swings.py` score=50 base |
| GEX Expiration Breakdown empty | ⏳ Will work during market hours | Polygon data available 9:30am+ |
| GEX Term Structure sparse | ⏳ Will show 15-25 bars during hours | Same — Polygon data |
| GEX Intraday Timeline | ❌ Needs Phase 4 build | `gex_calculator.py` + new endpoint |
| Discord swing extreme after hours | ❌ Needs fix in flow_monitor.py | Check all webhook posting paths |
| Performance page display | ⏳ Data correct, UI needs polish | `arka.js` loadPerformance() |
| Dashboard home redesign | ❌ Pending — biggest effort | New George-style layout |
| GEX Range Bound Levels | ❌ Needs Phase 5 build | `analysis.js` GEX tab |
| GEX Gate (wall blocking) | ❌ Needs Phase 1-3 build | New `gex_state.py` + `gex_gate.py` |

---

## QUICK FIXES — RUN THESE MONDAY MORNING

### Fix 1: Create missing log directories
```bash
cd ~/trading-ai
mkdir -p logs/gex logs/arka logs/chakra logs/signals logs/internals logs/swings
echo "✅ Directories created"
```

### Fix 2: Clear stale ARKA state from Friday
```bash
python3 << 'PYEOF'
import json
from datetime import date
from pathlib import Path

path = f"logs/arka/summary_{date.today()}.json"
# Only clear if exists and has old equity positions
if Path(path).exists():
    d = json.loads(Path(path).read_text())
    open_pos = d.get("open_positions", {})
    if open_pos:
        print(f"Found {len(open_pos)} stale positions: {list(open_pos.keys())}")
        d["open_positions"] = {}
        d["trades"] = 0
        json.dump(d, open(path, 'w'), indent=2)
        print("✅ ARKA state cleared")
    else:
        print("✅ ARKA state clean — nothing to clear")
else:
    print("✅ No state file yet — clean start")
PYEOF
```

### Fix 3: Verify Discord webhooks are working
```bash
python3 << 'PYEOF'
import os, requests
from dotenv import load_dotenv
load_dotenv()

webhooks = {
    "TRADES": os.getenv("DISCORD_TRADES_WEBHOOK", ""),
    "SCALP_EXTREME": os.getenv("DISCORD_ARKA_SCALP_EXTREME", "") or os.getenv("DISCORD_FLOW_EXTREME", ""),
    "SWINGS_SIGNALS": os.getenv("DISCORD_ARKA_SWINGS_SIGNALS", "") or os.getenv("DISCORD_FLOW_SIGNALS", ""),
    "APP_HEALTH": os.getenv("DISCORD_APP_HEALTH", ""),
}

for name, url in webhooks.items():
    if url:
        print(f"✅ {name}: configured ({url[:40]}...)")
    else:
        print(f"⚠️  {name}: NOT configured in .env")
PYEOF
```

### Fix 4: Verify Alpaca account is clean
```bash
curl -s "https://paper-api.alpaca.markets/v2/positions" \
  -H "APCA-API-KEY-ID: $(grep ALPACA_API_KEY .env | cut -d= -f2)" \
  -H "APCA-API-SECRET-KEY: $(grep ALPACA_SECRET_KEY .env | cut -d= -f2 | head -1)" | \
  python3 -c "
import json,sys
pos = json.load(sys.stdin)
if isinstance(pos, list) and len(pos) == 0:
    print('✅ No open positions — clean start')
elif isinstance(pos, list):
    for p in pos:
        print(f'  {p.get(\"symbol\")} qty={p.get(\"qty\")} asset={p.get(\"asset_class\")}')
else:
    print('API error:', pos)
"
```

### Fix 5: Close any expired/worthless options
```bash
# Only run this if there are open positions with today's or past expiry
python3 << 'PYEOF'
import httpx, os, re
from datetime import date
from dotenv import load_dotenv
load_dotenv()

headers = {
    "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY",""),
    "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET","") or os.getenv("ALPACA_SECRET_KEY",""),
}

r = httpx.get("https://paper-api.alpaca.markets/v2/positions", headers=headers, timeout=8)
positions = r.json() if r.status_code == 200 else []
today = date.today().strftime("%y%m%d")

expired = []
for p in positions:
    sym = p.get("symbol","")
    m = re.match(r"[A-Z]+(\d{6})[CP]\d+", sym)
    if m and m.group(1) <= today:
        expired.append(sym)

if expired:
    print(f"Found {len(expired)} expired/expiring positions:")
    for s in expired:
        print(f"  {s}")
    confirm = input("Close all? (y/n): ")
    if confirm.lower() == 'y':
        r = httpx.delete("https://paper-api.alpaca.markets/v2/positions",
                        headers=headers, timeout=10)
        print("Closed:", r.status_code)
else:
    print("✅ No expired positions to close")
PYEOF
```

---

## DURING MARKET HOURS — WATCH FOR THESE

### Green signals (things working correctly):
```
✅ ARKA ENGINE: TRADE — SPY PUT @ $XXX (conviction=67, fakeout=0.32)
✅ GEX Gate: Negative gamma + below zero: dealers amplifying downside +10
✅ Flow Monitor: SPY BEARISH EXTREME — dark_pool_pct=82%
✅ Position closed: SPY PUT +15.3% profit
```

### Red signals (things to investigate):
```
❌ ARKA: max concurrent positions (3) — normal, means 3 trades open
⚠️  GEX GATE BLOCKED SPY CALL: Call wall $XXX only 0.3% away — GOOD, working correctly
❌ Module not found: backend.arka.gex_gate — GEX gate not yet built, install Monday
❌ Options contract not found for SPY — rare, means no liquid contracts near ATM
```

---

## EOD CHECKLIST (After 4:00 PM ET)

```bash
cd ~/trading-ai

# 1. Check daily performance
curl -s http://localhost:5001/api/account/live-pnl | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'Daily P&L: \${d.get(\"daily_pnl\",0):.2f}')
print(f'Equity: \${d.get(\"equity\",0):.2f}')
print(f'Positions: {d.get(\"positions_count\",0)}')
"

# 2. Run postmarket swing scan
python3 backend/arka/arka_swings.py --postmarket 2>&1 | tail -10

# 3. Check for stuck positions
curl -s http://localhost:5001/api/arka/positions | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in d.get('positions',[]):
    print(f'OPEN: {p[\"ticker\"]} {p[\"type\"]} pnl=\${p[\"pnl\"]}')
"

# 4. Review today's ARKA trades
curl -s http://localhost:5001/api/arka/summary | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'Trades: {d.get(\"trades\",0)} | PnL: \${d.get(\"daily_pnl\",0):.2f}')
for t in d.get('trade_log',[]):
    print(f'  {t[\"time\"]} {t[\"ticker\"]} {t[\"side\"]} @\${t[\"price\"]} pnl={t.get(\"pnl\")}')
"

# 5. Stop non-essential processes (keep API running)
pkill -f "arka_engine" 2>/dev/null
pkill -f "flow_monitor" 2>/dev/null
echo "✅ EOD complete — API still running for dashboard"
```

---

## EMERGENCY PROCEDURES

### Kill everything and restart clean:
```bash
cd ~/trading-ai
pkill -f "uvicorn|arka_engine|flow_monitor" 2>/dev/null
sleep 3
nohup venv/bin/uvicorn backend.dashboard_api:app --host 0.0.0.0 --port 5001 > logs/dashboard_api.log 2>&1 &
nohup python3 -m backend.arka.arka_engine > logs/arka/arka_engine.log 2>&1 &
nohup python3 backend/chakra/flow_monitor.py --watch > logs/chakra/flow_monitor.log 2>&1 &
echo "✅ All systems restarted"
```

### Close all Alpaca positions (emergency only):
```bash
curl -s -X DELETE "https://paper-api.alpaca.markets/v2/positions" \
  -H "APCA-API-KEY-ID: $(grep ALPACA_API_KEY .env | cut -d= -f2)" \
  -H "APCA-API-SECRET-KEY: $(grep ALPACA_SECRET_KEY .env | cut -d= -f2 | head -1)"
echo "⚠️ All positions closed"
```

### Check what's using port 5001:
```bash
lsof -i :5001 | grep LISTEN
```

---

*CHAKRA Production Checklist v1.0*
*Last updated: March 28, 2026*
