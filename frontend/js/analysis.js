/* -----------------------------------------------------------
   CHAKRA — analysis.js  (rebuilt with GEX Expected Move,
   Price Action Map, GEX by Strike, Stock Rotation Analysis)
   ----------------------------------------------------------- */

// -- Pre-market ---------------------------------------------
async function loadPremarket() {
  const d = await getCached('/api/premarket', 60000);
  if (!d || d.error) {
    if ($('pmContent')) $('pmContent').innerHTML = '<div class="empty">Pre-market data loads at 8am ET</div>';
    buildSchedule();
    return;
  }
  const tickers = d.tickers || {};
  const items   = Object.values(tickers);
  if ($('pmContent')) {
    $('pmContent').innerHTML = items.length ? items.map(function(t) {
      var bias    = t.bias || {};
      var biasStr = bias.bias || 'NEUTRAL';
      var score   = bias.score || 50;
      var rsi     = bias.rsi || 0;
      var mom     = bias.momentum_5d || 0;
      var col     = biasStr==='BULLISH'?'var(--green)':biasStr==='BEARISH'?'var(--red)':'var(--gold)';
      var factors = bias.factors || [];
      return '<div style="padding:10px 0;border-bottom:1px solid var(--border)">'
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'
        + '<div><strong style="font-size:13px">' + t.ticker + '</strong>'
        + '<span style="font-size:10px;color:var(--sub);margin-left:8px">' + (t.name||'') + '</span></div>'
        + '<span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;background:'+col+'15;color:'+col+'">' + biasStr + '</span>'
        + '</div>'
        + '<div style="display:flex;gap:16px;font-size:10px;color:var(--sub)">'
        + '<span>RSI <strong style="color:var(--text)">' + (rsi>0?rsi.toFixed(1):'--') + '</strong></span>'
        + '<span>Score <strong style="color:'+col+'">' + score + '</strong></span>'
        + '<span>5D <strong style="color:' + (mom>=0?'var(--green)':'var(--red)') + '">' + (mom>=0?'+':'') + (mom||0).toFixed(2) + '%</strong></span>'
        + '</div>'
        + (factors.length ? '<div style="font-size:9px;color:var(--sub);margin-top:4px">' + factors.slice(0,2).join(' - ') + '</div>' : '')
        + '</div>';
    }).join('') : '<div class="empty">No pre-market data</div>';
  }
  buildSchedule();
}


// -- GEX Tab (Full CHAKRA-style) ---------------------------
// Initialize from sessionStorage so bottom panels use the same ticker as George card
let _gexTicker = sessionStorage.getItem('gexActiveTicker') || 'SPY';
function selectGexTicker(t) {
  _gexTicker = t;
  // Keep _gexActiveTicker in sync so George card matches bottom panels
  if (typeof _gexActiveTicker !== 'undefined') _gexActiveTicker = t;
  sessionStorage.setItem('gexActiveTicker', t);
  document.querySelectorAll('[id^="gbt-"]').forEach(b => b.classList.remove('active-gex'));
  $('gbt-' + t)?.classList.add('active-gex');
  loadGex();
}

async function loadGex() {
  // Auto-refresh every 15s when GEX tab is active
  if (!window._gexPriceTimer) {
    window._gexPriceTimer = setInterval(() => {
      const pane = document.getElementById('p-analysis-gex');
      if (pane && pane.classList.contains('active')) loadGex();
    }, 15000);
  }
  const ticker = _gexTicker || 'SPX';
  const [gex, px] = await Promise.all([
    get('/api/options/gex?ticker=' + ticker),
    getCached('/api/prices/live', 10000),
  ]);

  if (!gex || gex.error) {
    $('gexStatBar').innerHTML = '<div class="empty" style="grid-column:1/-1">⏳ GEX data not available — run the GEX calculator or wait for next compute</div>';
    ['gexLadder','gexWalls3','gexDealer','gexGuidance','gexDirectionBody',
     'gexRiskMeters','priceActionMap','gexExpectedMove','gexByStrike'].forEach(id => {
      if ($(id)) $(id).innerHTML = '<div class="empty">—</div>';
    });
    return;
  }

  // Use live price if available, fall back to GEX heatmap price
  const _liveSpx = px && px[ticker] ? parseFloat(px[ticker].price || 0) : 0;
  const spx     = _liveSpx || parseFloat(gex.spx_price || gex.spot || 0);
  const cw      = parseFloat(gex.call_wall || gex.top_call_wall || 0);
  const pw      = parseFloat(gex.put_wall  || gex.top_put_wall  || 0);
  const zg      = parseFloat(gex.zero_gamma || spx);
  const netGex  = parseFloat(gex.net_gex) || parseFloat(gex.net_total_gex || 0) / 1e9;
  const callGex = parseFloat(gex.call_gex || 0);
  const putGex  = parseFloat(gex.put_gex  || 0);
  const isNeg   = gex.regime === 'NEGATIVE_GAMMA';
  const regCol  = isNeg ? 'var(--red)' : 'var(--green)';
  const ivSkew  = parseFloat(gex.iv_skew || 0);
  const atmIV   = parseFloat(gex.atm_iv || gex.iv || 14);
  const bias    = gex.bearish_skew ? 'BEARISH' : gex.bullish_bias ? 'BULLISH' : 'NEUTRAL';
  const biasCol = bias === 'BEARISH' ? 'var(--red)' : bias === 'BULLISH' ? 'var(--green)' : 'var(--gold)';
  const sc2 = parseFloat(gex.second_call_wall || 0);
  const sp2 = parseFloat(gex.second_put_wall  || 0);
  const roomCall = cw - spx, roomPut = spx - pw;

  // Header ticker
  const headerEl = $('gexTickerHeader');
  if (headerEl) headerEl.textContent = ticker + ' @ ' + (spx > 0 ? '$' + spx.toLocaleString() : '—');

  // Stat bar
  const _regimePlain = { POSITIVE_GAMMA:'Stabilizing', NEGATIVE_GAMMA:'Explosive', LOW_VOL:'Pinned', NEUTRAL:'Neutral' };
  const _netGexAbs = Math.abs(netGex);
  const _netGexStr = _netGexAbs >= 1 ? (netGex >= 0 ? '+' : '') + netGex.toFixed(1) + 'B' : (netGex >= 0 ? '+' : '') + (netGex * 1000).toFixed(0) + 'M';
  const _ivLean = ivSkew < -0.1 ? 'Call lean' : ivSkew > 0.1 ? 'Put lean' : 'Balanced';
  $('gexStatBar').innerHTML = [
    { l:'Market Mode',      sub: gex.regime ? gex.regime.replace(/_/g,' ') : '—',        v: _regimePlain[gex.regime] || (gex.regime||'—'),     c: regCol },
    { l:'Dealer Balance',   sub: netGex >= 0 ? 'Net long exposure' : 'Net short exposure', v: _netGexStr,                                         c: netGex >= 0 ? 'var(--green)' : 'var(--red)' },
    { l:'Upside Ceiling',   sub: 'Call Wall — price tends to stall here',                  v: cw ? '$' + cw.toLocaleString() : '—',               c: 'var(--red)' },
    { l:'Downside Floor',   sub: 'Put Wall — price tends to bounce here',                  v: pw ? '$' + pw.toLocaleString() : '—',               c: 'var(--green)' },
    { l:'Flip Point',       sub: 'Zero Gamma — market becomes explosive past this',        v: zg ? '$' + zg.toLocaleString() : '—',               c: 'var(--gold)' },
    { l:'Options Lean',     sub: ivSkew < -0.1 ? 'More call buying than puts' : ivSkew > 0.1 ? 'More put buying than calls' : 'Balanced call/put demand', v: _ivLean, c: biasCol },
  ].map(x => `<div class="int-card">
    <div class="int-label">${x.l}</div>
    <div class="int-val" style="color:${x.c}">${x.v}</div>
    <div style="font-size:8px;color:var(--sub);margin-top:2px;line-height:1.3">${x.sub}</div>
  </div>`).join('');

  // -- EXPECTED MOVE -----------------------------------------
  _renderExpectedMove(gex, spx, atmIV);

  // -- PRICE ACTION MAP --------------------------------------
  _renderPriceActionMap(gex, spx, cw, pw, zg);

  // -- CHAKRA-style sections ----------------------------------
  _renderStrategyCard(gex, spx, cw, pw, zg, netGex, gex.regime || '');
  _renderSessionAnalysis(gex, spx, cw, pw, netGex, gex.regime || '', ticker);
  _renderSignalPanel(gex, spx, cw, pw, netGex, gex.regime || '', ivSkew);
  _renderHedgingWindows(spx, cw, pw, netGex);

  // -- GEX BY STRIKE (lollipop chart) -----------------------
  _renderGexByStrike(gex, spx, cw, pw, zg);

  // -- ARJUN PATH PREDICTION --------------------------------
  _renderArjunPath(gex, spx, cw, pw);

  // -- Walls panel -------------------------------------------
  if ($('gexWalls3')) $('gexWalls3').innerHTML = [
    { l: '🔴 Call Wall',  v: cw  ? '$'+cw.toLocaleString()  : '—', sub: cw  ? '+'+(cw-spx).toFixed(0)+' pts' : '', c:'var(--red)' },
    { l: '🟢 Put Wall',   v: pw  ? '$'+pw.toLocaleString()  : '—', sub: pw  ? '-'+(spx-pw).toFixed(0)+' pts' : '', c:'var(--green)' },
    { l: '⚡ Zero Gamma', v: zg  ? '$'+zg.toLocaleString()  : '—', sub: Math.abs(spx-zg).toFixed(0)+' pts away', c:'var(--gold)' },
    { l: '2nd Call',      v: sc2 ? '$'+sc2.toLocaleString() : '—', sub: '', c:'rgba(255,61,90,0.6)' },
    { l: '2nd Put',       v: sp2 ? '$'+sp2.toLocaleString() : '—', sub: '', c:'rgba(0,208,132,0.6)' },
  ].map(x => `<div style="background:var(--bg3);border:1px solid var(--border);border-radius:7px;padding:10px">
    <div style="font-size:9px;color:var(--sub);margin-bottom:4px">${x.l}</div>
    <div style="font-size:15px;font-weight:700;font-family:JetBrains Mono,monospace;color:${x.c}">${x.v}</div>
    <div style="font-size:9px;color:var(--sub)">${x.sub}</div>
  </div>`).join('');

  // Dealer guidance
  if ($('gexDealer')) $('gexDealer').innerHTML = `
    <div style="font-size:12px;font-weight:600;color:${regCol};margin-bottom:6px">${isNeg ? '⚠️ Market is in Explosive Mode' : '✅ Market is in Stabilizing Mode'}</div>
    <div style="font-size:10px;color:var(--sub);line-height:1.7;margin-bottom:8px">
      ${isNeg
        ? '<strong style="color:var(--red)">Big moves are more likely today.</strong> Market makers have to buy when the market falls and sell when it rises — this <em>adds fuel</em> to moves rather than stopping them. Don\'t fade strong breakouts.'
        : '<strong style="color:var(--green)">Market tends to stay in a range today.</strong> Market makers are buying dips and selling rallies — this <em>dampens</em> big moves. Price tends to snap back toward its pin level rather than running away.'}
    </div>
    <div style="font-size:9px;padding:6px 10px;border-radius:4px;background:${isNeg ? 'rgba(255,61,90,0.08)' : 'rgba(0,208,132,0.08)'};color:${isNeg ? 'var(--red)' : 'var(--green)'}">
      ${isNeg ? '⚡ Tip: Trade breakouts, not fades. Let winners run.' : '💡 Tip: Fade the extremes. Buy dips, sell rips. Expect price to gravitate back to center.'}
    </div>`;

  // GEX guidance
  if ($('gexGuidance')) $('gexGuidance').innerHTML = `
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      <span class="chip ${bias==='BULLISH'?'cg':bias==='BEARISH'?'cr':'cm'}">${bias === 'BULLISH' ? '📈 More calls bought' : bias === 'BEARISH' ? '📉 More puts bought' : '⚖️ Balanced'}</span>
      <span class="chip ${isNeg?'cr':'cg'}">${isNeg ? '⚡ Explosive' : '🧲 Stabilizing'}</span>
    </div>
    <div style="font-size:10px;line-height:1.8">
      <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
        <span style="color:var(--sub)">Today's range</span>
        <strong>$${pw.toLocaleString()} — $${cw.toLocaleString()}</strong>
      </div>
      <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
        <span style="color:var(--sub)">Room to ceiling</span>
        <strong style="color:var(--red)">${roomCall.toFixed(0)} pts &nbsp;(${(roomCall/spx*100).toFixed(2)}%)</strong>
      </div>
      <div style="display:flex;justify-content:space-between;padding:4px 0">
        <span style="color:var(--sub)">Room to floor</span>
        <strong style="color:var(--green)">${roomPut.toFixed(0)} pts &nbsp;(${(roomPut/spx*100).toFixed(2)}%)</strong>
      </div>
    </div>`;

  // Direction table
  if ($('gexDirectionBody')) {
    const accel = Math.round(50 + netGex * 5);
    $('gexDirectionBody').innerHTML = `
    <div style="margin-bottom:14px">
      <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:5px">
        <span style="color:var(--sub)">MOMENTUM</span>
        <span class="mn" style="color:${netGex>=0?'var(--green)':'var(--red)'}">
          ${netGex>=0?'BULLISH':'BEARISH'} ${Math.abs(netGex).toFixed(2)}B
        </span>
      </div>
    </div>
    <div>
      <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:5px">
        <span style="color:var(--sub)">ACCELERATOR</span>
        <span class="mn" style="color:${accel>60?'var(--green)':accel<40?'var(--red)':'var(--gold)'}">
          ${accel>50?'+':''}${accel-50} ${accel>65?'STRONG':accel>55?'MODERATE':'NEUTRAL'}
        </span>
      </div>
      <div style="height:8px;background:var(--bg3);border-radius:4px;overflow:hidden;position:relative">
        <div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--border)"></div>
        <div style="position:absolute;${accel>=50?'left:50%;width:'+(accel-50)+'%':'right:50%;width:'+(50-accel)+'%'};
          top:0;bottom:0;background:${accel>=50?'var(--green)':'var(--red)'};border-radius:4px"></div>
      </div>
    </div>`;
  }

  // Risk meters
  const pinRisk   = Math.round(Math.max(0, Math.min(100, 100-(Math.min(roomCall,roomPut)/(spx*0.05)*100))));
  const breakRisk = Math.round(Math.max(0, Math.min(100, isNeg?75:Math.abs(netGex)<5?60:35)));
  if ($('gexRiskMeters')) $('gexRiskMeters').innerHTML = `
    <div style="margin-bottom:14px">
      <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:5px">
        <span style="color:var(--sub)">PIN RISK</span>
        <span class="mn" style="color:${pinRisk>60?'var(--red)':'var(--gold)'}">${pinRisk}%</span>
      </div>
      <div style="height:7px;background:var(--bg3);border-radius:4px;overflow:hidden">
        <div style="width:${pinRisk}%;height:100%;background:${pinRisk>60?'var(--red)':'var(--gold)'};border-radius:4px"></div>
      </div>
    </div>
    <div>
      <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:5px">
        <span style="color:var(--sub)">BREAKOUT RISK</span>
        <span class="mn" style="color:${breakRisk>60?'var(--red)':'var(--purple)'}">${breakRisk}%</span>
      </div>
      <div style="height:7px;background:var(--bg3);border-radius:4px;overflow:hidden">
        <div style="width:${breakRisk}%;height:100%;background:${breakRisk>60?'var(--red)':'var(--purple)'};border-radius:4px"></div>
      </div>
    </div>`;

  // GEX Heatmap canvas
  _drawGexHeatmap(gex, spx, cw, pw, zg);

  // Extended sections
  _loadGexExtendedSections(ticker);
  _addTopSpTickers();

  // Global index tiles
  const idxList = (gs().indices || DEF.indices);
  if ($('idxTime')) $('idxTime').textContent = new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});
  if (px && !px.error && $('idxTiles')) {
    $('idxTiles').innerHTML = idxList.map(sym => {
      const d2 = px[sym] || {};
      const pct = parseFloat(d2.chg_pct || 0);
      return `<div style="background:var(--bg3);border:1px solid ${pct>=0?'rgba(0,208,132,0.15)':'rgba(255,61,90,0.15)'};border-radius:7px;padding:9px">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span class="mn" style="font-size:11px;font-weight:700">${sym}</span>
          <span style="font-size:10px">${IF[sym]||''}</span>
        </div>
        <div style="font-size:8px;color:var(--sub);margin-top:2px">${IN[sym]||sym}</div>
        <div class="mn" style="font-size:13px;font-weight:700;margin-top:5px">${f$(d2.price)}</div>
        <div class="mn" style="font-size:10px;color:${pc(pct)};margin-top:1px">${fp(pct)}</div>
      </div>`;
    }).join('');
  }
}

