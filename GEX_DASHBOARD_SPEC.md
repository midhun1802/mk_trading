# GEX Dashboard Redesign — Implementation Spec
## Target: Match George's GEX Analysis Page
### File: frontend/js/analysis.js + frontend/dashboard.html

---

## CRITICAL RULES
- **DO NOT modify any existing rendering functions in analysis.js**
- **DO NOT touch the Term Structure, Expiration Breakdown, or Intraday Timeline sections**
- Only ADD new functions and HTML sections
- Test with: `node --check frontend/js/analysis.js` after every edit
- The GEX tab is triggered by `id === 'gex'` in the tab switcher

---

## WHAT TO BUILD

### Step 1: Add HTML skeleton to dashboard.html

Find the GEX tab panel in dashboard.html (look for `id="gexBody"` or the GEX panel container).
Add this HTML ABOVE the existing GEX content (Term Structure etc):

```html
<!-- GEX Analysis Header — George Style -->
<div id="gexAnalysisHeader" style="margin-bottom:14px">

  <!-- Last Session Banner (shows when market closed) -->
  <div id="gexLastSessionBanner" style="display:none;
    background:rgba(255,179,71,0.08);border:1px solid rgba(255,179,71,0.3);
    border-radius:6px;padding:8px 14px;margin-bottom:10px;
    display:flex;align-items:center;gap:8px">
    <span style="font-size:11px">🕐</span>
    <span id="gexLastSessionText" style="font-size:11px;color:var(--gold)">
      Last Session — Loading...
    </span>
  </div>

  <!-- Ticker Selector Row -->
  <div id="gexTickerRow" style="display:flex;gap:6px;flex-wrap:wrap;
    margin-bottom:12px;padding:10px;background:var(--bg2);border-radius:7px;
    border:1px solid var(--border)">
    <!-- Populated by JS -->
  </div>

  <!-- Main GEX Strategy Card -->
  <div id="gexStrategyCard" style="background:var(--bg2);border-radius:9px;
    border:1px solid var(--border);padding:16px;margin-bottom:10px">
    <div style="text-align:center;padding:20px;color:var(--sub);font-size:12px">
      Loading GEX analysis...
    </div>
  </div>

</div>
```

---

### Step 2: Add CSS to dashboard.css

```css
/* GEX Analysis — George Style */
.gex-ticker-btn {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  border-radius: 20px;
  border: 1px solid var(--border);
  background: var(--bg3);
  cursor: pointer;
  font-size: 10px;
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  color: var(--sub);
  transition: all 0.15s;
  white-space: nowrap;
}
.gex-ticker-btn:hover { border-color: var(--accent); color: var(--text); }
.gex-ticker-btn.active {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.gex-ticker-btn .gex-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.gex-ticker-btn .gex-amt {
  font-size: 8px;
  opacity: 0.8;
}

.gex-progress-bar {
  height: 6px;
  border-radius: 3px;
  background: var(--bg3);
  overflow: hidden;
  flex: 1;
}
.gex-progress-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.4s ease;
}

.gex-metric-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-top: 14px;
}
.gex-metric-box {
  background: var(--bg3);
  border-radius: 6px;
  padding: 10px 12px;
}
.gex-metric-label {
  font-size: 7px;
  letter-spacing: 1px;
  color: var(--sub);
  text-transform: uppercase;
  margin-bottom: 5px;
}
.gex-metric-value {
  font-size: 13px;
  font-weight: 700;
  color: var(--text);
  line-height: 1.2;
}
.gex-metric-sub {
  font-size: 9px;
  color: var(--sub);
  margin-top: 3px;
}

@media (max-width: 768px) {
  .gex-metric-grid { grid-template-columns: repeat(2, 1fr); }
}
```

---

### Step 3: Add JS functions to analysis.js

**IMPORTANT:** Add these as NEW standalone functions. Do NOT modify existing ones.
Place them BEFORE the existing GEX rendering functions in analysis.js.

