# CHAKRA — Neural Trading OS

An autonomous options trading system built around institutional flow analysis, GEX (Gamma Exposure) regime awareness, RSI divergence detection, and multi-agent AI deliberation. Trades SPY/QQQ/IWM 0-1DTE options via Alpaca paper trading.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CHAKRA Trading OS                            │
│                                                                     │
│  ┌────────────┐   ┌─────────────┐   ┌─────────────────────────┐   │
│  │  ARJUN     │   │   ARKA      │   │       CHAKRA            │   │
│  │  AI Agents │──▶│  Scalp Eng  │   │  Flow + Divergence      │   │
│  │  (Claude)  │   │  60s scans  │   │  Monitor (5 min)        │   │
│  └────────────┘   └─────────────┘   └─────────────────────────┘   │
│         │                │                      │                   │
│         ▼                ▼                      ▼                   │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │            GEX Gate (dealer wall awareness)              │      │
│  └──────────────────────────────────────────────────────────┘      │
│         │                │                      │                   │
│         └────────────────┴──────────────────────┘                  │
│                          │                                          │
│                 ┌─────────────────┐                                 │
│                 │  Alpaca Paper   │                                 │
│                 │  Trading API    │                                 │
│                 └─────────────────┘                                 │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Stack

| Layer | Technology |
|-------|-----------|
| Backend API | FastAPI + Uvicorn on port 5001 |
| Frontend | Static HTML/JS on port 8000 |
| Market Data | Polygon.io REST API |
| Execution | Alpaca Paper Trading API |
| AI Agents | Anthropic Claude (claude-sonnet-4-6) |
| ML Models | XGBoost (fakeout filter) |
| Python | 3.11, virtualenv at `~/trading-ai/venv/` |

---

## Quick Start

```bash
cd ~/trading-ai

# 1. Dashboard API (port 5001)
nohup venv/bin/uvicorn backend.dashboard_api:app --host 0.0.0.0 --port 5001 > logs/dashboard_api.log 2>&1 &

# 2. ARKA Scalper Engine (main trading engine)
nohup python3 -m backend.arka.arka_engine > logs/arka/arka_engine.log 2>&1 &

# 3. CHAKRA Flow Monitor (options flow + divergence scanner)
nohup python3 backend/chakra/flow_monitor.py --watch > logs/chakra/flow_monitor.log 2>&1 &

# 4. Flow Scalper (institutional flow execution)
nohup python3 -m backend.arka.flow_scalper > logs/arka/flow_scalper.log 2>&1 &

# 5. Premarket swing scan (run at 8:15 AM ET)
python3 backend/arka/arka_swings.py --premarket
```

---

## Module Reference

### Core Engines (always running)

---

#### `backend/dashboard_api.py`
**FastAPI backend — the nerve centre of the entire system.**

All browser dashboard data flows through this single file (~4,200 lines). Key responsibilities:
- Serves live Alpaca P&L, positions, and closed-order performance
- Proxies Polygon.io market data (prices, options snapshots, GEX)
- Exposes ARJUN signal analysis endpoints
- Hosts the Heat Seeker scan endpoint (`/api/heatseeker/scan`)
- Serves the swing watchlist and manual entry endpoints
- Handles contract picker requests (async parallel Alpaca + Polygon calls)

Key endpoints:
| Endpoint | Description |
|---------|-------------|
| `GET /api/account/live-pnl` | Live equity, daily P&L, open positions (10s refresh) |
| `GET /api/arka/positions` | Live Alpaca options positions |
| `GET /api/arka/performance` | Closed orders + FIFO P&L calculation |
| `GET /api/flow/signals` | Latest flow monitor signals from cache |
| `GET /api/swings/watchlist` | Swing screener candidates |
| `POST /api/swings/manual-entry` | Manual options order placement |
| `GET /api/options/gex/expiry-breakdown` | Per-expiry GEX breakdown |
| `GET /api/options/contracts/picker` | Smart contract picker (ATM/OTM, async) |
| `GET /api/heatseeker/scan` | Heat Seeker momentum scan |
| `GET /api/signals` | ARJUN multi-agent signal analysis |

---

#### `backend/arka/arka_engine.py`
**The main autonomous trading engine — the heart of the system.**

Runs a 60-second scan loop during market hours (9:30 AM–3:58 PM ET). On each cycle:

