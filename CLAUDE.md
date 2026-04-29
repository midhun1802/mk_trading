# CHAKRA — Neural Trading OS
## Claude Code Session Guide

---

## STACK
- Backend: FastAPI + Uvicorn on port 5001 (`backend/dashboard_api.py`)
- Frontend: Static files on port 8000 (`frontend/dashboard.html`)
- Market data: Polygon.io | Execution: Alpaca Paper Trading
- Python 3.11, virtualenv at `~/trading-ai/venv/`
- Paper account: PA34OLZI1DZM ($4K equity, Epoch 3 — fresh start Apr 21 2026)

## START COMMANDS
```bash
# Dashboard API
nohup venv/bin/uvicorn backend.dashboard_api:app --host 0.0.0.0 --port 5001 > logs/dashboard_api.log 2>&1 &

# ARKA Scalper Engine
nohup python3 -m backend.arka.arka_engine > logs/arka/arka_engine.log 2>&1 &

# Flow Monitor
nohup python3 backend/chakra/flow_monitor.py --watch > logs/chakra/flow_monitor.log 2>&1 &

# Swings premarket scan (run at 8:15am ET)
python3 backend/arka/arka_swings.py --premarket

# Flow Scalper (pure institutional flow execution — run alongside ARKA engine)
nohup python3 -m backend.arka.flow_scalper > logs/arka/flow_scalper.log 2>&1 &
```

## KEY FILES
| File | Role |
|------|------|
| `frontend/js/arjun.js` | ARJUN signals tab — George-style signal cards |
| `frontend/js/arka.js` | Trading tab — swing cards, positions |
| `frontend/js/analysis.js` | Analysis tab — **OFF-LIMITS, working well, do not touch** |
| `frontend/js/core.js` | Tab system, live prices, market status |
| `frontend/js/system.js` | System tab |
| `backend/dashboard_api.py` | All API endpoints |
| `backend/arka/arka_engine.py` | ARKA scalper — options only, 1-3 contracts |
| `backend/arka/flow_scalper.py` | Flow scalper — pure institutional flow execution |
| `backend/arka/arka_swings.py` | ARKA swings screener + execution |
| `backend/arka/arka_discord_notifier.py` | Entry/exit Discord alerts |
| `backend/chakra/flow_monitor.py` | Options flow + dark pool scanner |
| `backend/internals/market_internals.py` | Market internals (Discord disabled) |
| `backend/arjun/agents/gex_calculator.py` | GEX computation |
| `logs/chakra/flow_signals_latest.json` | Flow signals cache (ARKA reads this) |
| `logs/chakra/watchlist_latest.json` | Swing screener candidates |
| `logs/gex/` | GEX state files (create if missing: mkdir -p logs/gex) |

## HARD RULES — NEVER VIOLATE
- **NEVER touch `frontend/js/analysis.js`** — it works, leave it alone
- ARKA buys OPTIONS ONLY — no equity, no inverse ETFs
- Max 3 contracts per ARKA scalp trade, 1 contract for swings
- After-hours Discord: only SPY/QQQ/SPX allowed (9:30am-4pm ET gate)
- Use `python3 << 'PYEOF' ... PYEOF` heredoc style for all Python patches
- Always check `node --check frontend/js/*.js` after editing JS files

---

## PENDING ITEMS — DO THESE IN ORDER

### PRIORITY 1: GEX Gate (highest trading impact — do Monday morning)
Full implementation guide is in `GEX_Implementation_Guide.docx`.
Creates a GEX-aware conviction filter that blocks trades against dealer walls.

**Phase 1 — Create `backend/arka/gex_state.py`:**
- `load_gex_state(ticker)` — loads `logs/gex/gex_latest_{ticker}.json`, enforces 10-min TTL
- Returns: regime, zero_gamma, call_wall, put_wall, net_gex, spot, pct_to_call_wall, pct_to_put_wall, above_zero_gamma, cliff_today
- Returns None if file missing or stale

**Phase 2 — Create `backend/arka/gex_gate.py`:**
6 rules:
1. Block CALLs within 0.4% of call wall
2. Block PUTs within 0.4% of put wall
3. Penalize -12 conviction if within 1.0% of respective wall
4. Penalize -8 in POSITIVE_GAMMA when chasing momentum
5. Boost +10 in NEGATIVE_GAMMA when direction aligned
6. Boost +8 when within $1.50 of zero gamma (explosive zone)
7. Boost +6 when GEX cliff expiring today

**Phase 3 — Wire into `arka_engine.py`:**
- After conviction calculation, before order placement
- If gate blocks: log reason, add to scan feed, skip signal
- If gate adjusts: update conviction, log adjustment

