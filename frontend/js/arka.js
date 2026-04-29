/* ═══════════════════════════════════════════════════════════
   CHAKRA — arka.js
   ARKA live feed · scan history · trade log · engine controls
   ═══════════════════════════════════════════════════════════ */

// ── Contract symbol parser ─────────────────────────────────
// Input:  "SPY260414C00681000"
// Output: { underlying:"SPY", expiry:"2026-04-14", type:"CALL", strike:681, dte:0,
//           label:"$681 CALL 0DTE", shortLabel:"$681C 0DTE" }
function parseContract(sym) {
  if (!sym) return null;
  const m = sym.match(/^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d+)$/);
  if (!m) return null;
  const underlying = m[1];
  const expiry = `20${m[2]}-${m[3]}-${m[4]}`;
  const isCall = m[5] === 'C';
  const strike = parseInt(m[6], 10) / 1000;
  const today  = new Date(); today.setHours(0,0,0,0);
  const expDate = new Date(expiry); expDate.setHours(0,0,0,0);
  const dte = Math.max(0, Math.round((expDate - today) / 86400000));
  const dteLabel = dte === 0 ? '0DTE' : dte === 1 ? '1DTE' : dte + 'DTE';
  const typeStr  = isCall ? 'CALL' : 'PUT';
  return {
    underlying,
    expiry,
    type:       typeStr,
    strike,
    dte,
    label:      `$${strike % 1 === 0 ? strike : strike.toFixed(2)} ${typeStr} ${dteLabel}`,
    shortLabel: `$${strike % 1 === 0 ? strike : strike.toFixed(2)}${isCall ? 'C' : 'P'} ${dteLabel}`,
  };
}

// ── Positions sort state ───────────────────────────────────
let _sortCol = null;   // column key currently sorted
let _sortDir = 1;      // 1 = asc, -1 = desc

function _sortPositions(col) {
  if (_sortCol === col) {
    _sortDir *= -1;
  } else {
    _sortCol = col;
    _sortDir = col === 'pnl' ? -1 : 1;  // P&L defaults desc (best first)
  }
  const data = window._positionsData;
  if (!data || !data.length) return;

  data.sort((a, b) => {
    let av, bv;
    switch (col) {
      case 'ticker':  av = a.ticker || ''; bv = b.ticker || ''; break;
      case 'status':  av = a.status  || ''; bv = b.status  || ''; break;
      case 'dte': {
        const pa = parseContract(a.contract || a.trade_sym);
        const pb = parseContract(b.contract || b.trade_sym);
        av = pa ? pa.dte : 999; bv = pb ? pb.dte : 999; break;
      }
      case 'size':          av = parseFloat(a.size  || 0); bv = parseFloat(b.size  || 0); break;
      case 'entry':         av = parseFloat(a.entry || 0); bv = parseFloat(b.entry || 0); break;
      case 'current_price': av = parseFloat(a.current_price ?? a.current ?? 0);
                            bv = parseFloat(b.current_price ?? b.current ?? 0); break;
      case 'pnl':           av = parseFloat(a.pnl ?? 0); bv = parseFloat(b.pnl ?? 0); break;
      default: return 0;
    }
    if (typeof av === 'string') return _sortDir * av.localeCompare(bv);
    return _sortDir * (av - bv);
  });

  _renderPositions(data);
  _updateSortArrows();
}

function _updateSortArrows() {
  const header = document.getElementById('positionsHeader');
  if (!header) return;
  header.querySelectorAll('[data-sort]').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    if (!arrow) return;
    if (th.dataset.sort === _sortCol) {
      arrow.textContent = _sortDir === 1 ? ' ▲' : ' ▼';
      th.style.color = 'var(--blue)';
    } else {
      arrow.textContent = '';
      th.style.color = '';
    }
  });
}

function _initPositionsSortHeaders() {
  const header = document.getElementById('positionsHeader');
  if (!header || header.dataset.sortBound) return;
  header.dataset.sortBound = '1';
  header.querySelectorAll('[data-sort]').forEach(th => {
    th.addEventListener('click', () => _sortPositions(th.dataset.sort));
  });
}

// ── Decision label helpers ─────────────────────────────────
function _decisionLabel(decision) {
  if (!decision) return { label: 'SCANNING', color: 'var(--sub)' };
  const d = decision.toUpperCase();
  if (d.includes('TRADE') || d.includes('BUY') || d.includes('SELL'))
    return { label: d, color: 'var(--green)' };
  if (d.includes('BLOCKED:LUNCH'))   return { label: 'LUNCH BLOCK', color: 'var(--gold)' };
  if (d.includes('BLOCKED:CHOP'))    return { label: 'CHOP', color: 'var(--gold)' };
  if (d.includes('BLOCKED:FAKEOUT')) return { label: 'FAKEOUT', color: 'var(--red)' };
  if (d.includes('BLOCKED:STREAK'))  return { label: 'STREAK LIMIT', color: 'var(--red)' };
  if (d.includes('BLOCKED:CONV'))    return { label: 'LOW CONV', color: 'var(--sub)' };
  if (d.includes('BLOCKED'))         return { label: d.replace('BLOCKED:', ''), color: 'var(--red)' };
  if (d.includes('FLAT'))            return { label: 'FLAT', color: 'var(--sub)' };
  return { label: d.slice(0, 14), color: 'var(--sub)' };
}

// ── Sidebar scan feed (max 5, always visible) ──────────────
function loadSidebarScanFeed(scans) {
  const el = $('sbScanFeed'); if (!el) return;
  const last5 = scans.slice(-5).reverse();
  if (!last5.length) { el.innerHTML = '<div style="color:var(--sub);font-size:10px">No scans yet</div>'; return; }
  el.innerHTML = last5.map(s => {
    const sc  = parseFloat(s.score || 0);
    const col = sc >= 75 ? 'var(--green)' : sc >= 55 ? 'var(--gold)' : 'var(--sub)';
    const dl  = _decisionLabel(s.decision);
    return `<div style="display:flex;justify-content:space-between;align-items:center;
      padding:4px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;gap:5px;align-items:center">
        <span style="font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--sub)">${s.time || '—'}</span>
        <span style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:var(--blue)">${s.ticker || '—'}</span>
      </div>
      <div style="display:flex;gap:5px;align-items:center">
        <span style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:${col}">${sc.toFixed(0)}</span>
        <span style="font-size:8px;color:${dl.color}">${dl.label}</span>
      </div>
    </div>`;
  }).join('');
}

// ── ARKA Live Feed (uses scan_history from /api/arka/summary) ──
function loadArkaLiveFeed() {
  fetch('/api/arka/summary')
    .then(r => r.json())
    .then(data => {
      const el   = $('arka-live-feed');
      const stat = $('arka-feed-count');
      if (!el) return;

      // Use scan_history which has score + decision data
      const scans = data.scan_history || [];
      const entries = scans.slice().reverse(); // newest first
      if (stat) stat.textContent = entries.length + ' entries';

      // Also update sidebar
      loadSidebarScanFeed(scans);

      if (!entries.length) {
        el.innerHTML = '<div class="empty">No scans yet — market opens 9:30am ET</div>';
        return;
      }

      // Compact scan rows with conviction + fakeout + decision reasoning
      el.innerHTML = entries.slice(0, 80).map(e => {
        const sc      = parseFloat(e.score || e.conviction || 0);
        const fakeout = parseFloat(e.fakeout || 0);
        const col     = sc >= 75 ? 'var(--green)' : sc >= 55 ? 'var(--gold)' : 'var(--sub)';
        const dl      = _decisionLabel(e.decision);
        const isTrade = dl.label.includes('BUY') || dl.label.includes('SELL') || dl.label.includes('TRADE');

        // Condition reasoning line
        const reasons = [];
        if (sc >= 75)        reasons.push(`conv ${sc.toFixed(0)} ✓`);
        else if (sc >= 55)   reasons.push(`conv ${sc.toFixed(0)} ~`);
        else                 reasons.push(`conv ${sc.toFixed(0)} ✗`);
        if (fakeout > 0)     reasons.push(`fakeout ${(fakeout * 100).toFixed(0)}%${fakeout > 0.5 ? ' ⚠' : ''}`);

        return `<div class="arka-scan-card" style="${isTrade ? 'background:rgba(0,208,132,0.06);border-left:2px solid var(--green);' : ''}">
          <span class="arka-scan-time">${e.time || '—'}</span>
          <span class="arka-scan-ticker">${e.ticker || '—'}</span>
          <div style="flex:1;padding:0 6px">
            <div class="arka-scan-bar">
              <div class="arka-scan-bar-fill" style="width:${Math.min(sc,100)}%;background:${col}"></div>
            </div>
            <div style="font-size:8px;color:var(--sub);margin-top:2px">${reasons.join(' · ')}</div>
          </div>
          <span class="arka-scan-score" style="color:${col}">${sc.toFixed(0)}</span>
          <span class="arka-scan-decision" style="color:${dl.color}">${dl.label}</span>
        </div>`;
      }).join('');
    })
    .catch(() => {
      const s = $('arka-feed-count');
      if (s) s.textContent = 'API error';
    });
}
setInterval(loadArkaLiveFeed, 10000);

// ── Sidebar Screener Feed ───────────────────────────────
function loadSidebarScreeners() {
  fetch('/api/swings/watchlist')
    .then(r => r.json())
    .then(data => {
      const el    = $('sbScreenerFeed');
      const count = $('sbScreenerCount');
      if (!el) return;
      const candidates = (data.candidates || data.top5 || []).slice(0, 8);
      if (count) count.textContent = candidates.length + ' candidates';
      if (!candidates.length) {
        el.innerHTML = '<div style="color:var(--sub);font-size:10px">No candidates — runs at 8:15am ET</div>';
        return;
      }
      el.innerHTML = candidates.map(c => {
        const isCall  = c.direction === 'LONG';
        const dirCol  = isCall ? 'var(--green)' : 'var(--red)';
        const dirTxt  = isCall ? 'CALL' : 'PUT';
        const score   = c.score || 0;
        const scoreCol = score >= 75 ? 'var(--green)' : score >= 60 ? 'var(--gold)' : 'var(--sub)';
        const chg     = parseFloat(c.chg_pct || 0);
        const chgCol  = chg >= 0 ? 'var(--green)' : 'var(--red)';
        const chgStr  = (chg >= 0 ? '+' : '') + chg.toFixed(1) + '%';
        return `<div style="display:flex;justify-content:space-between;align-items:center;
          padding:5px 0;border-bottom:1px solid var(--border)">
          <div style="display:flex;gap:5px;align-items:center">
            <span style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:var(--blue)">${c.ticker}</span>
            <span style="font-size:8px;font-weight:700;color:${dirCol};background:${dirCol}22;padding:1px 4px;border-radius:3px">${dirTxt}</span>
          </div>
          <div style="display:flex;gap:6px;align-items:center">
            <span style="font-size:9px;color:${chgCol}">${chgStr}</span>
            <span style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:${scoreCol}">${score}</span>
          </div>
        </div>`;
      }).join('');
    })
    .catch(() => {
      const el = $('sbScreenerFeed');
      if (el) el.innerHTML = '<div style="color:var(--sub);font-size:10px">API error</div>';
    });
}
loadSidebarScreeners();
setInterval(loadSidebarScreeners, 60000);

