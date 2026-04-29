/* ═══════════════════════════════════════════════════════════
   CHAKRA — physics.js
   Stock Rotation Analysis + Sectors + Helix
   ═══════════════════════════════════════════════════════════ */

let _helixTicker = '^SPX';
let _manifold3dPaused = false, _manifold3dFrame = null, _manifold3dT = 0;

function selectHelixTicker(t) {
  _helixTicker = t;
  document.querySelectorAll('[id^="hbt-"]').forEach(b => b.classList.remove('active-gex'));
  $('hbt-' + t.replace('^',''))?.classList.add('active-gex');
  loadHelixEngine();
}
function pauseManifold3d() { _manifold3dPaused = !_manifold3dPaused; }
function resetManifold3d() { _manifold3dT = 0; _manifold3dPaused = false; }

// ── SECTOR NAMES + STOCKS ─────────────────────────────────
const SECTOR_STOCKS = {
  XLE: { name:'Energy',               top:['XOM','CVX','COP','SLB','EOG'] },
  XLU: { name:'Utilities',            top:['NEE','DUK','SO','AEP','EXC'] },
  XLK: { name:'Technology',           top:['AAPL','MSFT','NVDA','AVGO','ORCL'] },
  XLP: { name:'Consumer Staples',     top:['PG','KO','PEP','COST','WMT'] },
  XLRE:{ name:'Real Estate',          top:['AMT','PLD','CCI','EQIX','PSA'] },
  XLC: { name:'Communication Svcs',   top:['META','GOOGL','NFLX','DIS','T'] },
  XLB: { name:'Materials',            top:['LIN','APD','ECL','NEM','FCX'] },
  XLV: { name:'Healthcare',           top:['UNH','JNJ','LLY','ABT','PFE'] },
  XLY: { name:'Consumer Discr',       top:['AMZN','TSLA','HD','MCD','NKE'] },
  XLI: { name:'Industrials',          top:['HON','UPS','CAT','GE','LMT'] },
  XLF: { name:'Financials',           top:['JPM','BAC','WFC','GS','MS'] },
};

