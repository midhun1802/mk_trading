/* ═══════════════════════════════════════════════════════════
   CHAKRA — core.js
   Helpers · Settings · Tab system · Market status · Init
   ═══════════════════════════════════════════════════════════ */
'use strict';

// ── Helpers ────────────────────────────────────────────────
const $ = id => document.getElementById(id);
async function get(u) {
  try {
    const ctrl = new AbortController();
    const tid  = setTimeout(() => ctrl.abort(), 5000); // 5s timeout
    const r    = await fetch(u, { signal: ctrl.signal });
    clearTimeout(tid);
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}
function f$(n) {
  if (n == null) return '—';
  return '$' + parseFloat(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fp(n) { const v = parseFloat(n || 0); return (v >= 0 ? '+' : '') + v.toFixed(2) + '%'; }
function pc(n)  { return parseFloat(n || 0) >= 0 ? 'var(--green)' : 'var(--red)'; }
function ft(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }); }
  catch { return '—'; }
}
function toast(msg, dur = 2500) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), dur);
}

// ── Settings ───────────────────────────────────────────────
const DEF = {
  arjunTickers: ['SPY', 'QQQ', 'IWM', 'DIA'],
  sectors: ['XLK','XLF','XLE','XLV','XLI','XLP','XLY','XLU','XLRE','XLB','XLC'],
  indices: ['SPY','QQQ','IWM','DIA','^SPX','^RUT'],
};
const SN = { XLK:'Technology',XLF:'Financials',XLE:'Energy',XLV:'Healthcare',
  XLI:'Industrials',XLP:'Cons Staples',XLY:'Cons Discr',XLU:'Utilities',
  XLRE:'Real Estate',XLB:'Materials',XLC:'Comm Svcs',
  SPY:'S&P 500 ETF',QQQ:'Nasdaq ETF',IWM:'Russell 2000',DIA:'Dow Jones',
  '^SPX':'S&P 500 Index','^RUT':'Russell 2000 Index' };
const IN = { SPY:'S&P 500',QQQ:'Nasdaq',IWM:'Russell 2000',DIA:'Dow Jones',
  '^SPX':'SPX Index','^RUT':'RUT Index' };
const IF = { SPY:'🇺🇸',QQQ:'🇺🇸',IWM:'🇺🇸',DIA:'🇺🇸','^SPX':'📊','^RUT':'📊' };

function gs() {
  try { return { ...DEF, ...JSON.parse(localStorage.getItem('chakra') || '{}') }; }
  catch { return { ...DEF }; }
}

function openS() {
  const s = gs();
  $('sOverlay').style.display = 'block';
  $('sPanel').style.display = 'block';
  rChips('aTList', s.arjunTickers);
  rTgls('sTglList', s.sectors, DEF.sectors, 'sectors', SN);
  rTgls('iTglList', s.indices, DEF.indices, 'indices', IN);
}
function closeS() {
  $('sOverlay').style.display = 'none';
  $('sPanel').style.display = 'none';
}
function rChips(id, list) {
  $(id).innerHTML = list.map(t =>
    `<span class="tchip"><span>${t}</span><span class="rm" onclick="rmT('${t}')">×</span></span>`
  ).join('');
}
function rTgls(id, active, all, key, names) {
  $(id).innerHTML = all.map(sym =>
    `<span class="tgl ${active.includes(sym) ? 'on' : 'off'}" onclick="tgl('${sym}','${key}')">
      ${sym} <span style="font-size:8px;opacity:.7">${names[sym] || ''}</span>
    </span>`
  ).join('');
}
function addT() {
  const v = ($('nTicker').value || '').toUpperCase().trim();
  if (!v) return;
  const s = gs();
  if (!s.arjunTickers.includes(v)) { s.arjunTickers.push(v); localStorage.setItem('chakra', JSON.stringify(s)); }
  $('nTicker').value = '';
  rChips('aTList', gs().arjunTickers);
}
function rmT(t) {
  const s = gs(); s.arjunTickers = s.arjunTickers.filter(x => x !== t);
  localStorage.setItem('chakra', JSON.stringify(s)); rChips('aTList', s.arjunTickers);
}
function tgl(sym, key) {
  const s = gs();
  if (!s[key]) s[key] = [...DEF[key]];
  s[key] = s[key].includes(sym) ? s[key].filter(x => x !== sym) : [...s[key], sym];
  localStorage.setItem('chakra', JSON.stringify(s));
  const names = key === 'sectors' ? SN : IN;
  rTgls(key === 'sectors' ? 'sTglList' : 'iTglList', s[key], DEF[key], key, names);
}
function saveS() {
  closeS();
  const m = $('savedMsg'); m.style.opacity = '1';
  setTimeout(() => m.style.opacity = '0', 2000);
  toast('✓ Settings saved');
}

// ── Tab system ─────────────────────────────────────────────
const SUBTABS = {
  arjun:   [{ id:'signals', label:'⚡ Signals' },{ id:'performance', label:'📊 Trading' },{ id:'chat', label:'💬 Chat' }],
  trading: [{ id:'arka', label:'⚡ Current' },{ id:'chakra-swings', label:'🌀 Swings' },{ id:'arka-lotto-tab', label:'🎰 Lotto' }],
  analysis:[{ id:'analysis', label:'🌍 Market' },{ id:'flow', label:'🌊 Flow' },
             { id:'gex', label:'🎱 GEX' },{ id:'cockpit', label:'🎯 COCKPIT' },
             { id:'physics', label:'📊 Rotation' },
             { id:'helx', label:'🧠 Neural Pulse' },{ id:'head', label:'👑 Head' }],
  power:   [{ id:'lotto', label:'⚡ Lotto' }],
};

let _mainTab = localStorage.getItem('chakra-main') || 'arjun';
let _subTab  = localStorage.getItem('chakra-sub')  || 'signals';

function setMain(m) {
  if (typeof hsStopAutoRefresh === 'function' && m !== 'heatseeker') hsStopAutoRefresh();
  _mainTab = m;
  localStorage.setItem('chakra-main', m);
  document.querySelectorAll('.mtab').forEach(b => b.classList.remove('active'));
  $('mt-' + m)?.classList.add('active');
  // Show the correct top-level pane for main tabs with no subtabs
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  const directPane = $('p-' + m + '-lotto') || $('p-' + m);
  if (directPane) directPane.classList.add('active');
  renderSubtabs(m);
  const first = SUBTABS[m]?.find(s => !s.sep);
  if (first) setSub(first.id);
  // Fire loaders for main tabs that own their pane directly
  if (m === 'power')      loadLottoLive();
  if (m === 'heatseeker') { if (typeof initHeatSeeker === 'function') initHeatSeeker(); }
}
function renderSubtabs(m) {
  const bar = $('subTabbar');
  bar.innerHTML = (SUBTABS[m] || []).map(s =>
    s.sep ? '<div class="stab-sep"></div>'
          : `<button class="stab" id="st-${s.id}" onclick="setSub('${s.id}')">${s.label}</button>`
  ).join('');
}
function setSub(s) {
  _subTab = s;
  localStorage.setItem('chakra-sub', s);
  document.querySelectorAll('.stab').forEach(b => b.classList.remove('active'));
  $('st-' + s)?.classList.add('active');
  // If current main tab has no subtabs (e.g. heatseeker, power), show its direct pane instead
  if (!SUBTABS[_mainTab] || !SUBTABS[_mainTab].length) {
    document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
    const directPane = $('p-' + _mainTab + '-lotto') || $('p-' + _mainTab);
    if (directPane) directPane.classList.add('active');
    return;
  }
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  const pane = $('p-' + _mainTab + '-' + s);
  if (pane) pane.classList.add('active');
  loadPane(_mainTab + '-' + s);
}