// ── Pair BUY+STOP entries into completed position records ──
function _buildPositions(tlog) {
  const positions = [];
  const open = {};
  for (const t of tlog) {
    const key = t.ticker;
    if (t.side === 'BUY' || t.side === 'SHORT') {
      open[key] = { ...t };
    } else if ((t.side === 'STOP' || t.side === 'SELL' || t.side === 'CLOSE' || t.side === 'COVER' || t.side === 'TARGET') && open[key]) {
      const entry = open[key];
      const pnl   = parseFloat(t.pnl ?? 0);
      positions.push({
        date:       new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }),
        ticker:     key,
        type:       'ODTE',
        action:     (open[key]?.side==='SHORT'?'SHORT':'BUY') + (pnl >= 0 ? ' ✓' : ' ✗'),
        size:       entry.qty || t.qty || 1,
        entry:      entry.price,
        entryTime:  entry.time,
        exit:       t.price,
        exitTime:   t.time,
        pnl,
        exitReason: t.side,
        status:     'CLOSED',
      });
      delete open[key];
    }
  }
  // Any still-open positions
  for (const key of Object.keys(open)) {
    positions.push({ ...open[key], type: 'ODTE', action: 'BUY', status: 'OPEN', pnl: null });
  }
  return positions.reverse();
}

// ── Main ARKA tab loader ───────────────────────────────────
async function loadArka() {
  const [sum, int2, acct] = await Promise.all([
    get('/api/arka/summary'),
    get('/api/internals'),
    getCached('/api/account', 15000),
  ]);

  if (sum) {
    // Use Alpaca equity-based daily P&L (equity − last_equity) as source of truth.
    // ARKA's internal daily_pnl counter can drift when trades are closed outside
    // the engine (manual closes, UI closes, restarts).
    const alpacaEq     = parseFloat(acct?.equity      || 0);
    const alpacaLastEq = parseFloat(acct?.last_equity || alpacaEq);
    const alpacaDailyPnl = alpacaEq > 0 ? alpacaEq - alpacaLastEq : parseFloat(sum.daily_pnl || 0);

    $('aPnl').textContent = (alpacaDailyPnl >= 0 ? '+' : '') + f$(alpacaDailyPnl);
    $('aPnl').style.color = alpacaDailyPnl > 0 ? 'var(--green)' : alpacaDailyPnl < 0 ? 'var(--red)' : 'var(--text)';
    $('aTrades').textContent = (sum.trades || 0) + ' trades executed';

    const scans = sum.scan_history || [];
    if ($('aScans'))   $('aScans').textContent   = scans.length;
    if ($('aScanCnt')) $('aScanCnt').textContent = scans.length + ' SCANS';
    if ($('aStreak'))  $('aStreak').textContent  = sum.losing_streak || 0;
    if ($('aStreak'))  $('aStreak').style.color  = (sum.losing_streak || 0) >= 3 ? 'var(--red)' : 'var(--text)';

    // Expose latest scan per ticker globally so arjun.js index cards can show ARKA status
    const _latestByTicker = {};
    for (const s of scans) {
      const t = (s.ticker || '').toUpperCase();
      if (!_latestByTicker[t] || s.time > _latestByTicker[t].time) _latestByTicker[t] = s;
    }
    window._arkaLastScans = _latestByTicker;

    // Render the main scan feed (still in ARKA tab but sidebar also updates)
    _renderMainScanFeed(scans);
    loadSidebarScanFeed(scans);

    // Positions table — merge live API positions with closed trades from log
    const tlog = sum.trade_log || [];
    try {
      const livePosData = await get('/api/arka/positions');
      const livePos = (livePosData?.positions || []).map(p => {
        const _sym = p.contract || p.trade_sym || '';
        const _pc  = parseContract(_sym);
        return {
          date:      new Date().toLocaleDateString('en-US', {month:'short',day:'numeric'}),
          ticker:    p.ticker,
          contract:  _sym,
          trade_sym: _sym,
          type:      _pc ? (_pc.dte === 0 ? '0DTE' : `${_pc.dte}DTE`) : (p.type || '—'),
          action:    p.action || (p.direction?.includes('SHORT') ? 'BUY PUT' : 'BUY CALL'),
          size:      p.size,
          entry:     p.entry,
          entryTime: '—',
          exit:      null,
          exitTime:  null,
          current_price: p.current_price,
          current:   p.current_price,
          stop:      p.stop,
          target:    p.target,
          pnl:       p.pnl,
          pnl_pct:   p.pnl_pct,
          status:    'OPEN',
          direction: p.direction,
        };
      });
      const closedTrades = _buildPositions(tlog);
      _renderPositions([...livePos, ...closedTrades]);
    } catch(e) {
      _renderPositions(_buildPositions(tlog));
    }
  }

  if (int2) {
    const r   = int2.risk || {};
    const mod = int2.arka_mod || {};
    const vix = int2.vix || {};
    const tlt = int2.tlt || {};
    const gld = int2.gld || {};
    const mode = r.mode || 'NEUTRAL';
    const rc   = mode==='RISK_ON'?'var(--green)':mode==='RISK_OFF'?'var(--red)':'var(--gold)';
    $('aRisk').innerHTML = `
      <div class="mn" style="font-size:18px;font-weight:700;color:${rc}">${mode}</div>
      <div style="font-size:10px;color:var(--sub);margin-top:3px">${r.description||''}</div>
      <div style="font-size:10px;margin-top:5px;display:flex;gap:8px;flex-wrap:wrap">
        ${vix.close?`<span>VIX <strong>${vix.close}</strong></span>`:''}
        ${tlt.close?`<span>TLT <strong style="color:${pc(tlt.chg_pct)}">${tlt.chg_pct>=0?'+':''}${(tlt.chg_pct||0).toFixed(2)}%</strong></span>`:''}
        ${gld.close?`<span>GLD <strong style="color:${pc(gld.chg_pct)}">${gld.chg_pct>=0?'+':''}${(gld.chg_pct||0).toFixed(2)}%</strong></span>`:''}
      </div>
      <div style="margin-top:6px;font-size:11px">Modifier: <span class="mn" style="color:${(mod.modifier||0)>=0?'var(--green)':'var(--red)'}">
        ${(mod.modifier||0)>=0?'+':''}${mod.modifier||0} pts
      </span></div>
      ${(mod.reasons||[]).length?`<div style="margin-top:4px">${mod.reasons.map(r2=>`<div style="font-size:9px;color:var(--sub)">• ${r2}</div>`).join('')}</div>`:''}`;
  }
}

// ── Main scan feed render (ARKA tab) ───────────────────────
function _renderMainScanFeed(scans) {
  const el = $('arka-live-feed'); if (!el) return;
  const entries = scans.slice().reverse();
  if ($('arka-feed-count')) $('arka-feed-count').textContent = entries.length + ' entries';
  if (!entries.length) { el.innerHTML = '<div class="empty">No scans yet — market opens 9:30am ET</div>'; return; }

  el.innerHTML = entries.slice(0, 80).map(e => {
    const sc      = parseFloat(e.score || e.conviction || 0);
    const fakeout = parseFloat(e.fakeout || 0);
    const col     = sc >= 75 ? 'var(--green)' : sc >= 55 ? 'var(--gold)' : 'var(--sub)';
    const dl      = _decisionLabel(e.decision);
    const isTrade = dl.label.includes('BUY') || dl.label.includes('SELL') || dl.label.includes('TRADE');
    const reasons = [];
    if (sc > 0) reasons.push(`conv ${sc.toFixed(0)}${sc >= 75 ? ' ✓' : sc >= 55 ? ' ~' : ' ✗'}`);
    if (fakeout > 0) reasons.push(`fakeout ${(fakeout * 100).toFixed(0)}%${fakeout > 0.5 ? ' ⚠' : ''}`);
    return `<div class="arka-scan-card" style="${isTrade ? 'background:rgba(0,208,132,0.06);border-left:2px solid var(--green);' : ''}">
      <span class="arka-scan-time">${e.time || '—'}</span>
      <span class="arka-scan-ticker">${e.ticker || '—'}</span>
      <div style="flex:1;padding:0 6px">
        <div class="arka-scan-bar"><div class="arka-scan-bar-fill" style="width:${Math.min(sc,100)}%;background:${col}"></div></div>
        <div style="font-size:8px;color:var(--sub);margin-top:2px">${reasons.join(' · ')}</div>
      </div>
      <span class="arka-scan-score" style="color:${col}">${sc.toFixed(0)}</span>
      <span class="arka-scan-decision" style="color:${dl.color}">${dl.label}</span>
    </div>`;
  }).join('');
}