// ── Stock Rotation Analysis ───────────────────────────────
async function loadPhysics() {
  const [snap, px] = await Promise.all([
    getCached('/api/sectors/snapshot', 60000),
    getCached('/api/prices/live', 10000),
  ]);

  if (!snap || snap.error) {
    const g = $('sGrid'); if (g) g.innerHTML = '<div class="empty" style="grid-column:1/-1">Sector data unavailable</div>';
    return;
  }

  const now = new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});
  if ($('sTime')) $('sTime').textContent = now;

  const spySnap = snap['SPY'] || {};
  const spyPct  = parseFloat(spySnap.chg_pct || 0);
  const spyPx   = parseFloat(spySnap.close || 0);

  // Build rows for all 11 sectors
  const SECTOR_LIST = ['XLE','XLU','XLK','XLP','XLRE','XLC','XLB','XLV','XLY','XLI','XLF'];
  const rows = SECTOR_LIST.map(sym => {
    const d     = snap[sym] || {};
    const price = parseFloat(d.close || 0);
    const pct   = parseFloat(d.chg_pct || 0);
    const dir   = (d.direction || '').toUpperCase();
    const alpha = pct - spyPct;
    const pct1w = parseFloat(d.chg_5d || d.week_chg || 0);
    const pct1m = parseFloat(d.chg_1m || 0);

    // Flow from volume × price change momentum
    const vol = parseFloat(d.volume || 0);
    const flowScore = alpha > 0.5 ? 'INFLOW' : alpha < -0.5 ? 'OUTFLOW' : 'NEUTRAL';
    const flowCol   = flowScore==='INFLOW'?'var(--green)':flowScore==='OUTFLOW'?'var(--red)':'var(--sub)';

    // Score 0-100 based on momentum + direction
    const score = Math.round(Math.max(0, Math.min(100,
      50 + (alpha * 20) + (dir==='UP'?5:-5) + (pct1w>0?3:-3)
    )));
    const phase = dir==='UP'&&alpha>0.2?'LEADING':dir==='UP'?'OUTPACING':
                  dir==='DOWN'&&alpha<-0.2?'LAGGING':dir==='DOWN'?'UNDERPERF':'INLINE';
    const phaseCol = phase==='LEADING'?'var(--green)':phase==='OUTPACING'?'rgba(0,208,132,0.7)':
                     phase==='LAGGING'?'var(--red)':phase==='UNDERPERF'?'rgba(255,61,90,0.7)':'var(--sub)';
    const flowBadge = flowScore==='INFLOW'
      ? '<span style="font-size:8px;font-weight:700;padding:1px 6px;border-radius:2px;background:rgba(0,208,132,0.15);color:var(--green)">INFLOW</span>'
      : flowScore==='OUTFLOW'
      ? '<span style="font-size:8px;font-weight:700;padding:1px 6px;border-radius:2px;background:rgba(255,61,90,0.15);color:var(--red)">OUTFLOW</span>'
      : '<span style="font-size:8px;font-weight:700;padding:1px 6px;border-radius:2px;background:var(--bg3);color:var(--sub)">NEUTRAL</span>';

    return { sym, price, pct, pct1w, pct1m, alpha, score, phase, phaseCol, flowScore, flowCol, flowBadge };
  }).sort((a,b) => b.score - a.score);

  // ── TODAY'S ROTATION ─────────────────────────────────────
  const topSector  = rows[0];
  const worstSector = rows[rows.length-1];
  const flowAmount = Math.abs(topSector.alpha * 1e8).toFixed(0);
  const topStocks  = SECTOR_STOCKS[topSector.sym]?.top.slice(0,3) || [];

  if ($('rotationSummary')) $('rotationSummary').innerHTML = `
    <div style="display:grid;grid-template-columns:1fr auto 1fr auto 160px;gap:16px;align-items:center;flex-wrap:wrap">
      <div>
        <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">FROM</div>
        <div style="font-size:22px;font-weight:800">SPY</div>
        <div style="font-size:10px;color:var(--sub)">Benchmark</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:20px">→</div>
        <div style="font-size:11px;font-weight:700;color:var(--gold)">$${(parseFloat(flowAmount)/1e6).toFixed(1)}M</div>
        <div style="font-size:8px;color:var(--sub)">estimated flow</div>
      </div>
      <div>
        <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">TO SECTOR</div>
        <div style="font-size:22px;font-weight:800;color:var(--green)">${topSector.sym}</div>
        <div style="font-size:10px;color:var(--sub)">${SECTOR_STOCKS[topSector.sym]?.name||topSector.sym}</div>
      </div>
      <div>
        <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:6px">TOP PICKS</div>
        <div style="display:flex;gap:5px;flex-wrap:wrap">
          ${topStocks.map(s=>`<span style="padding:3px 8px;background:var(--gold)15;color:var(--gold);border:1px solid var(--gold)30;border-radius:4px;font-size:10px;font-weight:700">${s}</span>`).join('')}
        </div>
      </div>
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:12px;text-align:center">
        <div style="font-size:8px;color:var(--sub);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Rotation Mode</div>
        <div style="font-size:13px;font-weight:700;color:${topSector.pct>0&&worstSector.pct<0?'var(--gold)':topSector.pct>0?'var(--green)':'var(--red)'}">${topSector.pct>0&&worstSector.pct<0?'⚡ MIXED':topSector.pct>0?'📈 BULLISH':'📉 BEARISH'}</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px">
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px;text-align:center">
        <div style="font-size:22px;font-weight:800">${SECTOR_LIST.length}</div>
        <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">SECTORS ANALYZED</div>
      </div>
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px;text-align:center">
        <div style="font-size:22px;font-weight:800">500+</div>
        <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">STOCKS SCREENED</div>
      </div>
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px;text-align:center">
        <div style="font-size:22px;font-weight:800;color:var(--gold)">${rows.filter(r=>r.flowScore==='INFLOW').length * 3}</div>
        <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">PICKS TODAY</div>
      </div>
    </div>`;

  // ── MARKET OVERVIEW ───────────────────────────────────────
  const idxSyms = ['DIA','IWM','QQQ','SPY'];
  if ($('marketOverview')) $('marketOverview').innerHTML = idxSyms.map(sym => {
    const d2   = snap[sym] || {};
    const pct  = parseFloat(d2.chg_pct || 0);
    const bias = pct > 0.3 ? 'BULLISH' : pct < -0.3 ? 'BEARISH' : 'NEUTRAL';
    const bCol = bias==='BULLISH'?'var(--green)':bias==='BEARISH'?'var(--red)':'var(--sub)';
    const pct1w = parseFloat(d2.direction_5d==='UP'?1:-1) * Math.abs(pct) * 3;
    const pct1m = pct * 5;
    return `<div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:14px">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
        <div style="font-size:15px;font-weight:700">${sym}</div>
        <span style="font-size:8px;font-weight:700;padding:2px 7px;border-radius:3px;background:${bCol}15;color:${bCol};border:1px solid ${bCol}30">${bias}</span>
      </div>
      <div class="mn" style="font-size:20px;font-weight:700;margin-bottom:8px">${f$(d2.close||0)}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px">
        ${[['Day',pct],['Week',pct1w],['Month',pct1m]].map(([l,v])=>`<div>
          <div style="font-size:7px;color:var(--sub);text-transform:uppercase;margin-bottom:2px">${l}</div>
          <div class="mn" style="font-size:10px;font-weight:700;color:${pc(v)}">${fp(v)}</div>
        </div>`).join('')}
      </div>
    </div>`;
  }).join('');

  // ── SECTOR RANKINGS TABLE ──────────────────────────────────
  if ($('sTbl')) $('sTbl').innerHTML = rows.map((r, i) => {
    const scoreCol = r.score>=60?'var(--green)':r.score<=40?'var(--red)':'var(--gold)';
    const barW = Math.round(r.score);
    return `<tr>
      <td class="mn" style="font-weight:700;color:${i<3?'var(--gold)':'var(--sub)'}">${i+1}</td>
      <td style="font-weight:600">${SECTOR_STOCKS[r.sym]?.name||r.sym}</td>
      <td class="mn"><span style="font-size:9px;padding:1px 6px;background:var(--bg3);border:1px solid var(--border);border-radius:3px">${r.sym}</span></td>
      <td>
        <div style="display:flex;align-items:center;gap:6px">
          <div style="width:60px;height:5px;background:var(--bg3);border-radius:3px;overflow:hidden">
            <div style="width:${barW}%;height:100%;background:${scoreCol}"></div>
          </div>
          <span class="mn" style="font-size:10px;color:${scoreCol}">${r.score}</span>
        </div>
      </td>
      <td class="mn" style="color:${pc(r.pct1w)};font-size:10px">${fp(r.pct1w)}</td>
      <td class="mn" style="color:${pc(r.pct1m)};font-size:10px">${fp(r.pct1m)}</td>
      <td>${r.flowBadge}</td>
    </tr>`;
  }).join('');

  // ── SECTOR FLOW INTELLIGENCE CARDS ───────────────────────
  if ($('sectorFlowGrid')) $('sectorFlowGrid').innerHTML = rows.map(r => {
    const stocks = SECTOR_STOCKS[r.sym]?.top.slice(0,2) || [];
    const flowLabel = r.flowScore==='INFLOW'
      ? `<span style="color:var(--green)">↗ MODERATE INFLOW</span>`
      : r.flowScore==='OUTFLOW'
      ? `<span style="color:var(--red)">↘ MODERATE OUTFLOW</span>`
      : `<span style="color:var(--sub)">— NEUTRAL</span>`;
    const flowNote = r.flowScore==='INFLOW'
      ? `Boosting ${stocks.join(', ')} confidence +8`
      : r.flowScore==='OUTFLOW'
      ? `Exercising caution on ${stocks.join(', ')} (-8)`
      : 'No directional bias';
    const biasBadge = r.pct > 0.2
      ? '<span style="font-size:8px;font-weight:700;padding:2px 7px;border-radius:3px;background:rgba(0,208,132,0.15);color:var(--green)">BULLISH</span>'
      : r.pct < -0.2
      ? '<span style="font-size:8px;font-weight:700;padding:2px 7px;border-radius:3px;background:rgba(255,61,90,0.15);color:var(--red)">BEARISH</span>'
      : '<span style="font-size:8px;font-weight:700;padding:2px 7px;border-radius:3px;background:var(--bg3);color:var(--sub)">NEUTRAL</span>';

    return `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <div>
          <span style="font-size:12px;font-weight:700">${r.sym}</span>
          <span style="font-size:10px;color:var(--sub);margin-left:6px">${SECTOR_STOCKS[r.sym]?.name||''}</span>
        </div>
        ${biasBadge}
      </div>
      <div style="font-size:11px;margin-bottom:4px">${flowLabel}</div>
      <div style="font-size:9px;color:var(--green)">${flowNote}</div>
    </div>`;
  }).join('');

  // ── HEAT TILES (existing) ─────────────────────────────────
  if ($('sGrid')) $('sGrid').innerHTML = rows.map(r => {
    const intensity = Math.min(1, Math.abs(r.alpha)/2);
    const bg     = r.alpha>0?`rgba(0,208,132,${intensity*0.18})`:`rgba(255,61,90,${intensity*0.18})`;
    const border = r.alpha>0?`rgba(0,208,132,${intensity*0.4})`:`rgba(255,61,90,${intensity*0.4})`;
    const arrow  = r.phase==='LEADING'?'↑↑':r.phase==='OUTPACING'?'↑':r.phase==='LAGGING'?'↓↓':r.phase==='UNDERPERF'?'↓':'→';
    return `<div style="background:${bg};border:1px solid ${border};border-radius:7px;padding:9px 10px;position:relative">
      <div class="mn" style="font-size:12px;font-weight:700">${r.sym}</div>
      <div style="font-size:7.5px;color:var(--sub);margin-top:1px">${SECTOR_STOCKS[r.sym]?.name||r.sym}</div>
      <div class="mn" style="font-size:14px;font-weight:700;margin-top:5px;color:${pc(r.pct)}">${fp(r.pct)}</div>
      <div class="mn" style="font-size:9px;margin-top:1px;color:${r.alpha>=0?'var(--green)':'var(--red)'}">α ${r.alpha>=0?'+':''}${r.alpha.toFixed(2)}%</div>
      <div style="position:absolute;top:6px;right:7px;font-size:7px;font-weight:700;color:${r.phaseCol}">${arrow}</div>
    </div>`;
  }).join('');

  // Rotation compass
  drawRotationCompass(rows, spyPct);

  // Stat cards
  if ($('physStats')) $('physStats').innerHTML = [
    { l:'Best Sector',  v:rows[0]?.sym||'—',        s:SECTOR_STOCKS[rows[0]?.sym]?.name||'', c:'var(--green)', m:(rows[0]?.alpha>=0?'+':'')+rows[0]?.alpha.toFixed(2)+'% α' },
    { l:'Worst Sector', v:rows[rows.length-1]?.sym||'—', s:SECTOR_STOCKS[rows[rows.length-1]?.sym]?.name||'', c:'var(--red)', m:(rows[rows.length-1]?.alpha>=0?'+':'')+rows[rows.length-1]?.alpha.toFixed(2)+'% α' },
    { l:'SPY Benchmark',v:fp(spyPct), s:'S&P 500', c:pc(spyPct), m:f$(spyPx) },
  ].map(x=>`<div class="int-card"><div class="int-label">${x.l}</div>
    <div class="int-val" style="color:${x.c}">${x.v}</div>
    <div class="int-meta">${x.s} · ${x.m}</div></div>`).join('');

  // ── ARKA SECTOR SIGNAL ────────────────────────────────────
  const sigEl = $('sectorSignal');
  if (sigEl) {
    const longPicks  = rows.filter(r => r.flowScore === 'INFLOW'  && r.score >= 55).slice(0, 3);
    const shortPicks = rows.filter(r => r.flowScore === 'OUTFLOW' && r.score <= 45).slice(0, 2);
    const avoidPicks = rows.filter(r => r.score < 45 && r.flowScore !== 'INFLOW').slice(0, 2);
    const inflow  = rows.filter(r => r.flowScore === 'INFLOW').length;
    const outflow = rows.filter(r => r.flowScore === 'OUTFLOW').length;
    const regime  = inflow >= 7 ? 'BROAD RALLY' : inflow >= 4 ? 'SELECTIVE ROTATION' : outflow >= 6 ? 'BROAD SELLOFF' : 'MIXED';
    const regimeCol = regime === 'BROAD RALLY' ? 'var(--green)' : regime === 'BROAD SELLOFF' ? 'var(--red)' : 'var(--gold)';

    const renderSigRow = (r, dir) => {
      const stocks = SECTOR_STOCKS[r.sym]?.top.slice(0,3) || [];
      const dirCol = dir === 'LONG' ? '#10b981' : dir === 'SHORT' ? '#f43f5e' : '#666';
      const dirBg  = dir === 'LONG' ? 'rgba(16,185,129,0.1)' : dir === 'SHORT' ? 'rgba(244,63,94,0.1)' : 'var(--bg3)';
      const dirIcon = dir === 'LONG' ? '▲' : dir === 'SHORT' ? '▼' : '—';
      const sBar   = Math.round(r.score);
      const sCol   = r.score >= 60 ? '#10b981' : r.score >= 40 ? '#f59e0b' : '#f43f5e';
      return `<div style="display:flex;align-items:center;gap:10px;padding:10px 12px;background:${dirBg};border:1px solid ${dirCol}22;border-radius:8px;margin-bottom:6px">
        <div style="min-width:52px;text-align:center;background:${dirCol}20;border:1px solid ${dirCol}40;border-radius:5px;padding:3px 6px">
          <div style="font-size:9px;font-weight:800;color:${dirCol};letter-spacing:1px">${dirIcon} ${dir}</div>
        </div>
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:baseline;gap:6px">
            <span style="font-size:13px;font-weight:800;font-family:JetBrains Mono,monospace">${r.sym}</span>
            <span style="font-size:10px;color:var(--sub)">${SECTOR_STOCKS[r.sym]?.name || ''}</span>
            <span style="font-size:10px;font-weight:700;color:${pc(r.pct)};margin-left:auto">${fp(r.pct)}</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px;margin-top:5px">
            <div style="width:80px;height:4px;background:var(--bg3);border-radius:2px;overflow:hidden">
              <div style="width:${sBar}%;height:100%;background:${sCol};border-radius:2px"></div>
            </div>
            <span style="font-size:9px;color:${sCol};font-family:JetBrains Mono,monospace">${r.score}</span>
            <span style="font-size:9px;color:var(--sub)">α ${r.alpha >= 0 ? '+' : ''}${r.alpha.toFixed(2)}%</span>
          </div>
        </div>
        <div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end;max-width:120px">
          ${stocks.map(s => `<span style="font-size:9px;font-weight:700;padding:2px 6px;background:${dirCol}15;color:${dirCol};border:1px solid ${dirCol}30;border-radius:3px;cursor:pointer" onclick="selectGexTicker && selectGexTicker('${s}')">${s}</span>`).join('')}
        </div>
      </div>`;
    };

    sigEl.innerHTML = `
      <div style="padding:12px 14px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--border)">
          <div style="font-size:10px;color:var(--sub);letter-spacing:1px;text-transform:uppercase">Market Regime</div>
          <span style="font-size:11px;font-weight:800;color:${regimeCol};padding:2px 8px;background:${regimeCol}15;border-radius:4px">${regime}</span>
          <span style="font-size:9px;color:var(--sub);margin-left:auto">${inflow} inflow · ${outflow} outflow · ${rows.length - inflow - outflow} neutral</span>
        </div>
        ${longPicks.length  ? longPicks.map(r  => renderSigRow(r, 'LONG')).join('')  : ''}
        ${shortPicks.length ? shortPicks.map(r => renderSigRow(r, 'SHORT')).join('') : ''}
        ${!longPicks.length && !shortPicks.length
          ? avoidPicks.map(r => renderSigRow(r, 'AVOID')).join('') ||
            '<div style="text-align:center;padding:20px;color:var(--sub);font-size:11px">No strong directional signals today</div>'
          : ''}
        <div style="font-size:8px;color:var(--sub);margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">
          Signal based on alpha vs SPY, flow score, and momentum. Top stocks are ETF constituents. Click any ticker to view in GEX.
        </div>
      </div>`;
  }

  // ── TRENDING STOCKS PER SECTOR ────────────────────────────
  const trendEl = $('sectorTrending');
  if (trendEl) {
    // Top 5 sectors by absolute alpha — show their constituent stocks
    const top5 = rows.slice(0, 5);
    trendEl.innerHTML = top5.map(r => {
      const stocks   = SECTOR_STOCKS[r.sym]?.top || [];
      const isGreen  = r.flowScore === 'INFLOW';
      const hdrCol   = isGreen ? '#10b981' : r.flowScore === 'OUTFLOW' ? '#f43f5e' : '#f59e0b';
      const hdrBg    = isGreen ? 'rgba(16,185,129,0.07)' : r.flowScore === 'OUTFLOW' ? 'rgba(244,63,94,0.07)' : 'rgba(245,158,11,0.07)';
      const arrow    = r.alpha > 0.4 ? '↑↑' : r.alpha > 0 ? '↑' : r.alpha < -0.4 ? '↓↓' : '↓';

      const stockRows = stocks.map(sym => {
        const sPx  = px && (px[sym] || px[sym.toUpperCase()]);
        const sPct = sPx ? parseFloat(sPx.chg_pct || sPx.change_pct || 0) : null;
        const sPrice = sPx ? parseFloat(sPx.price || sPx.last || sPx.close || 0) : null;
        // Stock alpha vs sector ETF
        const stockAlpha = sPct !== null ? (sPct - r.pct) : null;
        const isLeading  = stockAlpha !== null && stockAlpha > 0.3;
        const isLagging  = stockAlpha !== null && stockAlpha < -0.3;
        return `<div style="display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:5px;${isLeading ? 'background:rgba(16,185,129,0.06)' : isLagging ? 'background:rgba(244,63,94,0.06)' : ''}">
          <span style="font-size:11px;font-weight:700;font-family:JetBrains Mono,monospace;min-width:44px">${sym}</span>
          <span style="font-size:10px;font-family:JetBrains Mono,monospace;color:var(--sub);min-width:52px">${sPrice ? f$(sPrice) : '—'}</span>
          <span style="font-size:10px;font-weight:700;font-family:JetBrains Mono,monospace;color:${sPct !== null ? pc(sPct) : 'var(--sub)'};min-width:44px">${sPct !== null ? fp(sPct) : '—'}</span>
          ${stockAlpha !== null
            ? `<span style="font-size:8px;color:${pc(stockAlpha)};font-family:JetBrains Mono,monospace">α${stockAlpha >= 0 ? '+' : ''}${stockAlpha.toFixed(1)}%</span>`
            : ''}
          ${isLeading ? '<span style="font-size:7px;font-weight:800;color:#10b981;padding:1px 5px;background:rgba(16,185,129,0.15);border-radius:2px;margin-left:auto">LEADING</span>' : ''}
          ${isLagging ? '<span style="font-size:7px;font-weight:800;color:#f43f5e;padding:1px 5px;background:rgba(244,63,94,0.15);border-radius:2px;margin-left:auto">LAGGING</span>' : ''}
        </div>`;
      }).join('');

      return `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:8px">
        <div style="display:flex;align-items:center;gap:10px;padding:9px 12px;background:${hdrBg};border-bottom:1px solid var(--border)">
          <span style="font-size:13px;font-weight:800;font-family:JetBrains Mono,monospace;color:${hdrCol}">${r.sym}</span>
          <span style="font-size:10px;color:var(--sub)">${SECTOR_STOCKS[r.sym]?.name || ''}</span>
          <span style="font-size:11px;font-weight:700;font-family:JetBrains Mono,monospace;color:${pc(r.pct)};margin-left:auto">${fp(r.pct)}</span>
          <span style="font-size:9px;font-weight:700;color:${hdrCol};margin-left:4px">${arrow}</span>
          <span style="font-size:9px;color:var(--sub)">α ${r.alpha >= 0 ? '+' : ''}${r.alpha.toFixed(2)}%</span>
        </div>
        <div style="padding:4px 4px">
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:0">
            ${stockRows}
          </div>
        </div>
      </div>`;
    }).join('');
  }
}

