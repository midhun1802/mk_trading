/**
 * CHAKRA Heat Seeker — Unusual Options Flow UI (Dual-Mode)
 * Scalp: 0DTE/1DTE ATM sweeps | Swing: OTM multi-week institutional flow
 */

let _hsAutoRefreshTimer = null;
let _hsMode = sessionStorage.getItem('hsMode') || 'scalp';

// ── Init ─────────────────────────────────────────────────────────────────────
async function initHeatSeeker() {
  hsSetMode(_hsMode, false); // restore mode without scanning
  await hsLoadWatchlist();

  hsStartAutoRefresh(); // silent 25s background refresh
}

// ── Mode toggle ───────────────────────────────────────────────────────────────
function hsSetMode(mode, scan) {
  _hsMode = (mode === 'scalp' || mode === 'swing') ? mode : 'scalp';
  sessionStorage.setItem('hsMode', _hsMode);

  // Toggle button active state
  document.getElementById('hs-mode-scalp')?.classList.toggle('active', _hsMode === 'scalp');
  document.getElementById('hs-mode-swing')?.classList.toggle('active', _hsMode === 'swing');

  // Mode hint text
  const hint = document.getElementById('hs-mode-hint');
  if (hint) {
    hint.textContent = _hsMode === 'scalp'
      ? '⚡ Scalp: SPY QQQ IWM — 0DTE/1DTE ATM sweeps only'
      : '🌊 Swing: Any ticker — OTM multi-week institutional flow';
  }

  // DTE column visibility — always show
  const dteTh = document.getElementById('hs-th-dte');
  if (dteTh) dteTh.style.display = '';

  // Clear table on mode switch
  const tbody = document.getElementById('hs-signals-body');
  if (tbody) {
    tbody.innerHTML = `<tr><td colspan="15" style="text-align:center;color:var(--sub);padding:30px;">
      Mode switched to ${_hsMode.toUpperCase()} — click ⚡ SCAN NOW</td></tr>`;
  }
  const statusEl = document.getElementById('hs-scan-status');
  if (statusEl) { statusEl.textContent = ''; }
}

// ── Watchlist ─────────────────────────────────────────────────────────────────
async function hsLoadWatchlist() {
  try {
    const r = await fetch('/api/heatseeker/watchlist');
    const data = await r.json();
    if (data && data.tickers) hsRenderPills(data.tickers);
  } catch(e) {
    console.error('[HeatSeeker] Load watchlist failed:', e);
  }
}

function hsRenderPills(tickers) {
  const el = document.getElementById('hs-watchlist-pills');
  if (!el) return;
  el.innerHTML = (tickers || []).map(t => `
    <span class="hs-pill">
      ${t}
      <span class="hs-pill-remove" onclick="hsRemoveTicker('${t}')">✕</span>
    </span>`).join('');
}

async function hsAddTicker() {
  const input = document.getElementById('hs-ticker-input');
  if (!input) return;
  const ticker = input.value.trim().toUpperCase();
  if (!ticker) return;
  try {
    const r = await fetch('/api/heatseeker/watchlist', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker})
    });
    const data = await r.json();
    if (data && data.tickers) {
      hsRenderPills(data.tickers);
      input.value = '';
    }
  } catch(e) {
    console.error('[HeatSeeker] Add ticker failed:', e);
  }
}

async function hsRemoveTicker(ticker) {
  try {
    const r = await fetch('/api/heatseeker/watchlist/' + ticker, {method: 'DELETE'});
    const data = await r.json();
    if (data && data.tickers) hsRenderPills(data.tickers);
  } catch(e) {
    console.error('[HeatSeeker] Remove ticker failed:', e);
  }
}