```javascript
// ══════════════════════════════════════════════════════════════════════════════
// GEX ANALYSIS — GEORGE STYLE
// Added: March 2026
// ══════════════════════════════════════════════════════════════════════════════

// Tickers to show in selector row with their GEX amounts
const GEX_TICKER_UNIVERSE = [
  'SPY','QQQ','IWM','SPX','RUT','NVDA','TSLA','AAPL','MSFT','AMZN','GOOGL','META','GLD','SLV'
];

// Track currently selected GEX ticker
let _gexActiveTicker = 'SPY';

// ── Main entry point — call this when GEX tab opens ──────────────────────────
async function renderGEXGeorgeStyle(ticker = null) {
  if (ticker) _gexActiveTicker = ticker;
  _renderGEXTickerRow();
  await _renderGEXStrategyCard(_gexActiveTicker);
  _updateGEXLastSessionBanner();
}

// ── Ticker selector row ────────────────────────────────────────────────────────
function _renderGEXTickerRow() {
  const el = $('gexTickerRow');
  if (!el) return;

  el.innerHTML = GEX_TICKER_UNIVERSE.map(t => `
    <button class="gex-ticker-btn ${t === _gexActiveTicker ? 'active' : ''}"
      onclick="_gexSelectTicker('${t}')">
      <span class="gex-dot"></span>
      <span>${t}</span>
      <span class="gex-amt" id="gexTickerAmt_${t}">$—</span>
    </button>
  `).join('');

  // Load GEX amounts for each ticker in background
  _loadGEXAmounts();
}

// ── Load GEX $ amounts for ticker row ──────────────────────────────────────────
async function _loadGEXAmounts() {
  // Load SPY first (most important), then others quietly
  try {
    const spyData = await getCached('/api/options/gex?ticker=SPY', 60000);
    if (spyData && spyData.net_gex) {
      const amt = Math.abs(spyData.net_gex) >= 1e9
        ? '$' + (spyData.net_gex / 1e9).toFixed(1) + 'B'
        : '$' + (spyData.net_gex / 1e6).toFixed(0) + 'M';
      const el = $('gexTickerAmt_SPY');
      if (el) el.textContent = amt;
    }
  } catch(e) {}

  // Load SPX separately
  try {
    const spxData = await getCached('/api/options/gex?ticker=SPX', 60000);
    if (spxData && spxData.net_gex) {
      const amt = '$' + (Math.abs(spxData.net_gex) / 1e9).toFixed(1) + 'B';
      const el = $('gexTickerAmt_SPX');
      if (el) el.textContent = amt;
    }
  } catch(e) {}
}

// ── Switch active ticker ───────────────────────────────────────────────────────
async function _gexSelectTicker(ticker) {
  _gexActiveTicker = ticker;
  // Update active state on buttons
  document.querySelectorAll('.gex-ticker-btn').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.trim().startsWith(ticker));
  });
  await _renderGEXStrategyCard(ticker);
}

// ── Last Session Banner ────────────────────────────────────────────────────────
function _updateGEXLastSessionBanner() {
  const banner = $('gexLastSessionBanner');
  const text   = $('gexLastSessionText');
  if (!banner || !text) return;

  const now = new Date();
  const etOffset = -5; // EST (adjust for EDT: -4)
  const etHour = (now.getUTCHours() + etOffset + 24) % 24;
  const isWeekend = now.getDay() === 0 || now.getDay() === 6;
  const isMarketHours = !isWeekend && etHour >= 9 && etHour < 16;

  if (!isMarketHours) {
    banner.style.display = 'flex';
    const days = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    // Show last trading day
    const lastTrade = new Date(now);
    while (lastTrade.getDay() === 0 || lastTrade.getDay() === 6) {
      lastTrade.setDate(lastTrade.getDate() - 1);
    }
    text.textContent = `Last Session — ${days[lastTrade.getDay()]}, ${months[lastTrade.getMonth()]} ${lastTrade.getDate()}`;
  } else {
    banner.style.display = 'none';
  }
}

// ── Main strategy card ─────────────────────────────────────────────────────────
async function _renderGEXStrategyCard(ticker) {
  const card = $('gexStrategyCard');
  if (!card) return;

  card.innerHTML = `<div style="text-align:center;padding:20px;color:var(--sub);font-size:11px">
    Loading ${ticker} GEX analysis...
  </div>`;

  try {
    const data = await getCached(`/api/options/gex?ticker=${ticker}`, 30000);
    if (!data || data.error) {
      card.innerHTML = `<div style="text-align:center;padding:20px;color:var(--sub);font-size:11px">
        GEX data unavailable — loads during market hours
      </div>`;
      return;
    }

    const spot        = parseFloat(data.spot || 0);
    const callWall    = parseFloat(data.call_wall || 0);
    const putWall     = parseFloat(data.put_wall  || 0);
    const zeroGamma   = parseFloat(data.zero_gamma || spot);
    const netGex      = parseFloat(data.net_gex || 0);
    const netGexB     = netGex / 1e9;
    const regime      = data.regime || 'UNKNOWN';
    const isPositive  = regime === 'POSITIVE_GAMMA' || netGex > 0;
    const regimeCol   = isPositive ? 'var(--green)' : 'var(--red)';
    const regimeTxt   = isPositive ? 'Positive Gamma' : 'Negative Gamma';
    const aboveZero   = spot > zeroGamma;

    // Dollar bias
    const callGexB    = parseFloat(data.call_gex_dollars || 0) / 1e9;
    const putGexB     = parseFloat(data.put_gex_dollars  || 0) / 1e9;
    const biasRatio   = parseFloat(data.bias_ratio || 1);
    const deskState   = biasRatio > 2
      ? `${biasRatio.toFixed(1)}x put heavy`
      : biasRatio < 0.5
      ? `${(1/biasRatio).toFixed(1)}x call heavy`
      : 'Balanced';

    // Regime call
    const regimeCall  = data.regime_call || (isPositive && aboveZero ? 'SHORT_THE_POPS' :
                        isPositive && !aboveZero ? 'BUY_THE_DIPS' : 'FOLLOW_MOMENTUM');
    const regimeCallLabels = {
      'SHORT_THE_POPS':  { strategy: 'Range Bound', sub: `Counter-trend — P on rips toward pin $${zeroGamma.toFixed(2)}`, entry: `P on rips toward $${zeroGamma.toFixed(2)}`, target: `Pin $${zeroGamma.toFixed(2)}`, out: 'No invalidation level' },
      'BUY_THE_DIPS':    { strategy: 'Range Bound', sub: `Counter-trend — C on dips toward pin $${zeroGamma.toFixed(2)}`, entry: `C on dips toward $${zeroGamma.toFixed(2)}`, target: `Pin $${zeroGamma.toFixed(2)}`, out: 'No invalidation level' },
      'FOLLOW_MOMENTUM': { strategy: 'Trending', sub: `Momentum — follow breakout direction`, entry: `Enter breakout with momentum`, target: callWall > spot ? `Call wall $${callWall.toFixed(2)}` : `Put wall $${putWall.toFixed(2)}`, out: `Zero gamma flip $${zeroGamma.toFixed(2)}` },
      'NEUTRAL':         { strategy: 'Watch', sub: 'No strong directional bias', entry: '—', target: '—', out: '—' },
    };
    const rc = regimeCallLabels[regimeCall] || regimeCallLabels['NEUTRAL'];

    // Acceleration
    const accelUp   = parseFloat(data.accel_up   || 0);
    const accelDown = parseFloat(data.accel_down  || 0);
    const accelTotal = accelUp + accelDown || 1;
    const accelPct   = Math.round(accelUp / accelTotal * 100);

    // Pin proximity
    const pctToCall = callWall > spot ? ((callWall - spot) / spot * 100) : 0;
    const pctToPut  = putWall < spot  ? ((spot - putWall) / spot * 100)  : 0;
    const nearestWallPct = Math.min(pctToCall || 99, pctToPut || 99);
    const pinRisk   = Math.max(0, Math.min(100, Math.round(100 - nearestWallPct * 10)));
    const breakout  = 100 - pinRisk;

    // Expected move
    const emPts     = parseFloat(data.expected_move_pts || 0);
    const upper1sd  = parseFloat(data.upper_1sd || spot * 1.01);
    const lower1sd  = parseFloat(data.lower_1sd || spot * 0.99);
    const spotIn1sd = spot >= lower1sd && spot <= upper1sd;

    // GEX strength
    const gexAbsB   = Math.abs(netGexB);
    const strength  = gexAbsB > 3 ? 'STRONG' : gexAbsB > 1 ? 'MODERATE' : 'WEAK';
    const strengthCol = gexAbsB > 3 ? 'var(--green)' : gexAbsB > 1 ? 'var(--gold)' : 'var(--sub)';

    // Structure description
    const pinDist   = Math.abs(spot - zeroGamma) / spot * 100;
    const structTxt = pinDist < 0.5 ? 'At pin — expect oscillation'
                    : pinDist < 1.5 ? `Pin ${pinDist.toFixed(1)}% ${spot > zeroGamma ? 'above' : 'below'}`
                    : `Pin ${pinDist.toFixed(1)}% away — no flip anchor`;
    const structSub = zeroGamma ? `Zero gamma: $${zeroGamma.toFixed(2)}` : 'No flip level';

    // Base path
    const basePath  = regimeCall === 'SHORT_THE_POPS' ? 'Fade rallies, target pin'
                    : regimeCall === 'BUY_THE_DIPS'    ? 'Buy dips, target pin'
                    : regimeCall === 'FOLLOW_MOMENTUM' ? 'Follow breakout direction'
                    : 'No forward path yet';
    const basePathSub = emPts > 0 ? `±$${emPts.toFixed(2)} expected move` : 'Awaiting projection metadata';

    card.innerHTML = `
      <!-- STRATEGY HEADER -->
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px">
        <div>
          <div style="font-size:16px;font-weight:800;color:var(--text);line-height:1">${rc.strategy}</div>
          <div style="font-size:11px;color:var(--sub);margin-top:4px">${rc.sub}</div>
        </div>
        <div style="text-align:right">
          <span style="font-size:10px;font-weight:700;padding:4px 10px;border-radius:20px;
            background:${regimeCol}18;color:${regimeCol};border:1px solid ${regimeCol}35">
            ${regimeTxt}
          </span>
          <div style="font-size:11px;font-weight:700;color:var(--text);margin-top:5px">
            $${gexAbsB.toFixed(1)}B total GEX
          </div>
          <div style="font-size:9px;font-weight:700;color:${strengthCol};letter-spacing:1px">
            ${strength}
          </div>
        </div>
      </div>

      <!-- ENTRY / TARGET / OUT -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;
        padding:12px;background:var(--bg3);border-radius:7px;margin-bottom:14px">
        <div>
          <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">Entry</div>
          <div style="font-size:11px;font-weight:700;color:var(--text);line-height:1.3">${rc.entry}</div>
        </div>
        <div>
          <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">Target</div>
          <div style="font-size:11px;font-weight:700;color:var(--green);line-height:1.3">${rc.target}</div>
        </div>
        <div>
          <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">Out</div>
          <div style="font-size:11px;font-weight:700;color:var(--red);line-height:1.3">${rc.out}</div>
        </div>
      </div>

      <!-- PIN / WALLS INFO ROW -->
      <div style="font-size:9px;color:var(--sub);margin-bottom:12px;display:flex;gap:16px;flex-wrap:wrap">
        ${zeroGamma ? `<span>Pin <strong style="color:var(--gold)">$${zeroGamma.toFixed(2)}</strong> (${pinDist.toFixed(1)}% ${spot > zeroGamma ? 'below' : 'above'})</span>` : ''}
        ${callWall ? `<span>Call wall <strong style="color:var(--green)">$${callWall.toFixed(2)}</strong></span>` : ''}
        ${putWall  ? `<span>Put wall <strong style="color:var(--red)">$${putWall.toFixed(2)}</strong></span>`   : ''}
        <span>Walls <strong style="color:${biasRatio > 1.5 ? 'var(--red)' : biasRatio < 0.67 ? 'var(--green)' : 'var(--sub)'}">${biasRatio.toFixed(1)}x ${biasRatio > 1 ? 'put' : 'call'} heavy</strong></span>
      </div>

      <!-- PROGRESS BARS: PIN RISK / BREAKOUT / ACCEL -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:14px">
        <div>
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:8px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">Pin Risk</span>
            <span style="font-size:9px;font-weight:700;color:var(--gold)">${pinRisk}%</span>
          </div>
          <div class="gex-progress-bar">
            <div class="gex-progress-fill" style="width:${pinRisk}%;background:var(--gold)"></div>
          </div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:8px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">Breakout</span>
            <span style="font-size:9px;font-weight:700;color:var(--purple)">${breakout}%</span>
          </div>
          <div class="gex-progress-bar">
            <div class="gex-progress-fill" style="width:${breakout}%;background:var(--purple)"></div>
          </div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:8px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">Accel</span>
            <span style="font-size:9px;color:var(--sub)">${accelUp > 0 ? '↑'+accelUp : '—'} / ${accelDown > 0 ? '↓'+accelDown : '—'}</span>
          </div>
          <div class="gex-progress-bar" style="position:relative">
            <!-- Center marker -->
            <div style="position:absolute;left:50%;top:0;width:2px;height:100%;background:var(--border);z-index:1"></div>
            <!-- Directional fill from center -->
            <div class="gex-progress-fill" style="
              position:absolute;
              ${accelUp > accelDown
                ? `left:50%;width:${Math.min(50, accelPct/2)}%;background:var(--green)`
                : `right:50%;width:${Math.min(50, (100-accelPct)/2)}%;background:var(--red)`
              }
            "></div>
          </div>
        </div>
      </div>

      <!-- BOTTOM 4 METRICS: BASE PATH / EXPECTED MOVE / STRUCTURE / DESK STATE -->
      <div class="gex-metric-grid">
        <div class="gex-metric-box">
          <div class="gex-metric-label">Base Path</div>
          <div class="gex-metric-value" style="font-size:11px">${basePath}</div>
          <div class="gex-metric-sub">${basePathSub}</div>
        </div>
        <div class="gex-metric-box">
          <div class="gex-metric-label">Expected Move</div>
          <div class="gex-metric-value" style="font-size:11px;color:${spotIn1sd?'var(--green)':'var(--gold)'}">
            ${emPts > 0 ? `Spot ${spotIn1sd ? 'inside' : 'outside'} 1σ` : '±1σ not computed'}
          </div>
          <div class="gex-metric-sub">
            ${emPts > 0 ? `$${lower1sd.toFixed(2)} – $${upper1sd.toFixed(2)}` : 'Projection not ranked'}
          </div>
        </div>
        <div class="gex-metric-box">
          <div class="gex-metric-label">Structure</div>
          <div class="gex-metric-value" style="font-size:11px">${structTxt}</div>
          <div class="gex-metric-sub">${structSub}</div>
        </div>
        <div class="gex-metric-box">
          <div class="gex-metric-label">Desk State</div>
          <div class="gex-metric-value" style="font-size:11px;color:${biasRatio > 2 ? 'var(--red)' : biasRatio < 0.5 ? 'var(--green)' : 'var(--sub)'}">
            ${deskState}
          </div>
          <div class="gex-metric-sub">
            ${biasRatio > 2 ? 'Bearish dealer lean' : biasRatio < 0.5 ? 'Bullish dealer lean' : 'Late session'}
          </div>
        </div>
      </div>

      <!-- REGIME CALL BANNER (if strong signal) -->
      ${regimeCall !== 'NEUTRAL' ? `
      <div style="margin-top:14px;padding:10px 14px;border-radius:6px;
        background:${regimeCol}0d;border:1px solid ${regimeCol}30;
        display:flex;justify-content:space-between;align-items:center">
        <div>
          <div style="font-size:8px;letter-spacing:1px;color:${regimeCol};font-weight:700;text-transform:uppercase">
            Today's GEX Regime
          </div>
          <div style="font-size:13px;font-weight:800;color:${regimeCol};margin-top:2px">
            ${regimeCall.replace(/_/g,' ')}
          </div>
        </div>
        <div style="font-size:9px;color:var(--sub);text-align:right;max-width:200px">
          ${regimeCall === 'SHORT_THE_POPS' ? 'Dealers fade upside — favor puts on rallies' :
            regimeCall === 'BUY_THE_DIPS'    ? 'Dealers support downside — favor calls on dips' :
            'Negative gamma amplifies moves in both directions'}
        </div>
      </div>` : ''}
    `;

  } catch(e) {
    card.innerHTML = `<div style="text-align:center;padding:20px;color:var(--sub);font-size:11px">
      Error loading GEX: ${e.message}
    </div>`;
  }
}
```