// -- Expected Move -----------------------------------------
function _renderExpectedMove(gex, spx, atmIV) {
  const el = $('gexExpectedMove'); if (!el) return;
  // Compute from ATM IV × price × sqrt(1/252) for daily EM
  const dailyEM   = spx * (atmIV / 100) * Math.sqrt(1/252);
  const emPct     = (dailyEM / spx * 100);
  const strikes   = gex.nearby_strikes || [];
  const cw        = parseFloat(gex.call_wall || gex.top_call_wall || 0);
  const pw        = parseFloat(gex.put_wall  || gex.top_put_wall  || 0);
  const upper1sd  = spx + dailyEM;
  const lower1sd  = spx - dailyEM;
  const upper2sd  = spx + dailyEM * 2;
  const lower2sd  = spx - dailyEM * 2;

  // Compute straddle from nearby strikes (ATM call + put premium)
  let straddlePrice = gex.atm_straddle || 0;
  if (!straddlePrice && strikes.length) {
    const atmStrike = strikes.reduce((best, s) => Math.abs(s.strike - spx) < Math.abs(best.strike - spx) ? s : best, strikes[0]);
    straddlePrice = (atmStrike.call_price || 0) + (atmStrike.put_price || 0);
    if (!straddlePrice) straddlePrice = dailyEM * 2; // fallback
  }
  if (!straddlePrice) straddlePrice = dailyEM * 2;

  const gexNote = parseFloat(gex.net_gex || 0) > 0
    ? { text: 'GEX: Range likely TIGHTER than EM', sub: 'Positive gamma + low momentum — dealers dampen moves.', col: 'var(--green)' }
    : { text: 'GEX: Range may be WIDER than EM',   sub: 'Negative gamma — dealers amplify directional moves.', col: 'var(--red)' };

  // Position of spot on the range bar (0=lower2sd, 100=upper2sd)
  const total = upper2sd - lower2sd;
  const spotPct = Math.max(2, Math.min(98, (spx - lower2sd) / total * 100));

  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap">
      <div>
        <span style="font-size:28px;font-weight:800;font-family:JetBrains Mono,monospace">
          ±${emPct.toFixed(2)}%
        </span>
        <span style="font-size:14px;color:var(--sub);margin-left:8px">±$${dailyEM.toFixed(2)}</span>
      </div>
      <div style="font-size:12px;color:var(--sub)">
        ATM Straddle: <strong style="color:var(--text)">$${straddlePrice.toFixed(2)}</strong>
      </div>
      <div style="display:flex;gap:6px">
        <span class="chip cm">STRADDLE</span>
        <span class="chip cb">LIVE</span>
      </div>
    </div>

    <!-- Range bar -->
    <div style="position:relative;margin-bottom:8px">
      <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--sub);margin-bottom:4px">
        <span style="color:rgba(255,61,90,0.7)">$${lower2sd.toFixed(2)}</span>
        <span style="color:rgba(255,61,90,0.4)">$${lower1sd.toFixed(2)}</span>
        <span style="color:var(--blue);font-weight:700">$${spx.toLocaleString()}</span>
        <span style="color:rgba(0,208,132,0.4)">$${upper1sd.toFixed(2)}</span>
        <span style="color:rgba(0,208,132,0.7)">$${upper2sd.toFixed(2)}</span>
      </div>
      <div style="height:20px;border-radius:10px;overflow:hidden;display:flex;position:relative">
        <div style="flex:1;background:rgba(255,61,90,0.25)"></div>
        <div style="flex:1;background:rgba(255,61,90,0.1)"></div>
        <div style="flex:1;background:rgba(0,208,132,0.1)"></div>
        <div style="flex:1;background:rgba(0,208,132,0.25)"></div>
        <!-- Spot indicator -->
        <div style="position:absolute;left:${spotPct}%;top:0;bottom:0;width:2px;background:var(--blue);transform:translateX(-50%)"></div>
        <div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:rgba(255,255,255,0.3);transform:translateX(-50%)"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:8px;color:var(--sub);margin-top:3px">
        <span>2SD</span><span>1SD</span><span style="color:var(--blue)">SPOT</span><span>1SD</span><span>2SD</span>
      </div>
    </div>

    <!-- GEX note -->
    <div style="background:${gexNote.col}08;border:1px solid ${gexNote.col}20;border-radius:6px;padding:10px;margin-bottom:10px">
      <div style="font-size:10px;font-weight:700;color:${gexNote.col}">↕ ${gexNote.text}</div>
      <div style="font-size:9px;color:var(--sub);margin-top:2px">${gexNote.sub}</div>
    </div>

    <!-- IV stats -->
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
      ${[
        ['ATM IV', atmIV.toFixed(1)+'%', 'var(--blue)'],
        ['IV Rank', (gex.iv_rank||50).toFixed(0), 'var(--text)'],
        ['HV20/50', (gex.hv20||12).toFixed(1)+'%', 'var(--sub)'],
        ['IV vs HV', Math.abs(atmIV-(gex.hv20||12))<2?'FAIR':atmIV>(gex.hv20||12)?'RICH':'CHEAP', atmIV>(gex.hv20||12)+2?'var(--red)':atmIV<(gex.hv20||12)-2?'var(--green)':'var(--gold)'],
      ].map(([l,v,c]) => `<div style="text-align:center">
        <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">${l}</div>
        <div style="font-size:14px;font-weight:700;color:${c}">${v}</div>
      </div>`).join('')}
    </div>`;
}

// -- Price Action Map (TILE version) -----------------------
function _renderPriceActionMap(gex, spx, cw, pw, zg) {
  var el = $('priceActionMap'); if (!el) return;
  var strikes = gex.nearby_strikes || [];

  if (!strikes.length) {
    el.innerHTML = '<div class="empty" style="grid-column:1/-1;padding:20px">No strike data</div>';
    return;
  }

  var sorted = strikes.slice().sort(function(a,b){return b.strike-a.strike;});
  var tickStep = sorted.length > 1 ? Math.abs(sorted[0].strike - sorted[1].strike) : 5;
  var maxGexStrike = sorted.reduce(function(mx,s){return Math.abs(s.net_gex||0)>Math.abs(mx.net_gex||0)?s:mx;}, sorted[0]);

  // Build tiles - only show labeled strikes (call wall, put wall, max gex, current, gamma flip)
  var tiles = [];
  sorted.forEach(function(s) {
    var isCallWall  = s.strike === cw;
    var isPutWall   = s.strike === pw;
    var isMaxGex    = s.strike === maxGexStrike.strike;
    var isCurrent   = Math.abs(s.strike - spx) < tickStep * 1.2;
    var isZeroGamma = Math.abs(s.strike - (zg||0)) < tickStep * 0.8;

    var tag='', col='', bg='', icon='', behavior='';
    if (isCallWall)    { tag='CALL WALL';   col='var(--red)';    bg='rgba(255,61,90,0.1)';    icon='🔴'; behavior='Dealers sell rallies here. Rejection likely.'; }
    else if (isMaxGex) { tag='MAX GEX';     col='var(--gold)';   bg='rgba(245,166,35,0.1)';   icon='📌'; behavior='Pin magnet. Price gravitates here EOD.'; }
    else if (isPutWall){ tag='PUT WALL';    col='var(--green)';  bg='rgba(0,208,132,0.1)';    icon='🟢'; behavior='Dealers buy dips here. Bounce likely.'; }
    else if (isCurrent){ tag='CURRENT';     col='var(--blue)';   bg='rgba(61,142,255,0.1)';   icon='◉';  behavior='Balanced dealer hedging at spot.'; }
    else if (isZeroGamma){tag='GAMMA FLIP'; col='var(--purple)'; bg='rgba(139,92,246,0.1)';   icon='⚡'; behavior='Regime transition zone.'; }

    if (!tag) return; // skip unlabeled strikes

    var dist  = ((s.strike - spx) / spx * 100);
    var distStr = isCurrent ? '↔ AT SPOT' : dist > 0 ? '↑ +'+dist.toFixed(1)+'% above' : '↓ '+Math.abs(dist).toFixed(1)+'% below';
    var net   = parseFloat(s.net_gex||0);

    tiles.push('<div style="background:'+bg+';border:1px solid '+col+'30;border-radius:8px;padding:12px;position:relative;cursor:default">'
      + '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">'
      + '<div style="font-size:16px;font-weight:800;font-family:JetBrains Mono,monospace;color:'+col+'">$'+s.strike.toLocaleString()+'</div>'
      + '<span style="font-size:8px;font-weight:700;padding:2px 7px;border-radius:3px;background:'+col+'15;color:'+col+';border:1px solid '+col+'30">'+tag+'</span>'
      + '</div>'
      + '<div style="font-size:9px;color:var(--sub);margin-bottom:6px">'+distStr+'</div>'
      + '<div style="font-size:10px;color:var(--text);line-height:1.4">'+behavior+'</div>'
      + '</div>');
  });

  // Also add an expandable "All strikes" section
  var allRows = sorted.map(function(s) {
    var isCur = Math.abs(s.strike-spx) < tickStep*1.2;
    var isCW  = s.strike===cw, isPW = s.strike===pw;
    var col2  = isCW?'var(--red)':isPW?'var(--green)':isCur?'var(--blue)':'var(--sub)';
    var dist  = ((s.strike-spx)/spx*100);
    return '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);'+(isCur?'background:rgba(61,142,255,0.04);':'')+'">'
      + '<span style="font-family:JetBrains Mono,monospace;font-size:10px;font-weight:'+(isCur?'700':'400')+';color:'+col2+'">$'+s.strike.toLocaleString()+'</span>'
      + '<span style="font-size:9px;color:var(--sub)">'+(dist>=0?'+':'')+dist.toFixed(1)+'%</span>'
      + '</div>';
  }).join('');

  el.innerHTML = tiles.join('')
    + '<div style="grid-column:1/-1;margin-top:4px">'
    + '<details><summary style="font-size:9px;color:var(--sub);cursor:pointer;padding:4px">All '+sorted.length+' strikes ▼</summary>'
    + '<div style="margin-top:6px;padding:8px;background:var(--bg3);border-radius:6px;max-height:200px;overflow-y:auto">'+allRows+'</div>'
    + '</details></div>';
}


// -- GEX by Strike (lollipop chart)
function _renderGexByStrike(gex, spx, cw, pw, zg) {
  // ── Strike Ladder — Diverging Bar Chart ────────────────────────────────
  var el = $('gexByStrike'); if (!el) return;
  var strikes = (gex.nearby_strikes || gex.top_strikes || gex.ladder || [])
    .slice().sort(function(a,b){ return b.strike - a.strike; });
  if (!strikes.length) { el.innerHTML = '<div class="empty" style="padding:20px">No strike data — available after GEX compute</div>'; return; }

  var fmtG = function(v) {
    var a = Math.abs(v);
    if (a >= 1e9) return (v/1e9).toFixed(1)+'B';
    if (a >= 1e6) return (v/1e6).toFixed(0)+'M';
    if (a >= 1e3) return (v/1e3).toFixed(0)+'K';
    return v.toFixed(0);
  };

  var maxCall = Math.max.apply(null, strikes.map(function(s){ return Math.abs(s.call_gex||0); })) || 1;
  var maxPut  = Math.max.apply(null, strikes.map(function(s){ return Math.abs(s.put_gex||0);  })) || 1;
  var maxSide = Math.max(maxCall, maxPut);

  // Key level lookup
  var _klMap = {};
  if (cw) _klMap[cw] = (_klMap[cw]||[]).concat([{l:'CW', c:'#10b981'}]);
  if (pw) { if (pw===cw) { _klMap[pw] = [{l:'BOTH WALLS', c:'#f59e0b'}]; } else _klMap[pw] = (_klMap[pw]||[]).concat([{l:'PW', c:'#f43f5e'}]); }
  if (zg) _klMap[Math.round(zg)] = (_klMap[Math.round(zg)]||[]).concat([{l:'ZEROγ', c:'#fbbf24'}]);
  var maxPainVal = parseFloat(gex.max_pain||0);
  if (maxPainVal) _klMap[maxPainVal] = (_klMap[maxPainVal]||[]).concat([{l:'PIN', c:'#a78bfa'}]);

  var rows = strikes.map(function(s) {
    var cg   = Math.abs(parseFloat(s.call_gex||0));
    var pg   = Math.abs(parseFloat(s.put_gex||0));
    var ng   = parseFloat(s.net_gex||0);
    var oi   = s.oi ? parseInt(s.oi).toLocaleString() : '';
    var callPct = (cg/maxSide*100).toFixed(1);
    var putPct  = (pg/maxSide*100).toFixed(1);
    var isSpot  = Math.abs(s.strike - spx) < 0.75;
    var kls     = _klMap[s.strike] || [];
    var rowBg   = isSpot ? 'background:rgba(14,165,233,0.08);border:1px solid rgba(14,165,233,0.3)' :
                  kls.length ? 'background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06)' :
                  'border:1px solid transparent';
    var tagHtml = kls.map(function(k) {
      return '<span style="font-size:7px;font-weight:800;padding:1px 4px;border-radius:2px;background:'+k.c+'22;color:'+k.c+';letter-spacing:0.5px;margin-left:3px">'+k.l+'</span>';
    }).join('');
    var netCol  = ng >= 0 ? '#10b981' : '#f43f5e';
    var strikeFw = (isSpot||kls.length) ? '800' : '500';
    var strikeCol = isSpot ? '#0ea5e9' : kls.length ? '#fff' : 'rgba(255,255,255,0.75)';

    return '<div style="display:grid;grid-template-columns:60px 1fr 1fr 54px;gap:4px;align-items:center;padding:4px 8px;border-radius:5px;margin-bottom:2px;'+rowBg+'">'
      // Strike + badges
      + '<div style="display:flex;align-items:center;gap:2px">'
      +   '<span style="font-family:JetBrains Mono,monospace;font-size:10px;font-weight:'+strikeFw+';color:'+strikeCol+'">'+s.strike.toFixed(0)+(isSpot?' ◀':'')+tagHtml+'</span>'
      + '</div>'
      // Call bar (green, fills left→right)
      + '<div style="display:flex;align-items:center;gap:4px">'
      +   '<div style="flex:1;height:10px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden">'
      +     '<div style="width:'+callPct+'%;height:100%;background:linear-gradient(90deg,#059669,#10b981);border-radius:3px;transition:width .4s"></div>'
      +   '</div>'
      +   '<span style="font-size:8px;color:#6ee7b7;font-family:JetBrains Mono,monospace;width:36px;text-align:right">'+fmtG(cg)+'</span>'
      + '</div>'
      // Put bar (red, fills right→left)
      + '<div style="display:flex;align-items:center;gap:4px">'
      +   '<span style="font-size:8px;color:#fca5a5;font-family:JetBrains Mono,monospace;width:36px;text-align:left">'+fmtG(pg)+'</span>'
      +   '<div style="flex:1;height:10px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden">'
      +     '<div style="float:right;width:'+putPct+'%;height:100%;background:linear-gradient(270deg,#be123c,#f43f5e);border-radius:3px;transition:width .4s"></div>'
      +   '</div>'
      + '</div>'
      // Net GEX
      + '<div style="text-align:right;font-size:9px;font-family:JetBrains Mono,monospace;color:'+netCol+';font-weight:700">'+(ng>=0?'+':'')+fmtG(ng)+'</div>'
      + '</div>';
  }).join('');

  // Header row
  var header = '<div style="display:grid;grid-template-columns:60px 1fr 1fr 54px;gap:4px;padding:3px 8px 6px;border-bottom:1px solid rgba(255,255,255,0.07);margin-bottom:4px">'
    + '<div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">STRIKE</div>'
    + '<div style="font-size:7px;letter-spacing:1px;color:#10b981;text-transform:uppercase;text-align:center">● CALLS</div>'
    + '<div style="font-size:7px;letter-spacing:1px;color:#f43f5e;text-transform:uppercase;text-align:center">● PUTS</div>'
    + '<div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;text-align:right">NET</div>'
    + '</div>';

  el.innerHTML = header + rows;
}

// -- GEX Heatmap Canvas (diverging side-by-side, v2) --------
function _drawGexHeatmap(gex, spx, cw, pw, zg) {
  const canvas = $('gexHeatCanvas');
  if (!canvas) return;
  const W = canvas.parentElement ? canvas.parentElement.offsetWidth - 4 : 600;
  const H = 260;
  canvas.width = W; canvas.height = H;
  if (canvas.parentElement) canvas.parentElement.style.position = 'relative';
  const ctx = canvas.getContext('2d');

  // Background
  ctx.fillStyle = '#0b0b12';
  ctx.fillRect(0, 0, W, H);

  const strikes = (gex.nearby_strikes || gex.top_strikes || gex.ladder || [])
    .slice().sort((a,b) => a.strike - b.strike);

  if (!strikes.length) {
    ctx.fillStyle = '#444';
    ctx.font = '12px DM Sans,sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Heatmap available after first GEX compute', W/2, H/2);
    return;
  }

  const PAD_L = 12, PAD_R = 12, PAD_T = 30, PAD_B = 26;
  const chartW = W - PAD_L - PAD_R;
  const chartH = H - PAD_T - PAD_B;
  const midY   = PAD_T + chartH / 2;  // zero line

  // Find max absolute for scaling — use both call_gex and put_gex
  const maxCall = Math.max(...strikes.map(s => Math.abs(parseFloat(s.call_gex||0))), 0.01);
  const maxPut  = Math.max(...strikes.map(s => Math.abs(parseFloat(s.put_gex||0))), 0.01);
  const maxAbs  = Math.max(maxCall, maxPut);
  const halfH   = chartH / 2 - 4;

  // Pair width: 2 bars per strike
  const pairGap = 1;
  const totalPairs = strikes.length;
  const pairW  = Math.max(6, Math.floor((chartW - pairGap * totalPairs) / totalPairs));
  const barW   = Math.max(3, Math.floor(pairW / 2) - 1);

  // Grid lines
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  [0.25, 0.5, 0.75, 1.0].forEach(f => {
    const yUp = midY - halfH * f;
    const yDn = midY + halfH * f;
    ctx.beginPath(); ctx.moveTo(PAD_L, yUp); ctx.lineTo(W-PAD_R, yUp); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(PAD_L, yDn); ctx.lineTo(W-PAD_R, yDn); ctx.stroke();
  });

  // Zero line
  ctx.strokeStyle = 'rgba(255,255,255,0.18)';
  ctx.lineWidth = 1;
  ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(PAD_L, midY); ctx.lineTo(W-PAD_R, midY); ctx.stroke();
  ctx.setLineDash([]);

  strikes.forEach((s, i) => {
    const callV = Math.abs(parseFloat(s.call_gex || 0));
    const putV  = Math.abs(parseFloat(s.put_gex  || 0));
    const xBase = PAD_L + i * (pairW + pairGap);
    const xCall = xBase;
    const xPut  = xBase + barW + 1;

    const isCW   = s.strike === cw;
    const isPW   = s.strike === pw;
    const isZG   = zg && Math.abs(s.strike - zg) < 1;
    const isSpot = spx && Math.abs(s.strike - spx) < (spx * 0.003);

    // Call bar (green, upward from midY)
    if (callV > 0) {
      const callH = Math.max(2, (callV / maxAbs) * halfH);
      const grad = ctx.createLinearGradient(0, midY - callH, 0, midY);
      grad.addColorStop(0, isCW ? '#34d399' : '#10b981');
      grad.addColorStop(1, 'rgba(16,185,129,0.25)');
      ctx.fillStyle = grad;
      ctx.fillRect(xCall, midY - callH, barW, callH);
      // top cap
      ctx.fillStyle = isCW ? '#6ee7b7' : '#34d399';
      ctx.fillRect(xCall, midY - callH, barW, 2);
    }

    // Put bar (red, downward from midY)
    if (putV > 0) {
      const putH = Math.max(2, (putV / maxAbs) * halfH);
      const grad = ctx.createLinearGradient(0, midY, 0, midY + putH);
      grad.addColorStop(0, 'rgba(244,63,94,0.25)');
      grad.addColorStop(1, isPW ? '#fb7185' : '#f43f5e');
      ctx.fillStyle = grad;
      ctx.fillRect(xPut, midY, barW, putH);
      // bottom cap
      ctx.fillStyle = isPW ? '#fda4af' : '#fb7185';
      ctx.fillRect(xPut, midY + putH - 2, barW, 2);
    }

    // Key level vertical line
    if (isCW || isPW || isZG || isSpot) {
      ctx.save();
      ctx.setLineDash([3,3]);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = isSpot ? '#38bdf8' : isZG ? '#fbbf24' : isCW ? '#34d399' : '#fb7185';
      ctx.globalAlpha = 0.7;
      ctx.beginPath();
      ctx.moveTo(xBase + pairW/2, PAD_T);
      ctx.lineTo(xBase + pairW/2, H - PAD_B);
      ctx.stroke();
      ctx.restore();
    }

    // Strike label — show all if ≤20, else every Nth
    const step = Math.max(1, Math.ceil(strikes.length / 14));
    if (i % step === 0 || isCW || isPW || isZG) {
      ctx.fillStyle = isSpot ? '#38bdf8' : isCW || isPW || isZG ? '#fbbf24' : '#555';
      ctx.font = (isCW || isPW || isZG || isSpot ? 'bold ' : '') + '8px JetBrains Mono';
      ctx.textAlign = 'center';
      ctx.fillText(s.strike.toFixed(0), xBase + pairW/2, H - PAD_B + 13);
    }
  });

  // Spot price line
  if (spx) {
    const spotFrac = strikes.findIndex(s => s.strike >= spx);
    if (spotFrac >= 0) {
      const sIdx = spotFrac > 0 ? spotFrac - 0.5 : 0;
      const sX   = PAD_L + sIdx * (pairW + pairGap) + pairW/2;
      ctx.save();
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 2;
      ctx.setLineDash([]);
      ctx.globalAlpha = 0.9;
      ctx.beginPath(); ctx.moveTo(sX, PAD_T - 6); ctx.lineTo(sX, H - PAD_B); ctx.stroke();
      // SPOT label
      ctx.fillStyle = '#38bdf8';
      ctx.font = 'bold 8px JetBrains Mono';
      ctx.textAlign = 'center';
      ctx.fillText('▼ SPOT', sX, PAD_T - 8);
      ctx.restore();
    }
  }

  // Top legend
  ctx.font = 'bold 9px JetBrains Mono';
  ctx.textAlign = 'left';
  // green square + label
  ctx.fillStyle = '#10b981'; ctx.fillRect(PAD_L, 6, 10, 8);
  ctx.fillStyle = '#aaa';    ctx.fillText('CALL GEX ↑', PAD_L + 13, 14);
  // red square + label
  ctx.fillStyle = '#f43f5e'; ctx.fillRect(PAD_L + 90, 6, 10, 8);
  ctx.fillStyle = '#aaa';    ctx.fillText('PUT GEX ↓', PAD_L + 103, 14);
  // spot
  ctx.strokeStyle = '#38bdf8'; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(PAD_L+180, 10); ctx.lineTo(PAD_L+190, 10); ctx.stroke();
  ctx.fillStyle = '#aaa'; ctx.fillText('SPOT', PAD_L + 193, 14);
}

// -- Flow --------------------------------------------------
async function loadFlow() {
  const d = await get('/api/flow/summary');
  if (!d || d.error) {
    if ($('dpBody')) $('dpBody').innerHTML = '<div class="empty">Flow data unavailable</div>';
    return;
  }
  const tickers = Object.keys(d);

  // Dark Pool
  if ($('dpBody')) {
    $('dpBody').innerHTML = tickers.map(t => {
      const dp = d[t]?.dark_pool || {};
      const bias = dp.bias || dp.dark_pool_bias || 'NEUTRAL';
      const vol  = dp.volume || dp.dark_pool_volume || 0;
      const bc   = bias==='BULLISH'?'var(--green)':bias==='BEARISH'?'var(--red)':'var(--gold)';
      return '<div style="display:flex;justify-content:space-between;align-items:center;padding:7px 8px;background:var(--bg3);border-radius:6px;margin-bottom:4px">'
        + '<span class="mn" style="font-weight:700;font-size:13px">' + t + '</span>'
        + '<span style="font-size:11px;color:' + bc + ';font-weight:600">' + bias + '</span>'
        + '<span style="font-size:10px;color:var(--sub)">' + vol.toLocaleString() + ' shares</span>'
        + '</div>';
    }).join('') || '<div class="empty">No flow data</div>';
  }

  // Unusual Options Activity
  const uoaEl = $('uoaBody');
  if (uoaEl) {
    const uoaLines = [];
    tickers.forEach(t => {
      const uoa = d[t]?.uoa || {};
      const unusual = uoa.unusual || [];
      unusual.slice(0,3).forEach(u => {
        const col = u.type==='call'?'var(--green)':'var(--red)';
        uoaLines.push('<div style="display:flex;justify-content:space-between;padding:6px 8px;background:var(--bg3);border-radius:6px;margin-bottom:4px">'
          + '<span class="mn" style="font-size:12px;font-weight:700">' + t + ' ' + u.type?.toUpperCase() + ' $' + u.strike + '</span>'
          + '<span style="font-size:10px;color:' + col + ';font-weight:600">' + (u.contract||'') + '</span>'
          + '</div>');
      });
    });
    uoaEl.innerHTML = uoaLines.join('') || '<div class="empty">No unusual activity</div>';
  }

  // Smart Money Conviction
  const smEl = $('smartMoneyBody');
  if (smEl) {
    const smLines = tickers.map(t => {
      const dp = d[t]?.dark_pool || {};
      const score = dp.score || 0;
      const bias  = dp.bias || 'NEUTRAL';
      const bar   = Math.round(score);
      const col   = score>=60?'var(--green)':score<=40?'var(--red)':'var(--gold)';
      return '<div style="padding:8px;background:var(--bg3);border-radius:6px;margin-bottom:6px">'
        + '<div style="display:flex;justify-content:space-between;margin-bottom:4px">'
        + '<span class="mn" style="font-weight:700">' + t + '</span>'
        + '<span style="color:' + col + ';font-size:11px;font-weight:600">' + bias + ' ' + score + '%</span>'
        + '</div>'
        + '<div style="background:var(--bg2);border-radius:3px;height:4px">'
        + '<div style="background:' + col + ';width:' + bar + '%;height:4px;border-radius:3px"></div>'
        + '</div></div>';
    });
    smEl.innerHTML = smLines.join('') || '<div class="empty">No smart money data</div>';
  }
}

// -- Neural Pulse (Helx) -----------------------------------
async function loadHelx() {
  const [int2, px] = await Promise.all([
    getCached('/api/internals', 30000),
    getCached('/api/prices/live', 10000),
  ]);
  const pulse = int2?.neural_pulse || {};
  // Use real IDs from HTML
  const scoreEl = $('pulseScore');
  const labelEl = $('pulseLabel');
  const trendEl = $('pulseTrend');
  const thrEl   = $('arkaThreshold');
  const pulseItems = [
    ['Neural Pulse', (pulse.score||50).toFixed(1), pulse.score>=60?'var(--green)':pulse.score<=40?'var(--red)':'var(--gold)'],
    ['Regime',       pulse.label||pulse.regime||'MODERATE', 'var(--blue)'],
    ['Trend',        pulse.trending||pulse.momentum||'NEUTRAL', (pulse.trending||pulse.momentum)==='FALLING'?'var(--red)':(pulse.trending||pulse.momentum)==='RISING'?'var(--green)':'var(--gold)'],
  ].map(function(item) {
    return '<div class="panel"><div class="ph"><span class="pt">'+item[0]+'</span></div>'
      + '<div class="pb"><div class="mn" style="font-size:22px;font-weight:700;color:'+item[2]+'">'+item[1]+'</div></div>'
      + '</div>';
  }).join('');
  const internalsHtml = int2
    ? '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">'
      + [['VIX',int2.vix?.close,'var(--red)'],['TLT',int2.tlt?.close,'var(--blue)'],['GLD',int2.gld?.close,'var(--gold)']].map(function(x) {
          return '<div style="text-align:center;background:var(--bg3);border-radius:6px;padding:8px">'
            + '<div style="font-size:8px;color:var(--sub)">'+x[0]+'</div>'
            + '<div class="mn" style="font-size:16px;font-weight:700;color:'+x[2]+'">'+(x[1]||'--')+'</div>'
            + '</div>';
        }).join('')
      + '</div>'
    : '<div class="empty">--</div>';
  if (scoreEl) scoreEl.textContent = (pulse.score||50).toFixed(1);
  if (labelEl) { labelEl.textContent = pulse.label||'--'; labelEl.style.color = pulse.score>=60?'var(--green)':pulse.score<=40?'var(--red)':'var(--gold)'; }
  if (trendEl) trendEl.textContent = pulse.trending||'--';
  if (thrEl)   thrEl.textContent   = '—';
}

// -- Head (Weekly Outlook) ---------------------------------
async function loadHead() {
  buildSchedule();
  const gex = await get('/api/options/spx-levels');
  const el  = $('weeklyHead');
  if (!el) return;
  if (gex && !gex.error) {
    const spx = gex.spx_price||0, cw = gex.call_wall||gex.top_call_wall||0, pw = gex.put_wall||gex.top_put_wall||0;
    const regimeCol = (gex.regime||'').includes('NEGATIVE') ? 'var(--red)' : 'var(--green)';
    el.innerHTML = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin-bottom:14px">'
      + '<div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px;text-align:center"><div style="font-size:7px;color:var(--sub);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">SPX</div><div class="mn" style="font-size:16px;font-weight:700">' + parseFloat(spx).toLocaleString() + '</div></div>'
      + '<div style="background:rgba(255,61,90,0.06);border:1px solid rgba(255,61,90,0.2);border-radius:6px;padding:10px;text-align:center"><div style="font-size:7px;color:var(--red);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">Call Wall</div><div class="mn" style="font-size:16px;font-weight:700;color:var(--red)">' + parseFloat(cw).toLocaleString() + '</div></div>'
      + '<div style="background:rgba(0,208,132,0.06);border:1px solid rgba(0,208,132,0.2);border-radius:6px;padding:10px;text-align:center"><div style="font-size:7px;color:var(--green);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">Put Wall</div><div class="mn" style="font-size:16px;font-weight:700;color:var(--green)">' + parseFloat(pw).toLocaleString() + '</div></div>'
      + '<div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px;text-align:center"><div style="font-size:7px;color:var(--sub);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">GEX Regime</div><div class="mn" style="font-size:13px;font-weight:700;color:' + regimeCol + '">' + (gex.regime||'--') + '</div></div>'
      + '</div>'
      + '<div style="font-size:10px;color:var(--sub);line-height:1.6">Range: <strong style="color:var(--text)">' + parseFloat(pw).toLocaleString() + ' -- ' + parseFloat(cw).toLocaleString() + '</strong> | To call: <strong style="color:var(--red)">' + (cw-spx).toFixed(0) + ' pts</strong> | To put: <strong style="color:var(--green)">' + (spx-pw).toFixed(0) + ' pts</strong></div>';
  } else {
    el.innerHTML = '<div class="empty">Weekly outlook requires GEX data — available during market hours</div>';
  }
}

function buildSchedule() {
  const el = $('scheduleTimeline'); if (!el) return;
  const now = new Date();
  const et  = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const curMins = et.getHours()*60 + et.getMinutes();
  const schedule = [
    { time:'8:00 AM',  mins:480,  title:'Morning Analysis',       desc:'ARJUN signal generation · pre-market gap scan',        color:'var(--blue)' },
    { time:'9:25 AM',  mins:565,  title:'Opening Bell Prep',       desc:'Final checks · GEX levels · risk mode confirmed',     color:'var(--gold)' },
    { time:'9:30 AM',  mins:570,  title:'Market Opens',            desc:'ARKA live · ORB formation · first 30min filter',      color:'var(--green)' },
    { time:'10:00 AM', mins:600,  title:'Silver Bullet Window',    desc:'Prime trading hours · highest quality setups',        color:'var(--green)' },
    { time:'12:00 PM', mins:720,  title:'Lunch Chop',              desc:'Lower quality signals · ARKA blocks automatically',   color:'var(--gold)' },
    { time:'1:30 PM',  mins:810,  title:'PM Session',              desc:'Afternoon setups resume · full conviction required',  color:'var(--blue)' },
    { time:'3:30 PM',  mins:930,  title:'⚡ Power Hour / Lottos',  desc:'50% threshold · ARKA unleashed · aggressive',        color:'var(--purple)' },
    { time:'3:58 PM',  mins:958,  title:'Auto-Close All',          desc:'All 0DTE positions closed automatically',             color:'var(--red)' },
    { time:'4:05 PM',  mins:965,  title:'Daily Recap',             desc:'Performance summary posted to Discord',               color:'var(--sub)' },
  ];
  el.innerHTML = schedule.map((s, i) => {
    const isPast = curMins > s.mins + 15;
    const isNow  = curMins >= s.mins && (i===schedule.length-1 || curMins < schedule[i+1].mins);
    const nowBadge = isNow ? ' <span style="font-size:8px;color:var(--blue);letter-spacing:1px">NOW</span>' : '';
    return '<div class="tl-row ' + (isNow?'active-now':'') + ' ' + (isPast?'past':'') + '">'
      + '<div class="tl-time">' + s.time + '</div>'
      + '<div class="tl-dot" style="background:' + s.color + '"></div>'
      + '<div class="tl-title">' + s.title + nowBadge + '</div>'
      + '<div class="tl-desc">' + s.desc + '</div>'
      + '</div>';
  }).join('');
}

// -- ARJUN Path Prediction for GEX tab ---------------------
async function _renderArjunPath(gex, spx, cw, pw) {
  var el = $('gexArjunPath'); if (!el) return;
  el.innerHTML = '<div class="ld"><div class="spin"></div></div>';

  // Get latest ARJUN signal for the active GEX ticker
  var ticker = (_gexTicker||'SPX').replace('^','');
  var sigs   = await getCached('/api/signals', 120000) || [];
  var sig    = sigs.find(function(s){ return (s.ticker||'').toUpperCase() === ticker.toUpperCase(); });

  var netGex = parseFloat(gex.net_gex)*1e9 || parseFloat(gex.net_total_gex || 0);
  var isPos  = netGex >= 0;
  var roomUp = cw - spx, roomDn = spx - pw;
  var bias   = roomUp < roomDn ? 'PUT WALL closer — downside bias' : 'CALL WALL closer — upside bias';

  // ARJUN signal
  var arjunSignal = sig ? (sig.signal||'HOLD').toUpperCase() : 'NO DATA';
  var arjunConf   = sig ? parseFloat(sig.confidence||50).toFixed(1) : '—';
  var sigCol = arjunSignal==='BUY'?'var(--green)':arjunSignal==='SELL'?'var(--red)':'var(--gold)';

  // Combined path: ARJUN + GEX
  var path, pathCol, pathIcon;
  if (arjunSignal==='BUY' && isPos) {
    path='BULLISH — ARJUN BUY + Positive GEX'; pathCol='var(--green)'; pathIcon='↑↑';
  } else if (arjunSignal==='SELL' && !isPos) {
    path='BEARISH — ARJUN SELL + Negative GEX'; pathCol='var(--red)'; pathIcon='↓↓';
  } else if (arjunSignal==='BUY') {
    path='CAUTIOUS BULL — ARJUN BUY, GEX dampening'; pathCol='rgba(0,208,132,0.7)'; pathIcon='↑';
  } else if (arjunSignal==='SELL') {
    path='CAUTIOUS BEAR — ARJUN SELL, GEX dampening'; pathCol='rgba(255,61,90,0.7)'; pathIcon='↓';
  } else {
    path='NEUTRAL — No strong directional signal'; pathCol='var(--gold)'; pathIcon='→';
  }

  el.innerHTML = '<div style="text-align:center;padding:8px 0 12px">'
    + '<div style="font-size:32px;margin-bottom:6px">'+pathIcon+'</div>'
    + '<div style="font-size:12px;font-weight:700;color:'+pathCol+'">'+path+'</div>'
    + '</div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">'
    + '<div style="background:var(--bg3);border-radius:6px;padding:8px;text-align:center">'
    + '<div style="font-size:7px;color:var(--sub);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px">ARJUN Signal</div>'
    + '<div style="font-size:14px;font-weight:700;color:'+sigCol+'">'+arjunSignal+'</div>'
    + '<div style="font-size:9px;color:var(--sub)">Conf: '+arjunConf+'%</div>'
    + '</div>'
    + '<div style="background:var(--bg3);border-radius:6px;padding:8px;text-align:center">'
    + '<div style="font-size:7px;color:var(--sub);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px">GEX Regime</div>'
    + '<div style="font-size:14px;font-weight:700;color:'+(isPos?'var(--green)':'var(--red)')+'">'+(isPos?'POSITIVE':'NEGATIVE')+'</div>'
    + '<div style="font-size:9px;color:var(--sub)">'+(netGex>=0?'+':'')+netGex.toFixed(1)+'B</div>'
    + '</div>'
    + '</div>'
    + '<div style="font-size:9px;color:var(--sub);padding:6px 8px;background:var(--bg3);border-radius:6px">'
    + bias + ' | Up to CW: <strong style="color:var(--red)">'+roomUp.toFixed(0)+'pts</strong>'
    + ' | Down to PW: <strong style="color:var(--green)">'+roomDn.toFixed(0)+'pts</strong>'
    + '</div>';
}

// ── Manifold Modifier Panel (physics tab + GEX tab) ──────────────────────────
async function loadManifoldModPanel(ticker) {
  ticker = ticker || (typeof window !== 'undefined' && window._manifoldActiveTicker) || sessionStorage.getItem('manifoldTicker') || 'SPY';
  try {
    const d = await fetch(`/api/analysis/manifold?ticker=${ticker}`).then(r => r.json());
    if (d.error) return;

    // flatten regime — API may return dict or string
    const _r      = d.regime || {};
    const regime  = typeof _r === 'string' ? _r :
                    (_r.regime || _r.label || _r.state || JSON.stringify(_r)).replace(/[{}"]/g,'').trim() || 'UNKNOWN';
    const phase   = d.phase_state  || 'UNKNOWN';
    const mod     = d.manifold_mod || 0;
    const gex     = d.gex_regime   || 'UNKNOWN';
    const bars    = d.bars_used    || 0;
    const ricci   = Array.isArray(d.ricci_flow) ? d.ricci_flow : [];
    const avgR    = ricci.length ? (ricci.reduce((a,b)=>a+b,0)/ricci.length).toFixed(3) : '—';

    // Fix: regime dict may carry arjun_score_modifier; prefer it over top-level manifold_mod
    const regimeDict = (typeof d.regime === 'object' && d.regime !== null) ? d.regime : {};
    const effectiveMod = mod || regimeDict.arjun_score_modifier || 0;
    const meanCurv   = parseFloat(regimeDict.mean_curvature || avgR || 0);
    const maxSpike   = parseFloat(regimeDict.max_spike || 0);

    const modCol  = effectiveMod > 3 ? 'var(--green)' : effectiveMod < -3 ? 'var(--red)' : 'var(--gold)';
    const gexCol  = gex.includes('NEG') ? 'var(--red)' : gex.includes('POS') ? 'var(--green)' : 'var(--sub)';
    const regCol  = regime.includes('SMOOTH') || regime === 'TRENDING' ? 'var(--green)'
                  : regime === 'REVERTING' || regime.includes('CHOP') ? 'var(--red)' : 'var(--gold)';

    // Plain English summary for the regime
    const regimePlain = {
      SMOOTH_TREND:  'Price is moving in a clean direction — good for trend trades',
      TRENDING:      'Steady momentum — follow the direction',
      REVERTING:     'Price keeps snapping back to center — fade the extremes',
      VOLATILE:      'Choppy, unpredictable — reduce size and be patient',
      CHOPPY:        'No clear direction — best to wait',
      NEUTRAL:       'No strong signal right now',
      EXPANSION:     'Volatility expanding — breakout conditions forming',
    };
    const regDesc = regimePlain[regime] || 'Analyzing price structure…';

    // Curvature plain English
    const curvLabel = meanCurv < 0.4 ? '✅ Smooth (trend intact)'
                    : meanCurv < 0.7 ? '〽️ Moderate (stay alert)'
                    : '⚠️ Curved (reversal risk)';
    const curvCol   = meanCurv < 0.4 ? 'var(--green)' : meanCurv < 0.7 ? 'var(--gold)' : 'var(--red)';

    // ARKA boost plain English
    const modLabel  = effectiveMod > 5 ? `+${effectiveMod} boost (strong setup)`
                    : effectiveMod > 0 ? `+${effectiveMod} small boost`
                    : effectiveMod < -5 ? `${effectiveMod} penalty (avoid trading)`
                    : effectiveMod < 0  ? `${effectiveMod} slight penalty`
                    : '0 (no adjustment)';

    const html = `
      <div style="grid-column:1/-1;background:${regCol}12;border:1px solid ${regCol}30;border-radius:6px;padding:8px 12px;margin-bottom:4px">
        <div style="font-size:8px;letter-spacing:1px;color:${regCol};font-weight:700;text-transform:uppercase;margin-bottom:3px">What the manifold says</div>
        <div style="font-size:11px;color:var(--text);font-weight:600">${regDesc}</div>
      </div>
      ${_mCard2('Market Pattern', regime.replace(/_/g,' '), regCol, 'How price is behaving')}
      ${_mCard2('Path Smoothness', curvLabel, curvCol, meanCurv > 0 ? `avg curvature ${meanCurv.toFixed(2)}` : '—')}
      ${_mCard2('ARKA Boost', modLabel, modCol, 'Adjusts trading conviction')}
      ${_mCard2('Trend Score', bars + ' bars', 'var(--sub)', 'Amount of data analyzed')}
    `;

    const el1 = document.getElementById('manifoldModBody');
    const el2 = document.getElementById('gexManifoldBody');
    if (el1) el1.innerHTML = html;
    if (el2) el2.innerHTML = html;

    // Feed live Isomap points into 3D canvas renderer and redraw both canvases
    window._manifoldPoints       = Array.isArray(d.points) ? d.points : [];
    window._manifoldRicci        = Array.isArray(d.ricci_flow) ? d.ricci_flow : [];
    window._manifoldRegime       = regime;
    window._manifoldActiveTicker = ticker;

    // Start loop if not running, then immediately redraw Phase Space canvas
    if (typeof _startManifold3d === 'function') {
      // resetManifold3d resets t=0 and unpauses; _startManifold3d restarts the loop
      if (typeof resetManifold3d === 'function') resetManifold3d();
      _startManifold3d();
    }
    const _mc = document.getElementById('manifold3dCanvas');
    if (_mc && typeof _drawManifold3d === 'function') _drawManifold3d(_mc, 0);

    // Redraw Ricci canvas
    if (typeof _drawRicci3d === 'function') _drawRicci3d();

    // Update status chip
    const chip = document.getElementById('viz3dStatus');
    if (chip) { chip.textContent = ticker + ' · ' + regime.replace(/_/g,' '); chip.style.color = regCol; }

  } catch(e) { console.warn('manifold panel:', e); }
}

function _mCard(label, value, color) {
  return `<div style="background:var(--bg3);border-radius:6px;padding:8px 10px;text-align:center">
    <div style="font-size:9px;color:var(--sub);margin-bottom:4px">${label}</div>
    <div style="font-size:13px;font-weight:700;color:${color}">${value}</div>
  </div>`;
}

function _mCard2(label, value, color, sub) {
  return `<div style="background:var(--bg3);border-radius:6px;padding:8px 10px;text-align:center">
    <div style="font-size:9px;color:var(--sub);margin-bottom:4px">${label}</div>
    <div style="font-size:13px;font-weight:700;color:${color}">${value}</div>
    <div style="font-size:8px;color:var(--sub);margin-top:3px;line-height:1.3">${sub}</div>
  </div>`;
}

// ── Manifold ticker selector ──────────────────────────────────────────────
function selectManifoldTicker(t) {
  window._manifoldActiveTicker = t;
  sessionStorage.setItem('manifoldTicker', t);
  // Update button styles
  ['SPY','QQQ','SPX','IWM','DIA','GLD','SLV'].forEach(tk => {
    const btn = document.getElementById('mfBtn_' + tk);
    if (!btn) return;
    btn.style.background = tk === t ? 'var(--blue)' : 'var(--bg3)';
    btn.style.color      = tk === t ? '#fff'        : 'var(--sub)';
  });
  // Update status chip
  const chip = document.getElementById('viz3dStatus');
  if (chip) chip.textContent = t;
  // Reload manifold panel + reset 3D canvas
  loadManifoldModPanel(t);
  if (typeof resetManifold3d === 'function') resetManifold3d();
}

// Call on analysis tab load
if (typeof window !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => {
    const _initTicker = sessionStorage.getItem('manifoldTicker') || 'SPY';
    loadManifoldModPanel(_initTicker);
    // Also refresh every 5 minutes
    setInterval(() => {
      const t = sessionStorage.getItem('manifoldTicker') || 'SPY';
      loadManifoldModPanel(t);
    }, 300_000);
  });
}

// ── Execution Gates ───────────────────────────────────────────────────────
async function loadExecutionGates() {
  const d = await getCached('/api/execution-gates', 20000);
  const el = $('gatesBody'); if (!el) return;
  if (!d || d.error) { el.innerHTML = '<div class="empty">Gates unavailable</div>'; return; }
  const gates = d.gates || {};
  const overall = d.overall || 'UNKNOWN';
  const ocol = overall==='GO'?'var(--green)':overall==='CAUTION'?'var(--gold)':'var(--red)';
  // Update the chip in the panel header
  const overallChip = $('gatesOverall');
  if (overallChip) { overallChip.textContent = (d.overall_icon||'') + ' ' + overall; overallChip.style.color = ocol; }
  el.innerHTML = ''
    + Object.entries(gates).map(([k, g]) => {
        const col = g.pass ? 'var(--green)' : 'var(--red)';
        const icon = g.pass ? '✅' : '❌';
        return '<div style="display:flex;justify-content:space-between;padding:6px 8px;background:var(--bg3);border-radius:6px;margin-bottom:4px">'
          + '<span style="font-size:11px;color:var(--sub)">' + (g.label||k) + '</span>'
          + '<span style="font-size:11px;color:' + col + ';font-weight:600">' + icon + ' ' + (g.value||'--') + '</span>'
          + '</div>';
      }).join('');
}

// ── ARKA Modifier ─────────────────────────────────────────────────────────
async function loadArkaModifier() {
  const d = await getCached('/api/command-center', 30000);
  const el = $('intMod'); if (!el) return;
  if (!d || d.error) { el.innerHTML = '<div class="empty">ARKA data unavailable</div>'; return; }
  const arka = d.arka || {};
  const sigs = (d.signals || []).slice(0, 3);
  el.innerHTML = '<div style="margin-bottom:8px">'
    + '<div style="display:flex;justify-content:space-between;padding:6px 8px;background:var(--bg3);border-radius:6px;margin-bottom:4px">'
    + '<span style="font-size:11px;color:var(--sub)">Session</span>'
    + '<span style="font-size:11px;color:var(--blue);font-weight:600">' + (arka.session||'--') + '</span>'
    + '</div>'
    + '<div style="display:flex;justify-content:space-between;padding:6px 8px;background:var(--bg3);border-radius:6px">'
    + '<span style="font-size:11px;color:var(--sub)">Regime</span>'
    + '<span style="font-size:11px;color:var(--gold);font-weight:600">' + (arka.regime||'--') + '</span>'
    + '</div></div>'
    + (sigs.length ? '<div style="font-size:9px;color:var(--sub);margin:6px 0 4px">LATEST SIGNALS</div>'
      + sigs.map(s => {
          const col = s.signal==='BUY'?'var(--green)':s.signal==='SELL'?'var(--red)':'var(--sub)';
          return '<div style="display:flex;justify-content:space-between;padding:5px 8px;background:var(--bg3);border-radius:6px;margin-bottom:3px">'
            + '<span class="mn" style="font-size:12px;font-weight:700">' + (s.ticker||'--') + '</span>'
            + '<span style="font-size:11px;color:' + col + ';font-weight:600">' + (s.signal||'HOLD') + ' ' + parseFloat(s.confidence||0).toFixed(0) + '</span>'
            + '</div>';
        }).join('') : '<div class="empty">No signals yet</div>');
}

// ── Market Signals ────────────────────────────────────────────────────────
async function loadMarketSignals() {
  const d = await getCached('/api/command-center', 30000);
  const el = $('helxSignals'); if (!el) return;
  if (!d) { el.innerHTML = '<div class="empty">No data</div>'; return; }
  const sigs = d.signals || [];
  el.innerHTML = sigs.length ? sigs.slice(0,5).map(s => {
    const col = s.signal==='BUY'?'var(--green)':s.signal==='SELL'?'var(--red)':'var(--sub)';
    const rr  = s.reward && s.risk ? (s.reward/s.risk).toFixed(1) : '--';
    return '<div style="padding:7px 8px;background:var(--bg3);border-radius:6px;margin-bottom:4px">'
      + '<div style="display:flex;justify-content:space-between">'
      + '<span class="mn" style="font-weight:700">' + s.ticker + '</span>'
      + '<span style="color:' + col + ';font-size:11px;font-weight:700">' + s.signal + ' ' + parseFloat(s.confidence||0).toFixed(0) + '</span>'
      + '</div>'
      + '<div style="font-size:9px;color:var(--sub)">Entry ' + parseFloat(s.entry||0).toFixed(2) + ' → Target ' + parseFloat(s.target||0).toFixed(2) + ' | R:R ' + rr + '</div>'
      + '</div>';
  }).join('') : '<div class="empty">No signals today</div>';
}

// ── Internals Cards (intCards grid) ──────────────────────────────────────
async function loadInternalsCards() {
  const el = $('intCards'); if (!el) return;
  const d = await getCached('/api/internals', 30000);
  if (!d) { el.innerHTML = '<div class="empty" style="grid-column:1/-1">No internals data</div>'; return; }
  const vix = d.vix || {}; const tlt = d.tlt || {}; const gld = d.gld || {};
  const ratio = d.spy_qqq_ratio || {};
  const cards = [
    ['VIX',  vix.close||'--',  vix.change_pct||0,  'var(--red)'],
    ['TLT',  tlt.close||'--',  tlt.change_pct||0,  'var(--blue)'],
    ['GLD',  gld.close||'--',  gld.change_pct||0,  'var(--gold)'],
    ['SPY/QQQ', (ratio.ratio||'--'), 0, 'var(--accent)'],
  ];
  el.innerHTML = cards.map(([label, val, chg, col]) => {
    const chgCol = chg > 0 ? 'var(--green)' : chg < 0 ? 'var(--red)' : 'var(--sub)';
    return `<div class="panel"><div class="ph"><span class="pt">${label}</span></div>
      <div class="pb" style="text-align:center">
        <div class="mn" style="font-size:22px;font-weight:700;color:${col}">${typeof val==='number'?val.toFixed(2):val}</div>
        ${chg ? `<div style="font-size:10px;color:${chgCol}">${chg>0?'+':''}${typeof chg==='number'?chg.toFixed(2):''}%</div>` : ''}
      </div></div>`;
  }).join('');
}
// ── Market Briefing (Pre + Post) ────────────────────────────────────────────
let _briefingMode = 'pre';

function switchBriefingMode(mode) {
  _briefingMode = mode;
  document.querySelectorAll('.mbriefing-btn').forEach(b => b.classList.remove('active-brief'));
  const btn = document.getElementById('mbtn-' + mode);
  if (btn) btn.classList.add('active-brief');
  loadMarketBriefing(true);
}

async function loadMarketBriefing(force) {
  const narrative = $('mbriefingNarrative');
  const dateEl    = $('mbriefingDate');
  if (!narrative) return;

  narrative.innerHTML = '<div class="ld"><div class="spin"></div>Fetching market data…</div>';

  try {
    // Parallel fetch: briefing + sectors for breadth
    const [data, sectorSnap] = await Promise.all([
      fetch('/api/market/briefing?mode=' + _briefingMode).then(r => r.json()),
      getCached('/api/sectors/snapshot', 60000).catch(() => null),
    ]);
    if (data.error) throw new Error(data.error);

    if (dateEl) dateEl.textContent = (data.date || '') + ' · ' + (data.generated || '');

    const allItems = [
      ...(data.markets?.US        || []),
      ...(data.markets?.Macro     || []),
      ...(data.markets?.Sentiment || []),
    ];
    const bySymbol = {};
    allItems.forEach(it => { bySymbol[it.symbol] = it; });

    const fmtPx  = v => v ? v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
    const fmtPct = v => (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
    const colOf  = v => v > 0 ? '#10b981' : v < 0 ? '#f43f5e' : '#6b7280';
    const arrowOf= v => v > 0.05 ? '▲' : v < -0.05 ? '▼' : '▶';

    // ── INDEX SCOREBOARD ────────────────────────────────────
    const indexSyms = ['SPY','QQQ','IWM','DIA'];
    const indexLabels = {SPY:'S&P 500',QQQ:'Nasdaq 100',IWM:'Russell 2000',DIA:'Dow Jones'};
    const indexRow = $('mktIndexRow');
    if (indexRow) {
      indexRow.innerHTML = indexSyms.map(sym => {
        const it  = bySymbol[sym] || {};
        const chg = parseFloat(it.change_pct || 0);
        const px  = parseFloat(it.price || 0);
        const col = colOf(chg);
        const bg  = chg > 0.2 ? 'rgba(16,185,129,0.06)' : chg < -0.2 ? 'rgba(244,63,94,0.06)' : 'var(--bg2)';
        const bord= chg > 0.2 ? 'rgba(16,185,129,0.25)' : chg < -0.2 ? 'rgba(244,63,94,0.25)' : 'var(--border)';
        // Bar fill: 0% = -3%, 50% = flat, 100% = +3%
        const barPct = Math.round(Math.min(100, Math.max(0, (chg + 3) / 6 * 100)));
        const barCol = chg >= 0 ? '#10b981' : '#f43f5e';
        return `<div style="background:${bg};border:1px solid ${bord};border-radius:10px;padding:14px 16px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
            <div>
              <div style="font-size:13px;font-weight:800;font-family:JetBrains Mono,monospace">${sym}</div>
              <div style="font-size:9px;color:var(--sub);margin-top:1px">${indexLabels[sym]||''}</div>
            </div>
            <span style="font-size:9px;font-weight:700;padding:2px 7px;border-radius:3px;background:${col}18;color:${col};border:1px solid ${col}33">
              ${arrowOf(chg)} ${fmtPct(chg)}
            </span>
          </div>
          <div style="font-size:22px;font-weight:800;font-family:JetBrains Mono,monospace;color:var(--fg);margin-bottom:10px">
            $${fmtPx(px)}
          </div>
          <div style="height:4px;background:var(--bg3);border-radius:2px;overflow:hidden">
            <div style="width:${barPct}%;height:100%;background:${barCol};border-radius:2px;transition:width 0.4s ease"></div>
          </div>
          <div style="display:flex;justify-content:space-between;margin-top:4px">
            <span style="font-size:7px;color:var(--sub)">BEAR</span>
            <span style="font-size:7px;color:var(--sub)">FLAT</span>
            <span style="font-size:7px;color:var(--sub)">BULL</span>
          </div>
        </div>`;
      }).join('');
    }

    // ── MACRO LIST ───────────────────────────────────────────
    const macroSyms = [
      {sym:'GLD',  icon:'🥇', label:'Gold'},
      {sym:'USO',  icon:'🛢',  label:'Oil (USO)'},
      {sym:'TLT',  icon:'📉', label:'10Y Bond (TLT)'},
      {sym:'UUP',  icon:'💵', label:'USD Index'},
    ];
    const macroEl = $('mktMacroList');
    if (macroEl) {
      macroEl.innerHTML = macroSyms.map(({sym, icon, label}) => {
        const it  = bySymbol[sym] || {};
        const chg = parseFloat(it.change_pct || 0);
        const px  = parseFloat(it.price || 0);
        const col = colOf(chg);
        return `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
          <span style="font-size:14px;width:20px;text-align:center">${icon}</span>
          <div style="flex:1">
            <div style="font-size:10px;font-weight:600">${label}</div>
            <div style="font-size:9px;color:var(--sub)">${sym}</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:11px;font-weight:700;font-family:JetBrains Mono,monospace">$${fmtPx(px)}</div>
            <div style="font-size:9px;font-weight:700;color:${col}">${arrowOf(chg)} ${fmtPct(chg)}</div>
          </div>
        </div>`;
      }).join('');
      if ($('mktMacroTs')) $('mktMacroTs').textContent = data.date || '—';
    }

    // ── SENTIMENT ────────────────────────────────────────────
    const sentSyms = [
      {sym:'VIXY', icon:'🌡', label:'VIX ETF',    note:'Vol spike = fear'},
      {sym:'TQQQ', icon:'🐂', label:'Bull 3x',    note:'Risk-on signal'},
      {sym:'SQQQ', icon:'🐻', label:'Bear 3x',    note:'Risk-off signal'},
    ];
    const vixChg   = parseFloat(bySymbol['VIXY']?.change_pct || 0);
    const tqqqChg  = parseFloat(bySymbol['TQQQ']?.change_pct || 0);
    const sqqqChg  = parseFloat(bySymbol['SQQQ']?.change_pct || 0);
    const sentBias = (sqqqChg > 0.5 || vixChg > 2)
      ? 'RISK-OFF' : (tqqqChg > 0.5 && vixChg < 0)
      ? 'RISK-ON'  : 'NEUTRAL';
    const sentCol  = sentBias === 'RISK-ON' ? '#10b981' : sentBias === 'RISK-OFF' ? '#f43f5e' : '#f59e0b';
    if ($('mktSentBadge')) {
      $('mktSentBadge').textContent = sentBias;
      $('mktSentBadge').style.color = sentCol;
    }
    const sentEl = $('mktSentList');
    if (sentEl) {
      sentEl.innerHTML = sentSyms.map(({sym, icon, label, note}) => {
        const it  = bySymbol[sym] || {};
        const chg = parseFloat(it.change_pct || 0);
        const col = colOf(sym === 'SQQQ' ? -chg : chg); // SQQQ up = bearish
        return `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
          <span style="font-size:14px;width:20px;text-align:center">${icon}</span>
          <div style="flex:1">
            <div style="font-size:10px;font-weight:600">${label}</div>
            <div style="font-size:8px;color:var(--sub)">${note}</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:11px;font-weight:700;font-family:JetBrains Mono,monospace;color:${colOf(chg)}">${arrowOf(chg)} ${fmtPct(chg)}</div>
          </div>
        </div>`;
      }).join('');
    }

    // ── VOLATILITY GAUGE ──────────────────────────────────────
    const volEl = $('mktVolPanel');
    if (volEl) {
      const vixyPx  = parseFloat(bySymbol['VIXY']?.price || 0);
      // VIXY ~$20 = VIX ~20, proxy: VIX ≈ vixyPx * 0.9 roughly
      const vixEst  = vixyPx ? (vixyPx * 0.9).toFixed(1) : '—';
      const vixLevel= vixyPx < 14 ? 'COMPLACENT' : vixyPx < 20 ? 'CALM' : vixyPx < 28 ? 'ELEVATED' : 'FEARFUL';
      const vixCol  = vixyPx < 14 ? '#10b981' : vixyPx < 20 ? '#f59e0b' : vixyPx < 28 ? '#f97316' : '#f43f5e';
      const vixPct  = Math.min(100, Math.max(0, (vixyPx / 60) * 100));
      const rkCols  = ['#10b981','#f59e0b','#f97316','#f43f5e'];
      const rkLabels= ['CALM','ELEVATED','HIGH','FEAR'];
      const rkIdx   = vixyPx < 16 ? 0 : vixyPx < 24 ? 1 : vixyPx < 36 ? 2 : 3;
      volEl.innerHTML = `
        <div style="margin-bottom:14px">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
            <div style="font-size:9px;color:var(--sub);letter-spacing:1px">VIX PROXY (VIXY)</div>
            <div style="font-size:22px;font-weight:800;font-family:JetBrains Mono,monospace;color:${vixCol}">~${vixEst}</div>
          </div>
          <div style="height:8px;background:var(--bg3);border-radius:4px;overflow:hidden;margin-bottom:4px">
            <div style="width:${vixPct.toFixed(1)}%;height:100%;background:linear-gradient(90deg,#10b981,#f59e0b,#f97316,#f43f5e);border-radius:4px;transition:width 0.5s"></div>
          </div>
          <div style="display:flex;justify-content:space-between">
            ${rkLabels.map((l,i) => `<span style="font-size:7px;font-weight:${i===rkIdx?'800':'400'};color:${i===rkIdx?rkCols[i]:'#444'}">${l}</span>`).join('')}
          </div>
        </div>
        <div style="background:${vixCol}12;border:1px solid ${vixCol}30;border-radius:6px;padding:8px 10px;text-align:center">
          <div style="font-size:11px;font-weight:800;color:${vixCol};letter-spacing:1px">${vixLevel}</div>
          <div style="font-size:8px;color:var(--sub);margin-top:3px">
            ${vixLevel==='COMPLACENT'?'Premium sellers favored — spreads tight'
             :vixLevel==='CALM'?'Normal conditions — standard sizing'
             :vixLevel==='ELEVATED'?'Options expensive — reduce contract size'
             :'Extreme fear — directional only, tight stops'}
          </div>
        </div>
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border)">
          <div style="font-size:8px;color:var(--sub);margin-bottom:6px;letter-spacing:1px;text-transform:uppercase">VIXY Change</div>
          <div style="font-size:13px;font-weight:700;font-family:JetBrains Mono,monospace;color:${colOf(vixChg)}">${arrowOf(vixChg)} ${fmtPct(vixChg)}</div>
        </div>`;
    }

    // ── NARRATIVE ────────────────────────────────────────────
    const isPre  = _briefingMode === 'pre';
    const titleEl= $('mktNarrativeTitle');
    if (titleEl) titleEl.textContent = isPre ? '🌅 Pre-Market Briefing' : '🌆 Post-Market Debrief';
    const badgeEl= $('mktBriefBadge');
    if (badgeEl) badgeEl.textContent = data.date || '—';

    const paras = (data.narrative || '').split('\n\n').filter(Boolean);
    narrative.innerHTML = paras.length
      ? paras.map((p, i) => `<p style="font-size:13px;line-height:1.85;color:var(--fg);
          ${i < paras.length-1 ? 'margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid var(--border)' : 'margin:0'}">${p}</p>`
        ).join('')
      : '<div style="color:var(--sub);font-size:12px;padding:10px">No briefing data available for this session.</div>';

    // ── SECTOR BREADTH ────────────────────────────────────────
    const breadthEl = $('mktBreadthRow');
    if (breadthEl && sectorSnap) {
      const SECTOR_LIST = ['XLE','XLU','XLK','XLP','XLRE','XLC','XLB','XLV','XLY','XLI','XLF'];
      const NAMES = {XLE:'Energy',XLU:'Utilities',XLK:'Tech',XLP:'Staples',XLRE:'Real Estate',
                     XLC:'Comm Svcs',XLB:'Materials',XLV:'Healthcare',XLY:'Cons Discr',XLI:'Industrials',XLF:'Financials'};
      const spyPct = parseFloat(sectorSnap['SPY']?.chg_pct || 0);
      const rows = SECTOR_LIST.map(sym => {
        const d   = sectorSnap[sym] || {};
        const pct = parseFloat(d.chg_pct || 0);
        const alpha = pct - spyPct;
        return {sym, name: NAMES[sym]||sym, pct, alpha};
      }).sort((a,b) => b.alpha - a.alpha);
      const leaders = rows.filter(r => r.alpha > 0.1);
      const laggers = rows.filter(r => r.alpha < -0.1);
      const neutrl  = rows.filter(r => Math.abs(r.alpha) <= 0.1);
      const lPct    = Math.round(leaders.length / SECTOR_LIST.length * 100);
      if ($('mktBreadthBadge')) {
        $('mktBreadthBadge').textContent = `${leaders.length} up · ${laggers.length} down`;
        $('mktBreadthBadge').style.color = leaders.length >= 7 ? '#10b981' : leaders.length <= 4 ? '#f43f5e' : '#f59e0b';
      }
      breadthEl.innerHTML = `
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:10px">
          <div>
            <div style="font-size:8px;color:var(--sub);letter-spacing:1px;text-transform:uppercase;margin-bottom:6px">Outperforming SPY (α>0)</div>
            ${leaders.slice(0,5).map(r => `
              <div style="display:flex;align-items:center;gap:8px;padding:5px 8px;background:rgba(16,185,129,0.05);border-radius:5px;margin-bottom:4px">
                <span style="font-size:10px;font-weight:700;font-family:JetBrains Mono,monospace;min-width:34px">${r.sym}</span>
                <span style="font-size:9px;color:var(--sub);flex:1">${r.name}</span>
                <span style="font-size:9px;font-weight:700;font-family:JetBrains Mono,monospace;color:${colOf(r.pct)}">${fmtPct(r.pct)}</span>
                <span style="font-size:8px;color:#10b981">α+${r.alpha.toFixed(1)}%</span>
              </div>`).join('')}
          </div>
          <div>
            <div style="font-size:8px;color:var(--sub);letter-spacing:1px;text-transform:uppercase;margin-bottom:6px">Underperforming SPY (α<0)</div>
            ${laggers.slice(0,5).map(r => `
              <div style="display:flex;align-items:center;gap:8px;padding:5px 8px;background:rgba(244,63,94,0.05);border-radius:5px;margin-bottom:4px">
                <span style="font-size:10px;font-weight:700;font-family:JetBrains Mono,monospace;min-width:34px">${r.sym}</span>
                <span style="font-size:9px;color:var(--sub);flex:1">${r.name}</span>
                <span style="font-size:9px;font-weight:700;font-family:JetBrains Mono,monospace;color:${colOf(r.pct)}">${fmtPct(r.pct)}</span>
                <span style="font-size:8px;color:#f43f5e">α${r.alpha.toFixed(1)}%</span>
              </div>`).join('')}
          </div>
        </div>
        <div style="background:var(--bg3);border-radius:6px;padding:6px 10px;display:flex;align-items:center;gap:8px">
          <div style="height:10px;flex:1;background:var(--bg2);border-radius:5px;overflow:hidden">
            <div style="width:${lPct}%;height:100%;background:linear-gradient(90deg,#10b981,#34d399);border-radius:5px;transition:width 0.5s"></div>
          </div>
          <span style="font-size:9px;color:#10b981;font-weight:700;min-width:50px">${leaders.length}/${SECTOR_LIST.length} bullish</span>
          <span style="font-size:9px;color:var(--sub)">vs SPY ${fmtPct(spyPct)}</span>
        </div>`;
    } else if (breadthEl) {
      breadthEl.innerHTML = '<div style="color:var(--sub);font-size:11px;padding:8px">Sector data unavailable — check /api/sectors/snapshot</div>';
    }

  } catch(e) {
    if (narrative) narrative.innerHTML =
      `<div style="color:var(--red);padding:20px;font-size:12px">⚠ ${e.message}</div>`;
  }
}