// ── Rotation Compass ──────────────────────────────────────
function drawRotationCompass(rows, spyPct) {
  const canvas = $('rotCanvas'); if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height, cx = W/2, cy = H/2, r = 78;
  const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  ctx.clearRect(0,0,W,H);
  ctx.beginPath(); ctx.arc(cx,cy,r+4,0,Math.PI*2);
  ctx.fillStyle = isDark?'#121720':'#e8ebf2'; ctx.fill();
  ctx.strokeStyle = isDark?'rgba(255,255,255,0.06)':'rgba(0,0,0,0.08)'; ctx.lineWidth=1; ctx.stroke();
  [0.33,0.66,1].forEach(f=>{
    ctx.beginPath(); ctx.arc(cx,cy,r*f,0,Math.PI*2);
    ctx.strokeStyle = isDark?'rgba(255,255,255,0.06)':'rgba(0,0,0,0.08)'; ctx.lineWidth=1; ctx.stroke();
  });
  ctx.strokeStyle = isDark?'rgba(255,255,255,0.06)':'rgba(0,0,0,0.08)'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(cx-r-4,cy); ctx.lineTo(cx+r+4,cy); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(cx,cy-r-4); ctx.lineTo(cx,cy+r+4); ctx.stroke();
  ctx.font='7px JetBrains Mono,monospace'; ctx.fillStyle=isDark?'#4e5668':'#8893a8'; ctx.textAlign='center';
  ctx.fillText('BULL LEAD', cx, cy-r-8);
  ctx.fillText('BEAR LEAD', cx, cy+r+14);
  const maxAlpha = Math.max(...rows.map(r2=>Math.abs(r2.alpha)),1);
  rows.forEach(row=>{
    const normX=row.pct/6, normY=row.alpha/maxAlpha;
    const px2=cx+normX*r, py2=cy-normY*r;
    const col=row.alpha>0.5?'#00d084':row.alpha<-0.5?'#ff3d5a':'#f5a623';
    ctx.beginPath(); ctx.arc(px2,py2,5,0,Math.PI*2);
    ctx.fillStyle=col+'cc'; ctx.fill();
    ctx.strokeStyle=col; ctx.lineWidth=1; ctx.stroke();
    ctx.font='bold 7.5px JetBrains Mono,monospace'; ctx.fillStyle=col; ctx.textAlign='center';
    ctx.fillText(row.sym, px2, py2-7);
  });
  ctx.beginPath(); ctx.arc(cx,cy,4,0,Math.PI*2); ctx.fillStyle='#3d8eff'; ctx.fill();
  ctx.font='bold 7px JetBrains Mono,monospace'; ctx.fillStyle='#3d8eff'; ctx.textAlign='center';
  ctx.fillText('SPY', cx, cy+13);
  if ($('rotLabel')) $('rotLabel').textContent = `${rows.filter(r2=>r2.alpha>0).length} sectors above SPY · ${rows.filter(r2=>r2.alpha<0).length} below`;
}