---

### Step 4: Wire into GEX tab trigger

Find in analysis.js where the GEX tab is activated (look for `gexTermBody` or the GEX section toggle).
Add a call to `renderGEXGeorgeStyle()` when the GEX tab opens:

```javascript
// Find this pattern and add the renderGEXGeorgeStyle() call:
if (id === 'gexTermBody') setTimeout(() => _renderTermStructure(ticker), 100);

// ADD AFTER (do not replace):
if (id === 'gexBody' || section === 'gex') {
  setTimeout(() => renderGEXGeorgeStyle(), 100);
}
```

Also find where the GEX tab click handler is and add:
```javascript
// When GEX sub-tab becomes active:
renderGEXGeorgeStyle();
```

---

### Step 5: Auto-refresh every 60 seconds during market hours

Add to the GEX tab initialization:
```javascript
// Auto-refresh GEX strategy card every 60s during market hours
let _gexRefreshInterval = null;

function _startGEXRefresh() {
  if (_gexRefreshInterval) clearInterval(_gexRefreshInterval);
  _gexRefreshInterval = setInterval(() => {
    const now = new Date();
    const etHour = (now.getUTCHours() - 5 + 24) % 24;
    const isMarketHours = now.getDay() >= 1 && now.getDay() <= 5 && etHour >= 9 && etHour < 16;
    if (isMarketHours) renderGEXGeorgeStyle();
  }, 60000);
}
```