// ══════════════════════════════════════════════════════════════════
// CHAKRA-STYLE GEX SECTIONS
// ══════════════════════════════════════════════════════════════════

function _renderStrategyCard(gex, spx, cw, pw, zg, netGex, regime) {
  const el = $('gexStrategyCard'); if (!el) return;
  const isNeg     = regime === 'NEGATIVE_GAMMA' || netGex < 0;
  const pinned    = regime === 'pinned' || regime === 'LOW_VOL';
  const roomCall  = cw - spx;
  const roomPut   = spx - pw;
  const pctCall   = (roomCall / spx * 100).toFixed(1);
  const pctPut    = (roomPut  / spx * 100).toFixed(1);

  // Determine strategy
  let stratName, stratDesc, stratColor, stratIcon, entry, target, invalidation;
  if (isNeg) {
    stratName = 'Trend Following'; stratIcon = '📈';
    stratDesc = 'Dealers amplify — trade WITH momentum';
    stratColor = 'var(--red)';
    entry = `Break above $${cw.toLocaleString()} or below $${pw.toLocaleString()}`;
    target = `1.5× the range extension`;
    invalidation = `Return inside the range`;
  } else if (pinned) {
    stratName = roomCall < roomPut ? 'Short Pops' : 'Buy Dips'; stratIcon = roomCall < roomPut ? '📉' : '📈';
    stratDesc = `Counter-trend — ${roomCall < roomPut ? 'P on rips toward pin' : 'C on dips toward pin'} $${(roomCall < roomPut ? cw : pw).toLocaleString()}`;
    stratColor = roomCall < roomPut ? 'var(--red)' : 'var(--green)';
    entry = `${roomCall < roomPut ? 'P on rips toward' : 'C on dips toward'} $${(roomCall < roomPut ? cw : pw).toLocaleString()}`;
    target = `Pin $${(roomCall < roomPut ? cw : pw).toLocaleString()}`;
    invalidation = `No invalidation level`;
  } else {
    stratName = 'Range Trade'; stratIcon = '↔️';
    stratDesc = 'Mean reversion between walls';
    stratColor = 'var(--gold)';
    entry = `Fade extremes — buy $${pw.toLocaleString()} / sell $${cw.toLocaleString()}`;
    target = `Midpoint $${((cw+pw)/2).toFixed(0)}`;
    invalidation = `Close outside either wall`;
  }

  // Regime badge
  const regimeBg  = netGex >= 0 ? 'rgba(0,208,132,0.15)' : 'rgba(255,61,90,0.15)';
  const regimeCol = netGex >= 0 ? 'var(--green)' : 'var(--red)';
  const regimeLbl = netGex >= 0 ? 'Positive Gamma' : 'Negative Gamma';
  const netB      = (Math.abs(netGex)).toFixed(1) + 'B';
  const accel     = Math.round(netGex * 3);

  el.innerHTML = `
    <div class="ph" style="justify-content:space-between">
      <span class="pt">${stratIcon} ${stratName}</span>
      <span style="font-size:9px;padding:3px 8px;border-radius:4px;background:${regimeBg};color:${regimeCol};font-weight:700">
        ${regimeLbl}<br><span style="font-size:8px">${netB} total GEX<br>${accel >= 0 ? '+' : ''}${accel} ${Math.abs(accel) > 10 ? 'STRONG' : 'MODERATE'}</span>
      </span>
    </div>
    <div class="pb">
      <div style="font-size:11px;color:var(--sub);margin-bottom:10px">${stratDesc}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px">
        <div><div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">ENTRY</div>
          <div style="font-size:11px;font-weight:600">${entry}</div></div>
        <div><div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">TARGET</div>
          <div style="font-size:11px;font-weight:600;color:var(--green)">${target}</div></div>
        <div><div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">OUT</div>
          <div style="font-size:11px;font-weight:600;color:var(--red)">${invalidation}</div></div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <span style="font-size:9px;padding:2px 8px;border-radius:3px;background:var(--bg3);color:var(--sub)">
          Pin $${(roomCall < roomPut ? cw : pw).toLocaleString()} (${roomCall < roomPut ? pctCall : pctPut}% ${roomCall < roomPut ? 'above' : 'below'})</span>
        <span style="font-size:9px;padding:2px 8px;border-radius:3px;background:var(--bg3);color:var(--sub)">
          Walls ${Math.abs(roomCall - roomPut) < 5 ? 'balanced' : roomCall < roomPut ? 'call lean' : 'put lean'}</span>
      </div>
    </div>`;
}