// ── Positions table render ─────────────────────────────────
function _renderPositions(positions) {
  const el = $('aPositionsBody'); if (!el) return;
  if (!positions.length) {
    el.innerHTML = `<tr><td colspan="10"><div class="empty">No trades today</div></td></tr>`;
    return;
  }
  el.innerHTML = positions.map((p, i) => {
    const pnl     = p.pnl;
    const pnlPct  = parseFloat(p.pnl_pct ?? 0);
    const pnlStr  = pnl === null ? '—' : (pnl >= 0 ? '+' : '') + f$(pnl);
    const pnlCol  = pnlPct >= 8 ? 'var(--gold)' : pnlPct > 0 ? 'var(--green)' : pnlPct < 0 ? 'var(--red)' : 'var(--sub)';
    const pnlPctStr = pnlPct !== 0 ? (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(1) + '%' : (pnl !== null ? '0.0%' : '—');

    // Progress bar: -30% stop → 0%, breakeven → 75%, +10% target → 100%
    const progressFill = p.status === 'OPEN'
      ? Math.max(0, Math.min(100, (pnlPct + 30) / 40 * 100)).toFixed(0)
      : null;

    // Parse contract for all derived fields
    const _pc = parseContract(p.contract || p.trade_sym);

    // ── DTE column ──────────────────────────────────────────────────────────
    const dte      = _pc ? _pc.dte : null;
    const dteLabel = _pc ? _pc.dteLabel : (p.type === 'ODTE' ? '0DTE' : p.type || '—');
    const dteBg    = dte === 0 ? 'rgba(245,166,35,0.15)' : 'rgba(61,142,255,0.12)';
    const dteCol   = dte === 0 ? 'var(--gold)' : 'var(--blue)';

    // ── Side column: BUY CALL / BUY PUT ─────────────────────────────────────
    // Derive from contract type first, fall back to p.action
    const _isCall  = _pc ? _pc.type === 'CALL' : (p.action || '').toUpperCase().includes('CALL') || (p.action || '').toUpperCase() === 'BUY';
    const sideWord = _isCall ? 'CALL' : 'PUT';
    const sideBuy  = (p.action || 'BUY').toUpperCase().includes('SELL') ? 'SELL' : 'BUY';
    const sideCol  = _isCall ? 'var(--green)' : 'var(--red)';

    // ── Strike · Type column ─────────────────────────────────────────────────
    const strikeStr = _pc
      ? `<span style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:800;color:${sideCol}">
           ${_pc.strike % 1 === 0 ? _pc.strike : _pc.strike.toFixed(1)}
         </span>
         <span style="font-size:9px;font-weight:700;margin-left:3px;padding:1px 5px;border-radius:3px;
           background:${sideCol}18;color:${sideCol};border:1px solid ${sideCol}40">${_pc.type}</span>`
      : `<span style="color:var(--sub);font-size:10px">${p.contract ? p.contract.slice(-8) : '—'}</span>`;

    // ── Current / Exit column ────────────────────────────────────────────────
    const currentPrice = p.current_price ?? p.current;
    const priceCell = p.status === 'OPEN'
      ? (currentPrice
          ? `<span style="color:var(--blue);font-family:'JetBrains Mono',monospace">${f$(currentPrice)}</span>
             <div style="font-size:7px;color:var(--sub);margin-top:1px">
               TP +10% / SL -30%
             </div>`
          : '—')
      : `${p.exit ? f$(p.exit) : '—'}
         <div style="font-size:8px;color:var(--sub);margin-top:1px">${p.exitTime || ''}</div>`;

    // ── Status ───────────────────────────────────────────────────────────────
    const statusBg  = p.status === 'CLOSED' ? 'rgba(0,208,132,0.1)' : 'rgba(61,142,255,0.1)';
    const statusCol = p.status === 'CLOSED' ? 'var(--green)' : 'var(--blue)';

    // ── Entry time as tooltip ─────────────────────────────────────────────────
    const entryTitle = p.entryTime ? ` title="${p.entryTime}"` : '';

    return `<tr style="cursor:pointer" onclick="_showTradeDetail(${i})" data-pos-idx="${i}">

      <!-- Ticker -->
      <td class="mn" style="font-weight:800;font-size:14px;letter-spacing:-0.3px">
        ${p.ticker}
        <div style="font-size:8px;color:var(--sub);font-weight:400;margin-top:1px">${p.date || ''}</div>
      </td>

      <!-- DTE -->
      <td>
        <span style="font-size:8px;font-weight:700;padding:2px 7px;border-radius:3px;
          background:${dteBg};color:${dteCol};border:1px solid ${dteCol}40;
          font-family:'JetBrains Mono',monospace">${dteLabel}</span>
      </td>

      <!-- Side: BUY CALL / BUY PUT -->
      <td>
        <div style="font-size:7px;color:var(--sub);margin-bottom:2px">${sideBuy}</div>
        <span style="font-size:9px;font-weight:800;padding:2px 8px;border-radius:3px;
          background:${sideCol}15;color:${sideCol};border:1px solid ${sideCol}40;
          font-family:'JetBrains Mono',monospace">${sideWord}</span>
      </td>

      <!-- Qty (contracts) -->
      <td class="mn" style="font-size:12px;font-weight:700;text-align:center">
        ${p.size || 1}
        <div style="font-size:7px;color:var(--sub);margin-top:1px">contract${(p.size||1)>1?'s':''}</div>
      </td>

      <!-- Entry price -->
      <td class="mn" style="font-size:12px;font-weight:700"${entryTitle}>
        ${f$(p.entry)}
        <div style="font-size:7px;color:var(--sub);margin-top:1px">${p.entryTime ? p.entryTime.slice(0,5) : ''}</div>
      </td>

      <!-- Strike · Type -->
      <td style="white-space:nowrap">${strikeStr}</td>

      <!-- Current / Exit -->
      <td class="mn" style="font-size:11px">${priceCell}</td>

      <!-- P&L -->
      <td class="mn" style="font-size:12px;font-weight:700;color:${pnlCol}">
        <div>${pnlStr}</div>
        <div style="font-size:10px;color:${pnlCol}">${pnlPctStr}</div>
        ${progressFill !== null
          ? `<div style="margin-top:3px;height:3px;border-radius:2px;background:var(--bg3);overflow:hidden;width:55px">
               <div style="height:100%;width:${progressFill}%;background:${pnlCol};border-radius:2px;transition:width 0.5s"></div>
             </div>`
          : ''}
      </td>

      <!-- Status -->
      <td>
        <span style="font-size:8px;font-weight:700;padding:2px 7px;border-radius:3px;
          background:${statusBg};color:${statusCol};border:1px solid ${statusCol}40">${p.status}</span>
      </td>

      <!-- Actions -->
      <td style="min-width:70px;white-space:nowrap">
        <span style="color:var(--gold);font-size:10px;cursor:pointer"
          onclick="_showTradeDetail(${i});event.stopPropagation()">View →</span>
        ${p.status === 'OPEN'
          ? `<button onclick="manualSell('${p.contract||p.ticker}',${p.size||0},event)"
               style="display:block;margin-top:4px;font-size:8px;font-weight:700;padding:2px 8px;border-radius:3px;
               border:1px solid var(--red);background:rgba(255,61,90,0.15);color:var(--red);
               cursor:pointer;font-family:'JetBrains Mono',monospace">CLOSE</button>`
          : ''}
      </td>
    </tr>`;
  }).join('');
  window._positionsData = positions;
  _initPositionsSortHeaders();
  _updateSortArrows();
}

// ── Trade detail modal ─────────────────────────────────────
function _showTradeDetail(idx) {
  const p = (window._positionsData || [])[idx]; if (!p) return;
  const pnl    = p.pnl;
  const pnlStr = pnl === null ? '—' : (pnl >= 0 ? '+' : '') + f$(pnl);
  const pnlCol = pnl === null ? 'var(--sub)' : pnl >= 0 ? 'var(--green)' : 'var(--red)';

  // Create modal overlay
  const existing = document.getElementById('tradeDetailModal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'tradeDetailModal';
  modal.style.cssText = `position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:500;
    display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)`;
  modal.onclick = e => { if (e.target === modal) modal.remove(); };

  modal.innerHTML = `
    <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;
      width:min(680px,95vw);max-height:85vh;overflow-y:auto;padding:24px;position:relative">

      <!-- Header -->
      ${(() => { const _mc = parseContract(p.contract || p.trade_sym); return _mc ? `
        <div style="display:inline-flex;align-items:center;gap:8px;padding:4px 12px;border-radius:5px;
          margin-bottom:14px;background:${_mc.type==='CALL'?'rgba(0,208,132,0.1)':'rgba(255,61,90,0.1)'};
          border:1px solid ${_mc.type==='CALL'?'var(--green)':'var(--red)'}40">
          <span style="font-size:10px;font-weight:700;color:${_mc.type==='CALL'?'var(--green)':'var(--red)'}">${_mc.type}</span>
          <span style="font-size:12px;font-weight:800;font-family:'JetBrains Mono',monospace">$${_mc.strike % 1 === 0 ? _mc.strike : _mc.strike.toFixed(2)}</span>
          <span style="font-size:10px;color:var(--gold);font-weight:700">${_mc.dte === 0 ? '0DTE' : _mc.dte+'DTE'}</span>
          <span style="font-size:9px;color:var(--sub)">${_mc.expiry}</span>
          <span style="font-size:9px;color:var(--sub);font-family:'JetBrains Mono',monospace">${p.contract}</span>
        </div>` : ''; })()}
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px">
        <div>
          <div style="font-size:22px;font-weight:700">Trade Details: ${p.ticker}</div>
          <div style="font-size:12px;color:var(--sub);margin-top:2px">${p.date} · ${p.action}</div>
        </div>
        <button onclick="document.getElementById('tradeDetailModal').remove()"
          style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;
          padding:6px 12px;cursor:pointer;color:var(--sub);font-size:12px">✕ Close</button>
      </div>

      <!-- Key metrics -->
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px">
        <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:6px">P&L</div>
          <div class="mn" style="font-size:22px;font-weight:700;color:${pnlCol}">${pnlStr}</div>
        </div>
        <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:6px">Entry Price</div>
          <div class="mn" style="font-size:22px;font-weight:700">${f$(p.entry)}</div>
          <div style="font-size:9px;color:var(--sub);margin-top:3px">${p.entryTime || '—'}</div>
        </div>
        <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:6px">Exit Price</div>
          <div class="mn" style="font-size:22px;font-weight:700">${p.exit ? f$(p.exit) : '—'}</div>
          <div style="font-size:9px;color:var(--sub);margin-top:3px">${p.exitTime || '—'}</div>
        </div>
        <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:6px">Size</div>
          <div class="mn" style="font-size:22px;font-weight:700">${p.size || '—'}</div>
          <div style="font-size:9px;color:var(--sub);margin-top:3px">contracts × 100 shares</div>
        </div>
      </div>

      <!-- Trade info grid -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
        <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:14px">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:12px">Trade Information</div>
          ${(() => {
            const _mc2 = parseContract(p.contract || p.trade_sym);
            return [
              ['TICKER',       p.ticker],
              ['CONTRACT',     p.contract || '—'],
              ['STRIKE',       _mc2 ? `$${_mc2.strike % 1 === 0 ? _mc2.strike : _mc2.strike.toFixed(2)}` : '—'],
              ['TYPE',         _mc2 ? _mc2.type : (p.action || '—')],
              ['EXPIRY',       _mc2 ? _mc2.expiry + ` (${_mc2.dte}DTE)` : '—'],
              ['PREMIUM PAID', p.entry ? `$${parseFloat(p.entry).toFixed(2)}/share · $${(parseFloat(p.entry)*100).toFixed(0)}/contract` : '—'],
              ['STATUS',       p.status],
            ].map(([k, v]) => `
              <div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)">
                <span style="font-size:9px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">${k}</span>
                <span style="font-size:11px;font-weight:600;font-family:${k==='CONTRACT'?'\'JetBrains Mono\',monospace':'inherit'};font-size:${k==='CONTRACT'?'9px':'11px'}">${v || '—'}</span>
              </div>`
            ).join('');
          })()}
        </div>
        <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:14px">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:12px">Exit Details</div>
          ${[
            ['EXIT TIME',   p.exitTime || '—'],
            ['EXIT PRICE',  p.exit ? f$(p.exit) : '—'],
            ['P&L',         pnlStr],
            ['EXIT REASON', p.exitReason || '—'],
          ].map(([k, v]) => `
            <div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)">
              <span style="font-size:9px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">${k}</span>
              <span style="font-size:11px;font-weight:600;${k === 'P&L' ? 'color:' + pnlCol : ''}">${v}</span>
            </div>`
          ).join('')}
        </div>
      </div>

      <!-- Risk Management -->
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:14px">
        <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:12px">Risk Management</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
          <div>
            <div style="font-size:9px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">Stop Loss Level</div>
            <div class="mn" style="font-size:18px;font-weight:700;color:var(--red)">-25%</div>
          </div>
          <div>
            <div style="font-size:9px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">P&L %</div>
            <div class="mn" style="font-size:18px;font-weight:700;color:${pnlCol}">
              ${p.entry && p.exit ? ((p.exit - p.entry) / p.entry * 100).toFixed(1) + '%' : '—'}
            </div>
          </div>
          <div>
            <div style="font-size:9px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">Status</div>
            <div class="mn" style="font-size:18px;font-weight:700;color:var(--green)">${p.status}</div>
          </div>
        </div>
      </div>

      <!-- ARJUN Analysis -->
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:14px;margin-top:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:var(--sub);text-transform:uppercase">ARJUN ANALYSIS</div>
          <button id="arjunReanalyzeBtn"
            onclick="reAnalyzeTrade('${p.ticker}','${p.entry}','${p.action}','${p.size||''}',${p.entry && p.exit ? ((p.exit-p.entry)/p.entry*100).toFixed(1) : 0})"
            style="font-size:9px;padding:3px 10px;border-radius:4px;cursor:pointer;
              border:1px solid var(--accent);background:transparent;color:var(--accent);
              font-family:'JetBrains Mono',monospace">⚡ Analyze</button>
        </div>
        <div id="arjunAnalysisText" style="font-size:11px;color:var(--sub);line-height:1.6;min-height:40px">
          Click ⚡ Analyze for ARJUN's assessment of this position.
        </div>
      </div>
    </div>`;

  document.body.appendChild(modal);
}

async function reAnalyzeTrade(ticker, entry, direction, contract, pnlPct) {
  const el  = document.getElementById('arjunAnalysisText');
  const btn = document.getElementById('arjunReanalyzeBtn');
  if (el)  el.textContent  = '🔄 Analyzing...';
  if (btn) { btn.textContent = '⏳'; btn.disabled = true; }
  try {
    const r = await fetch('/api/trades/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ticker,
        entry:     parseFloat(entry)   || 0,
        direction: direction           || 'CALL',
        contract:  contract            || '',
        pnl_pct:   parseFloat(pnlPct)  || 0,
      }),
    });
    const d = await r.json();
    if (el) el.textContent = d.analysis || d.error || 'No analysis returned.';
  } catch(e) {
    if (el) el.textContent = `Error: ${e.message}`;
  } finally {
    if (btn) { btn.textContent = '⚡ Analyze'; btn.disabled = false; }
  }
}

// ── Engine controls ────────────────────────────────────────
function engineAction(action, engine) {
  fetch(`/api/engine/${action}?engine=${engine}`, { method: 'POST' })
    .then(r => r.json())
    .then(d => { toast(d.message || d.status || 'Done'); loadEngineStatus(); })
    .catch(() => toast('⚠️ API error'));
}

async function restartAllModules(btn) {
  const statusEl = document.getElementById('restartStatusLine');
  btn.disabled = true;
  btn.textContent = '⟳ Restarting…';
  btn.style.color = 'var(--sub)';
  if (statusEl) statusEl.textContent = 'Killing old processes…';

  try {
    const r = await fetch('/api/engine/start?engine=all', { method: 'POST' });
    const d = await r.json();
    const started = d.started || [];
    const errors  = d.errors  || [];

    btn.textContent = '✓ All Modules Restarted';
    btn.style.color = 'var(--green)';
    btn.style.border = '1px solid rgba(0,208,132,0.4)';
    btn.style.background = 'rgba(0,208,132,0.1)';

    if (statusEl) {
      if (errors.length) {
        statusEl.style.color = 'var(--red)';
        statusEl.textContent = 'Errors: ' + errors.join(', ');
      } else {
        statusEl.style.color = 'var(--green)';
        statusEl.textContent = '✓ ' + started.join(' · ');
      }
    }

    toast('✓ Restarted: ' + (started.join(', ') || 'all modules'));
    setTimeout(() => loadEngineStatus(), 4000);
  } catch (e) {
    btn.textContent = '✗ Restart Failed';
    btn.style.color = 'var(--red)';
    if (statusEl) { statusEl.style.color = 'var(--red)'; statusEl.textContent = e.message; }
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = '⟳ RESTART ALL MODULES';
      btn.style.color = 'var(--gold)';
      btn.style.border = '1px solid rgba(255,179,71,0.35)';
      btn.style.background = 'rgba(255,179,71,0.12)';
      if (statusEl) { statusEl.style.color = 'var(--sub)'; statusEl.textContent = ''; }
    }, 5000);
  }
}

function _setArkaBtnState(arkaRunning) {
  // Update all Start/Stop ARKA button pairs to reflect current state
  const startIds = ['arkaStartBtn', 'arkaStartBtn2'];
  const stopIds  = ['arkaStopBtn',  'arkaStopBtn2'];
  startIds.forEach(id => {
    const btn = document.getElementById(id);
    if (!btn) return;
    if (arkaRunning) {
      // ARKA is running — disable Start, it's already on
      btn.disabled = true;
      btn.style.opacity = '0.35';
      btn.style.cursor = 'not-allowed';
      btn.title = 'ARKA is already running';
    } else {
      btn.disabled = false;
      btn.style.opacity = '1';
      btn.style.cursor = 'pointer';
      btn.title = '';
    }
  });
  stopIds.forEach(id => {
    const btn = document.getElementById(id);
    if (!btn) return;
    if (!arkaRunning) {
      // ARKA is stopped — disable Stop, nothing to stop
      btn.disabled = true;
      btn.style.opacity = '0.35';
      btn.style.cursor = 'not-allowed';
      btn.title = 'ARKA is not running';
    } else {
      btn.disabled = false;
      btn.style.opacity = '1';
      btn.style.cursor = 'pointer';
      btn.title = '';
    }
  });
}

function loadEngineStatus() {
  fetch('/api/engine/status')
    .then(r => r.json())
    .then(data => {
      const el = $('engine-status-grid');
      if (!el || !data) return;
      const engines = data.engines || {};
      const modules = data.modules || {};

      // Update Start/Stop button states based on ARKA running status
      const arkaRunning = !!(engines.arka?.running);
      _setArkaBtnState(arkaRunning);

      let h = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;">';
      Object.entries(engines).forEach(([name, info]) => {
        const on = info.running;
        h += `<div style="background:var(--bg3);border:1px solid ${on ? 'rgba(0,208,132,0.2)' : 'rgba(255,61,90,0.2)'};
          border-radius:6px;padding:8px;">
          <div style="font-weight:600;color:var(--text);text-transform:capitalize;font-size:11px">${name}</div>
          <div style="color:${on ? 'var(--green)' : 'var(--red)'};font-size:10px;margin-top:2px">
            ${on ? '● Running' : '○ Stopped'}${info.pid ? ' · PID ' + info.pid : ''}
          </div>
        </div>`;
      });
      h += '</div>';
      if (Object.keys(modules).length > 0) {
        h += '<div style="margin-top:12px;font-size:11px;color:var(--sub);margin-bottom:6px">Module Cache Health</div>';
        h += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:5px;">';
        Object.entries(modules).forEach(([name, info]) => {
          const ok = info.status === 'ok' || info.status === 'OK' || info.fresh === true;
          h += `<div style="background:var(--bg3);border:1px solid ${ok ? 'rgba(61,208,132,0.15)' : 'rgba(245,166,35,0.15)'};
            border-radius:4px;padding:5px 7px;">
            <div style="color:var(--text);font-weight:600;font-size:11px">${name}</div>
            <div style="color:${ok ? 'var(--green)' : 'var(--gold)'};font-size:10px">${info.status || info.age || 'unknown'}</div>
          </div>`;
        });
        h += '</div>';
      }
      el.innerHTML = h;
    })
    .catch(() => {});
}
setInterval(loadEngineStatus, 15000);

// ── Swings watchlist loader ────────────────────────────────
async function loadSwingsWatchlist() {
  // Fetch watchlist and live prices in parallel
  const [data, livePx] = await Promise.all([
    fetch('/api/swings/watchlist').then(r => r.json()).catch(() => ({})),
    fetch('/api/prices/live').then(r => r.json()).catch(() => ({})),
  ]);

  const el    = $('swings-watchlist-table');
  const count = $('swingsCandidateCount');
  const candidates = data.candidates || data.top5 || [];

  // Override stale prices with live prices (livePx keyed by ticker)
  if (livePx && !livePx.error) {
    candidates.forEach(c => {
      const live = livePx[c.ticker] || livePx[(c.ticker || '').toUpperCase()];
      if (live && live.price > 0) {
        c.price   = live.price;
        c.chg_pct = live.chg_pct ?? c.chg_pct;
        c.mom5    = live.chg_pct ?? c.mom5;
      }
    });
  }

  if (count) count.textContent = candidates.length + ' candidates';
  if (!el) return;
  if (!candidates.length) {
    el.innerHTML = '<div class="empty">No candidates — scanner runs at 8:15am ET<br><span style="font-size:10px">Requires: 1.5x volume · 60%+ conviction · ICT structure</span></div>';
    return;
  }

      // George-style cards grid
      el.innerHTML = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;padding:4px">' +
        candidates.slice(0, 12).map(c => {
          const isCall  = c.direction === 'LONG';
          const recCol  = isCall ? 'var(--green)' : 'var(--red)';
          const recTxt  = isCall ? 'BUY CALLS' : 'BUY PUTS';
          const sentTxt = isCall ? 'BULLISH' : 'BEARISH';
          const price   = parseFloat(c.price || 0);
          const score   = c.score || 0;
          const rsi     = parseFloat(c.rsi || 0);
          const vol     = parseFloat(c.vol_ratio || 1);
          const volRatio = vol;
          const mom5    = parseFloat(c.mom5 || 0);
          const tp1     = parseFloat(c.tp1 || 0);
          const stop    = parseFloat(c.stop || 0);
          const atr     = parseFloat(c.atr_pct || 0);
          const rr      = parseFloat(c.rr || 0);
          const reasons = (c.reasons || []).slice(0, 3);
          const confCol = score >= 75 ? 'var(--green)' : score >= 60 ? 'var(--gold)' : 'var(--sub)';
          const rsiCol  = rsi > 70 ? 'var(--red)' : rsi < 30 ? 'var(--green)' : 'var(--text)';
          const volCol  = vol >= 2 ? 'var(--red)' : vol >= 1.5 ? 'var(--gold)' : 'var(--sub)';
          const volLbl  = vol >= 2 ? 'SURGE '+vol.toFixed(1)+'X' : vol >= 1.5 ? 'ELEVATED' : 'DELTA';
          const orbTrend = isCall ? 'BULLISH' : 'BEARISH';
          const orbCol   = isCall ? 'var(--green)' : 'var(--red)';
          const alignment = score >= 75 ? 'ALIGNED' : score >= 60 ? 'MIXED' : 'WATCHING';
          const alignCol  = score >= 75 ? 'var(--green)' : score >= 60 ? 'var(--gold)' : 'var(--sub)';

          // Sweep alert — derive from mom5
          const sweepTxt = Math.abs(mom5) >= 3
            ? (isCall
                ? `PUT_WALL swept — expecting bullish reversal`
                : `CALL_WALL swept — expecting bearish reversal`)
            : null;

          return `<div class="sc george-card" data-sym="${c.ticker}" style="min-width:0;border:1px solid rgba(155,89,182,0.2)">

            <!-- HEADER -->
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
              <div>
                <div style="font-size:16px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1.1">
                  ${c.ticker}
                  <span id="px-chg-${c.ticker}"
                    style="font-size:10px;color:${mom5>=0?'var(--green)':'var(--red)'};font-weight:600;margin-left:4px">
                    ${mom5>=0?'+':''}${mom5.toFixed(2)}%
                  </span>
                </div>
                <div style="font-size:11px;color:var(--sub);font-family:'JetBrains Mono',monospace;margin-top:2px">
                  <span id="px-price-${c.ticker}">$${price.toFixed(2)}</span>
                  <span style="font-size:7px;color:var(--purple);margin-left:5px">🌀 SWING</span>
                </div>
              </div>
              <div style="text-align:right">
                <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">CROWD SENTIMENT</div>
                <span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;
                  background:${recCol}18;color:${recCol};border:1px solid ${recCol}35">${sentTxt}</span>
              </div>
            </div>

            <!-- SWEEP ALERT -->
            ${sweepTxt ? `<div style="font-size:9px;color:var(--gold);margin-bottom:7px;padding:5px 8px;
              background:var(--gold)0d;border-radius:4px;border-left:2px solid var(--gold)">${sweepTxt}</div>` : ''}

            <!-- RSI · VOLUME · ORB TREND -->
            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;margin-bottom:8px">
              <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
                <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">RSI (14)</div>
                <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:${rsiCol}">
                  ${rsi > 0 ? rsi.toFixed(1) : '—'}
                </div>
                <div style="height:2px;background:var(--bg2);border-radius:1px;margin-top:3px;overflow:hidden">
                  <div style="width:${Math.min(rsi,100)}%;height:100%;background:${rsiCol}"></div>
                </div>
              </div>
              <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
                <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">VOLUME</div>
                <div style="font-size:9px;font-weight:700;color:${volCol}">${volLbl}</div>
                <div style="font-size:7px;color:var(--sub);margin-top:2px">${vol.toFixed(1)}x avg</div>
              </div>
              <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
                <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">ORB TREND</div>
                <div style="font-size:9px;font-weight:700;color:${orbCol}">${orbTrend}</div>
              </div>
            </div>

            <!-- PROFILE + CONFIDENCE -->
            <div style="background:var(--bg3);border-radius:5px;padding:7px 9px;margin-bottom:7px;border-left:2px solid ${confCol}">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
                <span style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">Profile</span>
                <span style="font-size:8px;font-weight:700;color:var(--text)">${isCall ? 'TREND BULLISH' : 'TREND BEARISH'}</span>
              </div>
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
                <span style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">Confidence</span>
                <span style="font-size:12px;font-weight:700;color:${confCol}">${score}%</span>
              </div>
              <div style="display:flex;justify-content:space-between;align-items:center">
                <span style="font-size:9px;font-weight:700;color:${alignCol}">${alignment}</span>
                <span style="font-size:8px;color:var(--sub)">0 POOLS</span>
              </div>
            </div>

            <!-- STRUCTURE TAGS -->
            <div style="display:flex;gap:3px;flex-wrap:wrap;margin-bottom:7px">
              <span style="font-size:7px;padding:2px 6px;border-radius:3px;background:${recCol}15;color:${recCol};border:1px solid ${recCol}30;font-weight:700">
                ${isCall ? 'OB: BULLISH' : 'OB: BEARISH'}
              </span>
              <span style="font-size:7px;padding:2px 6px;border-radius:3px;background:${recCol}15;color:${recCol};border:1px solid ${recCol}30;font-weight:700">
                ${isCall ? 'Liquidity: BUY_SIDE' : 'Liquidity: SELL_SIDE'}
              </span>
              <span style="font-size:7px;padding:2px 6px;border-radius:3px;background:rgba(255,179,71,0.15);color:var(--gold);border:1px solid rgba(255,179,71,0.3);font-weight:700">
                ATR ${atr.toFixed(1)}%
              </span>
            </div>

            <!-- LEVELS TO WATCH -->
            <div style="margin-bottom:8px">
              <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">LEVELS TO WATCH</div>
              ${tp1 ? `<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--border)">
                <span style="font-size:9px;color:var(--sub)">TP1 (+20%)</span>
                <span style="font-size:9px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--green)">$${tp1.toFixed(2)}</span>
              </div>` : ''}
              ${stop ? `<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--border)">
                <span style="font-size:9px;color:var(--sub)">Stop (-30%)</span>
                <span style="font-size:9px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--red)">$${stop.toFixed(2)}</span>
              </div>` : ''}
              ${rr ? `<div style="display:flex;justify-content:space-between;padding:3px 0">
                <span style="font-size:9px;color:var(--sub)">R/R</span>
                <span style="font-size:9px;font-weight:700;font-family:'JetBrains Mono',monospace">1:${rr.toFixed(2)}</span>
              </div>` : ''}
            </div>

            <!-- DISCOMFORT INDEX -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:9px">
              <span style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">DISCOMFORT INDEX</span>
              <div style="position:relative;width:40px;height:40px">
                <svg viewBox="0 0 40 40" style="transform:rotate(-90deg);width:40px;height:40px">
                  <circle cx="20" cy="20" r="16" fill="none" stroke="var(--bg3)" stroke-width="3.5"/>
                  <circle cx="20" cy="20" r="16" fill="none" stroke="${confCol}" stroke-width="3.5"
                    stroke-dasharray="${((100-score)/100*100.53).toFixed(1)} 100.53" stroke-linecap="round"/>
                </svg>
                <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
                  font-size:8px;font-weight:800;color:${confCol};font-family:'JetBrains Mono',monospace">
                  ${100-score}%
                </div>
              </div>
            </div>

            <!-- ARKA RECOMMENDS -->
            <div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:6px">
              <div style="font-size:7px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;margin-bottom:5px;text-align:center">
                A R K A &nbsp; R E C O M M E N D S
              </div>
              <div style="color:${recCol};font-size:14px;font-weight:800;text-align:center;padding:4px 0">${recTxt}</div>
              <div style="text-align:center;margin-top:4px;margin-bottom:6px">
                <span style="font-size:8px;color:var(--sub)">1 contract · ≤21 DTE · hold up to 3 weeks</span>
              </div>
              <!-- Manual buy button -->
              <div style="display:flex;gap:6px;justify-content:center;margin-top:6px">
                <button onclick="manualSwingBuy('${c.ticker}','${isCall?'call':'put'}')"
                  style="flex:1;padding:7px;border-radius:5px;cursor:pointer;font-size:10px;
                    font-family:'JetBrains Mono',monospace;font-weight:700;border:none;
                    background:${recCol};color:#fff;letter-spacing:.5px">
                  ${isCall ? '📈 BUY CALL' : '📉 BUY PUT'}
                </button>
                <button onclick="manualSwingWatch('${c.ticker}')"
                  style="padding:7px 10px;border-radius:5px;cursor:pointer;font-size:10px;
                    background:var(--bg3);color:var(--sub);border:1px solid var(--border)">
                  👁
                </button>
              </div>
            </div>

            <!-- FULL ANALYSIS expandable -->
            <div style="text-align:center;margin-bottom:6px">
              <button onclick="event.stopPropagation();var d=this.nextElementSibling;d.style.display=d.style.display==='none'?'block':'none';this.textContent=d.style.display==='none'?'▼ FULL ANALYSIS':'▲ COLLAPSE'"
                style="font-size:8px;color:var(--sub);background:none;border:1px solid var(--border);
                  cursor:pointer;padding:3px 12px;border-radius:3px;width:100%">
                ▼ FULL ANALYSIS
              </button>
              <div style="display:none;text-align:left;padding-top:8px;margin-top:6px;border-top:1px solid var(--border)">
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
                  <div style="background:var(--bg3);border-radius:5px;padding:8px;border-left:2px solid var(--green)">
                    <div style="font-size:7px;color:var(--sub);margin-bottom:2px">BULL SIGNALS</div>
                    <div style="font-size:18px;font-weight:800;color:var(--green)">
                      ${Math.round(Math.max(5, isCall
                        ? (rsi > 50 ? 15 : 5) + (mom5 > 0 ? 15 : 5) + (volRatio >= 1.5 ? 15 : 5) + Math.round(score * 0.2)
                        : (rsi < 40 ? 20 : 10) + (mom5 < -3 ? 5 : 0) + Math.round(score * 0.1)
                      ))}
                    </div>
                    <div style="font-size:8px;color:var(--sub);margin-top:3px;line-height:1.3">
                      RSI ${rsi.toFixed(0)} · Vol ${volRatio.toFixed(1)}x · Mom ${mom5>=0?'+':''}${mom5.toFixed(1)}%
                    </div>
                  </div>
                  <div style="background:var(--bg3);border-radius:5px;padding:8px;border-left:2px solid var(--red)">
                    <div style="font-size:7px;color:var(--sub);margin-bottom:2px">BEAR SIGNALS</div>
                    <div style="font-size:18px;font-weight:800;color:var(--red)">
                      ${Math.round(Math.max(5, !isCall
                        ? (rsi < 40 ? 20 : rsi < 50 ? 15 : 5) + (mom5 < -3 ? 20 : mom5 < 0 ? 12 : 5) + (volRatio >= 1.5 ? 15 : 5) + Math.round(score * 0.2)
                        : (rsi > 60 ? 15 : 8) + (mom5 > 3 ? 5 : 0) + Math.round(score * 0.1)
                      ))}
                    </div>
                    <div style="font-size:8px;color:var(--sub);margin-top:3px">
                      ${!isCall ? ((reasons||[]).slice(0,1).join('')||'Bearish structure') : 'Bounce potential · RSI '+rsi.toFixed(0)}
                    </div>
                  </div>
                </div>
                <div style="background:var(--bg3);border-radius:5px;padding:8px;margin-bottom:8px">
                  <div style="font-size:7px;color:var(--sub);margin-bottom:6px">TRADE PLAN</div>
                  ${[['TP1 (+20%)',tp1?'$'+tp1.toFixed(2):'—','var(--green)'],
                     ['Stop (-30%)',stop?'$'+stop.toFixed(2):'—','var(--red)'],
                     ['R/R','1:'+rr.toFixed(2),'var(--text)'],
                     ['Max Hold','21 days','var(--sub)']].map(([l,v,c])=>
                    '<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--border)">'+
                    '<span style="font-size:9px;color:var(--sub)">'+l+'</span>'+
                    '<span style="font-size:9px;font-weight:700;font-family:JetBrains Mono,monospace;color:'+c+'">'+v+'</span></div>'
                  ).join('')}
                </div>
                ${(reasons||[]).length ? '<div>'+
                  '<div style="font-size:7px;color:var(--sub);letter-spacing:.8px;margin-bottom:4px">ENTRY REASONS</div>'+
                  (reasons||[]).map(r=>'<div style="font-size:8px;color:var(--sub);padding:2px 0;border-bottom:1px solid var(--border)">✓ '+r.trim()+'</div>').join('')+
                  '</div>' : ''}
              </div>
            </div>

            <!-- FOOTER -->
            <div style="display:flex;justify-content:space-between;align-items:center;
                padding-top:6px;margin-top:4px;border-top:1px solid var(--border);font-size:9px">
              <span style="color:${mom5>=0?'var(--green)':'var(--red)'};font-family:'JetBrains Mono',monospace;font-weight:700">
                ${mom5>=0?'+':''}${mom5.toFixed(2)}%
              </span>
              <span style="font-size:7px;color:var(--purple)">≤21 DTE · 1 contract</span>
              <span class="chip" style="font-size:7px;background:${recCol}18;color:${recCol}">${isCall?'CALL':'PUT'}</span>
            </div>
          </div>`;
        }).join('') + '</div>';

  // Load swing positions
  try {
    const posData = await fetch('/api/swings/positions').then(r => r.json());
    const posEl   = $('swings-positions-table');
    const posCount = $('swingsOpenCount');
    const open = posData.open || [];
    if (posCount) posCount.textContent = open.length + ' open';
    if (posEl) {
      if (!open.length) { posEl.innerHTML = '<div class="empty">No open swing positions</div>'; }
      else posEl.innerHTML = open.map(p => {
        const pnl    = parseFloat(p.pnl || 0);
        const pnlPct = parseFloat(p.pnl_pct || 0);
        const pnlCol = pnlPct >= 8 ? 'var(--gold)' : pnlPct > 0 ? 'var(--green)' : pnlPct < 0 ? 'var(--red)' : 'var(--sub)';
        return `<div style="padding:8px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
          <div>
            <span style="font-weight:700;font-size:13px;font-family:'JetBrains Mono',monospace">${p.ticker || ''}</span>
            <span style="color:var(--sub);font-size:10px;margin-left:8px">${p.status || ''}</span>
          </div>
          <span style="color:${pnlCol};font-weight:700;font-family:'JetBrains Mono',monospace">
            ${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%
          </span>
        </div>`;
      }).join('');
    }
  } catch {}

  // Load scan feed into right panel — use ARJUN signals (CHAKRA swing engine)
  try {
    const sigs = await fetch('/api/signals').then(r => r.json());
    _renderSwingsSignalFeed(sigs || []);
  } catch { _renderSwingsSignalFeed([]); }
}
setInterval(loadSwingsWatchlist, 30000);

// ── Manual Swing Buy ──────────────────────────────────────────────────
async function manualSwingBuy(ticker, direction) {
  const confirmed = confirm(
    `Manual ${direction.toUpperCase()} order for ${ticker}?

` +
    `• 1 contract · ≤21 DTE · ATM strike
` +
    `• Stop: -30% · Target: +20%

` +
    `This will place a REAL paper trading order.`
  );
  if (!confirmed) return;

  try {
    const r = await fetch('/api/swings/manual-entry', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ticker, direction, source: 'manual' })
    });
    const d = await r.json();
    if (d.success) {
      toast(`✅ ${ticker} ${direction.toUpperCase()} order placed — ${d.contract || '1 contract'}`);
      loadSwingsWatchlist();
    } else {
      toast(`❌ Order failed: ${d.error || 'unknown error'}`, 4000);
    }
  } catch(e) {
    toast(`❌ Error: ${e.message}`, 4000);
  }
}

function manualSwingWatch(ticker) {
  toast(`👁 ${ticker} added to watchlist`);
}

// ── Swings right panel — CHAKRA/ARJUN signal feed ──────────
function _renderSwingsSignalFeed(sigs) {
  const el    = $('swingsScanRows');
  const count = $('swingsScanCount');
  if (!el) return;
  if (count) count.textContent = sigs.length;

  if (!sigs.length) {
    el.innerHTML = `<div style="padding:20px;text-align:center">
      <div style="color:var(--sub);font-size:11px;margin-bottom:8px">No CHAKRA signals today</div>
      <div style="font-size:9px;color:var(--sub)">ARJUN runs at 8:00am ET</div>
    </div>`;
    return;
  }

  // Deduplicate by ticker
  const seen = new Set();
  const unique = sigs.filter(s => {
    const k = (s.ticker || s.symbol || '').toUpperCase();
    if (!k || seen.has(k)) return false;
    seen.add(k); return true;
  });

  el.innerHTML = unique.map(s => {
    const sym   = (s.ticker || s.symbol || '').toUpperCase();
    const sig   = (s.signal || 'HOLD').toUpperCase();
    const conf  = parseFloat(s.confidence || 0);
    const col   = sig === 'BUY'  ? 'var(--green)' :
                  sig === 'SELL' ? 'var(--red)'   : 'var(--sub)';
    const confCol = conf >= 70 ? 'var(--green)' : conf >= 55 ? 'var(--gold)' : 'var(--sub)';
    const agents = s.agents || {};
    const bull   = agents.bull?.score || 0;
    const bear   = agents.bear?.score || 0;

    return `<div style="padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer;
        transition:background .15s"
        onclick="_showChakraSignalDetail('${sym}')"
        onmouseover="this.style.background='rgba(255,255,255,0.02)'"
        onmouseout="this.style.background='transparent'">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700">${sym}</span>
        <span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:10px;
          background:${col}15;color:${col};border:1px solid ${col}40">${sig}</span>
      </div>
      <div style="height:3px;background:var(--bg3);border-radius:2px;overflow:hidden;margin-bottom:4px">
        <div style="width:${conf}%;height:100%;background:${confCol};border-radius:2px"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--sub)">
        <span>Conf <span style="color:${confCol};font-weight:700">${conf.toFixed(1)}%</span></span>
        ${bull || bear ? `<span>🐂${bull} 🐻${bear}</span>` : ''}
      </div>
    </div>`;
  }).join('');

  window._chakraSignals = sigs;
}

// ── CHAKRA signal detail modal ─────────────────────────────
function _showChakraSignalDetail(sym) {
  const existing = document.getElementById('chakraSignalModal');
  if (existing) existing.remove();

  const sigs = window._chakraSignals || [];
  const s = sigs.find(x => (x.ticker || x.symbol || '').toUpperCase() === sym);
  if (!s) return;

  const sig    = (s.signal || 'HOLD').toUpperCase();
  const conf   = parseFloat(s.confidence || 0);
  const col    = sig === 'BUY' ? 'var(--green)' : sig === 'SELL' ? 'var(--red)' : 'var(--gold)';
  const agents = s.agents || {};
  const bull   = agents.bull   || {};
  const bear   = agents.bear   || {};
  const risk   = agents.risk_manager || {};
  const entry  = parseFloat(s.entry || s.entry_price || s.price || 0);
  const target = parseFloat(s.target || s.target_price || 0);
  const stop   = parseFloat(s.stop_loss || s.stop || 0);

  const modal = document.createElement('div');
  modal.id = 'chakraSignalModal';
  modal.style.cssText = `position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:600;
    display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)`;
  modal.onclick = e => { if (e.target === modal) modal.remove(); };

  modal.innerHTML = `
    <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;
      width:min(540px,95vw);max-height:85vh;overflow-y:auto;padding:24px">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px">
        <div>
          <div style="font-size:22px;font-weight:700">${sym}</div>
          <div style="font-size:11px;color:var(--sub);margin-top:2px">CHAKRA · ARJUN Signal</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <span style="font-size:13px;font-weight:700;padding:4px 14px;border-radius:10px;
            background:${col}15;color:${col};border:1px solid ${col}40">${sig}</span>
          <button onclick="document.getElementById('chakraSignalModal').remove()"
            style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;
            padding:6px 12px;cursor:pointer;color:var(--sub);font-size:12px">✕</button>
        </div>
      </div>

      <!-- Confidence -->
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:10px;
        padding:14px;margin-bottom:16px">
        <div style="font-size:9px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;margin-bottom:8px">Confidence</div>
        <div style="display:flex;align-items:center;gap:12px">
          <div style="font-size:36px;font-weight:800;font-family:'JetBrains Mono',monospace;
            color:${conf >= 70 ? 'var(--green)' : conf >= 55 ? 'var(--gold)' : 'var(--red)'}">${conf.toFixed(1)}%</div>
          <div style="flex:1">
            <div style="height:8px;background:var(--bg2);border-radius:4px;overflow:hidden">
              <div style="width:${conf}%;height:100%;background:${conf >= 70 ? 'var(--green)' : conf >= 55 ? 'var(--gold)' : 'var(--red)'};border-radius:4px"></div>
            </div>
            <div style="font-size:9px;color:var(--sub);margin-top:4px">${conf >= 70 ? 'HIGH conviction' : conf >= 55 ? 'MODERATE conviction' : 'LOW conviction'}</div>
          </div>
        </div>
      </div>

      <!-- Trade levels -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:16px">
        ${[['Entry', f$(entry), 'var(--text)'], ['Target', f$(target), 'var(--green)'], ['Stop', f$(stop), 'var(--red)']].map(([l, v, c]) =>
          `<div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:10px;text-align:center">
            <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">${l}</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:700;color:${c}">${v}</div>
          </div>`
        ).join('')}
      </div>

      <!-- Agent scores -->
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:16px">
        <div style="font-size:9px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;margin-bottom:10px">Agent Consensus</div>
        ${[
          ['🐂 Bull Agent',  bull.score || 0,  bull.key_catalyst || 'Bullish setup', 'var(--green)'],
          ['🐻 Bear Agent',  bear.score || 0,  bear.key_risk     || 'Monitor risk',  'var(--red)'],
          ['⚖️ Risk Manager', risk.decision === 'APPROVE' ? 100 : 0, risk.reason || risk.decision || '—', risk.decision === 'APPROVE' ? 'var(--green)' : 'var(--red)'],
        ].map(([name, score, note, c]) =>
          `<div style="margin-bottom:8px">
            <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:3px">
              <span style="font-weight:600">${name}</span>
              <span style="color:${c};font-family:'JetBrains Mono',monospace;font-weight:700">${typeof score === 'number' && score <= 100 ? score + '/100' : score}</span>
            </div>
            <div style="height:4px;background:var(--bg2);border-radius:2px;overflow:hidden;margin-bottom:2px">
              <div style="width:${Math.min(score,100)}%;height:100%;background:${c};border-radius:2px"></div>
            </div>
            <div style="font-size:9px;color:var(--sub)">${note.slice(0, 80)}</div>
          </div>`
        ).join('')}
      </div>

      <!-- Explanation -->
      ${s.explanation ? `
        <div style="background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:14px">
          <div style="font-size:9px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;margin-bottom:8px">Analysis</div>
          <div style="font-size:11px;color:var(--text);line-height:1.6">${s.explanation.slice(0, 500).replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>')}${s.explanation.length > 500 ? '…' : ''}</div>
        </div>` : ''}
    </div>`;

  document.body.appendChild(modal);
}

function _toggleScanFeedExpand() {
  const existing = document.getElementById('scanFeedModal');
  if (existing) { existing.remove(); return; }
  const sigs = window._chakraSignals || [];

  const modal = document.createElement('div');
  modal.id = 'scanFeedModal';
  modal.style.cssText = `position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:500;
    display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)`;
  modal.onclick = e => { if (e.target === modal) modal.remove(); };

  const seen = new Set();
  const unique = sigs.filter(s => {
    const k = (s.ticker || s.symbol || '').toUpperCase();
    if (!k || seen.has(k)) return false;
    seen.add(k); return true;
  });

  const rows = unique.map(s => {
    const sym  = (s.ticker || s.symbol || '').toUpperCase();
    const sig  = (s.signal || 'HOLD').toUpperCase();
    const conf = parseFloat(s.confidence || 0);
    const col  = sig === 'BUY' ? 'var(--green)' : sig === 'SELL' ? 'var(--red)' : 'var(--sub)';
    const confCol = conf >= 70 ? 'var(--green)' : conf >= 55 ? 'var(--gold)' : 'var(--sub)';
    const agents = s.agents || {};
    const bull = agents.bull?.score || 0, bear = agents.bear?.score || 0;
    return `<div style="display:grid;grid-template-columns:80px 1fr 80px 80px 80px;gap:10px;
        align-items:center;padding:10px 20px;border-bottom:1px solid var(--border);cursor:pointer"
        onclick="_showChakraSignalDetail('${sym}')">
      <span style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700">${sym}</span>
      <div>
        <div style="height:4px;background:var(--bg3);border-radius:2px;overflow:hidden">
          <div style="width:${conf}%;height:100%;background:${confCol};border-radius:2px"></div>
        </div>
      </div>
      <span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:${confCol};font-weight:700;text-align:right">${conf.toFixed(1)}%</span>
      <span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:10px;
        background:${col}15;color:${col};border:1px solid ${col}40;text-align:center">${sig}</span>
      <span style="font-size:9px;color:var(--sub);text-align:right">🐂${bull} 🐻${bear}</span>
    </div>`;
  }).join('');

  modal.innerHTML = `
    <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;
      width:min(680px,95vw);max-height:85vh;display:flex;flex-direction:column;overflow:hidden">
      <div style="padding:16px 20px;border-bottom:1px solid var(--border);
        display:flex;justify-content:space-between;align-items:center;flex-shrink:0">
        <div>
          <div style="font-size:16px;font-weight:700">🧠 CHAKRA Signal Overview</div>
          <div style="font-size:10px;color:var(--sub);margin-top:2px">${unique.length} tickers · ARJUN multi-agent analysis · click for details</div>
        </div>
        <button onclick="document.getElementById('scanFeedModal').remove()"
          style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;
          padding:6px 12px;cursor:pointer;color:var(--sub);font-size:12px">✕ Close</button>
      </div>
      <div style="display:grid;grid-template-columns:80px 1fr 80px 80px 80px;gap:10px;
        padding:6px 20px;background:var(--bg3);border-bottom:1px solid var(--border);flex-shrink:0">
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub)">TICKER</span>
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub)">CONFIDENCE</span>
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub);text-align:right">SCORE</span>
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub);text-align:center">SIGNAL</span>
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub);text-align:right">AGENTS</span>
      </div>
      <div style="overflow-y:auto;flex:1">${rows || '<div class="empty" style="padding:40px">No signals today — ARJUN runs at 8am ET</div>'}</div>
    </div>`;

  document.body.appendChild(modal);
}

function _toggleScanFeedExpand() {
  const existing = document.getElementById('scanFeedModal');
  if (existing) { existing.remove(); return; }
  const scans = window._allScans || [];

  const modal = document.createElement('div');
  modal.id = 'scanFeedModal';
  modal.style.cssText = `position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:500;
    display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)`;
  modal.onclick = e => { if (e.target === modal) modal.remove(); };

  const rows = scans.slice().reverse().map(e => {
    const sc      = parseFloat(e.score || 0);
    const fakeout = parseFloat(e.fakeout || 0);
    const col     = sc >= 75 ? 'var(--green)' : sc >= 55 ? 'var(--gold)' : 'var(--sub)';
    const dl      = _decisionLabel(e.decision);
    const isTrade = dl.label.includes('BUY') || dl.label.includes('SELL') || dl.label.includes('TRADE');
    return `<div style="display:grid;grid-template-columns:52px 44px 1fr 40px 120px;gap:8px;
        align-items:center;padding:7px 16px;border-bottom:1px solid var(--border);
        cursor:pointer;${isTrade ? 'background:rgba(0,208,132,0.05);' : ''}"
        onclick="_showScanDetail('${e.ticker}','${e.time}',${sc},${fakeout},'${e.decision||''}')">
      <span style="font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--sub)">${e.time || '—'}</span>
      <span style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--blue)">${e.ticker || '—'}</span>
      <div>
        <div style="height:4px;background:var(--bg3);border-radius:2px;overflow:hidden">
          <div style="width:${Math.min(sc,100)}%;height:100%;background:${col};border-radius:2px"></div>
        </div>
        <div style="font-size:8px;color:var(--sub);margin-top:2px">conv ${sc.toFixed(0)}${fakeout > 0 ? ` · fakeout ${(fakeout*100).toFixed(0)}%${fakeout > 0.5 ? ' ⚠' : ''}` : ''}</div>
      </div>
      <span style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:${col};text-align:right">${sc.toFixed(0)}</span>
      <span style="font-size:9px;color:${dl.color};text-align:right;font-weight:600">${dl.label}</span>
    </div>`;
  }).join('');

  modal.innerHTML = `
    <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;
      width:min(720px,95vw);max-height:85vh;display:flex;flex-direction:column;overflow:hidden">
      <div style="padding:16px 20px;border-bottom:1px solid var(--border);
        display:flex;justify-content:space-between;align-items:center;flex-shrink:0">
        <div>
          <div style="font-size:16px;font-weight:700">⚡ Full Scan History</div>
          <div style="font-size:10px;color:var(--sub);margin-top:2px">${scans.length} scans today · click any row for details</div>
        </div>
        <button onclick="document.getElementById('scanFeedModal').remove()"
          style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;
          padding:6px 12px;cursor:pointer;color:var(--sub);font-size:12px">✕ Close</button>
      </div>
      <!-- Column headers -->
      <div style="display:grid;grid-template-columns:52px 44px 1fr 40px 120px;gap:8px;
        padding:6px 16px;background:var(--bg3);border-bottom:1px solid var(--border);flex-shrink:0">
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub)">TIME</span>
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub)">TICKER</span>
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub)">CONVICTION</span>
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub);text-align:right">SC</span>
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub);text-align:right">DECISION</span>
      </div>
      <div style="overflow-y:auto;flex:1">${rows || '<div class="empty" style="padding:40px">No scan data</div>'}</div>
    </div>`;

  document.body.appendChild(modal);
}

// ── Individual scan detail modal ───────────────────────────
function _showScanDetail(ticker, time, score, fakeout, decision) {
  const existing = document.getElementById('scanDetailModal');
  if (existing) existing.remove();

  const sc   = parseFloat(score || 0);
  const fk   = parseFloat(fakeout || 0);
  const dl   = _decisionLabel(decision);
  const col  = sc >= 75 ? 'var(--green)' : sc >= 55 ? 'var(--gold)' : 'var(--red)';

  // Score breakdown
  const convPassed  = sc >= 55;
  const fakeoutPass = fk < 0.5;
  const conditions  = [
    { label: 'Conviction Score',     value: sc.toFixed(1),             passed: convPassed,  threshold: '≥55 to trade' },
    { label: 'Fakeout Probability',  value: (fk * 100).toFixed(1) + '%', passed: fakeoutPass, threshold: '<50% to proceed' },
    { label: 'Session Filter',       value: decision.includes('LUNCH') ? 'LUNCH BLOCKED' : 'OPEN', passed: !decision.includes('LUNCH'), threshold: 'Market hours only' },
    { label: 'Streak Guard',         value: decision.includes('STREAK') ? 'PAUSED' : 'OK',   passed: !decision.includes('STREAK'), threshold: 'Max 3 consecutive losses' },
    { label: 'Position Check',       value: decision.includes('ALREADY') ? 'ALREADY IN' : 'CLEAR', passed: !decision.includes('ALREADY'), threshold: 'No duplicate positions' },
  ];

  const modal = document.createElement('div');
  modal.id = 'scanDetailModal';
  modal.style.cssText = `position:fixed;inset:0;background:rgba(0,0,0,0.8);z-index:600;
    display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)`;
  modal.onclick = e => { if (e.target === modal) modal.remove(); };

  modal.innerHTML = `
    <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;
      width:min(480px,95vw);padding:24px;position:relative">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px">
        <div>
          <div style="font-size:20px;font-weight:700">Scan: ${ticker}</div>
          <div style="font-size:11px;color:var(--sub);margin-top:2px">${time} · ${new Date().toLocaleDateString('en-US',{month:'short',day:'numeric'})}</div>
        </div>
        <button onclick="document.getElementById('scanDetailModal').remove()"
          style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;
          padding:5px 10px;cursor:pointer;color:var(--sub);font-size:11px">✕</button>
      </div>

      <!-- Score gauge -->
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px;text-align:center">
        <div style="font-size:10px;letter-spacing:2px;color:var(--sub);text-transform:uppercase;margin-bottom:8px">Conviction Score</div>
        <div style="font-size:48px;font-weight:800;font-family:'JetBrains Mono',monospace;color:${col};line-height:1">${sc.toFixed(0)}</div>
        <div style="font-size:11px;color:var(--sub);margin:6px 0 10px">/ 100</div>
        <div style="height:8px;background:var(--bg2);border-radius:4px;overflow:hidden;margin:0 20px">
          <div style="width:${Math.min(sc,100)}%;height:100%;background:${col};border-radius:4px;transition:width 0.5s"></div>
        </div>
        <div style="margin-top:10px">
          <span style="font-size:10px;font-weight:700;padding:3px 10px;border-radius:10px;
            background:${dl.color}18;color:${dl.color};border:1px solid ${dl.color}40">${dl.label}</span>
        </div>
      </div>

      <!-- Condition checklist -->
      <div style="margin-bottom:16px">
        <div style="font-size:9px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;margin-bottom:8px">Condition Checklist</div>
        ${conditions.map(c => `
          <div style="display:flex;align-items:center;gap:10px;padding:7px 10px;
            background:${c.passed ? 'rgba(0,208,132,0.04)' : 'rgba(255,61,90,0.04)'};
            border:1px solid ${c.passed ? 'rgba(0,208,132,0.12)' : 'rgba(255,61,90,0.12)'};
            border-radius:6px;margin-bottom:5px">
            <span style="font-size:14px;flex-shrink:0">${c.passed ? '✅' : '❌'}</span>
            <div style="flex:1">
              <div style="font-size:11px;font-weight:600">${c.label}</div>
              <div style="font-size:9px;color:var(--sub)">${c.threshold}</div>
            </div>
            <span style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;
              color:${c.passed ? 'var(--green)' : 'var(--red)'}">${c.value}</span>
          </div>`
        ).join('')}
      </div>

      <!-- Fakeout note -->
      ${fk > 0 ? `
        <div style="background:${fk > 0.5 ? 'rgba(255,61,90,0.08)' : 'rgba(245,166,35,0.08)'};
          border:1px solid ${fk > 0.5 ? 'rgba(255,61,90,0.2)' : 'rgba(245,166,35,0.2)'};
          border-radius:8px;padding:10px 12px;font-size:10px;color:var(--sub)">
          <strong style="color:${fk > 0.5 ? 'var(--red)' : 'var(--gold)'}">
            ${fk > 0.5 ? '⚠️ High fakeout risk' : '📊 Fakeout probability'}
          </strong>
          — XGBoost model: ${(fk * 100).toFixed(1)}% chance this signal is a fakeout.
          ${fk > 0.5 ? 'ARKA blocked this trade.' : 'Within acceptable range.'}
        </div>` : ''}
    </div>`;

  document.body.appendChild(modal);
}

// ── Lotto live ─────────────────────────────────────────────
// ── Lotto Live Page ───────────────────────────────────────────────────────────
// CHAKRA-style index cards for SPX, RUT, SPY, QQQ, IWM, DIA
// Replaces the existing loadLottoLive() function

const LOTTO_INDEXES = ['SPX','RUT','SPY','QQQ','IWM','DIA'];

async function clearLottoState() {
  try {
    const r = await fetch('/api/lotto/clear', { method: 'POST' });
    const d = await r.json();
    if (d.cleared) {
      const liveEl = $('lottoLive');
      if (liveEl) liveEl.style.display = 'none';
      const phEl = $('phStatus');
      if (phEl) phEl.innerHTML = '<span style="color:var(--sub);font-size:11px">Lotto state cleared</span>';
    }
  } catch(e) { console.warn('clearLottoState error', e); }
}

async function loadLottoLive() {
  // Fetch lotto status — drive phStatus message + live trade card
  const d = await get('/api/lotto/status').catch(() => null);
  const phEl = $('phStatus');
  const liveEl = $('lottoLive');

  if (d) {
    if (d.active && d.trade) {
      const tr = d.trade;
      if (phEl) phEl.innerHTML = '<span style="color:var(--green);font-weight:700;font-size:11px">🚀 ACTIVE TRADE IN PROGRESS</span>';
      if (liveEl) {
        liveEl.style.display = 'block';
        if ($('lottoContract')) $('lottoContract').textContent = tr.contract?.symbol || '—';
        if ($('lottoEntry'))    $('lottoEntry').textContent    = '$' + (tr.entry_price  || 0).toFixed(2);
        if ($('lottoTarget'))   $('lottoTarget').textContent   = '$' + (tr.target_price || 0).toFixed(2);
        if ($('lottoStop'))     $('lottoStop').textContent     = '$' + (tr.stop_price   || 0).toFixed(2);
      }
    } else {
      if (liveEl) liveEl.style.display = 'none';
      if (phEl) {
        if (d.in_lotto_window) {
          phEl.innerHTML = '<span style="color:var(--gold);font-size:11px">🎯 Scanning for conviction &ge;50</span>';
        } else {
          phEl.innerHTML = '<span style="color:var(--sub);font-size:11px">Lotto window opens 3:30 PM ET</span>';
        }
      }
    }
  } else {
    if (liveEl) liveEl.style.display = 'none';
    if (phEl) phEl.innerHTML = '<span style="color:var(--sub);font-size:11px">No active lotto today</span>';
  }

  // Render index signal cards
  await _renderLottoIndexCards();
}

async function _renderLottoIndexCards() {
  const INDEXES = ['SPX','RUT','SPY','QQQ','IWM','DIA'];

  // Power Hour pane container
  const container = $('lottoIndexCards');
  if (!container) return;

  // Trading tab stub container (clear "Loading..." state)
  const tabContent = $('lottoTabContent');

  const gridHtml = '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px">'
    + INDEXES.map(t => `<div id="lotto-card-${t}" style="background:var(--bg2);border:1px solid var(--border2);
      border-radius:10px;padding:14px">
      <div style="font-size:15px;font-weight:800;color:var(--gold);font-family:JetBrains Mono,monospace">${t}</div>
      <div class="ld" style="margin-top:8px"><div class="spin"></div></div>
    </div>`).join('') + '</div>';

  container.innerHTML = gridHtml;
  // Mirror spinner into Trading tab while loading
  if (tabContent) tabContent.innerHTML = '<div style="color:var(--sub);font-size:10px;padding:8px 0;text-align:center">Loading index data…</div>';

  // Fetch GEX + flow data
  const [flowData] = await Promise.all([
    getCached('/api/flow/signals', 30000).catch(() => null),
  ]);

  // Load each ticker card
  for (const ticker of INDEXES) {
    const el = $(`lotto-card-${ticker}`);
    if (!el) continue;
    try {
      const gex = await getCached(`/api/options/gex?ticker=${ticker}`, 60000).catch(() => null);
      const spot    = gex?.spot || 0;
      const cw      = gex?.call_wall || 0;
      const pw      = gex?.put_wall  || 0;
      const regime  = gex?.regime    || '—';
      const netGex  = parseFloat(gex?.net_gex || 0);
      const isIndex = ticker === 'SPX' || ticker === 'RUT';

      const flow   = (flowData?.signals || []).find(s => s.ticker === ticker) || {};
      const bias   = flow.bias || 'NEUTRAL';
      const conf   = flow.confidence || 0;
      const biasCol = bias === 'BULLISH' ? 'var(--green)' : bias === 'BEARISH' ? 'var(--red)' : 'var(--sub)';
      const regimeCol = regime === 'pinned' ? 'var(--blue)' : regime.includes('explosive') || regime.includes('negative') ? 'var(--red)' : 'var(--gold)';
      const grade = conf >= 80 ? 'A+' : conf >= 65 ? 'A' : conf >= 50 ? 'B+' : conf >= 35 ? 'B' : '—';

      el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-size:15px;font-weight:800;color:var(--gold);font-family:JetBrains Mono,monospace">${ticker}</span>
          <span style="font-size:9px;padding:1px 5px;border-radius:3px;background:${isIndex?'rgba(255,140,0,0.15)':'rgba(0,208,132,0.1)'};
            color:${isIndex?'var(--gold)':'var(--green)'};border:1px solid ${isIndex?'var(--gold)':'var(--green)'}40">
            ${isIndex ? 'INFO ONLY' : '0DTE'}</span>
        </div>
        <div style="font-size:18px;font-weight:700;font-family:JetBrains Mono,monospace;margin-bottom:6px">
          ${spot > 0 ? '$' + spot.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—'}
        </div>
        <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px">
          ${cw ? `<span style="font-size:8px;padding:1px 5px;border-radius:3px;background:rgba(0,208,132,0.1);color:var(--green)">CW $${cw.toLocaleString()}</span>` : ''}
          ${pw ? `<span style="font-size:8px;padding:1px 5px;border-radius:3px;background:rgba(255,45,85,0.1);color:var(--red)">PW $${pw.toLocaleString()}</span>` : ''}
          ${regime !== '—' ? `<span style="font-size:8px;padding:1px 5px;border-radius:3px;color:${regimeCol};border:1px solid ${regimeCol}40">${regime.toUpperCase()}</span>` : ''}
        </div>
        <div style="display:flex;justify-content:space-between;background:var(--bg3);border-radius:5px;padding:6px 8px">
          <span style="font-size:10px;font-weight:700;color:${biasCol}">${bias === 'BULLISH' ? '🟢' : bias === 'BEARISH' ? '🔴' : '⚪'} ${bias}</span>
          <span style="font-size:10px;color:var(--sub)">${grade} · ${conf}%</span>
        </div>
        ${netGex !== 0 ? `<div style="font-size:8px;color:var(--sub);text-align:center;margin-top:4px">
          Net GEX <span style="color:${netGex>=0?'var(--green)':'var(--red)'}">
          ${netGex>=0?'+':''}${netGex.toFixed(2)}B</span></div>` : ''}`;
    } catch(e) {
      el.innerHTML = `<div style="color:var(--sub);font-size:10px">${ticker}: Error loading</div>`;
    }
  }

  // Copy finished cards into the Trading tab stub (avoids duplicate API calls)
  if (tabContent && container.innerHTML) {
    tabContent.innerHTML = container.innerHTML;
    // Re-map cloned IDs so they don't clash with the Power Hour copies
    tabContent.querySelectorAll('[id^="lotto-card-"]').forEach(el => {
      el.id = 'tab-' + el.id;
    });
  }
}