**Phase 4 — Smart DTE selection:**
- `select_optimal_dte(ticker, direction)` in arka_engine.py
- 0DTE negative gamma → prefer 0DTE (fast mover)
- 0DTE positive gamma → prefer 1DTE (avoid pinning)

**Phase 5 — GEX state writer:**
- After each GEX compute in `gex_calculator.py`, write `logs/gex/gex_latest_{ticker}.json`
- Include: regime, zero_gamma, call_wall, put_wall, net_gex, spot, cliff data, ts (unix timestamp)

### PRIORITY 2: GEX Intraday Timeline Logging
- Add `snapshot_gex_intraday(gex_result, ticker)` to `gex_calculator.py`
- Appends each GEX computation to `logs/gex/gex_intraday_{ticker}_{date}.json`
- Add API endpoint: `GET /api/options/gex/intraday?ticker=SPY`
- Returns array: [{ts, datetime, zero_gamma, call_wall, put_wall, net_gex, regime, spot}]
- This fixes the Intraday GEX Timeline tab in Analysis → GEX

### PRIORITY 3: GEX Tab Dashboard Fixes
**Expiration Breakdown:**
- Currently shows "0 expirations" after market close
- After-hours fix: use tomorrow as exp_start (already partially patched)
- The real fix: endpoint at `/api/options/gex/expiry-breakdown` needs to work with Polygon snapshot data during market hours — test Monday

**Term Structure:**
- Already rendering correctly with George-style bars
- Cliff detection working
- Will show full data (15-25 bars) during market hours Monday

**Range Bound Levels (GEX tab — new section):**
Add prominent range display at top of GEX tab showing:
- Call Wall: $XXX.XX (X.XX% away) — green
- Zero Gamma: $XXX.XX (ABOVE/BELOW) — yellow
- Put Wall: $XXX.XX (X.XX% away) — red  
- GEX Regime badge (POSITIVE/NEGATIVE/LOW_VOL)
- Cliff alert banner when cliff_today == true

### PRIORITY 4: Discord After-Hours Fix
**Issue:** Swing extreme Discord alerts still firing after market close
**Location:** `backend/chakra/flow_monitor.py`
**Fix needed:** The `post_dark_pool_alert` and `post_uoa_alert` after-hours gate exists but may not cover all posting paths. Check every place that calls a Discord webhook in flow_monitor.py and ensure the market hours gate is applied.
**Gate logic:**
```python
from zoneinfo import ZoneInfo
_et = datetime.now(ZoneInfo("America/New_York"))
_market_open = (_et.weekday() < 5 and
                ((_et.hour == 9 and _et.minute >= 30) or _et.hour > 9) and
                _et.hour < 16)
_ALWAYS_ON = {"SPY", "QQQ", "SPX"}
if not _market_open and ticker.upper() not in _ALWAYS_ON:
    return False
```

### PRIORITY 5: Conviction Score Fix (Swing Screener)
**Issue:** All swing candidates show score=60 (the minimum)
**Root cause:** Scoring was rebalanced to start at 50, but the watchlist JSON was saved before the fix. The new scoring takes effect Monday after `--premarket` scan runs.
**Verify Monday morning:** After premarket scan, check `logs/chakra/watchlist_latest.json` — scores should range from 50-95, not all 60.
**If still all 60:** Check `backend/arka/arka_swings.py` around line 300 — `score = 50` should be the starting point, not `score = 0`.

### PRIORITY 6: Performance Page Display Update
**Issue:** Performance page shows raw trade data but lacks:
- Cumulative P&L chart over time
- Win/loss breakdown by ticker
- Best/worst trades list
- Daily P&L bar chart
**Location:** `frontend/js/arka.js` — `loadPerformance()` function
**Data source:** `GET /api/arka/performance` (already fixed to pull from Alpaca)
**Goal:** George-style performance cards with charts

### PRIORITY 7: Dashboard Home — George-Style Redesign
**This is the biggest effort — do last**
**Goal:** Redesign ARJUN → Signals tab to match George's layout:
- Top row: Market regime bar (SPX price, VIX, regime label)
- Index cards row: SPX, SPY, QQQ, IWM, DIA with mini sparklines
- Below: ARJUN signal cards in grid (already George-style, mostly done)
- Right sidebar: CHAKRA Signals feed (already exists)
**Reference:** George dashboard screenshots in previous chat sessions

---

## ARCHITECTURE QUICK REFERENCE

### Signal Card Types (arjun.js)
- `_buildGeorgeCard(sig, px)` — Full ARJUN signal (has all 13 sections)
- `_buildIndexCard(ticker, px, analyze)` — SPX/RUT/SPY/QQQ/IWM/DIA stub
- `_buildEnrichedStubCard(ticker, px, analyze, tier)` — Other tickers