---

## WHAT THE FINAL RESULT LOOKS LIKE

```
┌─────────────────────────────────────────────────────────────────────┐
│ 🕐 Last Session — Friday, Mar 28              [only shows after hrs] │
├─────────────────────────────────────────────────────────────────────┤
│ ● SPY $6.4B  ● QQQ $0  ● IWM $0  ● SPX $22.3B  ● RUT...          │
│ [active ticker highlighted in accent color]                          │
├─────────────────────────────────────────────────────────────────────┤
│  Range Bound                        [Positive Gamma] ←green badge   │
│  Counter-trend — P on rips toward pin $640.00    $6.4B total GEX   │
│                                                          STRONG      │
│ ┌──────────────┬──────────────┬──────────────┐                      │
│ │ ENTRY        │ TARGET       │ OUT          │                      │
│ │ P on rips    │ Pin $640.00  │ No invalid.  │                      │
│ │ toward $640  │              │ level        │                      │
│ └──────────────┴──────────────┴──────────────┘                      │
│                                                                      │
│ Pin $640.00 (2.2% below)  Walls 2.1x put heavy                     │
│                                                                      │
│ PIN RISK    ████████░░░░ 65%                                        │
│ BREAKOUT    █████░░░░░░  35%                                        │
│ ACCEL       ░░░█████░░░  (↑12 / ↓18)                               │
│                                                                      │
│ ┌──────────────┬──────────────┬──────────────┬──────────────┐       │
│ │ BASE PATH    │ EXPECTED     │ STRUCTURE    │ DESK STATE   │       │
│ │ Fade rallies │ Spot inside  │ Pin 2.2%     │ 2.1x put     │       │
│ │ target pin   │ 1σ           │ below        │ heavy        │       │
│ │ ±$15.20 exp. │ $508–$539    │ Zero gamma   │ Bearish lean │       │
│ └──────────────┴──────────────┴──────────────┴──────────────┘       │
│                                                                      │
│ TODAY'S GEX REGIME: SHORT THE POPS                                  │
│ Dealers fade upside — favor puts on rallies                         │
└─────────────────────────────────────────────────────────────────────┘

[Existing panels below: Term Structure, Expiration Breakdown, etc.]
```