async function fetchLottoPrices() {
  // Fetch live prices + GEX for all lotto indexes
  const prices = {};
  try {
    // Get GEX data for each index
    const gexTickers = ['SPY','QQQ','IWM','SPX','RUT'];
    const gexResults = await Promise.allSettled(
      gexTickers.map(t => getCached(`/api/options/gex?ticker=${t}`, 60000))
    );
    gexTickers.forEach((t, i) => {
      const r = gexResults[i];
      if (r.status === 'fulfilled' && r.value) {
        const g = r.value;
        prices[t] = prices[t] || {};
        prices[t].spot = g.spot || 0;
        prices[t].gex  = {
          call_wall: g.call_wall,
          put_wall:  g.put_wall,
          regime:    g.regime,
          net_gex:   g.net_gex,
        };
      }
    });

    // Get live prices from Alpaca for tradeable ETFs
    const acct = await getCached('/api/account', 15000);
    if (acct?.prices) {
      ['SPY','QQQ','IWM','DIA'].forEach(t => {
        const p = acct.prices?.[t];
        if (p) {
          prices[t] = prices[t] || {};
          prices[t].spot = p.price || prices[t].spot || 0;
          prices[t].change = p.change || 0;
          prices[t].change_pct = p.change_pct || 0;
        }
      });
    }

    // DIA fallback
    if (!prices['DIA']) prices['DIA'] = { spot: 0, gex: {} };

  } catch(e) {
    console.error('fetchLottoPrices error:', e);
  }
  return prices;
}