1. **Position monitoring** — checks open positions every 15s for stop (-20%) and take profit (+40%) triggers, trailing stop above +50%
2. **VIX spike abort** — pauses all entries for 15 min if VIX spikes >2 points in 5 min
3. **SPY change fetch** — fetches intraday SPY % change via Polygon `/prev` endpoint
4. **Lotto engine** (3:00–3:57 PM) — power hour momentum plays, runs before flat gate
5. **Flat market gate** — skips scan if SPY move < 0.15% AND no strong flow signal (bypasses truly flat days where 0DTE theta kills premiums)
6. **Dynamic universe** — refreshes ticker list every 5 min from flow signals + swing watchlist
7. **Feature engineering** — for each ticker: 1-min bars → 50+ technical indicators (RSI, MACD, VWAP, EMA, ORB, Hurst exponent, DEX, entropy, iceberg detection)
8. **Conviction scoring** (0–100):
   - Flow signal: 80% weight (from `flow_signals_latest.json`)
   - ARJUN ML (XGBoost): 10% weight
   - Technical indicators: 10% weight
   - RSI divergence boost: +12 to +20 if aligned, -8 if opposing
   - GEX gate: adjustments and hard blocks
9. **GEX gate** — blocks/penalizes trades based on dealer walls and gamma regime
10. **XGBoost fakeout filter** — blocks if fakeout probability > 0.75
11. **Contract selection** — finds ATM/OTM options via Alpaca, verifies premium via Polygon
12. **Order placement** — 1–3 contracts, 0DTE or 1DTE
13. **PDT protection** — detects and alerts when Alpaca PDT rule blocks orders

Trade flow summary:
```
Bar fetch → Features → Conviction score → GEX gate → Fakeout filter → Contract picker → Order
```

Conviction threshold: 45 (normal), 72 (QQQ), adjusts dynamically based on session/regime.

---

#### `backend/chakra/flow_monitor.py`
**Options flow and dark pool scanner — the primary signal source for ARKA.**

Runs every 5 minutes. For each ticker in the watchlist:

1. **Dark pool scan** — fetches recent tape trades from Polygon, identifies prints >$500K with dark pool characteristics (trade size, off-exchange ratio)
2. **UOA (Unusual Options Activity)** — scans options snapshot for contracts with volume/OI ratio >3x, flags extreme sweeps
3. **Flow signal writing** — writes `logs/chakra/flow_signals_latest.json` which ARKA reads for its 80% flow weight
4. **Divergence scanner** (every 5 min) — calls `divergence_scanner.run_divergence_scan()` for RSI divergence alerts

Discord routing:
- Index extreme: `#arka-scalp-extreme`
- Index standard: `#arka-scalp-signals`
- Stock extreme: `#arka-swings-extreme`
- Stock standard: `#arka-swings-signals`
- After-hours gate: only SPY/QQQ/SPX posted outside 9:30 AM–4:00 PM ET

---

#### `backend/arka/flow_scalper.py`
**Pure institutional flow execution engine — independent of ARKA's conviction system.**

Runs a 30-second scan loop. Triggers an immediate trade when:
- Flow signal confidence = 100% (unconditional entry), OR
- Extreme flow signal ≥ 85%, OR
- Volume ratio ≥ 500x normal

Uses a simpler decision tree than ARKA — designed for fast reaction to very high-confidence flow signals. Has its own position management (TP +20%, SL -20%), max 2 simultaneous positions.

Strike selection: ATM floor for calls (≥spot), ATM ceiling for puts (≤spot) — prevents ITM entries.

---

### GEX (Gamma Exposure) System

---

#### `backend/arjun/agents/gex_calculator.py`
**Computes Gamma Exposure from live options chain data.**

Fetches the full options snapshot from Polygon for a ticker, then:
- Calculates net GEX per strike (calls add positive gamma, puts add negative)
- Identifies call wall (largest positive GEX strike), put wall (largest negative GEX strike)
- Identifies zero gamma level (where net GEX crosses zero)
- Determines regime: `POSITIVE_GAMMA` (range-bound), `NEGATIVE_GAMMA` (trending/volatile), `LOW_VOL`
- Detects cliff expiry (large GEX expiring today → volatility expansion expected)
- Computes `get_regime_call()`: `SHORT_THE_POPS` / `BUY_THE_DIPS` / `FOLLOW_MOMENTUM`
- Computes `compute_directional_exposure()`: call/put GEX dollar bias ratio
- Computes `compute_expected_move()`: ±1SD bounds from implied volatility
- Writes state to `logs/gex/gex_latest_{ticker}.json` after each computation