function _renderSessionAnalysis(gex, spx, cw, pw, netGex, regime, ticker) {
  const el = $('gexSessionAnalysis'); if (!el) return;
  const now    = new Date();
  const hour   = now.getHours() - 4; // ET approximation
  const isPos  = netGex >= 0;
  const pinned = regime === 'pinned' || regime === 'LOW_VOL';
  const roomCall = cw - spx;
  const roomPut  = spx - pw;
  const closer   = roomCall < roomPut ? 'call wall' : 'put wall';
  const pctEM    = ((Math.abs(roomCall < roomPut ? roomCall : roomPut)) / spx * 100).toFixed(1);

  const sessionBrief = pinned
    ? `Mean-revert session` : isPos ? `Range-bound session` : `Trend session`;
  const basePath = `${(isPos ? -1.5 : 2.5).toFixed(1)}% to $${(spx * (isPos ? 0.985 : 1.025)).toFixed(2)}`;
  const conf     = isPos ? (pinned ? 74 : 58) : 42;
  const timeLeft = `${Math.max(0, 16 - (hour + now.getMinutes()/60)).toFixed(1)}h left`;

  const userRead   = isPos
    ? `Market is range-bound today. The magnetic "pin" level is ${pctEM}% ${roomCall < roomPut ? 'above' : 'below'} current price at $${(roomCall < roomPut ? cw : pw).toLocaleString()}. Price tends to get pulled back toward it.`
    : `Market is in trending mode today. Moves will be bigger than usual — dealers add fuel to breakouts instead of slowing them down. Higher risk, higher reward.`;
  const traderPlan = pinned
    ? `${roomCall < roomPut ? '📉 Sell rallies (buy puts)' : '📈 Buy dips (buy calls)'} toward the pin at $${(roomCall < roomPut ? cw : pw).toLocaleString()}. Wait for price to reach an extreme before entering — don't chase the middle.`
    : `⚡ Trade breakouts. Enter when price clears the ${closer} with momentum. Add to winners; cut losers quickly if price reverses.`;
  const deskRead   = `Net dealer position: ${isPos ? '+' : ''}${(Math.abs(netGex)/1e9).toFixed(2)}B. Momentum ${netGex > 0 ? 'positive' : 'negative'} (${Math.abs(Math.round(netGex*3))} units). Model projection: ${isPos ? 'mean reversion' : 'trend amplification'} — ${conf}% confidence.`;

  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <div>
        <div style="font-size:9px;color:var(--sub);letter-spacing:1px;text-transform:uppercase">Today's Session</div>
        <div style="font-size:12px;font-weight:700;color:var(--gold);margin-top:2px">${sessionBrief}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:11px;color:var(--red)">${basePath}</div>
        <div style="font-size:9px;color:var(--sub)">${timeLeft} · ${conf}% conf</div>
      </div>
    </div>
    ${[
      ['In Plain English',  userRead],
      ['What To Do',        traderPlan],
      ['Technical Summary', deskRead],
    ].map(([l,v]) => `
      <div style="margin-bottom:8px">
        <div style="font-size:7px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">${l}</div>
        <div style="font-size:10px;color:var(--text);line-height:1.6">${v}</div>
      </div>`).join('')}`;
}

function _renderSignalPanel(gex, spx, cw, pw, netGex, regime, ivSkew) {
  const el    = $('gexSignalPanel');
  const label = $('gexSignalLabel');
  if (!el) return;

  const isPos   = netGex >= 0;
  const pinned  = regime === 'pinned' || regime === 'LOW_VOL';
  const roomCall = cw - spx;
  const roomPut  = spx - pw;
  const biasUp  = ivSkew < 0; // negative skew = call lean
  const align   = Math.round(55 + (isPos ? 20 : -20) + (biasUp ? 10 : -10));

  const signal  = pinned ? (roomCall < roomPut ? 'Lean with dips toward pin $' + cw.toLocaleString()
                                                 : 'Lean with rips toward pin $' + pw.toLocaleString())
                         : isPos ? 'Range trade between walls' : 'Follow momentum — trend mode';
  const severity = align > 70 ? 'STRONG' : align > 55 ? 'MODERATE' : 'WEAK';
  const sevCol   = align > 70 ? 'var(--green)' : align > 55 ? 'var(--gold)' : 'var(--sub)';

  if (label) { label.textContent = severity; label.style.color = sevCol; }

  // Forces
  const forces = [
    { name: 'Bias',       val: biasUp ? 0.8 : -0.6,  label: biasUp ? 'Bullish bias' : 'Bearish bias' },
    { name: 'Path',       val: isPos ? 0.6 : -0.7,   label: isPos ? 'Path up' : 'Path down' },
    { name: 'Dealers',    val: isPos ? 0.5 : -0.8,   label: isPos ? `Accel +${Math.round(netGex*3)}` : `Accel ${Math.round(netGex*3)}` },
    { name: 'Projection', val: isPos ? 0.4 : -0.5,   label: 'mean revers.' },
    { name: 'Pin',        val: Math.min(1, Math.max(-1, (spx - (cw+pw)/2) / ((cw-pw)/2))), label: `${(Math.min(roomCall,roomPut)/spx*100).toFixed(2)}% above` },
    { name: 'Charm',      val: isPos ? 0.3 : -0.4,   label: `${(cw/spx*100 - 100).toFixed(2)}×` },
  ];

  const basePct = (isPos ? -1.5 : 1.5).toFixed(1);
  const basePrice = (spx * (isPos ? 0.985 : 1.015)).toFixed(2);
  const conf = isPos ? (pinned ? 74 : 58) : 42;

  el.innerHTML = `
    <div style="font-size:11px;font-weight:700;color:${sevCol};margin-bottom:12px">${signal}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
      <div>
        <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">ARJUN SIGNAL</div>
        <div style="font-size:13px;font-weight:700">${isPos ? 'HOLD' : 'WATCH'}</div>
        <div style="font-size:9px;color:var(--sub)">Conf: ${conf}%</div>
      </div>
      <div>
        <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">GEX REGIME</div>
        <div style="font-size:13px;font-weight:700;color:${isPos ? 'var(--green)' : 'var(--red)'}">POSITIVE</div>
        <div style="font-size:9px;color:var(--sub)">${netGex >= 0 ? '+' : ''}${(netGex/1e9).toFixed(2)}B</div>
      </div>
    </div>
    <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:6px">FORCES</div>
    ${forces.map(f => {
      const pct = Math.abs(f.val * 100);
      const col = f.val > 0 ? 'var(--green)' : 'var(--red)';
      const left = f.val >= 0;
      return `<div style="display:grid;grid-template-columns:80px 1fr 100px;gap:6px;align-items:center;margin-bottom:5px">
        <span style="font-size:9px;color:var(--sub)">→ ${f.name}</span>
        <div style="height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;position:relative">
          <div style="position:absolute;${left?'left:50%':'right:50%'};top:0;bottom:0;width:${pct/2}%;background:${col};border-radius:3px"></div>
          <div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--border)"></div>
        </div>
        <span style="font-size:9px;color:${col};text-align:right">${f.label}</span>
      </div>`;
    }).join('')}
    <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border);display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
      <div><div style="font-size:7px;color:var(--sub);text-transform:uppercase;margin-bottom:2px">ENTRY</div>
        <div style="font-size:10px;font-weight:600">Call $${spx.toFixed(0)}</div></div>
      <div><div style="font-size:7px;color:var(--sub);text-transform:uppercase;margin-bottom:2px">TARGET</div>
        <div style="font-size:10px;font-weight:600;color:var(--green)">$${cw.toLocaleString()}<br>MAX GEX stretch</div></div>
      <div><div style="font-size:7px;color:var(--sub);text-transform:uppercase;margin-bottom:2px">INVALIDATION</div>
        <div style="font-size:10px;font-weight:600;color:var(--red)">$${pw.toLocaleString()}<br>PUT WALL failure</div></div>
    </div>`;
}

function _renderHedgingWindows(spx, cw, pw, netGex) {
  const el = $('gexHedgingWindows'); if (!el) return;
  const windows = [
    { time:'9:30',  label:'Open Bullet',   note:'High vol open' },
    { time:'9:55',  label:'Silver Bullet', note:'First reversion' },
    { time:'10:25', label:'Mid-Morn',      note:'Trend confirm' },
    { time:'10:55', label:'Late Morn',     note:'GEX reset' },
    { time:'11:55', label:'Lunch',         note:'Low vol pin' },
    { time:'12:55', label:'1h Open',       note:'PM direction' },
    { time:'1:58',  label:'Afternoon',     note:'Accel check' },
    { time:'2:25',  label:'Power App',     note:'Options expiry' },
    { time:'3:00',  label:'Power Hour',    note:'Max flow' },
    { time:'3:25',  label:'Lotto',         note:'0DTE final' },
  ];

  // Determine current window
  const now  = new Date();
  const etHr = now.getUTCHours() - 4;
  const etMn = now.getUTCMinutes();
  const etTot = etHr * 60 + etMn;

  const activeIdx = windows.reduce((best, w, i) => {
    const [h, m] = w.time.split(':').map(Number);
    const wTot = h * 60 + m;
    return wTot <= etTot ? i : best;
  }, -1);

  el.innerHTML = `<div style="font-size:9px;color:var(--sub);margin-bottom:8px;line-height:1.5;padding:6px 8px;background:var(--bg3);border-radius:5px">
    💡 <strong style="color:var(--text)">What are these?</strong> These are the times when market makers typically rebalance their hedges. Price often reverses or accelerates at these windows — they're the best times to look for entries.
  </div>
  <div style="display:flex;gap:6px;overflow-x:auto;padding-bottom:4px">` +
    windows.map((w, i) => {
      const isActive = i === activeIdx;
      const isPast   = i < activeIdx;
      return `<div style="min-width:70px;padding:8px;border-radius:6px;text-align:center;flex-shrink:0;
        background:${isActive ? 'var(--accent)' : isPast ? 'var(--bg3)' : 'var(--bg2)'};
        border:1px solid ${isActive ? 'var(--accent)' : 'var(--border)'};
        opacity:${isPast ? 0.5 : 1}">
        <div style="font-size:10px;font-weight:700;font-family:JetBrains Mono,monospace;
          color:${isActive ? '#fff' : 'var(--fg)'}">${w.time}</div>
        <div style="font-size:8px;color:${isActive ? 'rgba(255,255,255,0.8)' : 'var(--sub)'};margin-top:2px">${w.label}</div>
      </div>`;
    }).join('') + `</div>
    <div style="font-size:9px;color:var(--sub);margin-top:8px">
      Current: <strong style="color:var(--fg)">${activeIdx >= 0 ? windows[activeIdx].time + ' — ' + windows[activeIdx].label : 'Pre-market'}</strong>
      &nbsp;·&nbsp; Next: <strong style="color:var(--fg)">${activeIdx < windows.length-1 ? windows[activeIdx+1].time + ' ' + windows[activeIdx+1].label : 'Close'}</strong>
    </div>`;
}

// ══════════════════════════════════════════════════════════════════
// COLLAPSIBLE TOGGLE
// ══════════════════════════════════════════════════════════════════
function _toggleSection(id, hdr) {
  // Lazy-load timeline and term structure on expand
  const ticker = (typeof _gexTicker !== 'undefined' ? _gexTicker : 'SPY');
  if (id === 'gexTimelineBody') setTimeout(() => _renderGexTimeline(ticker), 100);
  if (id === 'gexTermBody')     setTimeout(() => _renderTermStructure(ticker), 100);
  const el = $(id); if (!el) return;
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : 'block';
  const arrow = hdr?.querySelector('span:last-child');
  if (arrow) arrow.textContent = open ? '▼' : '▲';
}

// ══════════════════════════════════════════════════════════════════
// EXPIRATION BREAKDOWN
// ══════════════════════════════════════════════════════════════════
async function _renderExpiryBreakdown(ticker) {
  const el    = $('gexExpiryBody');
  const badge = $('gexExpiryCount');
  if (!el) return;
  el.innerHTML = '<div class="ld" style="padding:16px"><div class="spin"></div></div>';

  try {
    const data = await getCached(`/api/options/gex/expiry-breakdown?ticker=${ticker}`, 120000);
    const exps = data?.expirations || [];
    if (badge) badge.textContent = `${exps.length} expirations`;
    if (!exps.length) { el.innerHTML = '<div class="empty" style="padding:16px">No expiry data — GEX compute needed</div>'; return; }

    const spot = data.spot || 0;

    el.innerHTML = '<div style="padding:10px;display:flex;flex-direction:column;gap:10px">' +
      exps.slice(0,15).map(e => {
        const isPos  = e.net_gex_b >= 0;
        const pct    = Math.min(100, e.pct_of_total);
        const col    = isPos ? 'var(--green)' : 'var(--red)';
        const dteStr = e.dte === 0 ? '0DTE' : e.dte === 1 ? '1DTE' : `${e.dte}DTE`;
        const label  = new Date(e.expiry + 'T12:00:00').toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'});

        const topStrikes = (e.top_strikes || []).slice(0,6).map(s => {
          const isPin = s.strike === e.pin;
          const isCW  = s.strike === e.call_wall;
          const isPW  = s.strike === e.put_wall;
          const bg    = isPin ? 'background:rgba(255,140,0,0.2);color:var(--gold);border:1px solid var(--gold)40'
                      : isCW  ? 'background:rgba(255,61,90,0.15);color:var(--red)'
                      : isPW  ? 'background:rgba(0,208,132,0.15);color:var(--green)'
                      : 'background:var(--bg3);color:var(--sub)';
          const tag   = isPin ? ' PIN' : isCW ? ' CALL WALL' : isPW ? ' PUT WALL' : '';
          return `<span style="font-size:8px;padding:2px 6px;border-radius:3px;${bg};font-family:JetBrains Mono,monospace">
            $${s.strike.toLocaleString()}${tag} <span style="opacity:0.7">${s.gex_m > 0 ? '+' : ''}${s.gex_m}M</span>
          </span>`;
        }).join('');

        return `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="font-size:11px;font-weight:700;font-family:JetBrains Mono,monospace">${label}</span>
              <span style="font-size:8px;padding:1px 6px;border-radius:3px;background:${isPos?'rgba(0,208,132,0.1)':'rgba(255,61,90,0.1)'};color:${col}">${dteStr}</span>
              ${e.pct_of_total > 15 ? `<span style="font-size:8px;color:var(--gold)">+y</span>` : ''}
              <span style="font-size:8px;color:var(--sub)">${e.pct_of_total}%</span>
              <span style="font-size:8px;color:var(--sub)">${e.strike_count} strikes</span>
            </div>
            <div style="font-size:11px;font-weight:700;font-family:JetBrains Mono,monospace;color:${col}">
              ${e.net_gex_b >= 0 ? '+' : ''}$${(e.net_gex_b * 1000).toFixed(0)}M
            </div>
          </div>
          <div style="height:3px;background:var(--bg3);border-radius:2px;margin-bottom:8px;overflow:hidden">
            <div style="width:${pct}%;height:100%;background:${col};border-radius:2px"></div>
          </div>
          <div style="font-size:9px;color:var(--sub);margin-bottom:6px">
            C $${(e.call_gex_b * 1000).toFixed(0)}M &nbsp; P -$${Math.abs(e.put_gex_b * 1000).toFixed(0)}M
          </div>
          <div style="display:flex;gap:4px;flex-wrap:wrap">${topStrikes}</div>
        </div>`;
      }).join('') + '</div>';
  } catch(e) {
    el.innerHTML = `<div class="empty" style="padding:16px">Error: ${e.message}</div>`;
  }
}

// ══════════════════════════════════════════════════════════════════
// INTRADAY GEX TIMELINE — area chart with gradient fills, v2
// ══════════════════════════════════════════════════════════════════
async function _renderGexTimeline(ticker) {
  const el = $('gexTimelineBody');
  if (!el || el.style.display === 'none') return;
  const badge = $('gexTimelineTicker');
  if (badge) badge.textContent = ticker;

  try {
    const intraday = await getCached(`/api/options/gex/intraday?ticker=${ticker}`, 30000);
    const canvas = $('gexTimelineCanvas');
    if (!canvas) return;

    const W = canvas.parentElement ? canvas.parentElement.offsetWidth - 4 : 600;
    const H = 200;
    canvas.width = W; canvas.height = H;
    if (canvas.parentElement) canvas.parentElement.style.position = 'relative';
    const ctx = canvas.getContext('2d');

    ctx.fillStyle = '#0b0b12';
    ctx.fillRect(0, 0, W, H);

    const snapshots = (intraday?.data || []);

    if (!snapshots.length) {
      ctx.fillStyle = '#444';
      ctx.font = '12px JetBrains Mono';
      ctx.textAlign = 'center';
      ctx.fillText('No intraday GEX data yet — starts populating at market open', W/2, H/2);
      return;
    }

    const PAD_L = 52, PAD_R = 16, PAD_T = 24, PAD_B = 28;
    const chartW = W - PAD_L - PAD_R;
    const chartH = H - PAD_T - PAD_B;

    const netVals = snapshots.map(s => parseFloat(s.net_gex || 0));
    const maxAbs  = Math.max(...netVals.map(Math.abs), 0.01);
    const midY    = PAD_T + chartH / 2;
    const halfH   = chartH / 2;

    // Grid lines
    ctx.strokeStyle = 'rgba(255,255,255,0.05)';
    ctx.lineWidth = 1;
    [0.5, 1.0].forEach(f => {
      const y1 = midY - halfH * f, y2 = midY + halfH * f;
      ctx.beginPath(); ctx.moveTo(PAD_L, y1); ctx.lineTo(W-PAD_R, y1); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(PAD_L, y2); ctx.lineTo(W-PAD_R, y2); ctx.stroke();
    });

    // Y-axis labels
    ctx.font = '8px JetBrains Mono';
    ctx.textAlign = 'right';
    const fmt = v => v >= 1 ? '+$' + v.toFixed(1) + 'B' : v <= -1 ? '-$' + Math.abs(v).toFixed(1) + 'B'
                    : v >= 0 ? '+$' + (v*1000).toFixed(0) + 'M' : '-$' + (Math.abs(v)*1000).toFixed(0) + 'M';
    const yAxisVals = [maxAbs, maxAbs/2, 0, -maxAbs/2, -maxAbs];
    yAxisVals.forEach(v => {
      const y = midY - (v / maxAbs) * halfH;
      ctx.fillStyle = v === 0 ? '#666' : v > 0 ? '#10b981' : '#f43f5e';
      ctx.fillText(fmt(v), PAD_L - 4, y + 3);
    });

    // Detect regime changes
    const regimes = snapshots.map(s => s.regime || (parseFloat(s.net_gex||0) > 0 ? 'POSITIVE' : 'NEGATIVE'));

    // Build point arrays
    const pts = snapshots.map((s, i) => {
      const x = PAD_L + (i / Math.max(snapshots.length - 1, 1)) * chartW;
      const y = midY - (parseFloat(s.net_gex||0) / maxAbs) * halfH;
      return {x, y, v: parseFloat(s.net_gex||0), regime: regimes[i], ts: s.ts || s.datetime || ''};
    });

    // Area fill — above zero (green) and below zero (red) separately
    const fillArea = (filterFn, colTop, colBot) => {
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(pts[0].x, midY);
      pts.forEach(p => ctx.lineTo(p.x, filterFn(p.v) ? p.y : midY));
      ctx.lineTo(pts[pts.length-1].x, midY);
      ctx.closePath();
      const grad = ctx.createLinearGradient(0, PAD_T, 0, PAD_T + chartH);
      grad.addColorStop(0, colTop);
      grad.addColorStop(1, colBot);
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.restore();
    };
    fillArea(v => v > 0, 'rgba(16,185,129,0.45)', 'rgba(16,185,129,0.02)');
    fillArea(v => v < 0, 'rgba(244,63,94,0.05)',  'rgba(244,63,94,0.45)');

    // Zero line
    ctx.strokeStyle = 'rgba(255,255,255,0.2)';
    ctx.lineWidth = 1; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(PAD_L, midY); ctx.lineTo(W-PAD_R, midY); ctx.stroke();
    ctx.setLineDash([]);

    // Main line (gradient color based on sign)
    for (let i = 1; i < pts.length; i++) {
      const prev = pts[i-1], cur = pts[i];
      ctx.strokeStyle = cur.v >= 0 ? '#10b981' : '#f43f5e';
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(prev.x, prev.y); ctx.lineTo(cur.x, cur.y); ctx.stroke();
    }

    // Regime change markers
    for (let i = 1; i < regimes.length; i++) {
      if (regimes[i] !== regimes[i-1]) {
        const x = pts[i].x;
        ctx.save();
        ctx.strokeStyle = '#fbbf24';
        ctx.lineWidth = 1.5;
        ctx.setLineDash([3,3]);
        ctx.beginPath(); ctx.moveTo(x, PAD_T); ctx.lineTo(x, PAD_T + chartH); ctx.stroke();
        ctx.restore();
        ctx.fillStyle = '#fbbf24';
        ctx.font = 'bold 7px JetBrains Mono';
        ctx.textAlign = 'center';
        ctx.fillText('FLIP', x, PAD_T + 10);
      }
    }

    // Current value badge (last point)
    const last = pts[pts.length-1];
    const lastFmt = fmt(last.v);
    ctx.fillStyle = last.v >= 0 ? '#10b981' : '#f43f5e';
    ctx.font = 'bold 10px JetBrains Mono';
    ctx.textAlign = 'left';
    ctx.fillText(lastFmt, last.x + 4, last.y - 4 < PAD_T + 12 ? PAD_T + 12 : last.y - 4);

    // Dot at last point
    ctx.beginPath();
    ctx.arc(last.x, last.y, 4, 0, Math.PI*2);
    ctx.fillStyle = last.v >= 0 ? '#10b981' : '#f43f5e';
    ctx.fill();

    // X-axis time labels (show up to 8 ticks)
    const step = Math.max(1, Math.ceil(snapshots.length / 8));
    ctx.fillStyle = '#555'; ctx.font = '7px JetBrains Mono'; ctx.textAlign = 'center';
    pts.forEach((p, i) => {
      if (i % step !== 0 && i !== pts.length-1) return;
      const raw = snapshots[i].datetime || snapshots[i].ts || '';
      let lbl = '';
      if (raw) {
        const d = new Date(typeof raw === 'number' ? raw * 1000 : raw);
        if (!isNaN(d)) {
          const etStr = d.toLocaleString('en-US', {timeZone:'America/New_York', hour:'2-digit', minute:'2-digit', hour12:false});
          lbl = etStr;
        }
      }
      if (lbl) ctx.fillText(lbl, p.x, H - PAD_B + 12);
    });

    // Panel title
    ctx.font = 'bold 9px JetBrains Mono';
    ctx.textAlign = 'left';
    ctx.fillStyle = '#666';
    ctx.fillText('NET GEX INTRADAY', PAD_L + 4, PAD_T - 6);

  } catch(e) { console.warn('Timeline error:', e); }
}

// ══════════════════════════════════════════════════════════════════
// TERM STRUCTURE — gradient bars with cliff annotations, v2
// ══════════════════════════════════════════════════════════════════
async function _renderTermStructure(ticker) {
  const el = $('gexTermBody');
  if (!el || el.style.display === 'none') return;
  try {
    const data = await getCached(`/api/options/gex/expiry-breakdown?ticker=${ticker}`, 120000);
    const exps = (data?.expirations || []).slice(0, 35);
    const canvas = $('gexTermCanvas');
    const cliffEl = $('gexTermCliff');
    if (!canvas) return;

    if (!exps.length) {
      canvas.style.display = 'none';
      if (cliffEl) cliffEl.textContent = 'Term structure — no expiry data in snapshot';
      return;
    }
    canvas.style.display = 'block';

    const W = canvas.parentElement ? canvas.parentElement.offsetWidth - 4 : 700;
    const H = 240;
    canvas.width  = W;
    canvas.height = H;
    if (canvas.parentElement) canvas.parentElement.style.position = 'relative';
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#0b0b12';
    ctx.fillRect(0, 0, W, H);

    const PAD_L = 52, PAD_R = 12, PAD_T = 28, PAD_B = 36;
    const chartW = W - PAD_L - PAD_R;
    const chartH = H - PAD_T - PAD_B;

    // Net GEX signed values — positive bar up from baseline, negative bar down
    const netVals = exps.map(e => parseFloat(e.net_gex_b ?? e.net_gex ?? 0));
    const absVals = netVals.map(Math.abs);
    const maxVal  = Math.max(...absVals, 0.01);

    const barW = Math.max(8, Math.floor((chartW - exps.length * 2) / exps.length));
    const midY = PAD_T + chartH / 2;
    const halfH = chartH / 2 - 2;

    // Detect cliffs (signed threshold for meaningful bars)
    const cliffs = [];
    for (let i = 1; i < netVals.length; i++) {
      const prevDte = exps[i-1]?.dte ?? 99;
      if (prevDte === 0) continue;
      if (absVals[i-1] > maxVal * 0.15 && absVals[i] / absVals[i-1] < 0.40) cliffs.push(i);
    }
    if (cliffEl) {
      cliffEl.style.color = cliffs.length > 0 ? '#f59e0b' : '#10b981';
      cliffEl.textContent = cliffs.length > 0
        ? `⚠ Cliff at ${exps[cliffs[0]]?.expiry || 'next expiry'} — large gamma rolls off`
        : '● No cliff detected — gamma spread evenly';
    }

    // Grid lines
    ctx.strokeStyle = 'rgba(255,255,255,0.05)'; ctx.lineWidth = 1;
    [0.5, 1.0].forEach(f => {
      const y1 = midY - halfH * f, y2 = midY + halfH * f;
      ctx.beginPath(); ctx.moveTo(PAD_L, y1); ctx.lineTo(W-PAD_R, y1); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(PAD_L, y2); ctx.lineTo(W-PAD_R, y2); ctx.stroke();
    });

    // Zero line
    ctx.strokeStyle = 'rgba(255,255,255,0.15)'; ctx.lineWidth = 1;
    ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(PAD_L, midY); ctx.lineTo(W-PAD_R, midY); ctx.stroke();
    ctx.setLineDash([]);

    // Y-axis labels
    ctx.font = '8px JetBrains Mono'; ctx.textAlign = 'right';
    const fmtV = v => {
      const a = Math.abs(v);
      return (v>=0?'+':'-') + (a >= 1 ? '$'+a.toFixed(1)+'B' : '$'+(a*1000).toFixed(0)+'M');
    };
    [maxVal, maxVal/2, 0, -maxVal/2, -maxVal].forEach(v => {
      const y = midY - (v / maxVal) * halfH;
      ctx.fillStyle = v === 0 ? '#555' : v > 0 ? '#10b981' : '#f43f5e';
      ctx.fillText(fmtV(v), PAD_L - 4, y + 3);
    });

    // Draw bars
    exps.forEach((e, i) => {
      const net     = netVals[i];
      const absNet  = Math.abs(net);
      const barH    = Math.max(3, (absNet / maxVal) * halfH);
      const x       = PAD_L + i * (barW + 2);
      const isPos   = net >= 0;
      const isCliff = cliffs.includes(i);
      const is0DTE  = (e.dte ?? 99) === 0;

      let col1, col2;
      if (isCliff)    { col1 = '#f59e0b'; col2 = 'rgba(245,158,11,0.18)'; }
      else if (is0DTE){ col1 = '#818cf8'; col2 = 'rgba(129,140,248,0.18)'; }
      else if (isPos) { col1 = '#10b981'; col2 = 'rgba(16,185,129,0.12)'; }
      else            { col1 = '#f43f5e'; col2 = 'rgba(244,63,94,0.12)'; }

      if (isPos) {
        // bar goes upward from midY
        const grad = ctx.createLinearGradient(0, midY - barH, 0, midY);
        grad.addColorStop(0, col1);
        grad.addColorStop(1, col2);
        ctx.fillStyle = grad;
        ctx.fillRect(x, midY - barH, barW, barH);
        ctx.fillStyle = col1; ctx.fillRect(x, midY - barH, barW, 2);
      } else {
        // bar goes downward from midY
        const grad = ctx.createLinearGradient(0, midY, 0, midY + barH);
        grad.addColorStop(0, col2);
        grad.addColorStop(1, col1);
        ctx.fillStyle = grad;
        ctx.fillRect(x, midY, barW, barH);
        ctx.fillStyle = col1; ctx.fillRect(x, midY + barH - 2, barW, 2);
      }

      // Cliff dashed marker
      if (isCliff) {
        ctx.save(); ctx.setLineDash([3,3]);
        ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(x + barW/2, PAD_T); ctx.lineTo(x + barW/2, PAD_T + chartH); ctx.stroke();
        ctx.restore();
      }

      // Value label above/below bar
      const labelY = isPos ? (midY - barH - (isCliff ? 16 : 3)) : (midY + barH + (isCliff ? 14 : 11));
      const valStr = absNet >= 1 ? '$' + absNet.toFixed(1) + 'B' : '$' + (absNet * 1000).toFixed(0) + 'M';
      ctx.fillStyle = isCliff ? '#f59e0b' : is0DTE ? '#818cf8' : '#888';
      ctx.font = (isCliff || is0DTE ? 'bold ' : '') + '7px JetBrains Mono';
      ctx.textAlign = 'center';
      ctx.fillText(valStr, x + barW/2, labelY);
      if (isCliff) {
        ctx.fillStyle = '#f59e0b'; ctx.font = 'bold 7px JetBrains Mono';
        ctx.fillText('CLIFF', x + barW/2, labelY - 9);
      }

      // Date / DTE label below chart
      const showLabel = exps.length <= 15 || i === 0 || isCliff || is0DTE
                        || i % Math.ceil(exps.length / 10) === 0;
      if (showLabel) {
        ctx.fillStyle = is0DTE ? '#818cf8' : isCliff ? '#f59e0b' : '#555';
        ctx.font = (is0DTE ? 'bold ' : '') + '7px JetBrains Mono';
        ctx.textAlign = 'center';
        const lbl = e.dte === 0 ? 'Today' : e.dte === 1 ? 'Tmrw'
                  : e.dte <= 7 ? e.dte + 'd'
                  : new Date((e.expiry||'') + 'T12:00').toLocaleDateString('en-US',{month:'short',day:'numeric'});
        ctx.fillText(lbl, x + barW/2, H - PAD_B + 14);
        if (e.dte <= 7 && e.dte !== 0 && e.expiry) {
          ctx.fillStyle = '#444'; ctx.font = '6px JetBrains Mono';
          ctx.fillText(e.expiry.slice(5), x + barW/2, H - PAD_B + 23);
        }
      }
    });

    // Top legend
    ctx.font = 'bold 9px JetBrains Mono'; ctx.textAlign = 'left';
    ctx.fillStyle = '#10b981'; ctx.fillRect(PAD_L, 6, 8, 8);
    ctx.fillStyle = '#888';    ctx.fillText('+GEX', PAD_L + 11, 14);
    ctx.fillStyle = '#f43f5e'; ctx.fillRect(PAD_L + 52, 6, 8, 8);
    ctx.fillStyle = '#888';    ctx.fillText('-GEX', PAD_L + 63, 14);
    ctx.fillStyle = '#f59e0b'; ctx.fillRect(PAD_L + 104, 6, 8, 8);
    ctx.fillStyle = '#888';    ctx.fillText('Cliff', PAD_L + 115, 14);
    ctx.fillStyle = '#818cf8'; ctx.fillRect(PAD_L + 148, 6, 8, 8);
    ctx.fillStyle = '#888';    ctx.fillText('0DTE', PAD_L + 159, 14);

  } catch(e) { console.warn('Term structure error:', e); }
}

// ══════════════════════════════════════════════════════════════════
// LOAD ALL NEW SECTIONS + ADD TOP S&P STOCKS TO TICKER BAR
// ══════════════════════════════════════════════════════════════════
function _loadGexExtendedSections(ticker) {
  _renderExpiryBreakdown(ticker);
  // Timeline and Term Structure are open by default — render them now
  setTimeout(() => _renderGexTimeline(ticker), 150);
  setTimeout(() => _renderTermStructure(ticker), 300);
}

function _addTopSpTickers() {
  const bar = document.querySelector('.gex-btn-bar') || document.getElementById('gexTickerBar');
  if (!bar) return;
  const TOP_SP = ['AAPL','MSFT','NVDA','AMZN','GOOGL','META','TSLA','AVGO','MSFT','BRK.B'];
  const existing = bar.querySelectorAll('.gex-btn');
  const existingTickers = Array.from(existing).map(b => b.textContent.trim());
  TOP_SP.forEach(t => {
    if (existingTickers.includes(t)) return;
    const btn = document.createElement('button');
    btn.className = 'gex-btn';
    btn.id = `gbt-${t}`;
    btn.textContent = t;
    btn.onclick = () => selectGexTicker(t);
    bar.appendChild(btn);
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// GEX ANALYSIS — GEORGE STYLE
// Added: March 2026
// ══════════════════════════════════════════════════════════════════════════════

const GEX_TICKER_UNIVERSE = [
  'SPY','QQQ','IWM','SPX','RUT','NVDA','TSLA','AAPL','MSFT','AMZN','GOOGL','META','GLD','SLV'
];

let _gexActiveTicker = sessionStorage.getItem('gexActiveTicker') || 'SPY';
let _gexRefreshInterval = null;

async function renderGEXGeorgeStyle(ticker) {
  if (ticker) { _gexActiveTicker = ticker; sessionStorage.setItem('gexActiveTicker', ticker); }
  else { _gexActiveTicker = sessionStorage.getItem('gexActiveTicker') || 'SPY'; }
  _renderGEXTickerRow();
  await _renderGEXStrategyCardGeorge(_gexActiveTicker);
  _updateGEXLastSessionBanner();
}

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
  _loadGEXAmounts();
}

async function _loadGEXAmounts() {
  try {
    const d = await getCached('/api/options/gex?ticker=SPY', 60000);
    if (d && d.net_gex) {
      const v = Math.abs(d.net_gex);
      const s = v >= 1e9 ? '$' + (d.net_gex/1e9).toFixed(1)+'B' : '$'+(d.net_gex/1e6).toFixed(0)+'M';
      const el = $('gexTickerAmt_SPY'); if (el) el.textContent = s;
    }
  } catch(e) {}
  try {
    const d = await getCached('/api/options/gex?ticker=SPX', 60000);
    if (d && d.net_gex) {
      const el = $('gexTickerAmt_SPX');
      if (el) el.textContent = '$'+(Math.abs(d.net_gex)/1e9).toFixed(1)+'B';
    }
  } catch(e) {}
}

async function _gexSelectTicker(ticker) {
  _gexActiveTicker = ticker;
  sessionStorage.setItem('gexActiveTicker', ticker);
  document.querySelectorAll('.gex-ticker-btn').forEach(btn => {
    btn.classList.toggle('active', btn.querySelector('span:nth-child(2)')?.textContent.trim() === ticker);
  });
  // Drive both top George card and bottom panels through the existing loadGex path
  selectGexTicker(ticker);
  // Also refresh manifold panel for the newly selected ticker
  loadManifoldModPanel(ticker);
}

function _updateGEXLastSessionBanner() {
  const banner = $('gexLastSessionBanner');
  const text   = $('gexLastSessionText');
  if (!banner || !text) return;
  const now = new Date();
  const etHour = (now.getUTCHours() - 5 + 24) % 24;
  const isWeekend = now.getDay() === 0 || now.getDay() === 6;
  const isMarketHours = !isWeekend && etHour >= 9 && etHour < 16;
  if (!isMarketHours) {
    banner.style.display = 'flex';
    const days   = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const last = new Date(now);
    while (last.getDay() === 0 || last.getDay() === 6) last.setDate(last.getDate() - 1);
    text.textContent = `Last Session — ${days[last.getDay()]}, ${months[last.getMonth()]} ${last.getDate()}`;
  } else {
    banner.style.display = 'none';
  }
}

async function _renderGEXStrategyCardGeorge(ticker) {
  const card = $('gexGeorgeCard');
  if (!card) return;
  card.innerHTML = `<div style="text-align:center;padding:20px;color:var(--sub);font-size:11px">Loading ${ticker} GEX analysis...</div>`;
  try {
    const data = await getCached(`/api/options/gex?ticker=${ticker}`, 30000);
    if (!data || data.error) {
      card.innerHTML = `<div style="text-align:center;padding:20px;color:var(--sub);font-size:11px">GEX data unavailable — loads during market hours</div>`;
      return;
    }

    const spot       = parseFloat(data.spot || data.spx_price || 0);
    const callWall   = parseFloat(data.call_wall || data.top_call_wall || 0);
    const putWall    = parseFloat(data.put_wall  || data.top_put_wall  || 0);
    const zeroGamma  = parseFloat(data.zero_gamma || spot);
    const netGex     = parseFloat(data.net_gex || 0);
    const netGexB    = netGex / 1e9;
    const regime     = data.regime || 'UNKNOWN';
    const isPositive = regime === 'POSITIVE_GAMMA' || netGex > 0;
    const regimeCol  = isPositive ? 'var(--green)' : 'var(--red)';
    const regimeTxt  = isPositive ? '🧲 Market Stabilizing' : '⚡ Market Explosive';
    const aboveZero  = spot > zeroGamma;

    const callGexB  = parseFloat(data.call_gex_dollars || 0) / 1e9;
    const putGexB   = parseFloat(data.put_gex_dollars  || 0) / 1e9;
    const biasRatio = parseFloat(data.bias_ratio || (putGexB && callGexB ? putGexB/callGexB : 1));
    const deskState = biasRatio > 2
      ? `${biasRatio.toFixed(1)}x put heavy`
      : biasRatio < 0.5
      ? `${(1/biasRatio).toFixed(1)}x call heavy`
      : 'Balanced';

    const regimeCall = data.regime_call || (isPositive && aboveZero ? 'SHORT_THE_POPS' :
                       isPositive && !aboveZero ? 'BUY_THE_DIPS' : 'FOLLOW_MOMENTUM');
    const rcLabels = {
      SHORT_THE_POPS:  { strategy:'📉 Sell the Rallies', sub:`Market is pinned — sell when price pops toward the ceiling. Dealers push it back down.`, entry:`Buy puts when price rips toward $${callWall > spot ? callWall.toFixed(2) : zeroGamma.toFixed(2)}`, target:`Pin level $${zeroGamma.toFixed(2)}`, out:'Price closes above the call wall' },
      BUY_THE_DIPS:    { strategy:'📈 Buy the Dips',    sub:`Market is pinned — buy when price drops toward the floor. Dealers push it back up.`, entry:`Buy calls when price dips toward $${putWall > 0 && putWall < spot ? putWall.toFixed(2) : zeroGamma.toFixed(2)}`, target:`Pin level $${zeroGamma.toFixed(2)}`, out:'Price closes below the put wall' },
      FOLLOW_MOMENTUM: { strategy:'⚡ Follow the Trend', sub:'Market is trending — trade in the direction of the breakout. Dealers amplify moves, don\'t fade them.', entry:'Enter when price breaks and holds above/below the flip point', target: callWall > spot ? `Upside ceiling $${callWall.toFixed(2)}` : `Downside floor $${putWall.toFixed(2)}`, out:`Reversal back through flip point $${zeroGamma.toFixed(2)}` },
      NEUTRAL:         { strategy:'⏸ Watch & Wait', sub:'No strong bias right now. Wait for a clearer setup before entering.', entry:'—', target:'—', out:'—' },
    };
    const rc = rcLabels[regimeCall] || rcLabels.NEUTRAL;

    const accelUp   = parseFloat(data.accel_up   || 0);
    const accelDown = parseFloat(data.accel_down  || 0);
    const accelTotal = (accelUp + accelDown) || 1;
    const accelPct   = Math.round(accelUp / accelTotal * 100);

    const pctToCall = callWall > spot ? ((callWall - spot) / spot * 100) : 0;
    const pctToPut  = putWall  < spot ? ((spot - putWall)  / spot * 100) : 0;
    const nearestPct = Math.min(pctToCall || 99, pctToPut || 99);
    const pinRisk  = Math.max(0, Math.min(100, Math.round(100 - nearestPct * 10)));
    const breakout = 100 - pinRisk;

    const emPts    = parseFloat(data.expected_move_pts || 0);
    const upper1sd = parseFloat(data.upper_1sd || spot * 1.01);
    const lower1sd = parseFloat(data.lower_1sd || spot * 0.99);
    const spotIn1sd = spot >= lower1sd && spot <= upper1sd;

    const gexAbsB   = Math.abs(netGexB);
    const strength  = gexAbsB > 3 ? 'STRONG' : gexAbsB > 1 ? 'MODERATE' : 'WEAK';
    const strengthCol = gexAbsB > 3 ? 'var(--green)' : gexAbsB > 1 ? 'var(--gold)' : 'var(--sub)';

    const pinDist  = zeroGamma ? Math.abs(spot - zeroGamma) / spot * 100 : 0;
    const structTxt = pinDist < 0.5 ? 'At pin — expect oscillation'
                    : pinDist < 1.5 ? `Pin ${pinDist.toFixed(1)}% ${spot > zeroGamma ? 'above' : 'below'}`
                    : `Pin ${pinDist.toFixed(1)}% away — no flip anchor`;
    const structSub = zeroGamma ? `Zero gamma: $${zeroGamma.toFixed(2)}` : 'No flip level';

    const basePath = regimeCall === 'SHORT_THE_POPS' ? 'Fade rallies, target pin'
                   : regimeCall === 'BUY_THE_DIPS'    ? 'Buy dips, target pin'
                   : regimeCall === 'FOLLOW_MOMENTUM' ? 'Follow breakout direction'
                   : 'No forward path yet';
    const basePathSub = emPts > 0 ? `±$${emPts.toFixed(2)} expected move` : 'Awaiting projection metadata';

    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px">
        <div>
          <div style="font-size:16px;font-weight:800;color:var(--text);line-height:1">${rc.strategy}</div>
          <div style="font-size:11px;color:var(--sub);margin-top:4px">${rc.sub}</div>
        </div>
        <div style="text-align:right">
          <span style="font-size:10px;font-weight:700;padding:4px 10px;border-radius:20px;background:${regimeCol}18;color:${regimeCol};border:1px solid ${regimeCol}35">${regimeTxt}</span>
          <div style="font-size:11px;font-weight:700;color:var(--text);margin-top:5px">$${gexAbsB.toFixed(1)}B total GEX</div>
          <div style="font-size:9px;font-weight:700;color:${strengthCol};letter-spacing:1px">${strength}</div>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:12px;background:var(--bg3);border-radius:7px;margin-bottom:14px">
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

      <div style="font-size:9px;color:var(--sub);margin-bottom:12px;display:flex;gap:16px;flex-wrap:wrap">
        ${zeroGamma ? `<span>Pin <strong style="color:var(--gold)">$${zeroGamma.toFixed(2)}</strong> (${pinDist.toFixed(1)}% ${spot > zeroGamma ? 'below' : 'above'})</span>` : ''}
        ${callWall  ? `<span>Call wall <strong style="color:var(--green)">$${callWall.toFixed(2)}</strong></span>` : ''}
        ${putWall   ? `<span>Put wall <strong style="color:var(--red)">$${putWall.toFixed(2)}</strong></span>` : ''}
        <span>Walls <strong style="color:${biasRatio > 1.5 ? 'var(--red)' : biasRatio < 0.67 ? 'var(--green)' : 'var(--sub)'}">${biasRatio.toFixed(1)}x ${biasRatio > 1 ? 'put' : 'call'} heavy</strong></span>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:14px">
        <div>
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:8px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">Pin Risk</span>
            <span style="font-size:9px;font-weight:700;color:var(--gold)">${pinRisk}%</span>
          </div>
          <div class="gex-progress-bar"><div class="gex-progress-fill" style="width:${pinRisk}%;background:var(--gold)"></div></div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:8px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">Breakout</span>
            <span style="font-size:9px;font-weight:700;color:var(--purple)">${breakout}%</span>
          </div>
          <div class="gex-progress-bar"><div class="gex-progress-fill" style="width:${breakout}%;background:var(--purple)"></div></div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:8px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">Accel</span>
            <span style="font-size:9px;color:var(--sub)">${accelUp > 0 ? '↑'+accelUp : '—'} / ${accelDown > 0 ? '↓'+accelDown : '—'}</span>
          </div>
          <div class="gex-progress-bar" style="position:relative">
            <div style="position:absolute;left:50%;top:0;width:2px;height:100%;background:var(--border);z-index:1"></div>
            <div class="gex-progress-fill" style="position:absolute;${accelUp > accelDown ? `left:50%;width:${Math.min(50,accelPct/2)}%;background:var(--green)` : `right:50%;width:${Math.min(50,(100-accelPct)/2)}%;background:var(--red)`}"></div>
          </div>
        </div>
      </div>

      <div class="gex-metric-grid">
        <div class="gex-metric-box">
          <div class="gex-metric-label">Today's Direction</div>
          <div class="gex-metric-value" style="font-size:11px">${basePath}</div>
          <div class="gex-metric-sub">${basePathSub}</div>
        </div>
        <div class="gex-metric-box">
          <div class="gex-metric-label">Today's Range</div>
          <div class="gex-metric-value" style="font-size:11px;color:${spotIn1sd?'var(--green)':'var(--gold)'}">${emPts > 0 ? `Price is ${spotIn1sd ? '✅ inside' : '⚠️ outside'} normal range` : 'Range not computed'}</div>
          <div class="gex-metric-sub">${emPts > 0 ? `Expected: $${lower1sd.toFixed(2)} – $${upper1sd.toFixed(2)}` : 'Awaiting data'}</div>
        </div>
        <div class="gex-metric-box">
          <div class="gex-metric-label">Pin Distance</div>
          <div class="gex-metric-value" style="font-size:11px">${structTxt}</div>
          <div class="gex-metric-sub">${structSub}</div>
        </div>
        <div class="gex-metric-box">
          <div class="gex-metric-label">Options Positioning</div>
          <div class="gex-metric-value" style="font-size:11px;color:${biasRatio > 2 ? 'var(--red)' : biasRatio < 0.5 ? 'var(--green)' : 'var(--sub)'}">${biasRatio > 2 ? `${biasRatio.toFixed(1)}× more puts` : biasRatio < 0.5 ? `${(1/biasRatio).toFixed(1)}× more calls` : 'Balanced'}</div>
          <div class="gex-metric-sub">${biasRatio > 2 ? 'Institutions hedging downside' : biasRatio < 0.5 ? 'Institutions positioned for upside' : 'No strong institutional lean'}</div>
        </div>
      </div>

      ${regimeCall !== 'NEUTRAL' ? `
      <div style="margin-top:14px;padding:12px 14px;border-radius:6px;background:${regimeCol}0d;border:1px solid ${regimeCol}30">
        <div style="font-size:8px;letter-spacing:1px;color:${regimeCol};font-weight:700;text-transform:uppercase;margin-bottom:4px">Today's Dealer Playbook</div>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px">
          <div style="font-size:14px;font-weight:800;color:${regimeCol}">${
            regimeCall === 'SHORT_THE_POPS' ? '📉 Sell the Rallies'
            : regimeCall === 'BUY_THE_DIPS' ? '📈 Buy the Dips'
            : '⚡ Follow the Trend'
          }</div>
          <div style="font-size:9px;color:var(--sub);text-align:right;max-width:220px;line-height:1.5">
            ${regimeCall === 'SHORT_THE_POPS' ? 'Dealers are fading upside. When price pops up, it gets sold back down. Buy puts on rips.'
            : regimeCall === 'BUY_THE_DIPS'   ? 'Dealers are supporting the floor. When price dips, it bounces. Buy calls on dips.'
            : 'No ceiling/floor today — dealers amplify moves. Follow the breakout, don\'t fight it.'}
          </div>
        </div>
      </div>` : ''}

      <div style="margin-top:16px">
        <div style="font-size:8px;letter-spacing:1.2px;color:var(--sub);font-weight:700;text-transform:uppercase;margin-bottom:10px">TRADE STRUCTURE</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">

          <!-- ⚡ SCALP panel -->
          <div style="padding:12px;background:var(--bg3);border-radius:8px;border:1px solid var(--border)">
            <div style="font-size:10px;font-weight:800;color:var(--gold);letter-spacing:.8px;margin-bottom:10px">⚡ SCALP — 0DTE</div>
            ${(() => {
              let ct, trigger, tgt, stop;
              if (regimeCall === 'SHORT_THE_POPS') {
                ct = 'PUT'; trigger = `Rip to $${callWall > spot ? callWall.toFixed(2) : zeroGamma.toFixed(2)} → fade`;
                tgt = `$${zeroGamma.toFixed(2)} pin`; stop = `Break above $${(spot * 1.003).toFixed(2)}`;
              } else if (regimeCall === 'BUY_THE_DIPS') {
                ct = 'CALL'; trigger = `Dip to $${putWall > 0 && putWall < spot ? putWall.toFixed(2) : zeroGamma.toFixed(2)} → bounce`;
                tgt = `$${zeroGamma.toFixed(2)} pin`; stop = `Break below $${(spot * 0.997).toFixed(2)}`;
              } else {
                const above = spot > zeroGamma;
                ct = above ? 'CALL' : 'PUT';
                trigger = above ? `Hold above $${zeroGamma.toFixed(2)} zero gamma` : `Hold below $${zeroGamma.toFixed(2)} zero gamma`;
                tgt = above ? (callWall ? `Call wall $${callWall.toFixed(2)}` : `+0.5% momentum`) : (putWall ? `Put wall $${putWall.toFixed(2)}` : `-0.5% momentum`);
                stop = `Flip back through $${zeroGamma.toFixed(2)}`;
              }
              const ctCol = ct === 'CALL' ? 'var(--green)' : 'var(--red)';
              return `
                <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
                  <span style="font-size:11px;font-weight:800;padding:2px 8px;border-radius:4px;background:${ctCol}20;color:${ctCol}">${ct}</span>
                  <span style="font-size:9px;color:var(--sub)">ATM · 0DTE · 1 contract</span>
                </div>
                <div style="display:grid;grid-template-columns:auto 1fr;gap:3px 8px;font-size:9px">
                  <span style="color:var(--sub)">Entry</span><span style="color:var(--text);font-weight:600">${trigger}</span>
                  <span style="color:var(--sub)">Target</span><span style="color:var(--green);font-weight:600">${tgt}</span>
                  <span style="color:var(--sub)">Stop</span><span style="color:var(--red);font-weight:600">${stop}</span>
                </div>`;
            })()}
          </div>

          <!-- 🌊 SWING panel -->
          <div style="padding:12px;background:var(--bg3);border-radius:8px;border:1px solid var(--border)">
            <div style="font-size:10px;font-weight:800;color:var(--purple);letter-spacing:.8px;margin-bottom:10px">🌊 SWING — 2–7 DTE</div>
            ${(() => {
              let ct, trigger, tgt, stop;
              if (regimeCall === 'SHORT_THE_POPS') {
                ct = 'PUT'; trigger = `Rejection at call wall $${callWall > 0 ? callWall.toFixed(2) : 'resistance'}`;
                tgt = putWall > 0 ? `Put wall $${putWall.toFixed(2)}` : `Zero gamma $${zeroGamma.toFixed(2)}`;
                stop = `Daily close above $${callWall > 0 ? callWall.toFixed(2) : (spot * 1.005).toFixed(2)}`;
              } else if (regimeCall === 'BUY_THE_DIPS') {
                ct = 'CALL'; trigger = `Bounce off put wall $${putWall > 0 ? putWall.toFixed(2) : 'support'}`;
                tgt = callWall > 0 ? `Call wall $${callWall.toFixed(2)}` : `Zero gamma $${zeroGamma.toFixed(2)}`;
                stop = `Daily close below $${putWall > 0 ? putWall.toFixed(2) : (spot * 0.995).toFixed(2)}`;
              } else {
                const above = spot > zeroGamma;
                ct = above ? 'CALL' : 'PUT';
                trigger = above ? `Breakout confirmed above $${zeroGamma.toFixed(2)}` : `Breakdown confirmed below $${zeroGamma.toFixed(2)}`;
                tgt = above ? (callWall ? `Call wall $${callWall.toFixed(2)}` : `+1.5% from entry`) : (putWall ? `Put wall $${putWall.toFixed(2)}` : `-1.5% from entry`);
                stop = `Reversal back inside $${zeroGamma.toFixed(2)} ±0.3%`;
              }
              const ctCol = ct === 'CALL' ? 'var(--green)' : 'var(--red)';
              return `
                <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
                  <span style="font-size:11px;font-weight:800;padding:2px 8px;border-radius:4px;background:${ctCol}20;color:${ctCol}">${ct}</span>
                  <span style="font-size:9px;color:var(--sub)">OTM · 2–7 DTE · 1 contract</span>
                </div>
                <div style="display:grid;grid-template-columns:auto 1fr;gap:3px 8px;font-size:9px">
                  <span style="color:var(--sub)">Entry</span><span style="color:var(--text);font-weight:600">${trigger}</span>
                  <span style="color:var(--sub)">Target</span><span style="color:var(--green);font-weight:600">${tgt}</span>
                  <span style="color:var(--sub)">Stop</span><span style="color:var(--red);font-weight:600">${stop}</span>
                </div>`;
            })()}
          </div>

        </div>
      </div>
    `;
  } catch(e) {
    card.innerHTML = `<div style="text-align:center;padding:20px;color:var(--sub);font-size:11px">Error loading GEX: ${e.message}</div>`;
  }
}