// ── Ticker Management ──────────────────────────────────────
const ARKA_DEFAULT_TICKERS   = ['SPY','QQQ'];
const CHAKRA_DEFAULT_TICKERS = [];  // uses Polygon screener
const TARAKA_DEFAULT_TICKERS = [];  // uses Polygon screener

function _loadTickers(key, defaults) {
  try { return JSON.parse(localStorage.getItem(key)) || defaults.slice(); }
  catch { return defaults.slice(); }
}
function _saveTickers(key, arr) {
  localStorage.setItem(key, JSON.stringify(arr));
}

function _renderTickerChips(containerId, tickers, removeKey) {
  const el = $(containerId); if (!el) return;
  if (!tickers.length) { el.innerHTML = '<span style="font-size:9px;color:var(--sub)">No custom tickers</span>'; return; }
  el.innerHTML = tickers.map(t =>
    '<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;'
    + 'background:var(--bg3);border:1px solid var(--border);border-radius:12px;'
    + 'font-size:10px;font-weight:700;font-family:\'JetBrains Mono\',monospace">'
    + t
    + '<button onclick="removeTicker(\'' + removeKey + '\',\'' + t + '\')" '
    + 'style="background:none;border:none;color:var(--sub);cursor:pointer;font-size:10px;padding:0 0 0 2px;line-height:1">×</button>'
    + '</span>'
  ).join('');
}