---

#### `backend/arka/gex_state.py`
**Loads and caches GEX state files with a 10-minute TTL.**

Reads `logs/gex/gex_latest_{ticker}.json`. Returns None if file is missing or older than 10 minutes. Provides: regime, zero_gamma, call_wall, put_wall, net_gex, spot, pct_to_call_wall, pct_to_put_wall, above_zero_gamma, cliff_today.

---

#### `backend/arka/gex_gate.py`
**Applies GEX-aware conviction filters before order placement.**

Six rules:
1. **Hard block** — CALL within 0.4% of call wall → block
2. **Hard block** — PUT within 0.4% of put wall → block
3. **Wall penalty** — within 1.0% of respective wall → -12 conviction
4. **Positive gamma penalty** — chasing momentum in POSITIVE_GAMMA → -8 conviction
5. **Negative gamma boost** — direction aligned in NEGATIVE_GAMMA → +10 conviction
6. **Zero gamma boost** — within $1.50 of zero gamma (explosive zone) → +8 conviction
7. **Cliff boost** — GEX cliff expiring today → +6 conviction
8. **Bias ratio hard block** — call/put GEX ratio >3.0 against direction → HARD BLOCK
9. **Regime penalty** — against-regime direction (SHORT_THE_POPS on CALL) → -20 conviction

---

### ARJUN AI Agents

---

#### `backend/arjun/agents/coordinator.py`
**Orchestrates the multi-agent deliberation for each signal.**

When ARKA's Heat Seeker scores a ticker highly, the coordinator runs:
1. **Analyst Agent** — fundamental + technical setup analysis
2. **Bull Agent** — argues for a bullish position
3. **Bear Agent** — argues for a bearish position
4. **Risk Manager** — evaluates position size, timing, macro context

Each agent calls Claude API. The coordinator synthesizes a final recommendation with direction, conviction boost/penalty, and reasoning. Result feeds into ARKA's conviction score.

---

#### `backend/arjun/agents/analyst_agent.py`
**Technical and fundamental analysis agent.**

Runs technical indicator analysis, identifies key levels (support/resistance, VWAP, ORB), and evaluates macro context. Outputs a structured analysis used by bull/bear agents.

---

#### `backend/arjun/agents/bull_agent.py` / `bear_agent.py`
**Adversarial agents that debate the trade.**

Bull agent argues why the setup is a buy. Bear agent argues why it's a fade or short. The coordinator uses both arguments to produce a balanced recommendation.

---

### Supporting ARKA Modules

---

#### `backend/arka/heat_seeker.py`
**Momentum and unusual activity scanner — the pre-filter for ARJUN deliberation.**

Scores each ticker 0–100 based on:
- Volume surge (relative to 20-day average)
- Options flow conviction from flow signals cache
- Price momentum (% change, proximity to HOD/LOD)
- Iceberg order detection

If score ≥ 80, triggers ARJUN multi-agent deliberation. Results cached for 5 minutes.

---

#### `backend/arka/lotto_engine.py`
**Power hour momentum play engine (3:00–3:57 PM ET).**

Runs during the final hour of trading, separate from ARKA's main conviction system. Looks for GEX-confirmed momentum setups in SPY/QQQ, uses GEX state from `logs/gex/gex_latest_SPY.json`. Targets 0DTE options for fast moves into close.

---

#### `backend/arka/arka_swings.py`
**Premarket swing screener and execution engine.**

Run at 8:15 AM ET. Scans a watchlist of 50+ tickers for multi-day swing setups using:
- Relative strength vs SPY
- Volume patterns (accumulation, dry-up)
- Technical structure (higher highs, support holds, sector alignment)
- GEX context (regime, wall distances)

Outputs `logs/chakra/watchlist_latest.json` with scored candidates. Top candidates are added to ARKA's scan universe for the day. Also supports direct swing options order placement.

---

#### `backend/arka/discord_notifier.py`
**All Discord notifications for the ARKA system.**

Handles entry alerts, exit alerts, position updates, system alerts, and EOD summaries. Routes to different Discord channels based on instrument type (SPX vs index vs stock) and signal strength (extreme vs standard). All notifications are async.