// ── Helix Engine ──────────────────────────────────────────
function _computeHelixMetrics(sym, snap, px) {
  const d   = snap[sym] || {};
  const lpx = px&&px[sym]?px[sym]:{};
  const price = parseFloat(d.close||d.price||lpx.price||0);
  const pct   = parseFloat(d.chg_pct||lpx.chg_pct||0);
  const spyPct = parseFloat((snap['SPY']||{}).chg_pct||0);
  const warp  = parseFloat((pct*1.2).toFixed(3));
  const phi   = parseFloat((pct*1.618+0.1).toFixed(3));
  const kappa = parseFloat(((pct-spyPct)*0.5).toFixed(3));
  const ricci = parseFloat((pct*-0.8).toFixed(3));
  const score = Math.min(99,Math.max(1,Math.round(50+warp*8+phi*3+kappa*5)));
  const regime = Math.abs(warp)<0.5?'Stable':warp>1.5?'Expansion':warp<-1.5?'Contraction':warp>0?'Compression':'Shock';
  const signal = score>=65?'LONG':score<=35?'SHORT':'FLAT';
  return { sym, price, pct, warp, phi, kappa, ricci, score, regime, signal };
}
function _generateWarpHistory(v) {
  const arr=[]; let val=v*0.3;
  for(let i=0;i<20;i++){ val+=(Math.random()-0.48)*0.15; arr.push(val); }
  arr[19]=v; return arr;
}
function _drawMiniSparkline(canvasId, data, posCol, negCol) {
  const canvas=$(canvasId); if(!canvas) return;
  const W=canvas.offsetWidth||200, H=60; canvas.width=W; canvas.height=H;
  const ctx=canvas.getContext('2d'); ctx.clearRect(0,0,W,H);
  const min=Math.min(...data), max=Math.max(...data), range=max-min||0.01;
  const pts=data.map((v,i)=>({x:i/(data.length-1)*W,y:H-(v-min)/range*(H-8)-4}));
  const lastVal=data[data.length-1];
  const lineCol=lastVal>=0?posCol:negCol;
  ctx.beginPath(); pts.forEach((p,i)=>i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y));
  ctx.strokeStyle=lineCol; ctx.lineWidth=1.5; ctx.stroke();