function removeTicker(key, ticker) {
  const arr = _loadTickers(key, []);
  const idx = arr.indexOf(ticker);
  if (idx > -1) arr.splice(idx, 1);
  _saveTickers(key, arr);
  _refreshTickerUI(key);
  toast('Removed ' + ticker);
}

function _refreshTickerUI(key) {
  if (key === 'arka_tickers') {
    const tickers = _loadTickers('arka_tickers', ARKA_DEFAULT_TICKERS);
    const all = [...new Set([...ARKA_DEFAULT_TICKERS, ...tickers])];
    _renderTickerChips('arkaTkrChips', tickers.filter(t => !ARKA_DEFAULT_TICKERS.includes(t)), key);
    if ($('arkaTkrCount')) $('arkaTkrCount').textContent = all.join(' · ');
  } else if (key === 'chakra_tickers') {
    const tickers = _loadTickers('chakra_tickers', []);
    _renderTickerChips('chakraTkrChips', tickers, key);
    if ($('chakraTkrCount')) $('chakraTkrCount').textContent = tickers.length ? tickers.join(' · ') : 'Polygon screener';
  } else if (key === 'taraka_tickers') {
    const tickers = _loadTickers('taraka_tickers', []);
    _renderTickerChips('tarakaTkrChips', tickers, key);
    if ($('tarakaTkrCount')) $('tarakaTkrCount').textContent = tickers.length ? tickers.join(' · ') : '$0.10–$5.00';
  }
}