---

#### `backend/arka/dynamic_universe.py`
**Builds the live ticker scan universe every 5 minutes.**

Combines: static core tickers (SPY, QQQ, IWM) + top swing watchlist candidates + active flow signal tickers + today's movers from market internals. ARKA scans this list each cycle instead of a fixed hardcoded list.

---

#### `backend/arka/order_guard.py`
**Pre-flight options order validation.**

Enforces: options-only (no equity), max 3 contracts per scalp, 1 contract per swing, valid symbol format. Blocks any non-options order before it reaches Alpaca.

---

#### `backend/arka/heat_seeker_bridge.py`
**Bridge between Heat Seeker and ARKA's main scan loop.**

Runs a background async loop to refresh Heat Seeker scores every 5 minutes and push them into ARKA's signal queue. Prevents ARJUN deliberation from blocking the main 60-second scan.

---

### CHAKRA Modules

---

#### `backend/chakra/divergence_scanner.py`
**Intraday RSI divergence detector — runs every 5 minutes.**

For each ticker (indexes + top 12 watchlist tickers):
1. Fetches last 40 five-minute bars from Polygon
2. Computes Wilder RSI series (14-period)
3. Calls `rsi_divergence.detect_rsi_divergence()` to find bullish, bearish, hidden bull, hidden bear patterns
4. Posts Discord embed when a new divergence forms (60-min per-ticker cooldown)
5. Results also feed into ARKA's conviction scoring (+12 to +20 if aligned with signal direction)

Market hours only (9:30 AM–4:00 PM ET weekdays).

---

#### `backend/chakra/modules/rsi_divergence.py`
**RSI divergence detection and scoring logic.**

`detect_rsi_divergence(closes, rsi_vals, lookback=14)` — finds swing highs/lows in both price and RSI over the lookback window and identifies:
- **Bullish divergence**: price makes lower low, RSI makes higher low (momentum building)
- **Bearish divergence**: price makes higher high, RSI makes lower high (momentum fading)
- **Hidden bullish**: price higher low, RSI lower low (trend continuation)
- **Hidden bearish**: price lower high, RSI higher high (trend continuation)

`score_divergence(div)` — returns a conviction point value (12–20) and direction (CALL/PUT).

---

#### `backend/chakra/modules/dex_calculator.py`
**Delta Exposure (DEX) computation.**

Computes net delta exposure across the options chain — the aggregate directional bias of market makers' hedging activity. High positive DEX = dealers long delta (bullish hedging), high negative = dealers short delta (bearish hedging). Used as a conviction component in ARKA.

---

#### `backend/chakra/modules/entropy_engine.py`
**Shannon entropy-based volatility regime detector.**

Measures price bar entropy (randomness vs directional structure). Low entropy = trending (directional), high entropy = random/choppy. Used to scale position size: directional regime gets 1.2x size, choppy gets 0.8x.

---

#### `backend/chakra/modules/hurst_engine.py`
**Hurst exponent calculator for trend persistence detection.**

H > 0.55 = trending (persistent), H < 0.45 = mean-reverting, H ≈ 0.5 = random walk. Used in ARKA to detect regime and adjust conviction: trending regime gets a boost for momentum trades, random regime raises the conviction threshold.

---

#### `backend/chakra/modules/iceberg_detector.py`
**Large hidden order (iceberg) detection from tape data.**

Fetches recent trades from Polygon, identifies clusters of same-direction prints at similar prices that suggest a large institutional order being worked in pieces. BULLISH icebergs boost CALL conviction, BEARISH icebergs boost PUT conviction.

---

#### `backend/chakra/modules/iv_skew.py`
**Implied volatility skew analysis.**

Measures the difference between put and call implied volatility at equidistant strikes from ATM. Negative skew (puts more expensive) = bearish hedging demand. Positive skew (calls more expensive) = bullish demand. Used as a sentiment confirmation in ARKA.

---

#### `backend/chakra/modules/vrp_engine.py`
**Volatility Risk Premium calculator.**

Computes the spread between implied volatility (from options chain) and realized volatility (from recent price bars). High VRP = IV is elevated relative to realized — options are "expensive." Used to filter against buying overpriced premium.

---

#### `backend/chakra/modules/prob_distribution.py`
**Probability distribution and tail risk estimator.**