### ARKA Engine Flow
```
Every 60s scan:
  1. Fetch 1-min bars (Polygon)
  2. Compute 50+ technical features
  3. Flow conviction (80%) + ARJUN ML (10%) + Technicals (10%)
  4. [NEW] GEX Gate — block/adjust based on walls/regime
  5. XGBoost fakeout filter
  6. If conviction >= 55 and not fakeout: find ATM options contract
  7. Place order: 1-3 contracts, 0-2 DTE
  8. Monitor: stop -30%, target +10%, EOD close 3:58pm ET
```

### Discord Channel Routing
- SPX → `#arka-spx-only`
- Index extreme → `#arka-scalp-extreme`  
- Index standard → `#arka-scalp-signals`
- Stock extreme → `#arka-swings-extreme`
- Stock standard → `#arka-swings-signals`
- ARKA trades → `#chakra-trades`
- After hours: only SPY/QQQ/SPX allowed

### Key API Endpoints
- `GET /api/account/live-pnl` — Live Alpaca P&L (10s refresh)
- `GET /api/arka/positions` — Live Alpaca positions
- `GET /api/arka/performance` — Closed orders + FIFO P&L
- `GET /api/swings/watchlist` — Swing screener candidates
- `POST /api/swings/manual-entry` — Manual options order
- `GET /api/options/gex/expiry-breakdown` — Per-expiry GEX
- `GET /api/options/gex/intraday` — Intraday GEX timeline [TO BUILD]

---

## GEX LOGS DIRECTORY
```bash
mkdir -p ~/trading-ai/logs/gex
```
Files written here:
- `gex_latest_{ticker}.json` — Current GEX state (written every GEX compute)
- `gex_intraday_{ticker}_{date}.json` — Intraday snapshots array
- `gex_term_structure_{ticker}.json` — Per-expiry breakdown

---

## TESTING CHECKLIST (after any JS edit)
```bash
node --check frontend/js/arka.js
node --check frontend/js/arjun.js
node --check frontend/js/core.js
# Then hard refresh: Cmd+Shift+R in browser
```

## TESTING CHECKLIST (after any Python edit)
```bash
python3 -c "import ast; ast.parse(open('backend/dashboard_api.py').read()); print('OK')"
python3 -c "import ast; ast.parse(open('backend/arka/arka_engine.py').read()); print('OK')"
```

---

*CHAKRA v3 — Neural Trading OS*
*Last updated: March 28, 2026*

---

## GEX ENHANCEMENT — PHASE 7 (George Video Insights)

Full implementation details in `GEX_COMPLETE_GUIDE.md`.

### Phase 7A — CRITICAL (Do immediately after Phase 1-3)
Add to `gex_calculator.py`:
- `get_regime_call()` → "SHORT_THE_POPS" / "BUY_THE_DIPS" / "FOLLOW_MOMENTUM"
- `compute_directional_exposure()` → call_gex_dollars, put_gex_dollars, bias_ratio
- Wire regime_call: against-regime penalty = -20 conviction
- Wire bias_ratio: >3.0 = HARD BLOCK in gex_gate.py
- Dashboard: prominent regime banner at top of GEX tab

### Phase 7B — HIGH
Add to `gex_calculator.py`:
- `compute_acceleration()` → accel_up, accel_down scores
- `compute_expected_move()` → upper_1sd, lower_1sd bounds
- ARKA: never select 0DTE strikes outside 1SD range
- ARKA: +10 conviction when acceleration >15 aligns with direction

### Phase 7C — MEDIUM (Polish)
- `find_pin_strikes()` in gex_calculator.py
- `/api/options/gex/heatmap` endpoint in dashboard_api.py
- Per-ticker GEX for NVDA/TSLA/AMZN/AAPL/MSFT/META/GOOGL/AMD/COIN/NFLX
- Wire per-ticker GEX into `arka_swings.py` scoring

## GEX LOGS DIRECTORY
```bash
mkdir -p ~/trading-ai/logs/gex
```
Files written here:
- `gex_latest_{ticker}.json` — Current GEX state (written after every compute)
- `gex_intraday_{ticker}_{date}.json` — Intraday snapshots array
- `gex_term_structure_{ticker}.json` — Per-expiry breakdown

## IMPLEMENTATION ORDER FOR MONDAY
1. `mkdir -p logs/gex` — create directory
2. Create `backend/arka/gex_state.py` (Phase 1)
3. Create `backend/arka/gex_gate.py` (Phase 2)
4. Wire gate into `arka_engine.py` (Phase 3)
5. Add `write_gex_state()` + George functions to `gex_calculator.py` (Phase 7A)
6. Start engines and verify gate logs appear in arka_engine.log
7. Continue with 7B, 4, 5, 7C in order