---

## TELL CLAUDE CODE

```
Read GEX_DASHBOARD_SPEC.md carefully.

Then implement it in this exact order:
1. Add the HTML skeleton to dashboard.html — find the GEX tab panel and add the gexAnalysisHeader div ABOVE existing GEX content
2. Add the CSS classes to dashboard.css
3. Add ALL the JavaScript functions to analysis.js as NEW functions — DO NOT modify any existing functions
4. Find where the GEX tab opens and add the renderGEXGeorgeStyle() call
5. Run: node --check frontend/js/analysis.js
6. Test by opening localhost:8000, going to Analysis → GEX tab

The API endpoint is /api/options/gex?ticker=SPY — it already exists and returns the data.
After market close, the card shows "Last Session" banner and last available data.
```

---

## NOTES FOR CLAUDE CODE

- `$()` is the existing shorthand for `document.getElementById()` — use it
- `getCached(url, ttl)` is the existing caching fetch helper — use it
- `var(--green)`, `var(--red)`, `var(--gold)`, `var(--purple)`, `var(--sub)`, `var(--text)`, `var(--bg2)`, `var(--bg3)`, `var(--border)`, `var(--accent)` are the CSS variables
- `var(--purple)` = CHAKRA brand color (#9B59B6)
- The GEX API may return limited data after hours — always handle null/undefined gracefully
- `data.regime_call`, `data.bias_ratio`, `data.accel_up` etc only exist after Phase 7A is built — use fallback logic with `||` operators

---

*GEX Dashboard Spec v1.0 — March 28, 2026*