Fits a distribution to recent returns, computes tail risk probability and expected move bounds. Used for position sizing and to avoid buying options where the expected payout doesn't cover the premium cost.

---

#### `backend/chakra/modules/hmm_regime.py`
**Hidden Markov Model regime classifier.**

Classifies market regime into bull/bear/sideways states using a 2 or 3-state HMM trained on recent returns and volatility. Used to inform ARKA's session bias and conviction adjustments.

---

#### `backend/chakra/sector_rotation.py`
**Sector ETF rotation tracker.**

Monitors XLK, XLF, XLE, XLV, XLI, XLY, XLP relative strength vs SPY. Detects which sectors are leading/lagging. Bearish rotation (defensive sectors outperforming) adds a bearish bias to conviction scoring.

---

#### `backend/internals/market_internals.py`
**Market internals tracker (advance/decline, TICK, breadth).**

Monitors NYSE A/D ratio, TICK extremes, and market breadth. Extreme negative breadth (-1000 TICK, heavy declines) adds conviction to PUT signals. Extreme positive breadth adds to CALL signals. Discord alerts disabled for noise reduction.

---

### Frontend

---

#### `frontend/dashboard.html`
**Main dashboard — single-page app.**

Five tabs: Trading, Signals (ARJUN), Analysis (GEX + charts), System, Heat Seeker.

---

#### `frontend/js/core.js`
**Tab system, live price ticker, market status bar.**

Handles tab switching, fetches live prices every 10 seconds from `/api/prices/live`, shows market open/closed status, drives the top status bar (SPY, QQQ, VIX, account equity).

---

#### `frontend/js/arka.js`
**Trading tab — positions, swing cards, performance.**

Shows open Alpaca positions with live P&L. Loads swing watchlist candidates as cards. Has manual entry form for options orders. Loads performance history with trade log.

---

#### `frontend/js/arjun.js`
**Signals tab — ARJUN signal cards in George-style layout.**

Renders multi-agent ARJUN analysis cards. Each card shows the bull/bear/risk analysis, conviction score, technical components, and flow signal. Index cards get a compact layout; full ARJUN signals get a detailed 13-section card.

---

#### `frontend/js/analysis.js`
**Analysis tab — GEX charts, options flow, sector rotation.**

**Do not modify** — working correctly. Handles GEX heatmap, term structure (bar chart per expiry), expiration breakdown, IV skew chart, sector rotation display.

---

#### `frontend/js/system.js`
**System tab — engine status, logs, health checks.**

Shows running status of all engines, last scan times, log tail output, Discord channel status.

---

#### `frontend/js/heat_seeker.js`
**Heat Seeker tab — momentum scanner.**

Shows Heat Seeker scores for all tickers in the dynamic universe. Cards show momentum score, volume surge, options flow, and ARJUN deliberation status.

---

### Utility / Reference

---

#### `backend/arka/eod_closer.py`
**End-of-day position closer.**

Called by ARKA engine at 3:58 PM ET to close all open options positions. Prevents theta decay overnight. Uses sell orders (not DELETE) for options reliability.

---

#### `backend/arka/feature_engineer.py`
**Technical feature computation for the XGBoost fakeout model.**

Computes the same 50+ features used during model training: RSI, MACD histogram, EMA stack, VWAP distance, ORB relationship, volume ratio, ATR. Used in ARKA to score each bar before conviction scoring.

---

#### `backend/arka/train_arka.py`
**XGBoost model training script.**

Trains the fakeout detection model on historical ARKA trade data. Run offline to retrain. Outputs `backend/arjun/models/arka_scalp_model.json`.

---

#### `backend/arka/weekly_postmortem.py`
**Weekly performance analysis and Discord summary.**

Run every Friday after close. Analyzes the week's trades: win rate, average P&L, best/worst setups, session breakdown (morning/lunch/power). Posts a structured Discord summary.

---

#### `backend/chakra/daily_briefing.py`
**Morning market briefing generator.**

Run at 8:00 AM ET. Compiles: overnight futures, sector premarket moves, key economic calendar events, GEX levels for SPY/QQQ, top swing candidates. Posts to Discord.

---

#### `backend/chakra/eod_summary.py`
**End-of-day performance summary.**

Posts a Discord recap after market close: trades taken, P&L, heat map of sectors, any notable flow signals that fired.