function loadPane(key) {
  if      (key === 'arjun-signals')         {
    if (window._cache) { delete window._cache['/api/signals']; delete window._cache['/api/prices/live']; }
    loadSignals();
  }
  else if (key === 'arjun-performance')     loadPerformance();
  else if (key === 'trading-arka-indexes')  loadArkaIndexes();
  else if (key === 'trading-arka-stocks')   loadArkaStocks();
  else if (key === 'trading-arka-lotto-tab') { loadLottoLive(); }
  else if (key === 'trading-arka')          loadArka();
  else if (key === 'trading-chakra-swings') loadSwingsWatchlist();
  else if (key === 'trading-taraka')        loadTaraka();
  else if (key === 'analysis-analysis')     loadMarketBriefing();
  else if (key === 'analysis-gex')          loadGex();
  else if (key === 'analysis-cockpit')      { if (typeof initCockpit === 'function') initCockpit(); }
  else if (key === 'analysis-physics')      { loadPhysics(); loadHelixEngine(); }
  else if (key === 'analysis-flow')         { loadFlow(); }
  else if (key === 'analysis-helx')        { loadHelx(); loadExecutionGates(); loadArkaModifier(); loadMarketSignals(); loadInternalsCards(); }
  else if (key === 'analysis-head')         loadHead();
}

// ── Market status ──────────────────────────────────────────
function updMkt() {
  const now = new Date();
  const et = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const h = et.getHours(), m = et.getMinutes(), d = et.getDay();
  const isWeekday = d >= 1 && d <= 5;
  const isOpen  = isWeekday && (h > 9 || (h === 9 && m >= 30)) && h < 16;
  const isPre   = isWeekday && h >= 4 && (h < 9 || (h === 9 && m < 30));
  const el = $('mktBadge');
  if (isOpen)      { el.textContent = '● LIVE';     el.className = 'live'; }
  else if (isPre)  { el.textContent = '● PRE-MKT';  el.className = 'premarket'; }
  else             { el.textContent = '● CLOSED';   el.className = 'closed'; }

  const isPH    = isWeekday && isOpen && (h > 15 || (h === 15 && m >= 30));
  const isPrePH = isWeekday && isOpen && !isPH;
  const wrap    = $('lottoBadgeWrap');
  const lottoEl = $('lottoStatus');
  if (lottoEl) {
    if (isPH) {
      lottoEl.textContent = '🔥 UNLEASHED';
      lottoEl.style.color = '';
      if (wrap) wrap.className = 'lotto-hero state-unleashed';
    } else if (isPrePH) {
      lottoEl.textContent = 'STANDBY';
      lottoEl.style.color = '';
      if (wrap) wrap.className = 'lotto-hero state-standby';
    } else {
      lottoEl.textContent = 'WAITING';
      lottoEl.style.color = '';
      if (wrap) wrap.className = 'lotto-hero state-waiting';
    }
  }
  if (typeof loadLottoLive === "function") loadLottoLive(); // Always load lotto cards
}

// ── API cache — prevents duplicate calls within 30s ────────
const _cache = {};
window._cache = _cache;  // expose so other scripts can clear entries

// Clear stale cache on page load to prevent blank data after refresh
window.addEventListener('load', () => {
  Object.keys(_cache).forEach(k => delete _cache[k]);
});
async function getCached(url, ttlMs = 30000) {
  const now = Date.now();
  if (_cache[url] && (now - _cache[url].ts) < ttlMs) return _cache[url].data;
  const data = await get(url);
  if (data) _cache[url] = { data, ts: now };
  return data;
}

// ── Header data — signals + P&L only (sidebar owns portfolio) ──
async function loadHeader() {
  // Fire account + signals in parallel but with caching
  const [acct, sigs] = await Promise.all([
    getCached('/api/account', 15000),
    getCached('/api/signals', 20000),
  ]);
  if (acct) {
    const pv = parseFloat(acct.portfolio_value || 0);
    if($('hPV')) $('hPV').textContent = f$(pv);
    if($('hPV')) $('hPV').style.color = pv >= 100000 ? 'var(--green)' : 'var(--gold)';
    if($('hCash')) $('hCash').textContent = 'Cash: ' + f$(acct.cash);
  }
  if (sigs && Array.isArray(sigs)) {
    const b  = sigs.filter(s => s.signal?.toUpperCase() === 'BUY').length;
    const se = sigs.filter(s => s.signal?.toUpperCase() === 'SELL').length;
    const h  = sigs.filter(s => s.signal?.toUpperCase() === 'HOLD').length;
    $('hSig').textContent  = sigs.length;
    $('hSigB').textContent = b + ' BUY · ' + se + ' SELL · ' + h + ' HOLD';
    // Also update P&L from arka summary (don't load /api/stats separately)
  }
  // Sidebar loads separately, non-blocking
  loadSidebar(acct, null);
}