// ── Scan ──────────────────────────────────────────────────────────────────────
async function hsRunScan() {
  const statusEl = document.getElementById('hs-scan-status');
  const tbody    = document.getElementById('hs-signals-body');
  if (!tbody) { console.error('[HeatSeeker] hs-signals-body not found'); return; }

  const modeLabel = _hsMode === 'scalp' ? '⚡ SCALP' : '🌊 SWING';
  if (statusEl) { statusEl.textContent = `⏳ ${modeLabel} scan…`; statusEl.style.color = '#f0c040'; }
  tbody.innerHTML = `<tr><td colspan="15" style="text-align:center;color:var(--sub);padding:30px;">
    ⏳ ${modeLabel} scan — fetching options chains…</td></tr>`;

  let data = null;
  try {
    const r = await fetch(`/api/heatseeker/scan?mode=${_hsMode}`, {
      cache: 'no-store',
      headers: {'Accept': 'application/json'}
    });
    if (!r.ok) {
      console.error('[HeatSeeker] scan HTTP error', r.status);
    } else {
      data = await r.json();
      console.log(`[HeatSeeker] ${_hsMode} scan: ${data.count} signals`, data.watchlist);
    }
  } catch(e) {
    console.error('[HeatSeeker] scan fetch error', e);
  }

  if (!data) {
    if (statusEl) { statusEl.textContent = '❌ Scan failed'; statusEl.style.color = '#f44'; }
    tbody.innerHTML = `<tr><td colspan="15" style="text-align:center;color:var(--red);padding:30px;">
      ❌ Scan failed — check browser console (F12)</td></tr>`;
    return;
  }

  const signals = data.signals || [];
  const lastEl  = document.getElementById('hs-last-scan');
  if (lastEl) lastEl.textContent = `Last ${_hsMode} scan: ${new Date().toLocaleTimeString()}`;
  if (statusEl) {
    const n = signals.length;
    statusEl.textContent = `✅ ${n} ${_hsMode} signal${n !== 1 ? 's' : ''}`;
    statusEl.style.color = n > 0 ? '#4f4' : 'var(--sub)';
  }

  try {
    hsRenderTable(signals);
    hsLoadSummary();
  } catch(e) {
    console.error('[HeatSeeker] render error', e);
    tbody.innerHTML = `<tr><td colspan="15" style="text-align:center;color:var(--red);padding:30px;">
      ❌ Render error: ${e.message}</td></tr>`;
  }
}

// ── Table renderer ────────────────────────────────────────────────────────────
function hsRenderTable(signals) {
  const tbody = document.getElementById('hs-signals-body');
  if (!tbody) return;

  if (!signals.length) {
    const now     = new Date();
    const etHour  = (now.getUTCHours() - 4 + 24) % 24;
    const afterHrs = etHour >= 16 || etHour < 9;
    const msg = afterHrs
      ? `🌙 After hours — ${_hsMode === 'scalp' ? '0DTE data is stale.' : 'Options data is stale.'} Best results 9:30am–4pm ET.`
      : `No ${_hsMode} flow detected above current thresholds`;
    tbody.innerHTML = `<tr><td colspan="15" style="text-align:center;color:var(--sub);padding:30px;">${msg}</td></tr>`;
    return;
  }

  const isScalp = _hsMode === 'scalp';

  tbody.innerHTML = signals.map(s => {
    const scoreColor = s.score >= 75 ? '#ff4444'
                     : s.score >= 55 ? '#f0c040'
                     : '#aaa';
    const biasColor  = (s.bias || '').includes('BULLISH') ? '#4caf50'
                     : (s.bias || '').includes('BEARISH') ? '#f44336'
                     : '#888';
    const typeColor  = s.type === 'CALL' ? '#4caf50' : '#f44336';
    const rowBg      = s.score >= 75 ? 'rgba(255,68,68,0.05)'
                     : s.score >= 55 ? 'rgba(240,192,64,0.04)'
                     : 'transparent';
    const prem       = s.premium >= 1_000_000
                     ? `$${(s.premium / 1_000_000).toFixed(2)}M`
                     : `$${(s.premium / 1_000).toFixed(1)}K`;

    // DTE display
    const dte      = s.dte !== undefined ? s.dte : '—';
    const dteLabel = dte === 0 ? '🔴 0DTE'
                   : dte === 1 ? '🟡 1DTE'
                   : `${dte}d`;

    // Delta color: ATM range highlighted in scalp mode
    const deltaVal = parseFloat(s.delta) || 0;
    const deltaColor = (isScalp && deltaVal >= 0.38 && deltaVal <= 0.65)
                     ? '#f0c040'  // ATM zone — gold in scalp mode
                     : 'inherit';

    return `<tr class="hs-row" style="background:${rowBg}">
      <td style="color:var(--gold);font-weight:bold">${s.ticker}</td>
      <td style="color:${typeColor};font-weight:bold">${s.type}</td>
      <td>$${s.strike}</td>
      <td style="font-size:11px">${s.expiry}</td>
      <td style="font-size:11px">${dteLabel}</td>
      <td>${(s.volume || 0).toLocaleString()}</td>
      <td style="color:#f0c040">${s.vol_mult}x</td>
      <td>${s.oi_ratio}</td>
      <td style="color:#4caf50;font-weight:bold">${prem}</td>
      <td>$${s.trade_px}</td>
      <td>${s.iv}%</td>
      <td style="color:${deltaColor}">${s.delta}</td>
      <td style="font-size:11px">${s.direction}</td>
      <td style="color:${biasColor};font-weight:bold">${s.bias}</td>
      <td style="color:${scoreColor};font-weight:bold;font-size:14px">${s.score}</td>
    </tr>`;
  }).join('');
}

// ── Auto refresh ──────────────────────────────────────────────────────────────
function hsToggleAutoRefresh() {
  // No-op — replaced by silent auto-refresh
}