function _startGEXRefresh() {
  if (_gexRefreshInterval) clearInterval(_gexRefreshInterval);
  _gexRefreshInterval = setInterval(() => {
    const pane = document.getElementById('p-analysis-gex');
    if (!pane || !pane.classList.contains('active')) return;
    const now = new Date();
    const etHour = (now.getUTCHours() - 5 + 24) % 24;
    if (now.getDay() >= 1 && now.getDay() <= 5 && etHour >= 9 && etHour < 16) {
      renderGEXGeorgeStyle(_gexActiveTicker);
    }
  }, 60000);
}

// Wire renderGEXGeorgeStyle into existing loadGex without modifying it
(function() {
  const _orig = window.loadGex;
  if (typeof _orig !== 'function') return;
  window.loadGex = async function() {
    await _orig.apply(this, arguments);
    const t = (typeof _gexTicker !== 'undefined' ? _gexTicker : null) || _gexActiveTicker || 'SPY';
    renderGEXGeorgeStyle(t);
    // Keep range-levels panel in sync on every GEX load
    if (typeof updateGexRangeLevels === 'function') updateGexRangeLevels(t);
  };
  _startGEXRefresh();
})();

// ── GEX Ticker Sync Patch ─────────────────────────────────────────────────────
// On page load _gexActiveTicker (sessionStorage) may differ from _gexTicker ('SPX').
// Bottom panels (gexStatBar, gexWalls3) use _gexTicker — sync them here.
(function _patchGexSync() {
  function _syncGexTickers() {
    if (typeof _gexActiveTicker !== 'undefined' && typeof _gexTicker !== 'undefined') {
      if (_gexActiveTicker !== _gexTicker) {
        if (typeof selectGexTicker === 'function') selectGexTicker(_gexActiveTicker);
      }
    }
  }
  // Sync after initial render (500ms delay so George card loads first)
  document.addEventListener('DOMContentLoaded', function() { setTimeout(_syncGexTickers, 500); });
  // Also patch renderGEXGeorgeStyle so it keeps _gexTicker in sync when called externally
  const _origRender = window.renderGEXGeorgeStyle;
  if (typeof _origRender === 'function') {
    window.renderGEXGeorgeStyle = async function(ticker) {
      if (ticker && typeof _gexTicker !== 'undefined' && ticker !== _gexTicker) {
        _gexTicker = ticker;
      }
      return _origRender.apply(this, arguments);
    };
  }
})();