// ── Portfolio Sidebar ──────────────────────────────────────
async function loadSidebar(acct, _unused) {
  if (!acct) return;
  const pv       = parseFloat(acct.portfolio_value || acct.equity || 0);
  const equity   = parseFloat(acct.equity || 0);
  const lastEq   = parseFloat(acct.last_equity || equity);
  const cash     = parseFloat(acct.cash || 0);
  const bp       = parseFloat(acct.buying_power || 0);
  const openPos  = (acct.positions || []).length;

  if (!$('sbTotalVal')) return;
  $('sbTotalVal').textContent = f$(pv);  // actual portfolio value from Alpaca
  if ($('sbCash'))    $('sbCash').textContent    = f$(cash);
  if ($('sbOpenPos')) $('sbOpenPos').textContent = openPos;
  if ($('sbBP'))      $('sbBP').textContent      = f$(bp);

  // ── Per-bucket P&L from real open positions ──
  // Contract symbol format: TICKER + YYMMDD + C/P + STRIKE, e.g. SPY260415C00688000
  const todayStr = (() => {
    const d = new Date();
    const yy = String(d.getFullYear()).slice(2);
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return yy + mm + dd;
  })();

  let odteUnr = 0, odteOpen = 0, odteCost = 0;
  let swingUnr = 0, swingOpen = 0, swingCost = 0;

  const allPos = acct.positions || [];
  for (const p of allPos) {
    const sym = (p.symbol || '').toUpperCase();
    const unr = parseFloat(p.unrealized_pl || 0);
    const mv  = parseFloat(p.market_value || 0);
    const cb  = parseFloat(p.cost_basis || (mv - unr));
    const m   = sym.match(/^[A-Z]+(\d{6})[CP]/);
    if (!m) continue;  // skip equity/non-options positions
    if (m[1] === todayStr) {
      odteUnr  += unr;  odteCost += Math.abs(cb);  odteOpen++;
    } else {
      swingUnr += unr;  swingCost += Math.abs(cb); swingOpen++;
    }
  }

  // Get ARKA summary — provides per-bucket realized P&L from closed trades today
  const arkaSumm = await getCached('/api/arka/summary', 30000);
  const odteRealizedPnl  = parseFloat(arkaSumm?.odte_realized_pnl  ?? 0);
  const swingRealizedPnl = parseFloat(arkaSumm?.swing_realized_pnl ?? 0);

  // Bucket P&L = realized (closed trades today) + unrealized (open positions)
  // This ensures ODTE + SWING = TOTAL always add up
  const odtePnlTotal  = odteRealizedPnl  + odteUnr;
  const swingPnlTotal = swingRealizedPnl + swingUnr;

  // Prefer Alpaca's own equity-delta as the TOTAL P&L (most accurate).
  // Fall back to sum of buckets when equity data is unavailable.
  const alpacaDelta   = equity > 0 ? equity - lastEq : null;
  const combinedPnl   = alpacaDelta !== null ? alpacaDelta : (odtePnlTotal + swingPnlTotal);

  // Remaining budget = cap + realized_pnl - open_cost_basis
  const ODTE_BUDGET  = 2000;
  const SWING_BUDGET = 2000;
  const odteRemain   = Math.max(0, ODTE_BUDGET  + odteRealizedPnl  - odteCost);
  const swingRemain  = Math.max(0, SWING_BUDGET + swingRealizedPnl - swingCost);

  // P&L is the big number; budget remaining is the small sub
  $('sbOdtePnl').textContent   = (odtePnlTotal >= 0 ? '+' : '') + f$(odtePnlTotal);
  $('sbOdtePnl').style.color   = odtePnlTotal >= 0 ? 'var(--green)' : 'var(--red)';
  $('sbOdteVal').textContent   = f$(odteRemain) + ' left';
  $('sbOdteOpen').textContent  = odteOpen + ' open';

  $('sbSwingPnl').textContent  = (swingPnlTotal >= 0 ? '+' : '') + f$(swingPnlTotal);
  $('sbSwingPnl').style.color  = swingPnlTotal >= 0 ? 'var(--green)' : 'var(--red)';
  $('sbSwingVal').textContent  = f$(swingRemain) + ' left';
  $('sbSwingOpen').textContent = swingOpen + ' open';

  // Budget progress bars: show % of cap consumed
  const _odteUsed  = Math.min(100, Math.max(0, (1 - odteRemain  / ODTE_BUDGET)  * 100));
  const _swingUsed = Math.min(100, Math.max(0, (1 - swingRemain / SWING_BUDGET) * 100));
  if ($('sbOdteBar'))  { $('sbOdteBar').style.width  = _odteUsed.toFixed(0)  + '%'; }
  if ($('sbSwingBar')) { $('sbSwingBar').style.width = _swingUsed.toFixed(0) + '%'; }

  // TOTAL P&L = ODTE + SWING (so the numbers always add up)
  if ($('sbTotalPnlBox')) {
    $('sbTotalPnlBox').textContent = (combinedPnl >= 0 ? '+' : '') + f$(combinedPnl);
    $('sbTotalPnlBox').style.color = combinedPnl >= 0 ? 'var(--green)' : 'var(--red)';
  }
  // Header "Today" and sidebar "Today:" also use the combined P&L
  $('sbTotalPnl').textContent = (combinedPnl >= 0 ? '+' : '') + f$(combinedPnl);
  $('sbTotalPnl').style.color = combinedPnl >= 0 ? 'var(--green)' : 'var(--red)';
  $('hPnl').textContent = (combinedPnl >= 0 ? '+' : '') + f$(combinedPnl);
  $('hPnl').style.color = combinedPnl >= 0 ? 'var(--green)' : 'var(--red)';

  // Open positions = all options positions (not equity)
  $('sbOpenPos').textContent = odteOpen + swingOpen;

  if (arkaSumm) {
    const tlog = arkaSumm.trade_log || [];
    const recentEl = $('sbRecentTrades');
    if (recentEl) {
      recentEl.innerHTML = tlog.length
        ? tlog.slice(-5).reverse().map(t => {
            const hasPnl = t.pnl != null && t.pnl !== '';
            const p = hasPnl ? parseFloat(t.pnl) : null;
            const pnlHtml = hasPnl
              ? `<span class="sb-trade-result" style="color:${p >= 0 ? 'var(--green)' : 'var(--red)'}">
                  ${p >= 0 ? '+' : ''}${f$(p)}</span>`
              : `<span class="sb-trade-result" style="color:var(--gold,#f5a623);font-size:9px">OPEN</span>`;
            return `<div class="sb-trade">
              <div class="sb-trade-head">
                <span class="sb-trade-ticker">${t.ticker || '—'}</span>
                ${pnlHtml}
              </div>
              <div class="sb-trade-meta">${t.side || '—'} · ${t.time || '—'}</div>
            </div>`;
          }).join('')
        : '<div style="color:var(--sub);font-size:10px;padding:4px 0">No trades today</div>';
    }
    if (arkaSumm.scan_history && typeof loadSidebarScanFeed === 'function') {
      loadSidebarScanFeed(arkaSumm.scan_history);
    }
  }
}

// ── Theme ──────────────────────────────────────────────────
function toggleTheme() {
  const html = document.documentElement;
  const isLight = html.getAttribute('data-theme') === 'light';
  const next = isLight ? 'dark' : 'light';
  html.setAttribute('data-theme', next === 'light' ? 'light' : '');
  $('themeBtn').textContent = next === 'light' ? '☀️' : '🌙';
  localStorage.setItem('chakra-theme', next);
}
function applyTheme() {
  const saved = localStorage.getItem('chakra-theme') || 'dark';
  if (saved === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
    const btn = $('themeBtn');
    if (btn) btn.textContent = '☀️';
  }
}

// ── Live price ticker ──────────────────────────────────────
let _prevPrices = {}, _pxTimer = null, _hdrTimer = null;

async function tickPrices() {
  const px = await get('/api/prices/live');
  if (!px || px.error) return;
  document.querySelectorAll('[data-sym]').forEach(card => {
    const sym = card.dataset.sym;
    const data = px[sym];
    if (!data) return;
    const priceEl = $('px-price-' + sym);
    const chgEl   = $('px-chg-'   + sym);
    if (!priceEl || !chgEl) return;
    const newPrice = parseFloat(data.price || 0);
    const pct = parseFloat(data.chg_pct || 0);
    const prev = _prevPrices[sym];
    if (priceEl.textContent !== f$(newPrice)) {
      priceEl.textContent = f$(newPrice);
      chgEl.textContent   = fp(pct);
      chgEl.style.color   = pc(pct);
      if (prev !== undefined) {
        const fc = newPrice > prev ? 'px-up' : 'px-dn';
        priceEl.classList.remove('px-up', 'px-dn');
        void priceEl.offsetWidth;
        priceEl.classList.add(fc);
      }
      _prevPrices[sym] = newPrice;
    }
  });
  const lu = $('lastUpdate');
  if (lu) lu.innerHTML = '<span class="live-dot"></span>Live · ' +
    new Date().toLocaleTimeString('en-US', { hour:'2-digit', minute:'2-digit', second:'2-digit' });
}

function startLiveTicker() {
  if (_pxTimer)  clearInterval(_pxTimer);
  if (_hdrTimer) clearInterval(_hdrTimer);
  tickPrices();
  _pxTimer  = setInterval(tickPrices,   10000);
  _hdrTimer = setInterval(loadHeader,   15000);
}

// ── Auto-refresh ───────────────────────────────────────────
let _refreshTimer = null, _refreshSecs = 60;
function setRefreshInterval(secs) {
  _refreshSecs = parseInt(secs) || 60;
  if (_refreshTimer) clearInterval(_refreshTimer);
  _refreshTimer = setInterval(silentRefresh, _refreshSecs * 1000);
}
async function silentRefresh() {
  const key = _mainTab + '-' + _subTab;
  if      (key === 'arjun-signals')       loadSignals();
  else if (key === 'trading-arka')        loadArka();
  else if (key === 'analysis-gex')        loadGex();
  else if (key === 'analysis-physics')    { loadPhysics(); loadHelixEngine(); }
  // analysis-helx handled above
}
function manualRefresh() {
  const icon = $('refreshIcon');
  icon.classList.add('spinning');
  Promise.all([loadHeader(), loadPane(_mainTab + '-' + _subTab), tickPrices()])
    .finally(() => {
      setTimeout(() => icon.classList.remove('spinning'), 600);
      toast('✓ Refreshed');
    });
}