function _hsSilentRefresh() {
  // Refresh data silently — no spinner, no status flash
  const mode = _hsMode || 'scalp';
  get(`/api/heatseeker/scan?mode=${mode}`)
    .then(data => {
      if (data && data.signals) {
        hsRenderTable(data.signals);
        // Update signal count quietly
        const statusEl = document.getElementById('hs-scan-status');
        if (statusEl) {
          statusEl.textContent = `${data.count || data.signals.length} ${mode} signals`;
        }
        // Update last scan time
        const lastEl = document.getElementById('hs-last-scan');
        if (lastEl) {
          const now = new Date();
          lastEl.textContent = `Last ${mode} scan: ${now.toLocaleTimeString()}`;
        }
      }
    })
    .catch(() => {}) // silent — never surface errors on background refresh
    .finally(() => {
      hsLoadSummary(); // always refresh summary too
    });
}

function hsStartAutoRefresh() {
  clearInterval(_hsAutoRefreshTimer);
  _hsSilentRefresh(); // immediate first refresh
  _hsAutoRefreshTimer = setInterval(_hsSilentRefresh, 25_000);
}

function hsStopAutoRefresh() {
  clearInterval(_hsAutoRefreshTimer);
}


// ── Flow Summary ─────────────────────────────────────────────────────────────
async function hsLoadSummary() {
    try {
        const data = await get(`/api/heatseeker/summary?mode=${_hsMode}`);
        hsRenderSummary(data);
    } catch(e) {
        console.warn('[HeatSeeker] Summary failed:', e);
    }
}

function hsRenderSummary(data) {
    const rows  = document.getElementById('hs-summary-rows');
    const timer = document.getElementById('hs-summary-time');
    if (!rows) return;

    if (!data.summary || data.summary.length === 0) {
        rows.innerHTML = '<div style="color:var(--sub);font-size:11px;padding:8px 14px;">No signals detected</div>';
        return;
    }

    if (timer) {
        const t = new Date(data.scanned_at);
        timer.textContent = `updated ${t.toLocaleTimeString()}`;
    }

    rows.innerHTML = data.summary.map(t => {
        const biasColor = t.bias === 'BEARISH' ? 'var(--red)'
                        : t.bias === 'BULLISH' ? 'var(--green)'
                        : 'var(--sub)';

        const callStr = t.top_bull_strike
            ? `<span style="color:var(--green);font-weight:bold;font-family:'JetBrains Mono',monospace;">CALL $${t.top_bull_strike}</span><span style="color:var(--sub);font-size:10px;"> ${t.top_bull_score}</span>`
            : `<span style="color:var(--border);">—</span>`;

        const putStr = t.top_bear_strike
            ? `<span style="color:var(--red);font-weight:bold;font-family:'JetBrains Mono',monospace;">PUT&nbsp; $${t.top_bear_strike}</span><span style="color:var(--sub);font-size:10px;"> ${t.top_bear_score}</span>`
            : `<span style="color:var(--border);">—</span>`;

        const bearW = Math.round(t.bear_pct * 0.5);
        const bullW = Math.round(t.bull_pct * 0.5);

        return `<div style="
            display:grid;
            grid-template-columns:55px 110px 130px 1fr 1fr;
            align-items:center;
            padding:8px 14px;
            border-bottom:1px solid var(--border);
            font-size:11px;
            gap:12px;
            transition:background 0.15s;
        " onmouseover="this.style.background='var(--bg3)'" onmouseout="this.style.background='transparent'">

            <span style="color:var(--gold);font-weight:bold;font-size:13px;font-family:'JetBrains Mono',monospace;letter-spacing:0.5px;">
                ${t.ticker}
            </span>

            <span style="color:${biasColor};font-weight:bold;font-size:11px;letter-spacing:0.5px;">
                ${t.bias_emoji} ${t.bias}
            </span>

            <span style="display:flex;align-items:center;gap:5px;">
                <span style="color:var(--red);font-size:10px;">${t.bear_count}▼</span>
                <span style="display:inline-flex;gap:1px;align-items:center;margin:0 2px;">
                    <span style="display:inline-block;width:${bearW}px;height:3px;background:var(--red);border-radius:1px 0 0 1px;opacity:0.7;"></span>
                    <span style="display:inline-block;width:${bullW}px;height:3px;background:var(--green);border-radius:0 1px 1px 0;opacity:0.7;"></span>
                </span>
                <span style="color:var(--green);font-size:10px;">${t.bull_count}▲</span>
            </span>

            <span>${callStr}</span>
            <span>${putStr}</span>
        </div>`;
    }).join('');
}

// Pause/resume silent refresh with page visibility
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearInterval(_hsAutoRefreshTimer);
    _hsAutoRefreshTimer = null;
  } else {
    // Only restart if Heat Seeker pane is currently active
    const pane = document.getElementById('p-heatseeker');
    if (pane && getComputedStyle(pane).display !== 'none') {
      hsStartAutoRefresh();
    }
  }
});