// ══════════════════════════════════════════════════════
// 🎯 TICKER COCKPIT — Per-ticker options dashboard
// ══════════════════════════════════════════════════════

let _cockpitTicker  = sessionStorage.getItem('cockpitTicker') || 'SPY';
let _cockpitInitted = false;

function initCockpit() {
  renderCockpitTickerSelector();
  loadCockpitData(_cockpitTicker);
  if (!_cockpitInitted) {
    _cockpitInitted = true;
    setInterval(() => {
      // Use ET time for market hours gate
      const _etNow   = new Date(new Date().toLocaleString('en-US', {timeZone:'America/New_York'}));
      const _eh = _etNow.getHours(), _em = _etNow.getMinutes();
      const _mktOpen = (_eh > 9 || (_eh === 9 && _em >= 30)) && _eh < 16;
      const pane = document.getElementById('p-analysis-cockpit');
      if (pane && pane.classList.contains('active') && _mktOpen) {
        loadCockpitData(_cockpitTicker);
      }
    }, 60000);
  }
}

function selectCockpitTicker(ticker) {
  _cockpitTicker = ticker;
  sessionStorage.setItem('cockpitTicker', ticker);
  renderCockpitTickerSelector();   // re-render so inline styles update
  loadCockpitData(ticker);
}