---

## Data Flow

```
Polygon.io  ──────────────────────────────────────────────────────┐
                                                                   │
  [Options snapshot]──▶ gex_calculator.py ──▶ gex_latest_SPY.json │
                                              gex_gate.py ─────────┤
                                                                   │
  [Stock trades] ──▶ flow_monitor.py ──▶ flow_signals_latest.json ▼
                                                                   │
  [1-min bars] ──▶ arka_engine.py ──▶ Features ──▶ Conviction ────┤
                        │                               │          │
                        ▼                               ▼          │
                  heat_seeker.py ──▶ ARJUN deliberation            │
                  (score ≥ 80)      (claude-sonnet-4-6)            │
                                         │                         │
                                         ▼                         │
                               Final conviction score ◀────────────┘
                                         │
                               [conviction ≥ threshold]
                                         │
                               contract picker
                                         │
                               Alpaca paper order
                                         │
                               position monitor (15s)
                                         │
                         stop/TP/trail ──▶ close order
```

---

## Key Configuration

All configuration is via environment variables in `.env`:

```bash
POLYGON_API_KEY=...          # Polygon.io API key (market data)
ALPACA_API_KEY=...           # Alpaca paper trading key
ALPACA_API_SECRET=...        # Alpaca paper trading secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets

ANTHROPIC_API_KEY=...        # Claude API key (ARJUN agents)

DISCORD_WEBHOOK_URL=...      # General alerts
DISCORD_FLOW_SIGNALS=...     # Flow monitor channel
DISCORD_ARKA_TRADES=...      # Trade entry/exit channel
```

---

## Logs Directory

```
logs/
├── arka/
│   ├── arka_engine.log       # Main engine (60s scan loop)
│   └── flow_scalper.log      # Flow scalper (30s scan loop)
├── chakra/
│   ├── flow_monitor.log      # Flow monitor (5 min)
│   ├── flow_signals_latest.json   # Live flow signal cache (ARKA reads)
│   └── watchlist_latest.json      # Swing screener candidates
├── gex/
│   ├── gex_latest_{ticker}.json   # Current GEX state per ticker
│   └── gex_intraday_{ticker}_{date}.json  # Intraday GEX snapshots
└── dashboard_api.log         # API server
```

---

## Known Issues / Pending Work

### PDT Rule
Alpaca paper account requires $25K equity to day trade (buy and sell same contract same day). When equity drops below $25K, all same-day position closes return HTTP 403. The system now sends a Discord alert when this happens so you can manually close via the Alpaca dashboard.

Workaround: Stocks always use 1DTE contracts (buy today, sell tomorrow = no day trade). Index 0DTE trades are still subject to PDT if the account drops below $25K.

### Swing Trade Gate Tuning (fixed Apr 29 2026)
Three gates were miscalibrated for stock options (all had index-centric thresholds):
- **OI gate**: was 100 for all tickers → now 25 for stocks, 100 for indexes
- **DTE selection**: 0DTE was chosen for NEGATIVE_GAMMA regime → stocks always use 1DTE minimum
- **Cost gate**: fixed $4/share max and $500 trade cap → stocks now use 4% of stock price max and $1,000 trade cap
- **Flat market gate**: threshold lowered from 0.30% to 0.15% SPY move; bypassed entirely when flow signal confidence ≥ 80%

### Pending
1. **GEX Intraday Timeline** — `/api/options/gex/intraday` endpoint (partially implemented, not wired to UI)
2. **Performance Page Charts** — cumulative P&L chart, win/loss by ticker, daily bar chart
3. **GEX Tab Range Bound Levels** — call wall / zero gamma / put wall levels at top of GEX tab

---

## Models

| File | Description |
|------|-------------|
| `backend/arjun/models/arka_scalp_model.json` | XGBoost fakeout filter (current) |
| `backend/arjun/models/xgboost_retrained_2026-04-17.json` | Most recent retrain |

Retrain with: `python3 backend/arka/train_arka.py`

---

## Paper Account

- Provider: Alpaca Paper Trading
- Account ID: PA34OLZI1DZM
- Starting equity: ~$4,000 (Epoch 3, reset Apr 21 2026)
- Strategy: options only, 0–1 DTE, 1–3 contracts per trade

---

*CHAKRA v3 — Built Apr 2026*