function addArkaTicker(raw) {
  const ticker = (raw||'').trim().toUpperCase().replace(/[^A-Z]/g,'');
  if (!ticker) return;
  const arr = _loadTickers('arka_tickers', ARKA_DEFAULT_TICKERS);
  if (!arr.includes(ticker)) { arr.push(ticker); _saveTickers('arka_tickers', arr); }
  _refreshTickerUI('arka_tickers');
  if ($('arkaTkrInput')) $('arkaTkrInput').value = '';
  toast('Added ' + ticker + ' to ARKA');
}

function addChakraTicker(raw) {
  const ticker = (raw||'').trim().toUpperCase().replace(/[^A-Z]/g,'');
  if (!ticker) return;
  const arr = _loadTickers('chakra_tickers', []);
  if (!arr.includes(ticker)) { arr.push(ticker); _saveTickers('chakra_tickers', arr); }
  _refreshTickerUI('chakra_tickers');
  if ($('chakraTkrInput')) $('chakraTkrInput').value = '';
  toast('Pinned ' + ticker + ' to CHAKRA watchlist');
}

function addTarakaTicker(raw) {
  const ticker = (raw||'').trim().toUpperCase().replace(/[^A-Z]/g,'');
  if (!ticker) return;
  const arr = _loadTickers('taraka_tickers', []);
  if (!arr.includes(ticker)) { arr.push(ticker); _saveTickers('taraka_tickers', arr); }
  _refreshTickerUI('taraka_tickers');
  if ($('tarakaTkrInput')) $('tarakaTkrInput').value = '';
  toast('Watching ' + ticker + ' in TARAKA');
}