function renderCockpitTickerSelector() {
  const el = document.getElementById('cockpitTickerRow');
  if (!el) return;
  const tickers = ['SPY','QQQ','SPX','IWM','DIA','NVDA','TSLA','AAPL','MSFT','AMZN','META','GOOGL','AMD','COIN','NFLX'];
  el.innerHTML = tickers.map(t =>
    `<button class="cockpit-ticker-btn ${t===_cockpitTicker?'active':''}"
       data-ticker="${t}"
       onclick="selectCockpitTicker('${t}')"
       style="font-size:10px;padding:4px 10px;border-radius:4px;cursor:pointer;
         border:1px solid ${t===_cockpitTicker?'var(--accent)':'var(--border)'};
         background:${t===_cockpitTicker?'rgba(97,218,251,0.1)':'var(--bg3)'};
         color:${t===_cockpitTicker?'var(--accent)':'var(--sub)'};
         font-family:'JetBrains Mono',monospace;font-weight:${t===_cockpitTicker?'700':'400'}"
       >${t}</button>`
  ).join('');
}

async function loadCockpitData(ticker) {
  const el = document.getElementById('cockpitBody');
  if (!el) return;
  el.innerHTML = `<div style="text-align:center;padding:30px;color:var(--sub);font-size:11px">
    Loading ${ticker} cockpit...</div>`;
  try {
    const [gex, flow] = await Promise.all([
      fetch(`/api/options/gex?ticker=${ticker}`).then(r=>r.json()).catch(()=>({})),
      fetch('/api/flow/signals').then(r=>r.json()).catch(()=>({})),
    ]);
    renderCockpit(ticker, gex, flow);
  } catch(e) {
    el.innerHTML = `<div style="text-align:center;padding:30px;color:var(--red);font-size:11px">Error: ${e.message}</div>`;
  }
}