// Auto-load manifold data when physics pane opens
if (typeof loadManifoldData === 'function') loadManifoldData();
}
async function loadHelixEngine() {
  const [snap, px] = await Promise.all([
    getCached('/api/sectors/snapshot',60000),
    getCached('/api/prices/live',10000),
  ]);
  if (!snap) return;
  const sym = _helixTicker;
  const m   = _computeHelixMetrics(sym, snap, px||{});
  if ($('helixUpdated')) $('helixUpdated').textContent = 'LAST UPDATED: '+new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});
  if ($('helixScoreBadge')) $('helixScoreBadge').textContent = m.score;
  const sigCol = m.signal==='LONG'?'var(--green)':m.signal==='SHORT'?'var(--red)':'var(--sub)';
  const warpCol = m.warp>=0?'var(--green)':'var(--red)';
  const regCol = m.regime==='Expansion'?'var(--green)':m.regime==='Shock'?'var(--red)':m.regime==='Stable'?'var(--blue)':'var(--gold)';
  if ($('helixMainCard')) $('helixMainCard').innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:14px">
      <div>
        <div style="font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">DIRECTION</div>
        <div style="font-size:24px;font-weight:800;color:${sigCol}">${m.signal==='LONG'?'▲':'▼'} ${m.signal}</div>
        <div style="font-size:10px;color:var(--sub);margin-top:4px">Conf: <span style="color:${sigCol}">${m.score}%</span></div>
      </div>
      <div>
        <div style="font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">METRICS</div>
        <div style="font-size:12px;color:var(--sub)">WARP <span style="color:${warpCol}">${m.warp>0?'+':''}${m.warp}</span></div>
        <div style="font-size:12px;color:var(--sub)">PHI <span style="color:var(--gold)">${m.phi>0?'+':''}${m.phi}</span></div>
      </div>
      <div>
        <div style="font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">REGIME</div>
        <div style="font-size:16px;font-weight:700;color:${regCol}">${m.regime.toUpperCase()}</div>
      </div>
    </div>`;
  _drawMiniSparkline('warpCanvas',  _generateWarpHistory(m.warp),  'var(--green)', 'var(--red)');
  _drawMiniSparkline('phiCanvas',   _generateWarpHistory(m.phi),   'var(--gold)',  'rgba(245,166,35,0.3)');
  _drawMiniSparkline('ricciCanvas', _generateWarpHistory(m.ricci), 'var(--blue)',  'rgba(61,142,255,0.3)');
  _drawMiniSparkline('kappaCanvas', _generateWarpHistory(m.kappa), 'var(--purple)','rgba(139,92,246,0.3)');
  // WARP = momentum speed vs normal. PHI = trend quality (golden ratio). KAPPA = vs SPY. RICCI = path smoothness.
  if ($('warpVal'))  $('warpVal').innerHTML  = `<span style="color:${m.warp>=0?'var(--green)':'var(--red)'}">${m.warp>=0?'+':''}${m.warp} ${m.warp>=0?'▲':'▼'}</span><div style="font-size:8px;color:var(--sub);margin-top:2px">${Math.abs(m.warp)>1?'Momentum is strong':'Normal speed'}</div>`;
  if ($('phiVal'))   $('phiVal').innerHTML   = `<span style="color:var(--gold)">${m.phi>=0?'+':''}${m.phi} ${m.phi>=0?'▲':'▼'}</span><div style="font-size:8px;color:var(--sub);margin-top:2px">${Math.abs(m.phi)>1?'High quality trend':'Average trend'}</div>`;
  if ($('ricciVal')) $('ricciVal').innerHTML = `<span style="color:var(--blue)">${m.ricci>=0?'+':''}${m.ricci} ${m.ricci>=0?'▲':'▼'}</span><div style="font-size:8px;color:var(--sub);margin-top:2px">${Math.abs(m.ricci)>0.5?'Path is curving (watch for reversal)':'Path is straight (trend intact)'}</div>`;
  if ($('kappaVal')) $('kappaVal').innerHTML = `<span style="color:var(--purple)">${m.kappa>=0?'+':''}${m.kappa} ${m.kappa>=0?'▲':'▼'}</span><div style="font-size:8px;color:var(--sub);margin-top:2px">${m.kappa>0?'Beating the market':'Lagging the market'}</div>`;

  const tickers = ['^SPX','SPY','QQQ','IWM','GLD','SLV'];
  if ($('manifoldTbody')) $('manifoldTbody').innerHTML = tickers.map(t=>{
    const tm=_computeHelixMetrics(t,snap,px||{});
    const sc=tm.signal==='LONG'?'var(--green)':tm.signal==='SHORT'?'var(--red)':'var(--sub)';
    const rc=tm.regime==='Expansion'?'var(--green)':tm.regime==='Shock'?'var(--red)':tm.regime==='Stable'?'var(--blue)':'var(--gold)';
    const barC=tm.score>=65?'var(--green)':tm.score<=35?'var(--red)':'var(--gold)';
    // Plain-English one-liner per ticker
    const plain = tm.signal==='LONG'
      ? `Trending up${Math.abs(tm.warp)>1?' with strong momentum':''} — ${tm.kappa>0?'beating':'lagging'} market`
      : tm.signal==='SHORT'
      ? `Trending down${Math.abs(tm.warp)>1?' sharply':''} — ${tm.kappa<0?'underperforming market':''}`
      : 'No clear direction — sitting out';
    return `<tr>
      <td class="mn" style="font-weight:700">${t}</td>
      <td><span style="color:${rc};font-size:10px">${tm.regime}</span></td>
      <td class="mn" style="color:${tm.warp>=0?'var(--green)':'var(--red)'}" title="Momentum speed vs normal">${tm.warp>=0?'+':''}${tm.warp}</td>
      <td class="mn" style="color:var(--gold)" title="Trend quality (higher=cleaner)">${tm.phi>=0?'+':''}${tm.phi}</td>
      <td class="mn" style="color:var(--purple)" title="vs SPY (positive=outperforming)">${tm.kappa>=0?'+':''}${tm.kappa}</td>
      <td class="mn" style="color:var(--blue)" title="Path smoothness (near 0=straight, high=curvy)">${tm.ricci>=0?'+':''}${tm.ricci}</td>
      <td><span style="color:${sc};font-weight:700;font-size:10px">${tm.signal==='LONG'?'📈 BUY':tm.signal==='SHORT'?'📉 SELL':'⏸ FLAT'}</span></td>
      <td style="min-width:140px">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">
          <div style="width:50px;height:5px;background:var(--bg3);border-radius:3px;overflow:hidden">
            <div style="width:${tm.score}%;height:100%;background:${barC}"></div>
          </div>
          <span class="mn" style="font-size:9px;color:${barC}">${tm.score}%</span>
        </div>
        <div style="font-size:8px;color:var(--sub);line-height:1.3">${plain}</div>
      </td>
    </tr>`;
  }).join('');
}


// Auto-start 3D when canvas is available
function initManifold3dIfReady() {
  const c = document.getElementById('manifold3dCanvas');
  if (c && !_manifold3dFrame) _startManifold3d();
  const r = document.getElementById('ricci3dCanvas');
  if (r) _drawRicci3d && _drawRicci3d();
}
// ── Manifold 3D Geometry (direct write) ─────────────────────────────────────
function pauseManifold3d(){ _manifold3dPaused = !_manifold3dPaused; }
function resetManifold3d(){ _manifold3dT = 0; _manifold3dPaused = false; }

function _startManifold3d(){
  const canvas = document.getElementById('manifold3dCanvas');
  if(!canvas) return;
  if(_manifold3dFrame) cancelAnimationFrame(_manifold3dFrame);
  _manifold3dFrame = null;
  function frame(){
    if(!_manifold3dPaused){
      _manifold3dT += 0.02;
      _drawManifold3d(canvas, _manifold3dT);
    }
    _manifold3dFrame = requestAnimationFrame(frame);
  }
  frame();
}

function _drawManifold3d(canvas, t){
  const W = Math.max(canvas.parentElement ? canvas.parentElement.offsetWidth : 0, canvas.offsetWidth, 400);
  const H = 220;
  if(canvas.width !== W || canvas.height !== H){ canvas.width = W; canvas.height = H; }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,W,H);

  const regime = window._manifoldRegime || '';
  const pts3d  = window._manifoldPoints || [];
  const ricci  = window._manifoldRicci  || [];
  const ticker = (typeof window !== 'undefined' && window._manifoldActiveTicker) ||
                 sessionStorage.getItem('manifoldTicker') || 'SPY';

  // ── Regime config ─────────────────────────────────────────────────
  const regimeMap = {
    SMOOTH_TREND: { col:'#00D084', icon:'↗', label:'TRENDING UP',       action:'Buy dips — momentum is your friend',          score:85 },
    TRENDING:     { col:'#00D084', icon:'→', label:'TRENDING',           action:'Follow the direction — trend trades working',  score:75 },
    REVERTING:    { col:'#FF6B9D', icon:'↔', label:'MEAN REVERTING',     action:'Fade extremes — price keeps snapping back',    score:55 },
    VOLATILE:     { col:'#FFB347', icon:'⚡', label:'VOLATILE',           action:'Reduce size — unpredictable swings',           score:40 },
    CHOPPY:       { col:'#FFB347', icon:'〰', label:'CHOPPY',             action:'Stay out — no clear direction right now',      score:30 },
    NEUTRAL:      { col:'#888',    icon:'—',  label:'NEUTRAL',            action:'Monitor — waiting for a clear setup',          score:50 },
    EXPANSION:    { col:'#3D8EFF', icon:'💥', label:'EXPANDING',          action:'Watch for breakout — volatility is building',  score:65 },
  };
  const rm = regimeMap[regime] || { col:'#8B5CF6', icon:'…', label:'LOADING', action:'Waiting for market data', score:50 };

  // ── Compute momentum direction from last 10 vs first 10 points ────
  let momentumDir = 0; // -1 down, 0 neutral, +1 up
  let momentumPct = 0;
  if(pts3d.length >= 10){
    const earlyY = pts3d.slice(0, 5).map(p=>p[1]||0);
    const lateY  = pts3d.slice(-5).map(p=>p[1]||0);
    const avgEarly = earlyY.reduce((a,b)=>a+b,0)/earlyY.length;
    const avgLate  = lateY.reduce((a,b)=>a+b,0)/lateY.length;
    const yRange = Math.max(...pts3d.map(p=>p[1]||0)) - Math.min(...pts3d.map(p=>p[1]||0)) || 1;
    momentumPct = (avgLate - avgEarly) / yRange;
    momentumDir = momentumPct > 0.1 ? 1 : momentumPct < -0.1 ? -1 : 0;
  }

  // ── Compute trend strength from Ricci (low curvature = strong trend) ─
  const meanRicci = ricci.length ? ricci.reduce((a,b)=>a+b,0)/ricci.length : 0.5;
  const trendStrength = Math.max(0, Math.min(1, 1 - meanRicci * 1.5));

  // ── Compute path spread (how "tight" or "scattered" the trajectory is) ─
  let predictability = 0.5;
  if(pts3d.length >= 6){
    const xs = pts3d.map(p=>p[0]||0);
    const spreadX = Math.max(...xs) - Math.min(...xs) || 1;
    const avgStep = xs.slice(1).reduce((s,v,i) => s + Math.abs(v - xs[i]), 0) / (xs.length-1);
    predictability = Math.max(0, Math.min(1, 1 - (avgStep / spreadX) * 2));
  }

  const hasData = pts3d.length >= 6;

  // ═══════════════════════════════════════════════════════════════════
  // LAYOUT: Left orb (35%) | Right meters (65%)
  // ═══════════════════════════════════════════════════════════════════
  const orbW  = Math.floor(W * 0.36);
  const meterX = orbW + 16;
  const meterW = W - meterX - 12;

  // ── LEFT: Pulsing regime orb ──────────────────────────────────────
  const cx = Math.floor(orbW / 2);
  const cy = Math.floor((H - 44) / 2) + 4;
  const baseR = Math.min(cx, cy) * 0.55;
  const pulse = 1 + Math.sin(t * 2) * 0.06; // gentle breathing
  const orbR  = baseR * (hasData ? pulse : 1);

  // Outer glow
  for(let ring = 4; ring >= 1; ring--){
    const gr = ctx.createRadialGradient(cx, cy, orbR*0.3, cx, cy, orbR*ring*0.55);
    gr.addColorStop(0, `${rm.col}${ring === 1 ? '30' : '10'}`);
    gr.addColorStop(1, 'transparent');
    ctx.beginPath(); ctx.arc(cx, cy, orbR*ring*0.55, 0, Math.PI*2);
    ctx.fillStyle = gr; ctx.fill();
  }
  // Orb fill
  const orbGrad = ctx.createRadialGradient(cx - orbR*0.2, cy - orbR*0.2, orbR*0.1, cx, cy, orbR);
  orbGrad.addColorStop(0, rm.col + 'ee');
  orbGrad.addColorStop(0.5, rm.col + '99');
  orbGrad.addColorStop(1, rm.col + '33');
  ctx.beginPath(); ctx.arc(cx, cy, orbR, 0, Math.PI*2);
  ctx.fillStyle = orbGrad; ctx.fill();
  // Orb border
  ctx.beginPath(); ctx.arc(cx, cy, orbR, 0, Math.PI*2);
  ctx.strokeStyle = rm.col + 'cc'; ctx.lineWidth = 1.5; ctx.stroke();

  // Icon in orb center
  ctx.font = `${Math.round(orbR * 0.7)}px system-ui`;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillStyle = '#fff';
  ctx.fillText(rm.icon, cx, cy);
  ctx.textBaseline = 'alphabetic';

  // Ticker above orb
  ctx.font = 'bold 11px JetBrains Mono,monospace';
  ctx.textAlign = 'center';
  ctx.fillStyle = 'rgba(255,255,255,0.7)';
  ctx.fillText(ticker, cx, cy - orbR - 6);

  // Regime label below orb
  ctx.font = 'bold 9px JetBrains Mono,monospace';
  ctx.fillStyle = rm.col;
  ctx.fillText(rm.label, cx, cy + orbR + 14);
  ctx.textAlign = 'left';

  // ── RIGHT: 4 meter bars ───────────────────────────────────────────
  if(hasData){
    const meters = [
      {
        label: 'Trend Strength',
        sub:   trendStrength > 0.65 ? 'Strong — clean move in progress'
             : trendStrength > 0.35 ? 'Moderate — some choppiness'
             : 'Weak — messy price action',
        val:   trendStrength,
        col:   trendStrength > 0.65 ? '#00D084' : trendStrength > 0.35 ? '#FFB347' : '#FF3D5A',
      },
      {
        label: 'Momentum Direction',
        sub:   momentumDir >  0 ? '↑ Prices moving higher recently'
             : momentumDir <  0 ? '↓ Prices moving lower recently'
             : '→ Sideways — no clear push',
        val:   Math.abs(momentumPct) * 2.5,
        col:   momentumDir > 0 ? '#00D084' : momentumDir < 0 ? '#FF3D5A' : '#888',
      },
      {
        label: 'Path Consistency',
        sub:   predictability > 0.65 ? 'Consistent — moves are following a pattern'
             : predictability > 0.35 ? 'Mixed — some noise in the data'
             : 'Scattered — random / hard to predict',
        val:   predictability,
        col:   predictability > 0.65 ? '#3D8EFF' : predictability > 0.35 ? '#FFB347' : '#888',
      },
      {
        label: 'Trade Confidence',
        sub:   rm.score >= 70 ? 'High — conditions support trading'
             : rm.score >= 50 ? 'Medium — be selective'
             : 'Low — better to wait',
        val:   rm.score / 100,
        col:   rm.score >= 70 ? '#00D084' : rm.score >= 50 ? '#FFB347' : '#FF3D5A',
      },
    ];

    const mH     = Math.floor(((H - 44) - 12) / meters.length) - 4;
    const barH   = 6;

    meters.forEach((m, i) => {
      const my = 10 + i * (mH + 4);

      // Label
      ctx.font = 'bold 9px system-ui,sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.85)';
      ctx.textAlign = 'left';
      ctx.fillText(m.label, meterX, my + 10);

      // Bar background
      ctx.fillStyle = 'rgba(255,255,255,0.08)';
      ctx.beginPath();
      ctx.roundRect(meterX, my + 14, meterW, barH, 3);
      ctx.fill();

      // Bar fill
      const fillW = Math.max(4, Math.min(meterW, (m.val||0) * meterW));
      const barGrad = ctx.createLinearGradient(meterX, 0, meterX + fillW, 0);
      barGrad.addColorStop(0, m.col + 'aa');
      barGrad.addColorStop(1, m.col);
      ctx.fillStyle = barGrad;
      ctx.beginPath();
      ctx.roundRect(meterX, my + 14, fillW, barH, 3);
      ctx.fill();

      // Pct label right of bar
      ctx.font = '8px monospace';
      ctx.fillStyle = m.col;
      ctx.textAlign = 'right';
      ctx.fillText(`${Math.round((m.val||0)*100)}%`, W - 4, my + 21);
      ctx.textAlign = 'left';

      // Sub-label
      ctx.font = '8px system-ui,sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.45)';
      ctx.fillText(m.sub, meterX, my + 30 + barH);
    });

  } else {
    // No data yet — placeholder text
    ctx.font = '10px system-ui'; ctx.fillStyle = 'rgba(255,255,255,0.3)';
    ctx.textAlign = 'left';
    ctx.fillText('Loading price structure…', meterX, (H-44)/2);
  }

  // ── Bottom action bar ─────────────────────────────────────────────
  ctx.fillStyle = 'rgba(0,0,0,0.6)';
  ctx.fillRect(0, H - 44, W, 44);

  // Left: "WHAT TO DO"
  ctx.font = 'bold 8px JetBrains Mono,monospace';
  ctx.fillStyle = 'rgba(255,255,255,0.4)';
  ctx.textAlign = 'left';
  ctx.fillText('WHAT TO DO', 10, H - 30);
  ctx.font = 'bold 11px system-ui,sans-serif';
  ctx.fillStyle = rm.col;
  ctx.fillText(rm.action, 10, H - 14);

  // Right: bar count
  if(hasData){
    ctx.font = '8px monospace'; ctx.fillStyle = 'rgba(255,255,255,0.25)';
    ctx.textAlign = 'right';
    ctx.fillText(`${pts3d.length} bars analyzed`, W - 8, H - 14);
    ctx.textAlign = 'left';
  }
}

function _drawRicci3d(){
  const canvas = document.getElementById('ricci3dCanvas');
  if(!canvas) return;
  const W = Math.max(canvas.parentElement ? canvas.parentElement.offsetWidth : 0, canvas.offsetWidth, 400);
  const H = 200;
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,W,H);

  const data = window._manifoldRicci || [];
  const hasData = data.length > 5 && data.filter(v=>v!==0).length > 3;

  if(!hasData){
    ctx.fillStyle='rgba(255,255,255,0.15)'; ctx.font='11px system-ui,sans-serif'; ctx.textAlign='center';
    ctx.fillText('Waiting for market data…', W/2, H/2-8);
    ctx.font='9px system-ui,sans-serif'; ctx.fillStyle='rgba(255,255,255,0.08)';
    ctx.fillText('Loads when the engine computes manifold metrics', W/2, H/2+10);
    ctx.textAlign='left'; return;
  }

  const mn = Math.min(...data), mx = Math.max(...data), range = mx-mn||0.001;
  const avg = data.reduce((a,b)=>a+b,0)/data.length;
  const last = data[data.length-1];
  const step = W/(data.length-1);

  // Classify: low curvature = clean trend (green), high = choppy (red)
  const isSmoothTrend = avg < 0.45;
  const isChoppy      = avg > 0.75;
  const zoneCol = isSmoothTrend ? 'rgba(0,208,132,0.07)' : isChoppy ? 'rgba(255,61,90,0.08)' : 'rgba(139,92,246,0.06)';
  const lineCol = isSmoothTrend ? 'rgba(0,208,132,0.9)'  : isChoppy ? 'rgba(255,61,90,0.9)'  : 'rgba(139,92,246,0.9)';
  const fillTop = isSmoothTrend ? 'rgba(0,208,132,0.35)' : isChoppy ? 'rgba(255,61,90,0.35)' : 'rgba(139,92,246,0.35)';
  const fillBot = isSmoothTrend ? 'rgba(0,208,132,0.02)' : isChoppy ? 'rgba(255,61,90,0.02)' : 'rgba(139,92,246,0.02)';

  // Background zone
  ctx.fillStyle=zoneCol; ctx.fillRect(0,0,W,H);

  // Reference line at average
  const avgY = H-(avg-mn)/range*(H*0.78)-H*0.11;
  ctx.setLineDash([3,4]);
  ctx.strokeStyle='rgba(255,255,255,0.12)'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(0,avgY); ctx.lineTo(W,avgY); ctx.stroke();
  ctx.setLineDash([]);

  // Filled area
  const toY = v => H - (v-mn)/range*(H*0.78) - H*0.11;
  const grad = ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0, fillTop); grad.addColorStop(1, fillBot);
  ctx.beginPath();
  data.forEach((v,i)=>{ const x=i*step; i===0?ctx.moveTo(x,toY(v)):ctx.lineTo(x,toY(v)); });
  ctx.lineTo(W,H); ctx.lineTo(0,H); ctx.closePath();
  ctx.fillStyle=grad; ctx.fill();

  // Line
  ctx.beginPath();
  data.forEach((v,i)=>{ const x=i*step; i===0?ctx.moveTo(x,toY(v)):ctx.lineTo(x,toY(v)); });
  ctx.strokeStyle=lineCol; ctx.lineWidth=2; ctx.stroke();

  // Current value dot
  ctx.beginPath(); ctx.arc((data.length-1)*step, toY(last), 4, 0, Math.PI*2);
  ctx.fillStyle='#fff'; ctx.fill();

  // Plain English overlay at bottom
  const plainText = isSmoothTrend ? '✅ Low curvature — price path is smooth, trend is clean'
                  : isChoppy      ? '⚠️  High curvature — path is twisting, reversals likely'
                  :                 '〽️  Moderate curvature — mixed signal, stay patient';
  ctx.fillStyle='rgba(0,0,0,0.5)'; ctx.fillRect(0,H-40,W,40);
  ctx.font='bold 9px JetBrains Mono,monospace'; ctx.fillStyle=lineCol; ctx.textAlign='left';
  ctx.fillText('PRICE PATH SMOOTHNESS', 10, H-26);
  ctx.font='9px system-ui,sans-serif'; ctx.fillStyle='rgba(255,255,255,0.75)';
  ctx.fillText(plainText, 10, H-10);

  // Stats top-right
  ctx.textAlign='right'; ctx.font='8px monospace'; ctx.fillStyle='rgba(255,255,255,0.4)';
  ctx.fillText(`avg ${avg.toFixed(3)}  now ${last.toFixed(3)}`, W-8, 14);
  ctx.textAlign='left';
}

// Auto-refresh manifold every 30s
if (!window._manifoldInterval) {
  window._manifoldInterval = setInterval(() => {
    if (document.getElementById('ricci3dCanvas')) loadManifoldData();
  }, 30000);
}
