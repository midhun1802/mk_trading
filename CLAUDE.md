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

### ✅ DONE: GEX Gate (Phases 1–5 + 7A complete)
- `backend/arka/gex_state.py` — loads gex_latest_{ticker}.json with 10-min TTL
- `backend/arka/gex_gate.py` — 6-rule conviction filter (walls, regime, zero gamma, cliff, bias ratio)
- Wired into `arka_engine.py` after conviction, before order placement
- Smart DTE: NEGATIVE_GAMMA → 0DTE index / 1DTE stocks; POSITIVE_GAMMA → 1DTE
- GEX state writer in `gex_calculator.py` after each compute
- Phase 7A: regime_call (SHORT_THE_POPS/BUY_THE_DIPS/FOLLOW_MOMENTUM), directional bias ratio, acceleration, expected move

### ✅ DONE: RSI Divergence Scanner
- `backend/chakra/divergence_scanner.py` — runs every 5 min via flow_monitor
- Scans indexes + top 12 watchlist tickers for bullish/bearish/hidden divergence on 5-min bars
- Posts Discord embeds with 60-min per-ticker cooldown
- Wired into ARKA conviction scoring: aligned divergence +12–20pts, opposing -8pts

### ✅ DONE: Swing Gate Fixes (Apr 29 2026)
- OI gate: 100 → 25 for stocks (index-centric threshold was blocking all stock options)
- DTE: stocks always use 1DTE minimum (0DTE stock OI is near zero per-strike)
- Cost gate: fixed $4/share → 4% of stock price (scales with high-priced stocks like AMZN)
- Trade cap: $500 → $1,000 for stocks (uses swing budget pool, not 0DTE scalp budget)
- Flat gate: 0.30% → 0.15% SPY threshold; bypassed if flow signal confidence ≥ 80%

### ✅ DONE: PDT Alert System
- Discord alert fires when Alpaca returns 403 PDT on both entry and exit
- Alerts user to manually close position via Alpaca dashboard
- Stocks use 1DTE to avoid PDT (buy today, sell tomorrow = different days)

### PRIORITY 1: GEX Intraday Timeline Logging
- Add `snapshot_gex_intraday(gex_result, ticker)` to `gex_calculator.py`
- Appends each GEX computation to `logs/gex/gex_intraday_{ticker}_{date}.json`
- Add API endpoint: `GET /api/options/gex/intraday?ticker=SPY`
- Returns array: [{ts, datetime, zero_gamma, call_wall, put_wall, net_gex, regime, spot}]
- This fixes the Intraday GEX Timeline tab in Analysis → GEX

### PRIORITY 2: GEX Tab Range Bound Levels
Add prominent range display at top of GEX tab showing:
- Call Wall: $XXX.XX (X.XX% away) — green
- Zero Gamma: $XXX.XX (ABOVE/BELOW) — yellow
- Put Wall: $XXX.XX (X.XX% away) — red
- GEX Regime badge (POSITIVE/NEGATIVE/LOW_VOL)
- Cliff alert banner when cliff_today == true

### PRIORITY 3: Performance Page Display Update
**Issue:** Performance page shows raw trade data but lacks:
- Cumulative P&L chart over time
- Win/loss breakdown by ticker
- Best/worst trades list
- Daily P&L bar chart
**Location:** `frontend/js/arka.js` — `loadPerformance()` function
**Data source:** `GET /api/arka/performance` (already fixed to pull from Alpaca)
**Goal:** George-style performance cards with charts

### PRIORITY 4: Dashboard Home — George-Style Redesign
**This is the biggest effort — do last**
**Goal:** Redesign ARJUN → Signals tab to match George's layout:
- Top row: Market regime bar (SPX price, VIX, regime label)
- Index cards row: SPX, SPY, QQQ, IWM, DIA with mini sparklines
- Below: ARJUN signal cards in grid (already George-style, mostly done)
- Right sidebar: CHAKRA Signals feed (already exists)

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

