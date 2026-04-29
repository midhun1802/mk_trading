/* ═══════════════════════════════════════════════════════════
   CHAKRA — system.js
   System health · modules · crons · pipeline · cache health
   ═══════════════════════════════════════════════════════════ */

function sysAlert(msg, type = 'warn') {
  const el = $('sysAlerts'); if (!el) return;
  const color = type === 'err' ? '#ff2d55' : type === 'ok' ? '#00ff9d' : '#ffb347';
  el.innerHTML += `<div style="background:${color}18;border:1px solid ${color}44;border-radius:6px;
    padding:8px 12px;margin-bottom:6px;font-size:11px;color:${color}">${msg}</div>`;
}

async function loadSystem() {
  const alerts = $('sysAlerts'); if (alerts) alerts.innerHTML = '';
  const [acct, internals, arkaSession, signals] = await Promise.all([
    get('/api/account'), get('/api/internals'), get('/api/arka/session'), get('/api/signals'),
  ]);

  const set = (id, val, color) => { const e = $(id); if (e) { e.textContent = val; if (color) e.style.color = color; } };

  if (arkaSession) {
    const trades = arkaSession.trades_today || 0, conv = arkaSession.last_conviction || 0;
    set('sysArkaStatus', trades > 0 ? '📈 TRADING' : '👁 WATCHING', trades > 0 ? '#00ff9d' : '#ffb347');
    const badge = $('sysArkaBadge');
    if (badge) { badge.textContent = trades > 0 ? `${trades} TRADES` : 'NO TRADES YET'; badge.style.color = trades > 0 ? '#00ff9d' : '#ffb347'; }
    set('sysArkaConv', `Conviction: ${conv.toFixed(1)} / ${arkaSession.threshold || 55}`);
    set('sysArkaTrades', `Trades today: ${trades}`);
    set('sysArkaSession', `Session: ${arkaSession.session_mode || '—'}`);
  } else { set('sysArkaStatus', '⚠️ NO DATA', '#ff2d55'); sysAlert('⚠️ ARKA session endpoint unreachable', 'err'); }

  if (signals && signals.length > 0) {
    const latest = signals[signals.length - 1];
    const bull = latest.bull_score || 0, bear = latest.bear_score || 0;
    set('sysBull', bull.toFixed(0)); set('sysBear', bear.toFixed(0));
    const bb = $('sysBullBar'), berb = $('sysBearBar');
    if (bb)   bb.style.width   = Math.min(100, bull) + '%';
    if (berb) berb.style.width = Math.min(100, bear) + '%';
    set('sysArjunMode', `Mode: ${latest.action || latest.direction || '—'} · ${latest.ticker || ''}`);
  }

  if (internals) {
    const np = internals.neural_pulse?.score ?? internals.neural_pulse ?? '—';
    const vix = internals.vix?.value ?? '—';
    set('sysNeuralScore', np, np > 60 ? '#00ff9d' : np > 40 ? '#ffb347' : '#ff2d55');
    set('sysVix', vix, vix > 25 ? '#ff2d55' : vix > 18 ? '#ffb347' : '#00ff9d');
    set('sysVixReg', internals.vix?.regime || '');
    set('sysRisk', internals.risk?.mode || '—');
    set('sysHmm', internals.regime || '—');
  } else { sysAlert('⚠️ Market internals unreachable', 'warn'); }

  if (acct) {
    const fmt = v => '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const pv = parseFloat(acct.portfolio_value || 0), cash = parseFloat(acct.cash || 0);
    const pnl = parseFloat(acct.unrealized_pl || 0), pos = acct.positions?.length ?? '—';
    set('sysPortfolio', fmt(pv), '#00ff9d'); set('sysCash', fmt(cash));
    set('sysPnl', (pnl >= 0 ? '+' : '') + fmt(pnl), pnl >= 0 ? '#00ff9d' : '#ff2d55');
    set('sysPositions', pos);
  }

  // Fetch real engine health from /api/health
  let health = null;
  try { health = await fetch('/api/health').then(r => r.json()); } catch {}

  const arkaRunning  = health?.engines?.arka         ?? (arkaSession ? true : false);
  const flowRunning  = health?.engines?.flow_monitor ?? false;
  const flowAgeMin   = health?.data?.flow_signals_age_min ?? null;
  const flowFresh    = health?.data?.flow_fresh ?? false;
  const lastScanTime = arkaSession?.last_scan_time || '—';

  const engines = [
    ['ARKA Engine',
     arkaRunning
       ? `✅ Running${arkaSession?.last_conviction ? ' · conv ' + arkaSession.last_conviction.toFixed(0) : ''}`
       : '❌ Stopped',
     arkaRunning ? '#00ff9d' : '#ff2d55',
     'arka_engine'],
    ['Flow Monitor',
     flowRunning
       ? `✅ Running${flowAgeMin !== null ? ' · ' + flowAgeMin.toFixed(0) + 'm ago' : ''}${!flowFresh ? ' ⚠️' : ''}`
       : '❌ Stopped',
     flowRunning && flowFresh ? '#00ff9d' : flowRunning ? '#ffb347' : '#ff2d55',
     'flow_monitor'],
    ['ARJUN Signals',    signals?.length > 0 ? `✅ ${signals.length} today` : '⏳ None yet', '#ffb347', null],
    ['Market Internals', internals ? '✅ Live' : '❌ Down',              internals ? '#00ff9d' : '#ff2d55', null],
    ['Dashboard API',    '✅ Running',                                  '#00ff9d', null],
  ];
  const rows = $('sysEngineRows');
  if (rows) rows.innerHTML = engines.map(([name, status, color, engine]) =>
    `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)">
      <span style="font-size:11px;color:var(--sub)">${name}</span>
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:11px;font-weight:600;color:${color}">${status}</span>
        ${engine ? `<button onclick="_restartEngine('${engine}')"
          style="font-size:8px;padding:2px 6px;border-radius:3px;cursor:pointer;
          border:1px solid var(--border);background:var(--bg3);color:var(--sub);
          font-family:'JetBrains Mono',monospace">↺</button>` : ''}
      </div>
    </div>`
  ).join('');
}

async function _restartEngine(engine) {
  const btn = event.target;
  btn.textContent = '…';
  btn.disabled = true;
  try {
    const r = await fetch('/api/engine/start?engine=' + engine, {method: 'POST'});
    const d = await r.json();
    btn.textContent = d.error ? '✗' : '✓';
    setTimeout(() => { btn.textContent = '↺'; btn.disabled = false; }, 2000);
    setTimeout(() => loadSystem(), 3000);
  } catch {
    btn.textContent = '✗';
    btn.disabled = false;
  }
}

async function loadCacheHealth() {
  try {
    const d = await fetch('/api/system/health').then(r => r.json());
    const grid = $('sysCacheGrid'), badge = $('sysCacheOverall'), checked = $('sysCacheChecked');
    if (!grid) return;
    if (badge) {
      badge.textContent = d.overall || '—';
      badge.style.background = d.overall === 'HEALTHY' ? 'rgba(0,208,132,0.12)' : d.overall === 'DEGRADED' ? 'rgba(255,179,71,0.12)' : 'rgba(255,61,90,0.12)';
      badge.style.color = d.overall === 'HEALTHY' ? 'var(--green)' : d.overall === 'DEGRADED' ? 'var(--gold)' : 'var(--red)';
    }
    if (checked && d.checked_at) checked.textContent = 'checked ' + d.checked_at;
    if (d.caches) {
      grid.innerHTML = d.caches.map(c => {
        const col = c.status === 'FRESH' ? 'var(--green)' : c.status === 'STALE' ? 'var(--gold)' : c.status === 'OLD' ? 'var(--red)' : 'var(--sub)';
        const icon = c.status === 'MISSING' ? '✗' : c.status === 'FRESH' ? '✓' : '⚠';
        const age = c.age_min != null ? c.age_min + 'm ago' : 'missing';
        return `<div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.04)">
          <span style="font-size:11px">${c.name}</span>
          <span style="font-size:10px;font-weight:700;font-family:'JetBrains Mono',monospace;color:${col}">${icon} ${c.status === 'MISSING' ? 'MISSING' : age}</span>
        </div>`;
      }).join('');
    }
  } catch { const g = $('sysCacheGrid'); if (g) g.innerHTML = '<div style="color:var(--red);font-size:11px">API unavailable</div>'; }
}

async function loadSysModules() {
  const grid = $('sysModuleGrid');
  if (!grid) return;

  // Show loading state
  grid.innerHTML = '<div style="color:var(--sub);font-size:11px;grid-column:1/-1;padding:8px">Loading module status...</div>';

  let status = null;
  try { status = await fetch('/api/modules/status').then(r => r.json()); } catch {}

  // Session mapping and display names
  const SESSION_MAP = {
    dex:'S1', hurst:'S1', vrp:'S1',
    vex:'S2', charm:'S2', entropy:'S2',
    hmm:'S3', ivskew:'S3', iceberg:'S3',
    kyle_lambda:'S4', cot:'S4', prob_dist:'S4',
  };
  const DISPLAY_NAMES = {
    dex:'DEX / GEX', hurst:'Hurst H', vrp:'VRP',
    vex:'VEX / Vanna', charm:'Charm', entropy:'Entropy',
    hmm:'HMM Regime', ivskew:'IV Skew', iceberg:'Iceberg',
    kyle_lambda:'Kyle Lambda', cot:'COT', prob_dist:'Prob Dist',
  };
  const sc = { S1:'#00843D', S2:'#005A8E', S3:'#6A0DAD', S4:'#8B2000' };

  const modules = status?.modules || {};
  const entries = Object.keys(DISPLAY_NAMES).map(key => {
    const m = modules[key] || { status: 'MISSING', age_min: null };
    const s = m.status;
    const color = s === 'OK' ? 'var(--green)' : s === 'AGING' ? 'var(--gold)' : s === 'STALE' ? 'var(--gold)' : 'var(--red)';
    const icon  = s === 'OK' ? '✓' : s === 'MISSING' ? '✗' : '⚠';
    const age   = m.age_min != null ? `${Math.round(m.age_min)}m ago` : 'missing';
    const sess  = SESSION_MAP[key] || 'S1';
    return { name: DISPLAY_NAMES[key], sess, color, icon, age, status: s };
  });

  grid.innerHTML = entries.map(m =>
    `<div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px 12px;position:relative;overflow:hidden">
      <div style="position:absolute;top:0;left:0;width:3px;height:100%;background:${sc[m.sess]}"></div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
        <span style="font-size:11px;font-weight:700;padding-left:6px">${m.name}</span>
        <span style="font-size:9px;background:${sc[m.sess]}22;color:${sc[m.sess]};padding:2px 5px;border-radius:3px">${m.sess}</span>
      </div>
      <div style="font-size:11px;color:${m.color};font-weight:600;padding-left:6px">${m.icon} ${m.age}</div>
    </div>`
  ).join('');
}

async function refreshModules(force) {
  const btn = document.getElementById('moduleRefreshBtn');
  const btnF = document.getElementById('moduleForceBtn');
  if (btn)  { btn.textContent  = '⏳'; btn.disabled  = true; }
  if (btnF) { btnF.textContent = '⏳'; btnF.disabled = true; }
  try {
    const r = await fetch(`/api/modules/refresh?force=${!!force}`, { method: 'POST' });
    const d = await r.json();
    if (d.success) {
      if (btn)  btn.textContent  = '✓ Done';
    } else {
      if (btn)  btn.textContent  = '✗ Error';
    }
    setTimeout(() => {
      if (btn)  { btn.textContent  = '↺ Refresh'; btn.disabled  = false; }
      if (btnF) { btnF.textContent = '⚡ Force';  btnF.disabled = false; }
      loadSysModules();
    }, 2000);
  } catch(e) {
    if (btn)  { btn.textContent  = '↺ Refresh'; btn.disabled  = false; }
    if (btnF) { btnF.textContent = '⚡ Force';  btnF.disabled = false; }
  }
}

function loadSysCrons() {
  const crons = [
    ['7:00 AM','Daily Briefing → Discord','✅','briefing.log'],
    ['7:30 AM','DEX + Hurst + VRP (S1)','✅','session1_modules.log'],
    ['8:00 AM','ARJUN Daily Signals','✅','cron.log'],
    ['8:15 AM','CHAKRA Swings Entry','✅','swings/swings.log'],
    ['8:30 AM','ARKA Engine (nohup)','✅','arka/arka_*.log'],
    ['*/5 min','Flow Monitor + Iceberg','⚠️','session3_modules.log'],
    ['*/5 min','Health Monitor + Kyle Lambda','⚠️','session4_modules.log'],
    ['*/30 min','Entropy + VEX (S2)','⚠️','session2_modules.log'],
    ['*/30 min','ARJUN Healer','✅','chakra/healer.log'],
    ['3:02 PM','ARKA + TARAKA Journal','✅','arka/journal.log'],
    ['Sun 00:00','ARJUN Weekly Retrain','✅','retrain.log'],
  ];
  const body = $('sysCronBody');
  if (body) body.innerHTML = crons.map(([time, job, status, log]) => {
    const color = status === '✅' ? '#00ff9d' : status === '⚠️' ? '#ffb347' : '#ff2d55';
    return `<tr>
      <td style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--blue)">${time}</td>
      <td style="font-size:11px">${job}</td>
      <td style="font-weight:700;color:${color}">${status} ${status === '✅' ? 'OK' : 'NEED CRON'}</td>
      <td style="font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--sub)">logs/${log}</td>
    </tr>`;
  }).join('');
}

function loadSysPipeline() {
  const done = [
    ['12 Power Intelligence Modules','DEX · Hurst · VRP · VEX · Charm · Entropy · HMM · IV Skew · Iceberg · Lambda · COT · Prob Dist'],
    ['ARKA Live Engine','Conviction scoring · fakeout gating · lotto + MOC · paper trades'],
    ['ARJUN Multi-Agent','Bull Agent · Bear Agent · Risk Manager · Coordinator'],
    ['Pre-market gap modifier','Live Polygon snapshot → bias score adjustment at 8am'],
    ['Discord Integration','#arka · #chakra · #app_health · #taraka · #high_stakes'],
    ['Market Internals','Neural Pulse · VIX · SPX levels · sector rotation'],
    ['Dashboard Refactor','Split into CSS + 7 JS modules · CHAKRA-style UI'],
  ];
  const todo = [
    ['VEX Triple-Apply Fix','Remove duplicate VEX blocks in coordinator.py'],
    ['ARKA Options Engine','Enable 0DTE SPXW trading — config.yaml flag'],
    ['ARJUN Swing Executor','Wire signals → Alpaca equity orders'],
    ['TARAKA Complete','Confirm message flow after bot invite'],
    ['Run patch sessions 2-4','Wire Power Intelligence Modules into live scoring'],
    ['7 missing cron jobs','Wire session2/3/4 modules to scheduler'],
    ['SPX Dashboard Widget','Wire MarketRegimeBadge + useMarketData into dashboard'],
    ['Sector Rotation Tab','Full CHAKRA-style UI with flow intelligence grid'],
  ];
  const doneEl = $('sysDoneList'), todoEl = $('sysTodoList');
  if (doneEl) doneEl.innerHTML = done.map(([title, desc]) =>
    `<div style="padding:7px 0;border-bottom:1px solid var(--border)">
      <div style="font-size:11px;font-weight:600;color:var(--green)">✅ ${title}</div>
      <div style="font-size:10px;color:var(--sub);margin-top:2px">${desc}</div>
    </div>`
  ).join('');
  if (todoEl) todoEl.innerHTML = todo.map(([title, desc], i) =>
    `<div style="padding:7px 0;border-bottom:1px solid var(--border)">
      <div style="font-size:11px;font-weight:600;color:var(--blue)">${i + 1}. ${title}</div>
      <div style="font-size:10px;color:var(--sub);margin-top:2px">${desc}</div>
    </div>`
  ).join('');
}