// ── SYSTEM tab activation ──────────────────────────────────
const SYS_SUBTABS = [
  { id:'overview', label:'🖥️ Overview' },
  { id:'modules',  label:'⚡ Modules' },
  { id:'crons',    label:'⏱ Crons' },
  { id:'pipeline', label:'📋 Pipeline' },
];
function activateSystemTab() {
  document.querySelectorAll('.mtab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  $('mt-system')?.classList.add('active');
  const bar = $('subTabbar');
  bar.innerHTML = SYS_SUBTABS.map((t, i) =>
    `<button class="stab${i === 0 ? ' active' : ''}" onclick="sysSubTab('${t.id}',this)">${t.label}</button>`
  ).join('<div class="stab-sep"></div>');
  $('p-system-overview')?.classList.add('active');
  loadSystem();
}
function sysSubTab(id, btn) {
  document.querySelectorAll('.stab').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.querySelectorAll('[id^="p-system-"]').forEach(p => p.classList.remove('active'));
  $('p-system-' + id)?.classList.add('active');
  if (id === 'modules')  loadSysModules();
  if (id === 'crons')    loadSysCrons();
  if (id === 'pipeline') loadSysPipeline();
}

// ── Lotto countdown ────────────────────────────────────────
const MARKET_HOLIDAYS_2026 = [
  '2026-01-01','2026-01-19','2026-02-16','2026-04-03',
  '2026-05-25','2026-07-03','2026-09-07','2026-11-26','2026-12-25'
];

function isMarketDay(d) {
  const day = d.getDay();
  if (day === 0 || day === 6) return false;
  const iso = d.toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
  return !MARKET_HOLIDAYS_2026.includes(iso);
}

function nextLottoTime() {
  const now = new Date();
  const etStr = now.toLocaleString('en-US', { timeZone: 'America/New_York' });
  const etNow = new Date(etStr);

  // etNow is the "local-time hack": its .getHours() == ET hours, but .getTime() is off
  // by (EToffset − localOffset). Compute that skew so setHours(15,30) lands on real UTC.
  const etVsLocal = etNow.getTime() - now.getTime(); // ms skew

  function lotto330(hackDate) {
    const d = new Date(hackDate);
    d.setHours(15, 30, 0, 0);             // sets LOCAL hours to 15:30
    return new Date(d.getTime() - etVsLocal); // shift to real UTC for 15:30 ET
  }

  const todayTarget = lotto330(etNow);
  if (isMarketDay(etNow) && now < todayTarget) return todayTarget;

  // Find next market day
  let next = new Date(etNow);
  next.setDate(next.getDate() + 1);
  next.setHours(0, 0, 0, 0);
  let attempts = 0;
  while (!isMarketDay(next) && attempts < 10) {
    next.setDate(next.getDate() + 1);
    attempts++;
  }
  return lotto330(next);
}

function updateLottoCountdown() {
  const el = $('lotto-countdown'); if (!el) return;
  const phEl = $('phStatus');
  const now = new Date();
  const etStr = now.toLocaleString('en-US', { timeZone: 'America/New_York' });
  const etNow = new Date(etStr);
  const day = etNow.getDay();
  const isWeekend = day === 0 || day === 6;
  const iso = etNow.toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
  const isHoliday = MARKET_HOLIDAYS_2026.includes(iso);
  const isClosed  = isWeekend || isHoliday;
  const h = etNow.getHours(), m = etNow.getMinutes();
  const isLive = !isClosed && h === 15 && m >= 30 && m < 58;

  if (isLive) {
    el.textContent = '🎰 LIVE NOW';
    el.className = 'lotto-cd cd-live';
    el.style.color = '';
    return;
  }

  if (isClosed && phEl) {
    const dayName = day === 6 ? 'Saturday' : day === 0 ? 'Sunday' : 'Holiday';
    const wheels = Array.from({length:12}, (_, i) => {
      const a = i * 30 * Math.PI / 180;
      const x1 = 40 + 30*Math.sin(a), y1 = 40 - 30*Math.cos(a);
      const x2 = 40 + 20*Math.sin(a), y2 = 40 - 20*Math.cos(a);
      const col = i%3===0 ? '#ff3d5a' : i%3===1 ? '#00d084' : '#ffd700';
      return '<line x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+'" stroke="'+col+'" stroke-width="3" stroke-linecap="round"/>';
    }).join('');
    const dots = Array.from({length:6}, (_, i) => {
      const a = (i*60+15) * Math.PI / 180;
      const x = 40 + 33*Math.sin(a), y = 40 - 33*Math.cos(a);
      return '<circle cx="'+x+'" cy="'+y+'" r="2" fill="white" opacity="0.8"/>';
    }).join('');
    phEl.innerHTML =
      '<div style="text-align:center;padding:20px">' +
        '<svg width="80" height="80" viewBox="0 0 80 80" style="animation:spin 3s linear infinite">' +
          '<defs><style>@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}</style></defs>' +
          '<circle cx="40" cy="40" r="36" fill="none" stroke="#6c35de" stroke-width="3"/>' +
          '<circle cx="40" cy="40" r="28" fill="none" stroke="#6c35de" stroke-width="1" opacity="0.4"/>' +
          '<circle cx="40" cy="40" r="6" fill="#6c35de"/>' +
          wheels + dots +
        '</svg>' +
        '<div style="font-size:11px;color:var(--sub);margin-top:8px">Power Hour Lotto</div>' +
        '<div style="font-size:9px;color:var(--sub)">3:30–3:58 PM ET · Hero or Zero</div>' +
        '<div style="font-size:10px;color:var(--purple);margin-top:4px">'+dayName+' — Market Closed</div>' +
      '</div>';
  }

  const target = nextLottoTime();
  const diffMs = target.getTime() - now.getTime();
  if (diffMs <= 0) { el.textContent = '🎰 LIVE NOW'; return; }
  const secs = Math.floor(diffMs / 1000);
  const hrs  = Math.floor(secs / 3600);
  const mins = Math.floor((secs % 3600) / 60);
  const sec  = secs % 60;
  const pad  = n => n.toString().padStart(2, '0');
  const isHot = hrs === 0 && mins < 30;
  el.className = isHot ? 'lotto-cd cd-hot' : 'lotto-cd';
  el.style.color = '';
  if (isClosed) {
    const daysUntil = Math.ceil(diffMs / 86400000);
    const label = daysUntil <= 1 ? 'Tomorrow' : daysUntil === 2 ? 'Monday' : 'in '+daysUntil+'d';
    el.textContent = label + ' ' + pad(hrs%24) + ':' + pad(mins) + ':' + pad(sec);
  } else {
    el.textContent = pad(hrs) + ':' + pad(mins) + ':' + pad(sec) + ' ET';
  }
}


// ── Init ───────────────────────────────────────────────────
applyTheme();
updMkt();

async function loadArkaIndexes() {
  const cardsEl = $('indexGexCards');
  if (!cardsEl) return;
  // Clear stale cache for index data
  if (window._cache) {
    Object.keys(window._cache).forEach(k => {
      if (k.includes('options/gex') || k.includes('ticker/analyze') || k.includes('signals'))
        delete window._cache[k];
    });
  }
  const indexes = ['SPX','RUT','SPY','QQQ','IWM','DIA'];
  cardsEl.innerHTML = '<div class="ld"><div class="spin"></div></div>';
  cardsEl.style.gridTemplateColumns = '';

  // Fetch GEX + ARJUN signals + live prices + technicals in parallel
  const [gexCards, signals, livePrices, techCards] = await Promise.all([
    Promise.all(indexes.map(t => getCached('/api/options/gex?ticker='+t, 60000))),
    getCached('/api/signals', 20000),
    getCached('/api/prices/live', 15000).catch(() => ({})),
    Promise.all(indexes.map(t => getCached('/api/ticker/analyze?ticker='+t, 120000).catch(() => null))),
  ]);
  const sigMap = {};
  (signals||[]).forEach(s => { sigMap[s.ticker] = s; });

  cardsEl.innerHTML = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">' +
  indexes.map((t,i) => {
    const g   = gexCards[i] || {};
    const sig = sigMap[t]   || {};

    // GEX
    const regime  = g.regime || 'unknown';
    const regCol  = regime==='explosive'?'var(--red)':regime==='normal'?'var(--green)':'var(--gold)';
    const net     = parseFloat(g.net_gex||0);
    const netStr  = (net>=0?'+':'')+net.toFixed(3)+'B';
    const gexDir  = net >= 0 ? 'POS' : 'NEG';
    const gexDirCol = net >= 0 ? 'var(--green)' : 'var(--red)';

    // ARJUN signal
    const conf    = sig && sig.confidence ? parseFloat(sig.confidence) : 0;
    const signal  = sig.signal || 'HOLD';
    const price   = parseFloat((livePrices[t]?.price)||g.spx_price||g.spot||sig.price||0);
    const pct     = parseFloat((livePrices[t]?.chg_pct)||0);
    const pctCol  = pct >= 0 ? 'var(--green)' : 'var(--red)';
    const pctStr  = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
    const target  = parseFloat(sig.target||0);
    const stop    = parseFloat(sig.stop_loss||0);
    const rr      = parseFloat(sig.risk_reward||0);
    const agents  = sig.agents || {};
    const bull    = parseFloat(agents.bull?.score||0);
    const bear    = parseFloat(agents.bear?.score||0);
    const analyst = agents.analyst || {};
    const bearFactors = (analyst.bear_factors||[]).slice(0,3);
    const bullFactors = (analyst.bull_factors||[]).slice(0,2);

    // Crowd sentiment
    const isBull  = bull > bear;
    const sentCol = isBull ? 'var(--green)' : 'var(--red)';
    const sentTxt = isBull ? 'BULLISH' : 'BEARISH';

    // RSI from technicals API — SPX uses SPY proxy, RUT uses IWM proxy
    const techProxyIdx = t==='SPX' ? indexes.indexOf('SPY') : t==='RUT' ? indexes.indexOf('IWM') : i;
    const tech     = (techProxyIdx>=0 ? techCards[techProxyIdx] : techCards[i])?.sections?.technicals || {};
    const rsiMatch = (sig.explanation||'').match(/RSI\s+([\d.]+)/);
    const rsi      = tech.rsi ? parseFloat(tech.rsi).toFixed(1) : rsiMatch ? parseFloat(rsiMatch[1]).toFixed(1) : '—';
    const rsiSignal = tech.rsi_signal || '';
    const rsiCol   = rsiSignal==='OVERBOUGHT' ? 'var(--red)' : rsiSignal==='OVERSOLD' ? 'var(--green)' : 'var(--fg)';

    // Discomfort index (bear score as proxy)
    const discomfort = Math.round(bear);
    const discCol    = discomfort>=70?'var(--red)':discomfort>=50?'var(--gold)':'var(--green)';

    // Recommendation
    const isBlocked = (sig.explanation||'').toLowerCase().includes('block');
    const recText   = isBlocked
      ? 'BLOCKED: ' + (sig.explanation||'').match(/BLOCK[^.—]*/i)?.[0]?.replace('BLOCK — ','')?.slice(0,60) + '...'
      : signal + ' ' + t + ' @ $' + price.toFixed(2);
    const recCol    = isBlocked ? 'var(--red)' : signal==='BUY'?'var(--green)':signal==='SELL'?'var(--red)':'var(--sub)';

    // Option contract suggestion
    const contractSide = signal==='BUY' ? 'CALL' : signal==='SELL' ? 'PUT' : '—';
    const contractStrike = signal==='BUY' ? (g.call_wall||'—') : signal==='SELL' ? (g.put_wall||'—') : '—';

    return '<div style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden">' +
      // Header
      '<div style="padding:12px 14px;display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid var(--border)">' +
        '<div>' +
          '<div style="font-family:JetBrains Mono,monospace;font-weight:800;font-size:20px" data-sym="'+t+'">' + t + '</div>' +
          '<div style="font-size:10px;color:var(--sub)"><span id="px-price-'+t+'">$' + price.toFixed(2) + '</span> <span id="px-chg-'+t+'" style="color:'+pctCol+'">'+pctStr+'</span></div>' +
        '</div>' +
        '<div style="text-align:right">' +
          '<div style="font-size:9px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">Crowd Sentiment</div>' +
          '<div style="font-size:11px;font-weight:700;color:'+sentCol+'">'+sentTxt+'</div>' +
        '</div>' +
      '</div>' +
      // RSI / Vol / GEX row
      '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;border-bottom:1px solid var(--border)">' +
        '<div style="padding:8px 12px;border-right:1px solid var(--border)">' +
          '<div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">RSI (14)</div>' +
          '<div style="font-family:JetBrains Mono,monospace;font-size:18px;font-weight:700;color:'+rsiCol+'">' + rsi + '</div>' +
          '<div style="font-size:8px;color:'+sentCol+'">' + (analyst.bias||'—') + '</div>' +
        '</div>' +
        '<div style="padding:8px 12px;border-right:1px solid var(--border)">' +
          '<div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">GEX</div>' +
          '<div style="font-family:JetBrains Mono,monospace;font-size:18px;font-weight:700;color:'+gexDirCol+'">' + gexDir + '</div>' +
          '<div style="font-size:8px;color:'+gexDirCol+'">' + regime.toUpperCase() + '</div>' +
        '</div>' +
        '<div style="padding:8px 12px">' +
          '<div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">Confidence</div>' +
          '<div style="font-family:JetBrains Mono,monospace;font-size:18px;font-weight:700">' + conf.toFixed(0) + '</div>' +
          '<div style="font-size:8px;color:var(--sub)">' + signal + '</div>' +
        '</div>' +
      '</div>' +
      // GEX Levels
      '<div style="padding:10px 14px;border-bottom:1px solid var(--border)">' +
        '<div style="display:flex;justify-content:space-between;margin-bottom:4px">' +
          '<span style="font-size:9px;color:var(--sub)">CALL WALL</span>' +
          '<span style="font-family:JetBrains Mono,monospace;font-size:11px;color:var(--green);font-weight:700">$' + (g.call_wall||'—') + '</span>' +
        '</div>' +
        '<div style="display:flex;justify-content:space-between;margin-bottom:4px">' +
          '<span style="font-size:9px;color:var(--sub)">PUT WALL</span>' +
          '<span style="font-family:JetBrains Mono,monospace;font-size:11px;color:var(--red);font-weight:700">$' + (g.put_wall||'—') + '</span>' +
        '</div>' +
        '<div style="display:flex;justify-content:space-between;margin-bottom:4px">' +
          '<span style="font-size:9px;color:var(--sub)">ZERO GAMMA</span>' +
          '<span style="font-family:JetBrains Mono,monospace;font-size:11px;font-weight:700">$' + (g.zero_gamma||'—') + '</span>' +
        '</div>' +
        '<div style="display:flex;justify-content:space-between">' +
          '<span style="font-size:9px;color:var(--sub)">NET GEX</span>' +
          '<span style="font-family:JetBrains Mono,monospace;font-size:11px;color:'+gexDirCol+';font-weight:700">' + netStr + '</span>' +
        '</div>' +
      '</div>' +
      // Discomfort Index
      '<div style="padding:10px 14px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">' +
        '<div>' +
          '<div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:2px">Discomfort Index</div>' +
          '<div style="background:var(--bg3);border-radius:4px;height:4px;width:120px;overflow:hidden">' +
            '<div style="background:'+discCol+';height:100%;width:'+discomfort+'%;border-radius:4px"></div>' +
          '</div>' +
        '</div>' +
        '<div style="font-family:JetBrains Mono,monospace;font-size:20px;font-weight:700;color:'+discCol+'">' + discomfort + '%</div>' +
      '</div>' +
      // ARKA Recommends
      '<div style="padding:10px 14px;border-bottom:1px solid var(--border)">' +
        '<div style="font-size:7px;letter-spacing:2px;color:var(--sub);text-transform:uppercase;margin-bottom:6px">ARKA RECOMMENDS</div>' +
        '<div style="font-size:12px;font-weight:700;color:'+recCol+';line-height:1.4">' + recText + '</div>' +
        (contractSide !== '—' ? '<div style="margin-top:6px;font-size:9px;padding:3px 8px;border-radius:3px;background:var(--bg3);color:var(--sub);display:inline-block">0DTE $'+contractStrike+' '+contractSide+' · R/R '+rr.toFixed(2)+'</div>' : '') +
      '</div>' +
      // Bull/Bear scores
      '<div style="padding:8px 14px;display:flex;justify-content:space-between;align-items:center">' +
        '<div style="display:flex;gap:12px">' +
          '<span style="font-size:9px">🐂 <span style="font-family:JetBrains Mono,monospace;color:var(--green)">' + bull.toFixed(0) + '</span></span>' +
          '<span style="font-size:9px">🐻 <span style="font-family:JetBrains Mono,monospace;color:var(--red)">' + bear.toFixed(0) + '</span></span>' +
        '</div>' +
        '<div style="font-size:8px;padding:2px 8px;border-radius:3px;background:'+regCol+'20;color:'+regCol+';font-weight:700;text-transform:uppercase">' + regime + '</div>' +
      '</div>' +
    '</div>';
  }).join('') + '</div>';
}

async function loadArkaStocks() {
  loadOpenPositionsPanel();
  const el = $('stocksFlowFeed');
  if (!el) return;
  try {
    const signals = await getCached('/api/flow/signals', 30000);
    const list = (signals?.signals || []).filter(s =>
      !['SPY','QQQ','IWM','DIA','SPX'].includes(s.ticker)
    );
    const cnt = $('stocksCount');
    if (cnt) cnt.textContent = list.length + ' signals';

    // Extreme filter toggle
    const extremeOnly = window._stocksExtremeOnly || false;
    const filteredList = extremeOnly ? list.filter(s => s.is_extreme) : list;
    const extremeCount = list.filter(s => s.is_extreme).length;

    // Add filter controls above grid
    const filterBar = $('stocksFilterBar');
    if (!filterBar) {
      const fb = document.createElement('div');
      fb.id = 'stocksFilterBar';
      fb.style.cssText = 'display:flex;gap:8px;align-items:center;padding:8px 0 12px';
      fb.innerHTML = `
        <button onclick="window._stocksExtremeOnly=false;loadArkaStocks()"
          style="font-size:9px;padding:3px 10px;border-radius:4px;cursor:pointer;font-family:JetBrains Mono,monospace;
          border:1px solid var(--border);background:${!extremeOnly?'var(--accent)':'var(--bg3)'};color:${!extremeOnly?'#fff':'var(--sub)'}">
          ALL (${list.length})
        </button>
        <button onclick="window._stocksExtremeOnly=true;loadArkaStocks()"
          style="font-size:9px;padding:3px 10px;border-radius:4px;cursor:pointer;font-family:JetBrains Mono,monospace;
          border:1px solid rgba(255,61,90,0.4);background:${extremeOnly?'rgba(255,61,90,0.8)':'rgba(255,61,90,0.1)'};color:${extremeOnly?'#fff':'var(--red)'}">
          🔥 EXTREME ONLY (${extremeCount})
        </button>`;
      el.parentNode.insertBefore(fb, el);
    } else {
      filterBar.innerHTML = `
        <button onclick="window._stocksExtremeOnly=false;loadArkaStocks()"
          style="font-size:9px;padding:3px 10px;border-radius:4px;cursor:pointer;font-family:JetBrains Mono,monospace;
          border:1px solid var(--border);background:${!extremeOnly?'var(--accent)':'var(--bg3)'};color:${!extremeOnly?'#fff':'var(--sub)'}">
          ALL (${list.length})
        </button>
        <button onclick="window._stocksExtremeOnly=true;loadArkaStocks()"
          style="font-size:9px;padding:3px 10px;border-radius:4px;cursor:pointer;font-family:JetBrains Mono,monospace;
          border:1px solid rgba(255,61,90,0.4);background:${extremeOnly?'rgba(255,61,90,0.8)':'rgba(255,61,90,0.1)'};color:${extremeOnly?'#fff':'var(--red)'}">
          🔥 EXTREME ONLY (${extremeCount})
        </button>`;
    }

    if (!filteredList.length) {
      el.innerHTML = '<div class="empty" style="padding:40px;text-align:center">No stock flow signals yet<br><span style="font-size:10px;color:var(--sub)">Flow monitor scans every 5 min during market hours</span></div>';
      return;
    }
    // CHAKRA-style cards in a grid
    el.innerHTML = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;padding:4px">' +
    filteredList.map(s => {
      const isBull  = s.bias === 'BULLISH';
      const isBear  = s.bias === 'BEARISH';
      const col     = isBull ? 'var(--green)' : isBear ? 'var(--red)' : 'var(--sub)';
      const bgCol   = isBull ? 'rgba(0,208,132,0.06)' : isBear ? 'rgba(255,61,90,0.06)' : 'var(--bg2)';
      const conf    = Math.round(s.confidence || 0);
      const ratio   = Math.round(s.vol_oi_ratio || 0);
      const dp      = Math.round((s.dark_pool_pct || 0) * 100);
      const extreme = s.is_extreme;
      const action  = isBull ? 'BUY CALLS' : isBear ? 'BUY PUTS' : 'WATCH';
      const actionCol = isBull ? 'var(--green)' : isBear ? 'var(--red)' : 'var(--sub)';
      const ts      = s.timestamp ? new Date(s.timestamp).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'}) : '';
      return '<div style="background:' + bgCol + ';border:1px solid var(--border);border-radius:10px;padding:14px;position:relative">' +
        // Header
        '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">' +
          '<div>' +
            '<div style="font-family:JetBrains Mono,monospace;font-weight:800;font-size:18px">' + s.ticker + '</div>' +
            (ts ? '<div style="font-size:9px;color:var(--sub)">' + ts + '</div>' : '') +
          '</div>' +
          '<div style="text-align:right">' +
            (extreme ? '<div style="font-size:8px;font-weight:700;padding:2px 6px;border-radius:3px;background:rgba(255,61,90,0.2);color:var(--red);margin-bottom:3px">🔥 EXTREME</div>' : '') +
            '<div style="font-size:11px;font-weight:700;color:' + col + '">' + s.bias + '</div>' +
          '</div>' +
        '</div>' +
        // Stats row
        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px">' +
          '<div style="background:var(--bg3);border-radius:6px;padding:6px 8px;text-align:center">' +
            '<div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">Vol/OI</div>' +
            '<div style="font-family:JetBrains Mono,monospace;font-size:14px;font-weight:700">' + (ratio > 0 ? ratio + 'x' : '—') + '</div>' +
          '</div>' +
          '<div style="background:var(--bg3);border-radius:6px;padding:6px 8px;text-align:center">' +
            '<div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">Confidence</div>' +
            '<div style="font-family:JetBrains Mono,monospace;font-size:14px;font-weight:700;color:' + col + '">' + conf + '%</div>' +
          '</div>' +
          '<div style="background:var(--bg3);border-radius:6px;padding:6px 8px;text-align:center">' +
            '<div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">Dark Pool</div>' +
            '<div style="font-family:JetBrains Mono,monospace;font-size:14px;font-weight:700">' + (dp > 0 ? dp + '%' : '—') + '</div>' +
          '</div>' +
        '</div>' +
        // Confidence bar
        '<div style="background:var(--bg3);border-radius:4px;height:4px;margin-bottom:10px;overflow:hidden">' +
          '<div style="background:' + col + ';height:100%;width:' + conf + '%;border-radius:4px;transition:width 0.3s"></div>' +
        '</div>' +
        // ARKA Recommends
        '<div style="border-top:1px solid var(--border);padding-top:10px">' +
          '<div style="font-size:7px;letter-spacing:2px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">ARKA RECOMMENDS</div>' +
          '<div style="font-size:13px;font-weight:700;color:' + actionCol + '">' + action + ' on ' + s.ticker + '</div>' +
          '<div style="font-size:9px;color:var(--sub);margin-top:2px">Confidence: ' + conf + '%</div>' +
        '</div>' +
      '</div>';
    }).join('') + '</div>';
  } catch(e) {
    el.innerHTML = '<div class="empty">Loading flow signals...</div>';
  }
}

setInterval(updMkt, 30000);

// Live sector + price refresh every 15 seconds
setInterval(async () => {
  // Bust price cache so sectors always show fresh (signals use 30s TTL — no manual bust)
  if (window._cache) {
    delete window._cache['/api/prices/live'];
  }
  // Re-render current pane if it's ARJUN
  if (window._mainTab === 'arjun' || document.getElementById('p-arjun-signals')?.style.display !== 'none') {
    if (typeof loadArjun === 'function') loadArjun();
  }
  // Always refresh prices
  if (typeof tickPrices === 'function') tickPrices();
}, 15000);

// Restore last active tab
const _savedMain = localStorage.getItem('chakra-main') || 'arjun';
const _savedSub  = localStorage.getItem('chakra-sub')  || 'signals';
renderSubtabs(_savedMain);
document.querySelectorAll('.mtab').forEach(b => b.classList.remove('active'));
$('mt-' + _savedMain)?.classList.add('active');

// Paint the page first, then load data
requestAnimationFrame(() => {
  // Tabs with no subtabs (heatseeker, power) restore via setMain, not setSub
  if (!SUBTABS[_savedMain] || !SUBTABS[_savedMain].length) {
    setMain(_savedMain);
  } else {
    setSub(_savedSub);
  }
  loadHeader();            // 2 cached API calls
  setTimeout(() => startLiveTicker(), 1000);  // prices after 1s
  setRefreshInterval(120); // full refresh every 2min (was 60s)
});

updateLottoCountdown();
setInterval(updateLottoCountdown, 1000);
setInterval(() => {
  if ($('p-system-overview')?.classList.contains('active')) loadSystem();
}, 60000); // system tab refresh every 60s (was 30s)

// ── Rotation Benchmark Switcher ───────────────────────────────────────────
window._rotationBenchmark = 'SPY';

function setRotationBenchmark(ticker) {
  window._rotationBenchmark = ticker;
  // Update tab active state
  document.querySelectorAll('.rtab').forEach(b => b.classList.remove('active-rtab'));
  const tab = document.getElementById('rtab-' + ticker);
  if (tab) tab.classList.add('active-rtab');
  // Reload the rotation pane with new benchmark
  if (typeof loadRotation === 'function') loadRotation(ticker);
  else if (typeof setSub === 'function') setSub('Rotation'); // fallback re-render
}

// ── Manifold Live Data Feed ──────────────────────────────────────────────────
function loadManifoldData(ticker) {
  ticker = ticker || window._rotationBenchmark || 'SPY';
  fetch('/api/analysis/manifold?ticker=' + ticker)
    .then(r => r.json())
    .then(d => {
      if (d && d.ricci_flow && Array.isArray(d.ricci_flow)) {
        window._manifoldRicci   = d.ricci_flow;
        window._manifoldRegime  = d.regime || 'FLAT';
        window._manifoldWarp    = d.warp   || 0;
        window._manifoldPhase   = d.phase  || 'UNKNOWN';
        window._manifoldMod     = d.manifold_mod || 0;
        // Re-draw Ricci canvas with real data
        if (typeof _drawRicci3d === 'function') _drawRicci3d();
        // Update the stats below the 3D section
        const rEl = document.getElementById('manifoldAvgRicci');
        if (rEl && d.ricci_flow.length) {
          const avg = d.ricci_flow.reduce((a,b)=>a+b,0)/d.ricci_flow.length;
          rEl.textContent = avg.toFixed(3);
        }
        const regEl = document.getElementById('manifoldRegimeLabel');
        if (regEl) regEl.textContent = d.regime || 'FLAT';
        const phEl = document.getElementById('manifoldPhaseLabel');
        if (phEl) phEl.textContent = d.phase || 'UNKNOWN';
        const modEl = document.getElementById('manifoldConvMod');
        if (modEl) modEl.textContent = (d.manifold_mod >= 0 ? '+' : '') + (d.manifold_mod || 0);
      }
    })
    .catch(e => console.warn('Manifold data load failed:', e));
}

// Auto-refresh every 30s when on physics/analysis tab
if (!window._manifoldInterval) {
  window._manifoldInterval = setInterval(() => {
    const activePane = document.querySelector('.sub-btn.active');
    if (activePane && (activePane.dataset.sub === 'physics' || activePane.textContent.includes('Neural'))) {
      loadManifoldData();
    }
  }, 30000);
}

// ── Open Positions Panel (shown in Trading → Stocks) ──────────────────
async function loadOpenPositionsPanel() {
  const el = $('openPositionsPanel');
  if (!el) return;
  try {
    const [arkaPosData, swingsPosData] = await Promise.all([
      getCached('/api/arka/positions', 15000).catch(() => null),
      getCached('/api/swings/positions', 30000).catch(() => null),
    ]);

    const arkaPos   = arkaPosData?.positions || [];
    const swingsPos = swingsPosData?.open    || [];
    const total     = arkaPos.length + swingsPos.length;

    if (!total) { el.innerHTML = ''; return; }

    const rowStyle = 'display:grid;grid-template-columns:90px 70px 1fr 80px 80px 80px 80px;' +
                     'gap:8px;align-items:center;padding:8px 12px;font-size:11px;border-bottom:1px solid var(--border)';

    const arkaRows = arkaPos.map(p => {
      const pnl    = parseFloat(p.unrealized_pnl || p.pnl || 0);
      const pnlCol = pnl >= 0 ? 'var(--green)' : 'var(--red)';
      const pnlPct = parseFloat(p.unrealized_plpc || 0) * 100;
      return `<div style="${rowStyle}">
        <span style="font-weight:700;font-family:JetBrains Mono,monospace">${p.symbol||p.ticker}</span>
        <span style="font-size:8px;padding:1px 5px;border-radius:3px;background:rgba(0,208,132,0.1);color:var(--green)">0DTE</span>
        <span style="font-size:9px;color:var(--sub)">${p.contract||'equity'}</span>
        <span>$${parseFloat(p.avg_entry_price||p.entry||0).toFixed(2)}</span>
        <span style="color:var(--sub)">${p.qty||p.shares||'—'} shares</span>
        <span style="color:${pnlCol};font-weight:700">${pnl>=0?'+':''}$${Math.abs(pnl).toFixed(2)}</span>
        <span style="color:${pnlCol};font-size:9px">${pnlPct>=0?'+':''}${pnlPct.toFixed(1)}%</span>
      </div>`;
    }).join('');

    const swingRows = swingsPos.map(p => {
      const tp1 = p.tp1_hit ? '✅ TP1 hit' : '⏳ watching';
      return `<div style="${rowStyle};background:rgba(155,89,182,0.03)">
        <span style="font-weight:700;font-family:JetBrains Mono,monospace">${p.ticker}</span>
        <span style="font-size:8px;padding:1px 5px;border-radius:3px;background:rgba(155,89,182,0.15);color:var(--purple)">SWING</span>
        <span style="font-size:9px;color:var(--sub)">${p.contract||'—'}</span>
        <span>$${parseFloat(p.entry_price||0).toFixed(2)}</span>
        <span style="color:var(--sub)">${p.shares||'—'} shares</span>
        <span style="color:var(--gold);font-size:9px">${tp1}</span>
        <span style="color:var(--sub);font-size:9px">hold ${p.hold_days||0}d</span>
      </div>`;
    }).join('');

    el.innerHTML = `
      <div style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:4px">
        <div style="padding:10px 12px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
          <span style="font-size:9px;font-weight:700;letter-spacing:1px;color:var(--sub);text-transform:uppercase">📊 Open Positions (${total})</span>
          <span style="font-size:9px;color:var(--sub)">${arkaPos.length} scalp · ${swingsPos.length} swing</span>
        </div>
        <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;${rowStyle};padding-top:6px;padding-bottom:6px">
          <span>Ticker</span><span>Type</span><span>Contract</span><span>Entry</span><span>Size</span><span>P&L</span><span>%</span>
        </div>
        ${arkaRows}${swingRows}
      </div>`;
  } catch(e) {
    console.warn('Open positions error:', e);
  }
}

// ── GEX Range Bound Levels ─────────────────────────────────────────────────
// Populates #gexRangeLevels panel when the GEX tab is visible.
// Called by analysis.js selectGexTicker() and on first load.
let _gexRangeTicker = 'SPX';
async function updateGexRangeLevels(ticker) {
  if (ticker) _gexRangeTicker = ticker;
  // Prefer the lightweight range-levels endpoint (has cliff_today, regime_call, bias_ratio)
  // Fall back to /api/options/gex if gex_latest file not yet written
  let d = await getCached(`/api/options/gex/range-levels?ticker=${_gexRangeTicker}`, 60000);
  if (!d || d.error || d.stale) {
    const fb = await getCached(`/api/options/gex?ticker=${_gexRangeTicker}`, 120000);
    if (fb && !fb.error) d = Object.assign({}, fb, d || {});  // merge: range-levels wins if present
  }
  if (!d || (!d.spot && !d.spx_price)) return;

  const spot = parseFloat(d.spot || d.spx_price || 0);
  const cw   = parseFloat(d.call_wall || 0);
  const pw   = parseFloat(d.put_wall  || 0);
  const zg   = parseFloat(d.zero_gamma || 0);
  const regime   = (d.regime || '').toUpperCase();
  const cliff    = !!d.cliff_today;

  const fmt = v => v ? '$' + v.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0}) : '—';
  const pct = (a, b) => b ? ((a - b) / b * 100).toFixed(2) + '% away' : '—';

  const cwEl = document.getElementById('gexCallWall');
  const cwDEl = document.getElementById('gexCallWallDist');
  const pwEl = document.getElementById('gexPutWall');
  const pwDEl = document.getElementById('gexPutWallDist');
  const zgEl = document.getElementById('gexZeroGamma');
  const zgPEl = document.getElementById('gexZeroGammaPos');
  const regEl = document.getElementById('gexRegimeBadge');
  const clEl  = document.getElementById('gexCliffBadge');

  if (cwEl)  cwEl.textContent  = fmt(cw);
  if (cwDEl) cwDEl.textContent = cw && spot ? pct(cw, spot) : '—';
  if (pwEl)  pwEl.textContent  = fmt(pw);
  if (pwDEl) pwDEl.textContent = pw && spot ? pct(spot, pw).replace('-','') + ' away' : '—';
  if (zgEl)  zgEl.textContent  = fmt(zg);
  if (zgPEl) zgPEl.textContent = zg && spot ? (spot > zg ? 'ABOVE zero gamma' : 'BELOW zero gamma') : '—';
  if (zgPEl) zgPEl.style.color = spot > zg ? 'var(--green)' : 'var(--red)';

  if (regEl) {
    const rColors = {
      POSITIVE_GAMMA: { bg: 'rgba(0,208,132,0.15)', color: 'var(--green)' },
      NEGATIVE_GAMMA: { bg: 'rgba(255,61,90,0.15)',  color: 'var(--red)'   },
      LOW_VOL:        { bg: 'rgba(255,204,0,0.15)',   color: 'var(--gold)'  },
    };
    const rc = rColors[regime] || { bg: 'var(--bg3)', color: 'var(--sub)' };
    regEl.textContent = regime.replace(/_/g, ' ') || '—';
    regEl.style.background = rc.bg;
    regEl.style.color      = rc.color;
  }
  const rcEl = document.getElementById('gexRegimeCallBadge');
  if (rcEl) {
    const rc = (d.regime_call || '').toUpperCase();
    const rcColors = {
      SHORT_THE_POPS:  { bg: 'rgba(255,61,90,0.12)',   color: 'var(--red)'   },
      BUY_THE_DIPS:    { bg: 'rgba(0,208,132,0.12)',   color: 'var(--green)' },
      FOLLOW_MOMENTUM: { bg: 'rgba(255,204,0,0.12)',   color: 'var(--gold)'  },
    };
    const rcc = rcColors[rc] || { bg: 'var(--bg3)', color: 'var(--sub)' };
    rcEl.textContent = rc.replace(/_/g, ' ') || '—';
    rcEl.style.background = rcc.bg;
    rcEl.style.color      = rcc.color;
  }
  if (clEl) clEl.style.display = cliff ? 'block' : 'none';
}
// Auto-refresh range levels every 2 min when GEX tab is visible
setInterval(() => {
  if (document.getElementById('p-analysis-gex')?.style.display !== 'none') {
    updateGexRangeLevels();
  }
}, 120000);

// ── HTTP helpers (used by Heat Seeker and other modules) ─────────────────────
async function post(url, body) {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return r.ok ? await r.json() : null;
  } catch (e) {
    console.error('[POST]', url, e);
    return null;
  }
}

async function del(url) {
  try {
    const r = await fetch(url, { method: 'DELETE' });
    return r.ok ? await r.json() : null;
  } catch (e) {
    console.error('[DELETE]', url, e);
    return null;
  }
}