function renderCockpit(ticker, gex, flow) {
  const el = document.getElementById('cockpitBody');
  if (!el) return;

  // ── Normalize data ────────────────────────────────────────
  const _rawRegime  = (gex.regime || '').toLowerCase();
  const isPos       = ['pinned','positive','positive_gamma'].includes(_rawRegime) || (gex.net_gex||0) > 0;
  const isNeg       = ['negative','negative_gamma'].includes(_rawRegime);
  const regimeLabel = isPos ? 'POSITIVE γ' : isNeg ? 'NEGATIVE γ' : 'LOW VOL';
  const regimeCol   = isPos ? 'var(--green)' : isNeg ? 'var(--red)' : 'var(--gold)';

  const callWall  = gex.call_wall  || 0;
  const putWall   = gex.put_wall   || 0;
  const zeroGamma = gex.zero_gamma || 0;
  const spot      = gex.spot       || 0;
  const netGex    = gex.net_gex    || 0;   // already in billions
  const maxPain   = gex.max_pain   || 0;
  const bsl       = gex.bsl        || [];
  const ssl       = gex.ssl        || [];
  const eqh       = gex.eqh        || [];
  const eql       = gex.eql        || [];
  const ladder    = gex.ladder     || gex.top_strikes || [];

  const pctCall = callWall && spot ? ((callWall - spot) / spot * 100).toFixed(1) : '—';
  const pctPut  = putWall  && spot ? ((spot - putWall)  / spot * 100).toFixed(1) : '—';
  const pctZero = zeroGamma && spot ? ((spot - zeroGamma) / spot * 100).toFixed(1) : '—';

  const regimeCallText = gex.regime_call === 'FOLLOW_MOMENTUM' ? 'FOLLOW MOMENTUM'
                       : gex.regime_call === 'BUY_THE_DIPS'   ? 'BUY THE DIPS'
                       : isNeg ? 'FOLLOW MOMENTUM' : isPos ? 'SHORT THE POPS' : 'NEUTRAL';
  const rcCol = regimeCallText === 'FOLLOW MOMENTUM' ? 'var(--red)'
              : regimeCallText === 'BUY THE DIPS'    ? 'var(--green)' : 'var(--gold)';

  // Flow for this ticker
  const tickerFlow = (flow.signals || []).filter(s => s.ticker === ticker).slice(0, 5);

  // ── Build Strike Ladder ───────────────────────────────────
  const sortedLadder = [...ladder].sort((a,b) => b.strike - a.strike);
  const maxAbsGex    = Math.max(...sortedLadder.map(s => Math.abs(s.net_gex || 0)), 1);

  const fmtDollar = v => {
    const abs = Math.abs(v);
    if (abs >= 1e9)  return (v/1e9).toFixed(1)+'B';
    if (abs >= 1e6)  return (v/1e6).toFixed(0)+'M';
    if (abs >= 1e3)  return (v/1e3).toFixed(0)+'K';
    return v.toFixed(0);
  };

  // Key-level tag builder — a strike can have BOTH walls if they coincide
  const getStrikeTags = strike => {
    const tags = [];
    const isCallWall = callWall && strike === callWall;
    const isPutWall  = putWall  && strike === putWall;
    const isZero     = zeroGamma && Math.abs(strike - zeroGamma) < 0.6;
    const isPin      = (gex.pin_strikes||[]).some(p => Math.abs((p.strike||p) - strike) < 0.6);
    const is2ndCall  = gex.second_call && Math.abs(strike - gex.second_call) < 0.6;
    const is2ndPut   = gex.second_put  && Math.abs(strike - gex.second_put)  < 0.6;
    // When call_wall === put_wall, label it BOTH WALLS
    if (isCallWall && isPutWall) tags.push({ label:'BOTH WALLS', col:'#f59e0b' });
    else {
      if (isCallWall) tags.push({ label:'CALL WALL', col:'#00ff88' });
      if (isPutWall)  tags.push({ label:'PUT WALL',  col:'#ff4444' });
    }
    if (isZero)    tags.push({ label:'ZERO γ',    col:'#ffd700' });
    if (isPin)     tags.push({ label:'PIN',         col:'#a78bfa' });
    if (is2ndCall && !isCallWall) tags.push({ label:'2nd CALL', col:'#6ee7b7' });
    if (is2ndPut  && !isPutWall)  tags.push({ label:'2nd PUT',  col:'#fca5a5' });
    return tags;
  };

  // 1SD range for shading
  const upper1sdVal = parseFloat(gex.upper_1sd || 0);
  const lower1sdVal = parseFloat(gex.lower_1sd || 0);

  const ladderRows = sortedLadder.map(s => {
    const st       = s.strike;
    const isSpot   = spot && Math.abs(st - spot) < 0.75;
    const in1sd    = upper1sdVal && lower1sdVal && st <= upper1sdVal && st >= lower1sdVal;
    const tags     = getStrikeTags(st);
    const callBar  = Math.abs(s.call_gex||0) / maxAbsGex * 100;
    const putBar   = Math.abs(s.put_gex||0)  / maxAbsGex * 100;
    const netPos   = (s.net_gex||0) >= 0;
    const rowBg    = isSpot  ? 'rgba(14,165,233,0.10)' :
                    in1sd   ? 'rgba(255,255,255,0.015)' : 'transparent';
    const rowBd    = isSpot  ? '1px solid rgba(14,165,233,0.35)' :
                    tags.length ? `1px solid ${tags[0].col}22` : '1px solid transparent';

    const tagHtml = tags.map(t =>
      `<span style="font-size:7px;font-weight:700;padding:1px 4px;border-radius:2px;margin-right:2px;
        white-space:nowrap;background:${t.col}20;color:${t.col};letter-spacing:0.3px">${t.label}</span>`
    ).join('');

    return `<div style="display:grid;grid-template-columns:46px 1fr 52px;
      align-items:center;padding:3px 6px;border-radius:4px;gap:6px;
      background:${rowBg};border:${rowBd};margin-bottom:1px">

      <!-- Strike price -->
      <div style="font-size:10px;font-family:'JetBrains Mono',monospace;
        font-weight:${isSpot?'800':'500'};color:${isSpot?'var(--accent)':'var(--text)'}">
        ${st.toFixed(0)}${isSpot?' ◀':''}
      </div>

      <!-- Tag + dual bar -->
      <div>
        ${tagHtml ? `<div style="margin-bottom:3px">${tagHtml}</div>` : ''}
        <div style="display:flex;gap:2px;align-items:center;height:7px">
          <!-- Call bar (green, right-anchored) -->
          <div style="flex:1;height:100%;background:var(--bg3);border-radius:2px;overflow:hidden;position:relative">
            <div style="position:absolute;right:0;width:${callBar.toFixed(0)}%;height:100%;
              background:#10b981;opacity:0.8;border-radius:2px"></div>
          </div>
          <!-- Put bar (red, left-anchored) -->
          <div style="flex:1;height:100%;background:var(--bg3);border-radius:2px;overflow:hidden;position:relative">
            <div style="position:absolute;left:0;width:${putBar.toFixed(0)}%;height:100%;
              background:#f43f5e;opacity:0.8;border-radius:2px"></div>
          </div>
        </div>
      </div>

      <!-- Net GEX dollar -->
      <div style="font-size:8px;font-family:'JetBrains Mono',monospace;
        color:${netPos?'#6ee7b7':'#fca5a5'};text-align:right;font-weight:600">
        ${netPos?'+':'-'}${fmtDollar(Math.abs(s.net_gex||0))}
      </div>
    </div>`;
  }).join('');

  // ── Extra fields from gex_latest ─────────────────────────
  const upper1sd   = parseFloat(gex.upper_1sd || 0);
  const lower1sd   = parseFloat(gex.lower_1sd || 0);
  const emPts      = parseFloat(gex.expected_move_pts || 0);
  const accelUp    = parseFloat(gex.accel_up   || 0);
  const accelDown  = parseFloat(gex.accel_down || 0);
  const biasRatio  = parseFloat(gex.bias_ratio || 0);
  const pins       = gex.pin_strikes || [];
  const cliff      = gex.cliff || {};
  const updatedAt  = gex.updated || '—';
  const aboveZero  = gex.above_zero_gamma;

  // accel values are 0-100 scores, not dollar amounts
  const fmtAccel = v => {
    if (!v && v !== 0) return '—';
    if (v >= 1e6) { const b = v/1e9; return b>=1 ? b.toFixed(1)+'B' : (v/1e6).toFixed(0)+'M'; }
    return Math.round(v).toString();
  };

  // ── Render ────────────────────────────────────────────────
  el.innerHTML = `
  <!-- Header strip: spot + regime + last updated -->
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;
    background:var(--bg2);border:1px solid var(--border);border-radius:7px;padding:10px 14px">
    <div>
      <div style="font-size:8px;color:var(--sub);letter-spacing:1px;text-transform:uppercase">SPOT</div>
      <div style="font-size:18px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--accent)">
        ${spot ? '$' + spot.toFixed(2) : '—'}</div>
    </div>
    <div style="width:1px;height:36px;background:var(--border)"></div>
    <div>
      <div style="font-size:8px;color:var(--sub);letter-spacing:1px;text-transform:uppercase">REGIME</div>
      <div style="font-size:12px;font-weight:800;color:${regimeCol}">${regimeLabel}</div>
    </div>
    <div>
      <div style="font-size:8px;color:var(--sub);letter-spacing:1px;text-transform:uppercase">PLAYBOOK</div>
      <div style="font-size:12px;font-weight:800;color:${rcCol}">${regimeCallText}</div>
    </div>
    <div>
      <div style="font-size:8px;color:var(--sub);letter-spacing:1px;text-transform:uppercase">NET GEX</div>
      <div style="font-size:12px;font-weight:800;font-family:'JetBrains Mono',monospace;
        color:${netGex>=0?'var(--green)':'var(--red)'}">
        ${netGex>=0?'+':''}${netGex.toFixed(2)}B</div>
    </div>
    ${aboveZero !== undefined ? `<div>
      <div style="font-size:8px;color:var(--sub);letter-spacing:1px;text-transform:uppercase">vs ZERO γ</div>
      <div style="font-size:12px;font-weight:800;color:${aboveZero?'var(--green)':'var(--red)'}">
        ${aboveZero ? '▲ ABOVE' : '▼ BELOW'}</div>
    </div>` : ''}
    <div style="margin-left:auto;text-align:right">
      <div style="font-size:8px;color:var(--sub)">as of</div>
      <div style="font-size:9px;color:var(--sub)">${updatedAt}</div>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:180px 1fr 190px;gap:10px;min-height:400px">

    <!-- LEFT: KEY LEVELS -->
    <div style="background:var(--bg2);border-radius:7px;padding:12px;border:1px solid var(--border)">
      <div style="font-size:8px;font-weight:700;letter-spacing:1px;color:var(--sub);
        text-transform:uppercase;margin-bottom:10px">KEY LEVELS</div>

      ${callWall ? `<div style="margin-bottom:8px">
        <div style="font-size:8px;color:#00ff88;font-weight:700;letter-spacing:0.5px">CALL WALL</div>
        <div style="font-size:14px;font-weight:800;font-family:'JetBrains Mono',monospace;color:#00ff88">
          $${callWall}</div>
        <div style="font-size:9px;color:var(--sub)">+${pctCall}% away</div>
      </div>` : ''}

      ${putWall ? `<div style="margin-bottom:8px">
        <div style="font-size:8px;color:#ff4444;font-weight:700;letter-spacing:0.5px">PUT WALL</div>
        <div style="font-size:14px;font-weight:800;font-family:'JetBrains Mono',monospace;color:#ff4444">
          $${putWall}</div>
        <div style="font-size:9px;color:var(--sub)">-${pctPut}% away</div>
      </div>` : ''}

      ${zeroGamma ? `<div style="margin-bottom:8px">
        <div style="font-size:8px;color:#ffd700;font-weight:700;letter-spacing:0.5px">ZERO γ</div>
        <div style="font-size:13px;font-weight:800;font-family:'JetBrains Mono',monospace;color:#ffd700">
          $${zeroGamma}</div>
        <div style="font-size:9px;color:var(--sub)">${pctZero}% from spot</div>
      </div>` : ''}

      ${upper1sd && lower1sd ? `<div style="margin-bottom:8px;padding:6px 8px;
        background:rgba(97,218,251,0.05);border:1px solid rgba(97,218,251,0.15);border-radius:4px">
        <div style="font-size:8px;color:var(--accent);font-weight:700;letter-spacing:0.5px;margin-bottom:4px">
          EXP MOVE ±${emPts.toFixed(1)}pts</div>
        <div style="font-size:9px;font-family:'JetBrains Mono',monospace;color:var(--green)">
          ▲ $${upper1sd.toFixed(2)}</div>
        <div style="font-size:9px;font-family:'JetBrains Mono',monospace;color:var(--red)">
          ▼ $${lower1sd.toFixed(2)}</div>
      </div>` : ''}

      ${pins.length ? `<div style="margin-bottom:6px">
        <div style="font-size:8px;color:#a78bfa;font-weight:700;letter-spacing:0.5px;margin-bottom:3px">PIN STRIKES</div>
        ${pins.slice(0,4).map(p=>{const s=typeof p==='object'?(p.strike||p.price||p):p;return`<div style="font-size:10px;font-family:'JetBrains Mono',monospace;color:#a78bfa">$${(+s).toFixed(2)}</div>`}).join('')}
      </div>` : ''}

      ${cliff && cliff.expiry ? `<div style="padding:5px 7px;background:rgba(255,140,0,0.12);
        border:1px solid rgba(255,140,0,0.3);border-radius:4px;margin-top:4px">
        <div style="font-size:8px;color:var(--gold);font-weight:700">⚡ CLIFF ${cliff.expiry}</div>
        <div style="font-size:8px;color:var(--sub)">Large gamma expires soon</div>
      </div>` : ''}
    </div>

    <!-- CENTER: STRIKE LADDER / ACCELERATION / BIAS -->
    <div style="display:flex;flex-direction:column;gap:8px">

      <!-- Acceleration + Bias ratio -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
        <div style="background:var(--bg2);border-radius:6px;padding:10px 12px;border:1px solid var(--border)">
          <div style="font-size:8px;color:var(--sub);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">
            ACCEL UP</div>
          <div style="font-size:13px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--green)">
            ${fmtAccel(accelUp)}</div>
          <div style="font-size:8px;color:var(--sub);margin-top:2px">dealer buying pressure</div>
        </div>
        <div style="background:var(--bg2);border-radius:6px;padding:10px 12px;border:1px solid var(--border)">
          <div style="font-size:8px;color:var(--sub);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">
            ACCEL DOWN</div>
          <div style="font-size:13px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--red)">
            ${fmtAccel(accelDown)}</div>
          <div style="font-size:8px;color:var(--sub);margin-top:2px">dealer selling pressure</div>
        </div>
        <div style="background:var(--bg2);border-radius:6px;padding:10px 12px;border:1px solid var(--border)">
          <div style="font-size:8px;color:var(--sub);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">
            BIAS RATIO</div>
          <div style="font-size:13px;font-weight:800;font-family:'JetBrains Mono',monospace;
            color:${biasRatio>3?'var(--red)':biasRatio>1.5?'var(--gold)':'var(--green)'}">
            ${biasRatio.toFixed(1)}x</div>
          <div style="font-size:8px;color:var(--sub);margin-top:2px">
            ${biasRatio>3?'⛔ HARD BLOCK — extreme skew':biasRatio>1.5?'⚠️ Directional skew':'✅ Balanced flow'}</div>
        </div>
      </div>

      <!-- Strike Ladder -->
      <div style="background:var(--bg2);border-radius:7px;border:1px solid var(--border);overflow:hidden;flex:1">
        <div style="display:flex;align-items:center;justify-content:space-between;
          padding:8px 10px;background:var(--bg3);border-bottom:1px solid var(--border)">
          <div style="font-size:8px;font-weight:700;letter-spacing:1px;color:var(--sub);text-transform:uppercase">
            STRIKE LADDER</div>
          <div style="font-size:8px;color:var(--sub)">SPOT
            <span style="font-family:'JetBrains Mono',monospace;color:var(--accent);font-weight:800;margin-left:4px">
              ${spot ? '$'+spot.toFixed(2) : '—'}</span>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:52px 68px 1fr 60px;gap:6px;
          padding:4px 6px;border-bottom:1px solid var(--border)">
          <div style="font-size:7px;color:var(--sub);letter-spacing:1px">STRIKE</div>
          <div style="font-size:7px;color:var(--sub);letter-spacing:1px">LABEL</div>
          <div style="font-size:7px;color:var(--sub);letter-spacing:1px">GEX BAR</div>
          <div style="font-size:7px;color:var(--sub);letter-spacing:1px;text-align:right">$GEX</div>
        </div>
        <div style="padding:4px 0;max-height:280px;overflow-y:auto">
          ${sortedLadder.length ? ladderRows : (() => {
            // No full ladder yet (GEX not recomputed since engine restart) — show key levels
            const _cwEqPw = callWall && putWall && callWall === putWall;
            const _kl = _cwEqPw
              ? [
                  { strike: callWall,  label: 'BOTH WALLS', col: '#f59e0b' },
                  zeroGamma && { strike: zeroGamma, label: 'ZERO γ', col: '#ffd700' },
                  gex.second_call && { strike: gex.second_call, label: '2nd CALL', col: '#6ee7b7' },
                  gex.second_put  && { strike: gex.second_put,  label: '2nd PUT',  col: '#fca5a5' },
                ].filter(Boolean).sort((a,b) => b.strike - a.strike)
              : [
                  callWall  && { strike: callWall,  label: 'CALL WALL', col: '#00ff88' },
                  zeroGamma && { strike: zeroGamma, label: 'ZERO γ',    col: '#ffd700' },
                  putWall   && { strike: putWall,   label: 'PUT WALL',  col: '#ff4444' },
                  gex.second_call && { strike: gex.second_call, label: '2nd CALL', col: '#6ee7b7' },
                  gex.second_put  && { strike: gex.second_put,  label: '2nd PUT',  col: '#fca5a5' },
                ].filter(Boolean).sort((a,b) => b.strike - a.strike);
            if (!_kl.length) return `<div style="padding:20px;text-align:center;color:var(--sub);font-size:11px">Reload after next GEX compute for full ladder</div>`;
            return _kl.map(k => `<div style="display:flex;justify-content:space-between;align-items:center;
              padding:5px 6px;border-bottom:1px solid var(--border)">
              <span style="font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--text)">${k.strike}</span>
              <span style="font-size:9px;font-weight:700;padding:2px 6px;border-radius:3px;
                background:${k.col}20;color:${k.col}">${k.label}</span>
            </div>`).join('');
          })()}
        </div>
      </div>
    </div>

    <!-- RIGHT: REGIME + FLOW -->
    <div style="display:flex;flex-direction:column;gap:10px">

      <!-- Regime block -->
      <div style="background:var(--bg2);border-radius:7px;padding:12px;border:1px solid var(--border)">
        <div style="font-size:8px;font-weight:700;letter-spacing:1px;color:var(--sub);
          text-transform:uppercase;margin-bottom:8px">ARKA PLAYBOOK</div>
        <div style="font-size:10px;font-weight:700;color:${rcCol};margin-bottom:6px">${regimeCallText}</div>
        <div style="font-size:9px;color:var(--sub);line-height:1.5;margin-bottom:8px">
          ${regimeCallText==='SHORT THE POPS' ? 'Fade rallies — dealers absorb moves near call wall. Prefer PUTs on rips.' :
            regimeCallText==='FOLLOW MOMENTUM' ? 'Ride the trend — dealers amplify direction. Prefer 0DTE with momentum.' :
            regimeCallText==='BUY THE DIPS' ? 'Buy pullbacks — spot near/below zero gamma. CALLs on dips.' : 'No strong bias — wait for clearer setup.'}
        </div>
        <div style="border-top:1px solid var(--border);padding-top:8px">
          ${[
            ['DTE',    isNeg ? '0DTE preferred' : '1DTE preferred', isNeg ? 'var(--gold)' : 'var(--sub)'],
            ['Target', '+10% premium',  'var(--green)'],
            ['Stop',   '-30% premium',  'var(--red)'],
            ['Qty',    '2 contracts',   'var(--text)'],
          ].map(([l,v,c])=>`<div style="display:flex;justify-content:space-between;padding:2px 0">
            <span style="font-size:8px;color:var(--sub)">${l}</span>
            <span style="font-size:8px;font-weight:700;color:${c}">${v}</span>
          </div>`).join('')}
        </div>
      </div>

      <!-- Flow block -->
      <div style="background:var(--bg2);border-radius:7px;padding:12px;border:1px solid var(--border);flex:1">
        <div style="font-size:8px;font-weight:700;letter-spacing:1px;color:var(--sub);
          text-transform:uppercase;margin-bottom:8px">FLOW — ${ticker}</div>
        ${tickerFlow.length ? tickerFlow.map(s => {
          const bull = (s.bias||'').toUpperCase().includes('BULL') || (s.bias||'').toUpperCase().includes('CALL');
          const col  = bull ? 'var(--green)' : 'var(--red)';
          const conf = parseFloat(s.confidence||0);
          return `<div style="padding:5px 0;border-bottom:1px solid var(--border)">
            <div style="display:flex;justify-content:space-between;margin-bottom:3px">
              <span style="font-size:9px;font-weight:700;color:${col}">${bull?'📈':'📉'} ${s.bias||'?'}</span>
              <span style="font-size:9px;font-family:'JetBrains Mono',monospace;color:${col}">${conf.toFixed(0)}%</span>
            </div>
            <div style="height:3px;background:var(--bg3);border-radius:2px;overflow:hidden">
              <div style="width:${Math.min(100,conf)}%;height:100%;background:${col};border-radius:2px"></div>
            </div>
          </div>`;
        }).join('') : `<div style="text-align:center;padding:20px 0;color:var(--sub);font-size:10px">
          No ${ticker} flow signals</div>`}
      </div>

    </div>
  </div>`;
}