// Initialize ticker chips on page load
function initTickerUI() {
  _refreshTickerUI('arka_tickers');
  _refreshTickerUI('chakra_tickers');
  _refreshTickerUI('taraka_tickers');
}

// Auto-init
setTimeout(initTickerUI, 300);

// ── Manual sell button ────────────────────────────────────────────────────────
async function manualSell(ticker, qty, event) {
  if (event) event.stopPropagation();
  // Find the actual traded symbol (QQQ → SQQQ etc)
  const summary = await getCached('/api/arka/summary', 5000);
  const openPos = summary?.open_positions || {};
  const pos = openPos[ticker] || {};
  const tradeSym = pos.trade_sym || ticker;
  const actualQty = pos.qty || qty;

  if (!confirm(`Sell ${actualQty} shares of ${tradeSym} (${ticker} position)?`)) return;

  try {
    const r = await fetch('/api/arka/manual-close', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: ticker, trade_sym: tradeSym, qty: actualQty})
    });
    const result = await r.json();
    if (result.success) {
      alert('✅ Order placed: Sold ' + actualQty + ' ' + tradeSym);
      // Refresh positions
      if (window._cache) delete window._cache['/api/arka/summary'];
      loadArka();
    } else {
      alert('❌ Order failed: ' + (result.error || 'Unknown error'));
    }
  } catch(e) {
    alert('❌ Request failed: ' + e.message);
  }
}

async function _renderFuturesCards() {
  const pane = $('p-power-lotto');
  if (!pane) return;

  let container = $('futuresCards');
  if (!container) {
    container = document.createElement('div');
    container.id = 'futuresCards';
    container.style.cssText = 'margin-top:16px';
    pane.appendChild(container);
  }

  container.innerHTML = `
    <div style="font-size:10px;font-weight:700;color:var(--sub);letter-spacing:1px;margin-bottom:8px">
      ⚡ FUTURES — INDICATIVE (ETF-DERIVED)
    </div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
      ${['ES','NQ','RTY','YM'].map(s => `<div id="fut-card-${s}" style="background:var(--bg2);border:1px solid var(--border2);border-radius:8px;padding:10px">
        <div style="font-size:10px;font-weight:800;color:var(--purple);font-family:JetBrains Mono,monospace">${s}</div>
        <div class="ld"><div class="spin"></div></div>
      </div>`).join('')}
    </div>`;

  try {
    const data = await getCached('/api/futures/snapshot', 30000);
    for (const fut of (data?.futures || [])) {
      const el = $('fut-card-' + fut.symbol);
      if (!el) continue;
      const up = fut.chg_pct >= 0;
      const col = up ? 'var(--green)' : 'var(--red)';
      el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
          <span style="font-size:11px;font-weight:800;color:var(--purple);font-family:JetBrains Mono,monospace">${fut.symbol}</span>
          <span style="font-size:8px;color:var(--sub)">${fut.name}</span>
        </div>
        <div style="font-size:15px;font-weight:700;font-family:JetBrains Mono,monospace;color:var(--fg)">
          ${fut.price > 10000 ? fut.price.toLocaleString('en-US',{maximumFractionDigits:0}) : fut.price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
        </div>
        <div style="font-size:9px;margin-top:3px;color:${col}">
          ${up?'▲':'▼'} ${Math.abs(fut.chg_pct).toFixed(2)}%
        </div>`;
    }
  } catch(e) {
    console.error('Futures cards error:', e);
  }
}

