/* ═══════════════════════════════════════════════════════════
   ARKA — arjun.js  (CHAKRA-style v3)
   Dynamic universe · Collapsible tiers · Enriched stubs
   ═══════════════════════════════════════════════════════════ */

// ── State ──────────────────────────────────────────────────
let _arjunRefreshTimer = null;
const _analyzeCache    = {};   // per-ticker analyze cache

// ── Session state (set in loadSignals, read by card builders) ──
let _isPreMkt  = false;
let _isPostMkt = false;
let pmTickers  = {};   // pre-market scan file tickers (pm_high/pm_low/pm_last)

// ══════════════════════════════════════════════════════════════
//  DYNAMIC UNIVERSE STRATEGY
//
//  TIER 1 — ARJUN SIGNALS   (full George cards, sorted by confidence)
//    • Whatever tickers ARJUN ran on (/api/signals)
//    • + ALWAYS_SHOW core indices
//
//  TIER 2 — SECTOR ETFs     (collapsible, sorted by move size)
//    • All XL* tickers from /api/prices/live
//
//  TIER 3 — MOVERS          (sorted by |chg_pct| desc)
//    • Any live-price ticker moving >= MIN_MOVE_PCT
//
//  TIER 4 — ARKA SWINGS   (shown when Polygon screener runs)
//    • From /api/swings/watchlist
//
//  Tweak thresholds here:
// ══════════════════════════════════════════════════════════════
const UNIVERSE_CRITERIA = {
  MIN_MOVE_PCT:  0.5,
  ALWAYS_SHOW:   ['SPX','RUT','SPY','QQQ','IWM','DIA'],
  SECTOR_PREFIX: 'XL',
  SKIP_TICKERS:  ['^SPX','^RUT','XLF','XLK','XLE','XLV','XLI','XLP','XLY','XLU','XLRE','XLB','XLC'],   // sectors filtered
};

const TIER_CONFIG = {
  arjun:  { label: '🧠  ARJUN SIGNALS',  collapsed: false },
  sector: { label: '📊  SECTOR ETFs',    collapsed: true  },
  movers: { label: '🔥  MOVERS',         collapsed: false },
  chakra: { label: '🌀  ARKA SWINGS',  collapsed: false },
};

// ══════════════════════════════════════════════════════════════
//  LOAD SIGNALS — main entry point
// ══════════════════════════════════════════════════════════════
async function loadSignals() {
  const lu  = $('lastUpdate');
  const now = new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});
  if (lu) lu.innerHTML = '<span class="live-dot"></span>Live · ' + now;

  const grid = $('sigGrid');
  if (!grid) return;
  // Only show spinner on first load — skip if cards already rendered (prevents flash)
  const hasCards = grid.querySelector('[data-sym]');
  if (!hasCards) {
    grid.innerHTML = '<div class="ld"><div class="spin"></div>Building dynamic universe…</div>';
  }

  // Load accuracy widget + pipeline status in background (non-blocking)
  loadArjunAccuracy().catch(() => {});
  loadPipelineStatus().catch(() => {});

  // ── Fetch all data sources in parallel ─────────────────
  const [sigs, px, swings, swingPositions, pmData] = await Promise.all([
    getCached('/api/signals', 30000).catch(() => []),
    getCached('/api/prices/live',      10000).catch(() => ({})),
    getCached('/api/swings/watchlist', 60000).catch(() => ({})),
    getCached('/api/swings/positions', 60000).catch(() => ({})),
    getCached('/api/premarket',        60000).catch(() => ({})),
  ]);

  const prices       = px || {};
  // Merge pre-market live index prices into prices map
  const pmLive = pmData?.live_indexes || {};
  for (const [sym, d] of Object.entries(pmLive)) {
    if (d?.price > 0) {
      prices[sym] = prices[sym] || {};
      prices[sym].pm_price   = d.price;
      prices[sym].pm_chg_pct = d.chg_pct;
      prices[sym].pm_change  = d.change;
      prices[sym].prev_close = d.prev_close;
    }
  }
  // Pre-market levels from scan file (pm_high/pm_low/pm_last per ticker) — module-level state
  pmTickers = pmData?.tickers || {};

  // Detect session: pre-market = before 9:30am ET, post = after 4pm ET
  const _etNow = () => {
    const now = new Date();
    const etStr = now.toLocaleString('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: 'numeric', hour12: false });
    const [h, m] = etStr.split(':').map(Number);
    return { h, m, mins: h * 60 + m };
  };
  const _etMins = _etNow().mins;
  _isPreMkt  = _etMins < 9 * 60 + 30;
  _isPostMkt = _etMins >= 16 * 60;
  const swingTickers = (swings?.candidates || swings?.top5 || [])
    .map(c => (c.ticker || c).toUpperCase())
    .filter(t => !UNIVERSE_CRITERIA.SKIP_TICKERS.includes(t));

  // ── Signal map ─────────────────────────────────────────
  const sigMap = {};
  for (const s of (sigs || [])) {
    const k = (s.ticker || s.symbol || '').toUpperCase();
    if (!k || UNIVERSE_CRITERIA.SKIP_TICKERS.includes(k)) continue;
    if (!sigMap[k] || parseFloat(s.confidence||0) > parseFloat(sigMap[k].confidence||0))
      sigMap[k] = s;
  }

  // ── Build tiered universe ───────────────────────────────
  const seen = new Set();
  const tierMap = {};
  const add = (ticker, tier) => {
    const k = ticker.toUpperCase();
    if (seen.has(k) || UNIVERSE_CRITERIA.SKIP_TICKERS.includes(k)) return;
    seen.add(k); tierMap[k] = tier;
  };

  for (const k of Object.keys(sigMap))             add(k, 'arjun');
  for (const k of UNIVERSE_CRITERIA.ALWAYS_SHOW)   add(k, 'arjun');
  for (const k of Object.keys(prices))
    if (k.startsWith(UNIVERSE_CRITERIA.SECTOR_PREFIX)) add(k, 'sector');
  for (const [k, lv] of Object.entries(prices))
    if (!seen.has(k) && Math.abs(lv.chg_pct||0) >= UNIVERSE_CRITERIA.MIN_MOVE_PCT)
      add(k, 'movers');
  for (const k of swingTickers) if (!seen.has(k))  add(k, 'chakra');

  // ── Fetch live analyze for non-ARJUN tickers ───────────
  const nonArjun = Object.keys(tierMap).filter(k => !sigMap[k]);
  const analyzeResults = await Promise.allSettled(
    nonArjun.map(k => _fetchAnalyze(k))
  );
  const analyzeMap = {};
  nonArjun.forEach((k, i) => {
    if (analyzeResults[i].status === 'fulfilled' && analyzeResults[i].value)
      analyzeMap[k] = analyzeResults[i].value;
  });

  // SPX and RUT have broken price APIs — use SPY/IWM as proxy (awaited so cards render with data)
  const INDEX_PROXY = { SPX: 'SPY', RUT: 'IWM' };
  await Promise.all(Object.entries(INDEX_PROXY).map(async ([idx, proxy]) => {
    if (analyzeMap[idx] && analyzeMap[idx].sections?.technicals) return;
    const proxyData = analyzeMap[proxy]
      || await getCached('/api/ticker/analyze?ticker=' + proxy, 120000).catch(() => null);
    if (proxyData) analyzeMap[idx] = proxyData;
  }));

  // ── Sort within tiers ───────────────────────────────────
  const byTier = { arjun:[], sector:[], movers:[], chakra:[] };
  for (const [k, tier] of Object.entries(tierMap)) byTier[tier].push(k);
  byTier.arjun.sort((a,b) => {
    const aIdx = UNIVERSE_CRITERIA.ALWAYS_SHOW.indexOf(a);
    const bIdx = UNIVERSE_CRITERIA.ALWAYS_SHOW.indexOf(b);
    // Indexes always first, in ALWAYS_SHOW order
    if (aIdx !== -1 && bIdx !== -1) return aIdx - bIdx;
    if (aIdx !== -1) return -1;
    if (bIdx !== -1) return  1;
    // Non-index stocks sorted by confidence descending
    return parseFloat(sigMap[b]?.confidence||0) - parseFloat(sigMap[a]?.confidence||0);
  });
  const sortByMove = arr => arr.sort((a,b) =>
    Math.abs(prices[b]?.chg_pct||0) - Math.abs(prices[a]?.chg_pct||0));
  sortByMove(byTier.sector);
  sortByMove(byTier.movers);
  sortByMove(byTier.chakra);

  // ── Header stats ────────────────────────────────────────
  const allSigs = Object.values(sigMap);
  const b  = allSigs.filter(s => s.signal?.toUpperCase()==='BUY').length;
  const se = allSigs.filter(s => s.signal?.toUpperCase()==='SELL').length;
  const h  = allSigs.filter(s => s.signal?.toUpperCase()==='HOLD').length;
  if ($('sigCnt')) $('sigCnt').textContent =
    Object.keys(tierMap).length + ' LIVE · ' + allSigs.length + ' SIGNALS';
  const swingCandidates = swings?.candidates || swings?.top5 || [];
  const openSwings = swingPositions?.open || [];
  if ($('sigBrk')) $('sigBrk').textContent =
    b + ' BUY · ' + se + ' SELL · ' + h + ' HOLD · ' +
    '' +  // movers removed
    (swingTickers.length ? ' · ' + swingTickers.length + ' SWINGS' : '') +
    (openSwings.length ? ' · ' + openSwings.length + ' OPEN SWINGS' : '');

  // ── Market Regime Bar ──────────────────────────────────
  let html = '';
  try {
    const spyPx  = prices['SPY']  || {};
    const qqqPx  = prices['QQQ']  || {};
    const iwmPx  = prices['IWM']  || {};
    const diaPx  = prices['DIA']  || {};
    const vixPx  = prices['VIX']  || {};
    const spxPx  = prices['SPX']  || {};

    // Fetch sparklines in parallel (non-blocking — render bar first, inject after)
    const SPARK_TICKERS = ['SPY','QQQ','IWM','DIA'];

    const _sparkSvg = (closes, col, w=52, h=20) => {
      if (!closes || closes.length < 2) return `<svg width="${w}" height="${h}"></svg>`;
      const mn = Math.min(...closes), mx = Math.max(...closes);
      const rng = mx - mn || 0.01;
      const pts = closes.map((c, i) => {
        const x = (i / (closes.length - 1)) * w;
        const y = h - ((c - mn) / rng) * (h - 2) - 1;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(' ');
      return `<svg width="${w}" height="${h}" style="display:block">
        <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
      </svg>`;
    };

    const fmtChg = (p) => {
      const v = parseFloat(p.chg_pct || 0);
      const col = v > 0 ? 'var(--green)' : v < 0 ? 'var(--red)' : 'var(--sub)';
      return `<span style="color:${col};font-size:9px">${v >= 0 ? '+' : ''}${v.toFixed(2)}%</span>`;
    };
    const fmtPx = (p, label, sparkId) => {
      const price   = parseFloat(p.price || 0);
      if (!price) return '';
      const pct     = parseFloat(p.chg_pct || 0);
      const col     = pct > 0 ? 'var(--green)' : pct < 0 ? 'var(--red)' : 'var(--sub)';
      // Pre-market data: prefer pm_price if available and we're in extended hours
      const pmPrice = parseFloat(p.pm_price || 0);
      const pmPct   = parseFloat(p.pm_chg_pct || 0);
      const showPm  = (_isPreMkt || _isPostMkt) && pmPrice > 0;
      const dispPrice = showPm ? pmPrice : price;
      const dispPct   = showPm ? pmPct   : pct;
      const dispCol   = dispPct > 0 ? 'var(--green)' : dispPct < 0 ? 'var(--red)' : 'var(--sub)';
      const sessionBadge = _isPreMkt
        ? `<span style="font-size:6px;font-weight:700;letter-spacing:.8px;padding:1px 4px;
            border-radius:2px;background:rgba(255,204,0,0.15);color:var(--gold);
            border:1px solid rgba(255,204,0,0.3)">PRE</span>`
        : _isPostMkt
        ? `<span style="font-size:6px;font-weight:700;letter-spacing:.8px;padding:1px 4px;
            border-radius:2px;background:rgba(100,100,255,0.15);color:#8888ff;
            border:1px solid rgba(100,100,255,0.3)">POST</span>`
        : '';
      return `<div style="display:flex;flex-direction:column;align-items:center;padding:0 12px;
        border-right:1px solid var(--border);gap:2px">
        <div style="display:flex;align-items:center;gap:3px">
          <span style="font-size:7px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase">${label}</span>
          ${sessionBadge}
        </div>
        <span style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:800">${dispPrice.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
        <span style="color:${dispCol};font-size:9px">${dispPct >= 0 ? '+' : ''}${dispPct.toFixed(2)}%</span>
        ${showPm && price > 0 ? `<span style="font-size:7px;color:var(--sub)">prev ${price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>` : ''}
        <div id="${sparkId}" style="margin-top:2px">${_sparkSvg([], dispCol)}</div>
      </div>`;
    };

    // Regime from SPY 5-day trend (use pm_chg_pct during extended hours)
    const spy5 = _isPreMkt || _isPostMkt
      ? parseFloat(spyPx.pm_chg_pct || spyPx.chg_pct || 0)
      : parseFloat(spyPx.chg_pct || 0);
    const vix  = parseFloat(vixPx.price || 0);
    let regimeLabel = 'NEUTRAL', regimeCol = 'var(--sub)', regimeBg = 'var(--bg3)';
    if (spy5 <= -1.5 || vix >= 25) {
      regimeLabel = 'RISK OFF'; regimeCol = 'var(--red)'; regimeBg = 'rgba(255,61,90,0.12)';
    } else if (spy5 >= 1.5 && vix < 20) {
      regimeLabel = 'BULL TREND'; regimeCol = 'var(--green)'; regimeBg = 'rgba(0,208,132,0.12)';
    } else if (vix >= 20 && vix < 25) {
      regimeLabel = 'ELEVATED VOL'; regimeCol = 'var(--gold)'; regimeBg = 'rgba(255,204,0,0.1)';
    }

    const indexItems = [
      ['SPX', spxPx, 'spark-spx'],
      ['SPY', spyPx, 'spark-spy'],
      ['QQQ', qqqPx, 'spark-qqq'],
      ['IWM', iwmPx, 'spark-iwm'],
      ['DIA', diaPx, 'spark-dia'],
    ].filter(([,p]) => parseFloat(p.price||0) > 0);

    html += `<div style="grid-column:1/-1;background:var(--bg2);border:1px solid var(--border);
      border-radius:8px;padding:10px 16px;margin-bottom:4px;display:flex;
      align-items:center;justify-content:space-between;overflow-x:auto;gap:8px">
      <div style="display:flex;align-items:center;gap:0">
        ${indexItems.map(([l, p, sid]) => fmtPx(p, l, sid)).join('')}
      </div>
      <div style="display:flex;align-items:center;gap:10px;flex-shrink:0">
        ${vix ? `<div style="display:flex;flex-direction:column;align-items:center;padding:0 12px;border-right:1px solid var(--border)">
          <span style="font-size:7px;letter-spacing:1.5px;color:var(--sub)">VIX</span>
          <span style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:800;
            color:${vix>=25?'var(--red)':vix>=20?'var(--gold)':'var(--green)'}">${vix.toFixed(2)}</span>
          <span style="font-size:8px;color:var(--sub)">${vix>=25?'HIGH':vix>=20?'ELEV':'CALM'}</span>
        </div>` : ''}
        <div style="background:${regimeBg};border:1px solid ${regimeCol}40;border-radius:6px;
          padding:6px 14px;text-align:center">
          <div style="font-size:7px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;margin-bottom:2px">Regime</div>
          <div style="font-size:11px;font-weight:800;color:${regimeCol};letter-spacing:1px">${regimeLabel}</div>
        </div>
      </div>
    </div>`;

    // Inject sparklines async after grid renders (non-blocking)
    setTimeout(async () => {
      for (const t of SPARK_TICKERS) {
        const sparkEl = document.getElementById('spark-' + t.toLowerCase());
        if (!sparkEl) continue;
        try {
          const sd = await getCached(`/api/prices/sparkline?ticker=${t}&bars=30`, 60000);
          if (!sd || !sd.closes || sd.closes.length < 2) continue;
          const pct = parseFloat(prices[t]?.chg_pct || 0);
          const col = pct > 0 ? 'var(--green)' : pct < 0 ? 'var(--red)' : 'var(--sub)';
          sparkEl.innerHTML = _sparkSvg(sd.closes, col);
        } catch(_) {}
      }
    }, 200);
  } catch(_) {}

  const renderTier = (tier, tickers) => {
    if (!tickers.length) return;
    const cfg = TIER_CONFIG[tier];
    const id  = 'tier-' + tier;

    // Collapsible tier header
    html += `
    <div style="grid-column:1/-1;margin:8px 0 6px">
      <div onclick="_toggleTier('${id}')"
        style="display:flex;justify-content:space-between;align-items:center;
          padding:9px 14px;background:var(--bg3);border-radius:7px;cursor:pointer;
          border:1px solid var(--border);user-select:none;
          transition:background .15s">
        <span style="font-size:9px;letter-spacing:1.5px;color:var(--text);font-weight:700">
          ${cfg.label}
        </span>
        <div style="display:flex;gap:12px;align-items:center">
          <span style="font-size:8px;color:var(--sub)">${tickers.length} tickers</span>
          <span id="${id}-arrow"
            style="font-size:10px;color:var(--sub);transition:transform .2s;display:inline-block;
              transform:${cfg.collapsed ? 'rotate(-90deg)' : 'rotate(0deg)'}">▼</span>
        </div>
      </div>
    </div>
    <div id="${id}-cards"
      style="grid-column:1/-1;display:${cfg.collapsed ? 'none' : 'grid'};
        grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;
        margin-bottom:8px">`;

    for (const ticker of tickers) {
      const sig     = sigMap[ticker]    || null;
      const analyze = analyzeMap[ticker] || null;
      const isIndex = UNIVERSE_CRITERIA.ALWAYS_SHOW.includes(ticker);
      html += sig
        ? _buildGeorgeCard(sig, prices)
        : isIndex
          ? _buildIndexCard(ticker, prices, analyze)
          : _buildEnrichedStubCard(ticker, prices, analyze, tier);
    }

    html += `</div>`;
  };

  renderTier('arjun',  byTier.arjun);
  // Movers tier removed — not useful for trading decisions

  // ── ARKA Swings: screener candidates as George-style watch cards ──
  const swingCands = (swings?.candidates || swings?.top5 || []).slice(0, 10);
  if (swingCands.length) {
    html += `
    <div style="grid-column:1/-1;margin:8px 0 6px">
      <div onclick="_toggleTier('tier-swing')"
        style="display:flex;justify-content:space-between;align-items:center;
          padding:9px 14px;background:var(--bg3);border-radius:7px;cursor:pointer;
          border:1px solid rgba(155,89,182,0.3);user-select:none">
        <span style="font-size:9px;letter-spacing:1.5px;color:var(--text);font-weight:700">
          🌀  ARKA SWING WATCHLIST
        </span>
        <div style="display:flex;gap:12px;align-items:center">
          <span style="font-size:8px;color:var(--sub)">${swingCands.length} candidates · ≤21 DTE options</span>
          <span id="tier-swing-arrow" style="font-size:10px;color:var(--sub);transition:transform .2s;display:inline-block">▼</span>
        </div>
      </div>
    </div>
    <div id="tier-swing-cards"
      style="grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin-bottom:8px">`;
    for (const c of swingCands) {
      const lv      = px[c.ticker] || {};
      const price   = lv.price || c.price || 0;
      const pct     = lv.chg_pct || c.chg_pct || 0;
      const pctCol  = pct >= 0 ? 'var(--green)' : 'var(--red)';
      const score   = c.score || 0;
      const dir     = (c.direction || 'LONG').toUpperCase();
      const isCall  = dir === 'LONG';
      const recCol  = isCall ? 'var(--green)' : 'var(--red)';
      const recTxt  = isCall ? 'BUY CALLS' : 'BUY PUTS';
      const confCol = score >= 75 ? 'var(--green)' : score >= 60 ? 'var(--gold)' : 'var(--sub)';
      const mom5    = c.mom5 || 0;
      const rsi     = c.rsi || 0;
      const volRatio= c.vol_ratio || 1;
      const tp1     = c.tp1 || 0;
      const stop    = c.stop_loss || c.stop || 0;
      const reasons = (c.reasons || c.catalyst || '').toString().split('|').filter(Boolean).slice(0,2);

      html += `<div class="sc george-card" data-sym="${c.ticker}" style="min-width:0;border:1px solid rgba(155,89,182,0.25)">
        <!-- HEADER -->
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
          <div>
            <div style="font-size:16px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1.1">
              ${c.ticker}
              ${price ? `<span style="font-size:10px;color:${pctCol};font-weight:600;margin-left:4px">
                ${pct>=0?'+':''}${pct.toFixed(2)}%</span>` : ''}
            </div>
            <div style="font-size:11px;color:var(--sub);font-family:'JetBrains Mono',monospace;margin-top:2px">
              ${price ? f$(price) : '—'}
              <span style="font-size:7px;color:var(--purple);margin-left:5px">🌀 ARKA SWING</span>
            </div>
          </div>
          <div style="text-align:right">
            <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">DIRECTION</div>
            <span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;
              background:${recCol}18;color:${recCol};border:1px solid ${recCol}35">
              ${isCall ? 'BULLISH' : 'BEARISH'}
            </span>
          </div>
        </div>

        <!-- RSI · VOL · MOM -->
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;margin-bottom:8px">
          <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
            <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">RSI</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;
              color:${rsi>70?'var(--red)':rsi<30?'var(--green)':'var(--text)'}">
              ${rsi > 0 ? rsi.toFixed(1) : '—'}
            </div>
          </div>
          <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
            <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">VOL</div>
            <div style="font-size:10px;font-weight:700;color:${volRatio>=2?'var(--red)':volRatio>=1.5?'var(--gold)':'var(--sub)'}">
              ${volRatio > 0 ? volRatio.toFixed(1) + 'x' : '—'}
            </div>
          </div>
          <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
            <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">5D MOM</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;
              color:${mom5>0?'var(--green)':'var(--red)'}">
              ${mom5 !== 0 ? (mom5>=0?'+':'')+mom5.toFixed(1)+'%' : '—'}
            </div>
          </div>
        </div>

        <!-- SCORE + LEVELS -->
        <div style="background:var(--bg3);border-radius:5px;padding:7px 9px;margin-bottom:7px;border-left:2px solid var(--purple)">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            <span style="font-size:7px;color:var(--sub);text-transform:uppercase">CONVICTION</span>
            <span style="font-size:12px;font-weight:700;color:${confCol}">${score}/100</span>
          </div>
          ${tp1 ? `<div style="display:flex;justify-content:space-between;font-size:8px;margin-top:3px">
            <span style="color:var(--sub)">TP1</span>
            <span style="color:var(--green);font-family:'JetBrains Mono',monospace;font-weight:700">$${tp1.toFixed(2)}</span>
          </div>` : ''}
          ${stop ? `<div style="display:flex;justify-content:space-between;font-size:8px;margin-top:2px">
            <span style="color:var(--sub)">Stop</span>
            <span style="color:var(--red);font-family:'JetBrains Mono',monospace;font-weight:700">$${stop.toFixed(2)}</span>
          </div>` : ''}
        </div>

        <!-- REASONS -->
        ${reasons.length ? `<div style="background:var(--bg3);border-radius:4px;padding:6px 8px;margin-bottom:7px">
          ${reasons.map(r => `<div style="font-size:8px;color:var(--sub);padding:1px 0">· ${r.trim()}</div>`).join('')}
        </div>` : ''}

        <!-- ARKA RECOMMENDS -->
        <div style="border-top:1px solid var(--border);padding-top:8px">
          <div style="font-size:7px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;margin-bottom:5px;text-align:center">
            A R K A &nbsp; S W I N G S
          </div>
          <div style="color:${recCol};font-size:14px;font-weight:800;text-align:center;padding:4px 0">${recTxt}</div>
          <div style="text-align:center;margin-top:3px">
            <span style="font-size:8px;color:var(--purple)">1 contract · ≤21 DTE · hold up to 3 weeks</span>
          </div>
        </div>

        <!-- FOOTER -->
        <div style="display:flex;justify-content:space-between;align-items:center;
            padding-top:6px;margin-top:6px;border-top:1px solid var(--border);font-size:9px">
          <span style="color:${pctCol};font-family:'JetBrains Mono',monospace;font-weight:700">
            ${pct !== 0 ? (pct>=0?'+':'')+pct.toFixed(2)+'%' : '—'}
          </span>
          <span style="font-size:7px;color:var(--purple)">🌀 SWING</span>
          <span class="chip" style="font-size:7px;background:rgba(155,89,182,0.15);color:var(--purple)">${isCall?'CALL':'PUT'}</span>
        </div>
      </div>`;
    }
    html += `</div>`;
  }

  if (!html) html = `<div class="empty" style="grid-column:1/-1">No live data</div>`;

  grid.innerHTML = html;
  _loadBacktest(null);
  _loadValidation();
  // Enrich SPX/RUT cards with GEX levels
  _enrichIndexCards(['SPX','RUT']);

  if (_arjunRefreshTimer) clearTimeout(_arjunRefreshTimer);
  _arjunRefreshTimer = setTimeout(() => loadSignals(), 60000);

  // ── Auto-refresh stale signals during market hours ─────────────────
  // If cron didn't fire, auto-trigger refreshSignal() for each stale card
  if (!_isPreMkt && !_isPostMkt && sigs?.length) {
    const _staleTickers = (sigs || [])
      .filter(s => s.stale)
      .map(s => (s.ticker || s.symbol || '').toUpperCase())
      .filter(Boolean);
    if (_staleTickers.length > 0) {
      // Stagger refreshes 4s apart to avoid hammering the API
      _staleTickers.forEach((t, i) => {
        setTimeout(() => {
          // Only auto-refresh if still stale (user may have clicked manually)
          const card = document.querySelector(`[data-sym="${t}"]`);
          if (card && card.style.opacity !== '0.5') {
            refreshSignal(t);
          }
        }, 2000 + i * 4000);
      });
    }
  }
}

// ── Toggle tier collapse ────────────────────────────────────
function _toggleTier(id) {
  const cards = document.getElementById(id + '-cards');
  const arrow = document.getElementById(id + '-arrow');
  if (!cards) return;
  const isHidden = cards.style.display === 'none';
  cards.style.display    = isHidden ? 'grid' : 'none';
  if (arrow) arrow.style.transform = isHidden ? 'rotate(0deg)' : 'rotate(-90deg)';
}

// ── Fetch analyze (cached 90s) ──────────────────────────────
async function _fetchAnalyze(ticker) {
  const cached = _analyzeCache[ticker];
  if (cached && Date.now() - cached.ts < 90000) return cached.data;
  try {
    const r    = await fetch('/api/ticker/analyze?ticker=' + ticker);
    const data = await r.json();
    _analyzeCache[ticker] = { ts: Date.now(), data };
    return data;
  } catch { return null; }
}

// ══════════════════════════════════════════════════════════════
//  INDEX CARD — full George-style card for ALWAYS_SHOW tickers
//  (SPX, RUT, SPY, QQQ, IWM, DIA) when no ARJUN signal exists
//  Uses /api/ticker/analyze for conviction, direction, GEX, reasons
// ══════════════════════════════════════════════════════════════
function _buildIndexCard(ticker, px, analyze) {
  const sym      = ticker.toUpperCase();
  // SPX and RUT have no live price feed — use SPY/IWM as price proxy
  const PRICE_PROXY = { SPX: 'SPY', RUT: 'IWM' };
  const priceKey = PRICE_PROXY[sym] || sym;
  const lv       = px && (px[sym] || px[priceKey]) ? (px[sym] || px[priceKey]) : null;
  const price    = lv ? lv.price   : 0;
  const pct      = lv ? lv.chg_pct : 0;
  const pctCol   = pct >= 0 ? 'var(--green)' : 'var(--red)';

  // Pre-market / post-market data
  const pmPrice  = parseFloat(lv?.pm_price   || 0);
  const pmPct    = parseFloat(lv?.pm_chg_pct || 0);
  const pmChange = parseFloat(lv?.pm_change  || 0);
  const prevClose= parseFloat(lv?.prev_close || 0);
  // From premarket scan file (pm_high/pm_low/pm_last)
  const pmFile   = pmTickers[priceKey] || pmTickers[sym] || {};
  const pmLevels = pmFile?.levels || {};
  const pmHigh   = parseFloat(pmLevels.pm_high || 0);
  const pmLow    = parseFloat(pmLevels.pm_low  || 0);
  const pmLast   = parseFloat(pmLevels.pm_last || pmPrice || 0);

  // Session detection (module-level _isPreMkt / _isPostMkt set in loadSignals)
  const inExtended   = _isPreMkt || _isPostMkt;
  const sessionLabel = _isPreMkt ? 'PRE MKT' : _isPostMkt ? 'POST MKT' : 'LIVE';
  const sessionCol   = _isPreMkt ? 'var(--gold)' : _isPostMkt ? '#8888ff' : 'var(--green)';
  const pmCol    = pmPct >= 0 ? 'var(--green)' : 'var(--red)';

  // From /api/ticker/analyze
  const sections   = analyze?.sections || {};
  const priceData  = sections.price    || {};
  const arjunData  = sections.arjun    || {};
  const techData   = sections.technicals || {};
  const gexData    = sections.gex      || {};

  const conviction = parseInt(arjunData.conviction_score || 0);
  const direction  = (arjunData.direction || '').toUpperCase();
  const signal     = (arjunData.signal    || 'HOLD').toUpperCase();
  const reasons    = arjunData.reasons   || [];
  const threshold  = arjunData.threshold || 55;
  const wouldTrade = arjunData.would_trade ?? false;
  const vwapSig    = (priceData.vwap_signal || '').replace(/_/g,' ');
  const aboveVwap  = priceData.vwap_signal === 'ABOVE_VWAP';

  // Technicals
  const rsi        = parseFloat(techData.rsi || 0);
  const rsiSignal  = techData.rsi_signal || '';
  const rsiCol     = rsiSignal==='OVERBOUGHT'?'var(--red)':rsiSignal==='OVERSOLD'?'var(--green)':
                     rsi>70?'var(--red)':rsi<30?'var(--green)':'var(--text)';
  const macdTrend  = (techData.macd_trend || '').toLowerCase();
  const orbTrend   = macdTrend==='bullish'?'BULLISH':macdTrend==='bearish'?'BEARISH':'NEUTRAL';
  const orbCol     = orbTrend==='BULLISH'?'var(--green)':orbTrend==='BEARISH'?'var(--red)':'var(--sub)';
  const adx        = parseFloat(techData.adx || 0);
  const vol        = priceData.volume || 0;

  // GEX
  const callWall   = parseFloat(gexData.call_wall  || 0);
  const putWall    = parseFloat(gexData.put_wall   || 0);
  const zeroGamma  = parseFloat(gexData.zero_gamma || 0);
  const netGex     = parseFloat(gexData.net_gex    || 0);
  const gexRegime  = (gexData.regime || '').replace(/_/g,' ');
  const gexDir     = netGex > 0 ? 'POS' : netGex < 0 ? 'NEG' : 'NEUT';
  const gexDirCol  = netGex > 0 ? 'var(--green)' : netGex < 0 ? 'var(--red)' : 'var(--sub)';
  const regCol     = gexRegime.includes('NEG')?'var(--red)':gexRegime.includes('LOW')?'var(--blue)':'var(--gold)';

  // Sentiment
  const sentCol    = direction==='BULLISH'?'var(--green)':direction==='BEARISH'?'var(--red)':'var(--gold)';
  const confColor  = conviction>=55?'var(--green)':conviction>=35?'var(--gold)':'var(--sub)';
  const alignment  = conviction>=55?'ALIGNED':conviction>=35?'MIXED':'WATCHING';
  const alignCol   = conviction>=55?'var(--green)':conviction>=35?'var(--gold)':'var(--red)';

  // Discomfort (simplified for indexes — no vol ratio available)
  const volRatio   = parseFloat(techData.volume_ratio || 1);
  const netScore   = direction==='BULLISH'?20:direction==='BEARISH'?-20:0;
  const discomfort = _discomfortIndex(rsi, volRatio, netScore, gexDir, orbTrend, adx, 0, gexRegime);
  const discColor  = discomfort>=80?'var(--red)':discomfort>=60?'var(--gold)':'var(--green)';

  // Recommendation
  const recColor  = signal==='BUY'?'var(--green)':signal==='SELL'?'var(--red)':'var(--gold)';
  const recLabel  = signal==='BUY'?'BUY CALLS':signal==='SELL'?'BUY PUTS':'WATCHING';
  const subLabel  = wouldTrade ? 'Conviction threshold met'
    : conviction > 0 ? 'Score '+conviction+' — threshold '+threshold
    : 'Runs 8am ET · GEX live';

  // Tags
  const tags = _buildStructureTags(orbTrend, false, true, null, netGex, direction, 0, gexRegime);

  return `<div class="sc george-card ${signal.toLowerCase()}" data-sym="${sym}" style="min-width:0">

    <!-- ① HEADER -->
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
      <div>
        <div style="font-size:16px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1.1">
          ${sym}
          ${price ? `<span style="font-size:10px;color:${pctCol};font-weight:600;margin-left:4px">
            ${pct>=0?'+':''}${pct.toFixed(2)}%</span>` : ''}
        </div>
        <div style="font-size:11px;color:var(--sub);font-family:'JetBrains Mono',monospace;margin-top:2px">
          ${price ? f$(price) : '—'}
          <span style="font-size:7px;color:var(--sub);margin-left:5px;opacity:.7">📊 INDEX</span>
          ${sessionLabel ? `<span style="font-size:6px;font-weight:700;letter-spacing:.8px;padding:1px 5px;
            border-radius:2px;background:${sessionCol}18;color:${sessionCol};
            border:1px solid ${sessionCol}35;margin-left:4px">${sessionLabel}</span>` : ''}
        </div>
      </div>
      <div style="text-align:right">
        <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">CROWD SENTIMENT</div>
        <span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;
          background:${sentCol}18;color:${sentCol};border:1px solid ${sentCol}35">
          ${direction || '—'}
        </span>
      </div>
    </div>

    <!-- ① PRE-MARKET STRIP (only visible during extended hours or when pm data available) -->
    ${(inExtended || pmLast > 0) ? `
    <div style="background:var(--bg3);border-radius:5px;padding:6px 10px;margin-bottom:8px;
      border-left:3px solid ${sessionCol}">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <div style="font-size:7px;letter-spacing:1px;color:${sessionCol};font-weight:700;margin-bottom:2px">
            ${sessionLabel || 'PRE MKT'}
          </div>
          <div style="display:flex;align-items:baseline;gap:6px">
            <span style="font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:800;color:var(--text)">
              ${pmLast > 0 ? pmLast.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : (price > 0 ? f$(price) : '—')}
            </span>
            ${pmPct !== 0 ? `<span style="font-size:10px;font-weight:700;color:${pmCol}">
              ${pmPct >= 0 ? '+' : ''}${pmPct.toFixed(2)}%
              ${pmChange !== 0 ? `<span style="font-size:8px;opacity:.8">(${pmChange >= 0 ? '+' : ''}${pmChange.toFixed(2)})</span>` : ''}
            </span>` : ''}
          </div>
        </div>
        <div style="display:flex;gap:10px;text-align:center">
          ${pmHigh > 0 ? `<div>
            <div style="font-size:7px;color:var(--sub);letter-spacing:.5px">PM HIGH</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;color:var(--green)">${pmHigh.toFixed(2)}</div>
          </div>` : ''}
          ${pmLow > 0 ? `<div>
            <div style="font-size:7px;color:var(--sub);letter-spacing:.5px">PM LOW</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;color:var(--red)">${pmLow.toFixed(2)}</div>
          </div>` : ''}
          ${prevClose > 0 ? `<div>
            <div style="font-size:7px;color:var(--sub);letter-spacing:.5px">PREV</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;color:var(--sub)">${prevClose.toFixed(2)}</div>
          </div>` : ''}
        </div>
      </div>
    </div>` : ''}

    <!-- ② RSI · GEX · CONVICTION -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;margin-bottom:8px">
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">RSI (14)</div>
        <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:${rsiCol}">
          ${rsi > 0 ? rsi.toFixed(1) : '—'}
        </div>
        <div style="height:2px;background:var(--bg2);border-radius:1px;margin-top:3px;overflow:hidden">
          <div style="width:${Math.min(rsi||50,100)}%;height:100%;background:${rsiCol}"></div>
        </div>
      </div>
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">GEX</div>
        <div style="font-size:11px;font-weight:700;color:${gexDirCol}">${gexDir}</div>
        <div style="font-size:7px;color:${regCol};margin-top:2px">${gexRegime || '—'}</div>
      </div>
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">CONVICTION</div>
        <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:${confColor}">
          ${conviction > 0 ? conviction : '—'}
        </div>
        ${conviction > 0 ? `<div style="height:2px;background:var(--bg2);border-radius:1px;margin-top:3px;overflow:hidden">
          <div style="width:${Math.min(conviction,100)}%;height:100%;background:${confColor}"></div>
        </div>` : ''}
      </div>
    </div>

    <!-- ③ GEX LEVELS -->
    ${(callWall || putWall || zeroGamma) ? `<div style="background:var(--bg3);border-radius:5px;padding:7px 9px;margin-bottom:7px">
      <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:5px">GEX LEVELS</div>
      <div style="display:flex;flex-direction:column;gap:3px">
        ${callWall ? `<div style="display:flex;justify-content:space-between">
          <span style="font-size:8px;color:var(--sub)">Call Wall</span>
          <span style="font-size:9px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--red)">$${callWall.toLocaleString()}</span>
        </div>` : ''}
        ${putWall ? `<div style="display:flex;justify-content:space-between">
          <span style="font-size:8px;color:var(--sub)">Put Wall</span>
          <span style="font-size:9px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--green)">$${putWall.toLocaleString()}</span>
        </div>` : ''}
        ${zeroGamma && zeroGamma !== 500 ? `<div style="display:flex;justify-content:space-between">
          <span style="font-size:8px;color:var(--sub)">Zero Gamma</span>
          <span style="font-size:9px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--gold)">$${zeroGamma.toLocaleString()}</span>
        </div>` : ''}
        ${netGex !== 0 ? `<div style="display:flex;justify-content:space-between;padding-top:2px;border-top:1px solid var(--border)">
          <span style="font-size:8px;color:var(--sub)">Net GEX</span>
          <span style="font-size:9px;font-weight:700;font-family:'JetBrains Mono',monospace;color:${gexDirCol}">${netGex>=0?'+':''}${netGex.toFixed(2)}B</span>
        </div>` : ''}
      </div>
    </div>` : ''}

    <!-- ④ VWAP + REASONS -->
    ${(vwapSig || reasons.length) ? `<div style="background:var(--bg3);border-radius:5px;padding:7px 9px;margin-bottom:7px">
      ${vwapSig ? `<div style="display:flex;justify-content:space-between;align-items:center;
          ${reasons.length ? 'margin-bottom:5px;' : ''}">
        <span style="font-size:7px;color:var(--sub);text-transform:uppercase;letter-spacing:.8px">VWAP</span>
        <span style="font-size:9px;font-weight:700;color:${aboveVwap?'var(--green)':'var(--red)'}">
          ${vwapSig}
        </span>
      </div>` : ''}
      ${reasons.map(r => `<div style="font-size:8px;color:var(--sub);padding:1px 0">· ${r}</div>`).join('')}
    </div>` : ''}

    <!-- ⑤ ORB TREND + ADX -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-bottom:7px">
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">ORB TREND</div>
        <div style="font-size:9px;font-weight:700;color:${orbCol}">${orbTrend}</div>
      </div>
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">ADX</div>
        <div style="font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;
          color:${adx>25?'var(--green)':'var(--sub)'}">
          ${adx > 0 ? adx.toFixed(1) : '—'}
        </div>
      </div>
    </div>

    <!-- ⑥ STRUCTURE TAGS -->
    ${tags.length ? `<div style="display:flex;gap:3px;flex-wrap:wrap;margin-bottom:7px">
      ${tags.map(t=>`<span style="font-size:7px;padding:2px 6px;border-radius:3px;
        background:${t.c}15;color:${t.c};border:1px solid ${t.c}30;font-weight:700;white-space:nowrap">${t.t}</span>`).join('')}
    </div>` : ''}

    <!-- ⑦ ALIGNMENT -->
    <div style="background:var(--bg3);border-radius:5px;padding:6px 9px;margin-bottom:7px;
        border-left:2px solid ${confColor}">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:9px;font-weight:700;color:${alignCol}">${alignment}</span>
        ${conviction > 0 ? `<span style="font-size:8px;color:${confColor};font-weight:700">${conviction}/100</span>` : ''}
      </div>
    </div>

    <!-- ⑧ DISCOMFORT INDEX -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:9px">
      <span style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">DISCOMFORT INDEX</span>
      <div style="position:relative;width:40px;height:40px">
        <svg viewBox="0 0 40 40" style="transform:rotate(-90deg);width:40px;height:40px">
          <circle cx="20" cy="20" r="16" fill="none" stroke="var(--bg3)" stroke-width="3.5"/>
          <circle cx="20" cy="20" r="16" fill="none" stroke="${discColor}" stroke-width="3.5"
            stroke-dasharray="${(discomfort/100*100.53).toFixed(1)} 100.53" stroke-linecap="round"/>
        </svg>
        <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
          font-size:8px;font-weight:800;color:${discColor};font-family:'JetBrains Mono',monospace">
          ${discomfort}%
        </div>
      </div>
    </div>

    <!-- ⑨ ARJUN RECOMMENDS -->
    <div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:6px">
      <div style="font-size:7px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;
          margin-bottom:5px;text-align:center">
        A R J U N &nbsp; R E C O M M E N D S
      </div>
      <div style="color:${recColor};font-size:${wouldTrade?'14':'12'}px;font-weight:${wouldTrade?'800':'700'};
          text-align:center;padding:5px 0">
        ${recLabel}
      </div>
      <div style="text-align:center;margin-top:3px">
        <span style="font-size:8px;color:var(--sub)">${subLabel}</span>
      </div>
    </div>

    <!-- ⑩ BUY BUTTON — only when ARJUN recommends action -->
    <div style="margin-bottom:8px">
      ${wouldTrade ? `
      <button onclick="manualIndexBuy('${sym}','${direction==='BEARISH'?'put':'call'}')"
        style="width:100%;padding:8px;border-radius:5px;cursor:pointer;font-size:11px;
          font-family:'JetBrains Mono',monospace;font-weight:700;border:none;
          background:${sentCol};color:#fff;letter-spacing:.5px">
        ${direction==='BEARISH' ? '📉 BUY PUT' : '📈 BUY CALL'} — ${sym}
      </button>` : `
      <div style="text-align:center;padding:7px;font-size:9px;color:var(--sub);
        border:1px dashed var(--border);border-radius:5px;letter-spacing:.3px">
        ⏳ Building conviction — no trade yet
      </div>`}
    </div>

    <!-- ⑩b ARKA SCAN STATUS — shows last conviction score for this ticker -->
    ${(() => {
      const _arkaWatched = ['SPX','RUT','SPY','QQQ','IWM','DIA'].includes(sym);
      const _arkaScans   = window._arkaLastScans || {};
      const _lastScan    = _arkaScans[sym];
      if (!_arkaWatched) return '';
      const _scanScore   = _lastScan?.score ?? null;
      const _scanDecision= _lastScan?.decision ?? '—';
      const _scanTime    = _lastScan?.time ?? '—';
      const _scanCol     = _scanScore >= 55 ? 'var(--green)' : _scanScore >= 35 ? 'var(--gold)' : 'var(--sub)';
      return `<div style="background:var(--bg3);border-radius:5px;padding:6px 9px;margin-bottom:7px;
          border-left:2px solid var(--accent)">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">ARKA SCAN</span>
          <span style="font-size:7px;color:var(--sub)">${_scanTime}</span>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:3px">
          <span style="font-size:9px;font-weight:700;color:${_scanCol}">${_scanScore !== null ? 'conv ' + _scanScore.toFixed(0) : '👁 Watching'}</span>
          <span style="font-size:8px;color:var(--sub)">${_scanDecision}</span>
        </div>
      </div>`;
    })()}

    <!-- ⑪ FULL ANALYSIS (expandable) — built from /api/ticker/analyze data -->
    ${(() => {
      const _reasons  = reasons;
      const _bullFact = _reasons.filter(r => r.includes('+') && !r.includes('-'));
      const _bearFact = _reasons.filter(r => r.includes('-') && !r.includes('+'));
      const _bullScore = direction === 'BEARISH' ? Math.max(0, 100 - conviction) : conviction;
      const _bearScore = direction === 'BEARISH' ? conviction : Math.max(0, 100 - conviction);
      const _riskDec   = wouldTrade ? 'APPROVE' : 'BLOCK';
      const _riskCol   = wouldTrade ? 'var(--green)' : 'var(--red)';
      const _riskReason = wouldTrade
        ? `Conviction ${conviction}/100 meets threshold ${threshold}`
        : `Conviction ${conviction}/100 below threshold ${threshold}`;
      const _opts    = analyze?.sections?.options || {};
      const _pcRatio = parseFloat(_opts.put_call_ratio || 0);
      const _pcSig   = _opts.pc_signal || '—';
      const _pcCol   = _pcSig === 'BEARISH_FEAR' ? 'var(--red)' : _pcSig === 'BULLISH_GREED' ? 'var(--green)' : 'var(--sub)';
      const _mods    = analyze?.sections?.modules || {};
      const _hmmReg  = _mods.hmm_regime || '—';
      const _vrpSt   = _mods.vrp_state  || '—';
      const _hasData = _reasons.length > 0;

      if (!_hasData) return `<div style="text-align:center;margin-bottom:4px;padding:5px 0;
        font-size:8px;color:var(--sub);letter-spacing:.4px">
        ⏳ Loading analysis — refresh card to populate
      </div>`;

      return `
    <div style="text-align:center;margin-bottom:4px">
      <button onclick="event.stopPropagation();_toggleDetail('${sym}')"
        style="font-size:8px;color:var(--sub);background:none;border:1px solid var(--border);
          cursor:pointer;letter-spacing:.5px;padding:3px 12px;border-radius:3px">
        ▼ FULL ANALYSIS
      </button>
    </div>
    <div id="detail-${sym}" style="display:none;border-top:1px solid var(--border);padding-top:8px;margin-top:4px">
      <!-- OPTIONS PLAY -->
      ${(() => {
        const _isCall = direction === 'BULLISH' || signal === 'BUY';
        const _isPut  = direction === 'BEARISH' || signal === 'SELL';
        const _rnd    = sym === 'SPX' || sym === 'NDX' ? 5 : 1;
        const _strike = price > 0 ? Math.round(price / _rnd) * _rnd : 0;
        const _dte    = conviction >= 55 ? '0DTE' : '1DTE';
        const _nearCW = callWall && price > 0 && Math.abs(price - callWall) / price < 0.005;
        const _nearPW = putWall  && price > 0 && Math.abs(price - putWall)  / price < 0.005;
        const _why    = _nearCW  ? `Near call wall $${callWall} — rejection play`
                      : _nearPW  ? `Near put wall $${putWall} — bounce play`
                      : zeroGamma && zeroGamma !== 500 && price > 0 && Math.abs(price - zeroGamma) / price < 0.012
                        ? `Near gamma flip $${zeroGamma} — explosive move setup`
                      : _isCall  ? `Bullish conviction ${conviction}/100`
                      : _isPut   ? `Bearish conviction ${conviction}/100`
                      : `Watching — no directional edge yet`;
        if (!_strike) return '';
        if (_isCall || _isPut) {
          const _type    = _isCall ? 'CALL' : 'PUT';
          const _typeCol = _isCall ? 'var(--green)' : 'var(--red)';
          const _icon    = _isCall ? '📈' : '📉';
          return `<div style="background:var(--bg3);border-radius:5px;padding:10px;margin-bottom:8px;
              border-left:3px solid ${_typeCol}">
            <div style="font-size:7px;letter-spacing:1px;color:var(--sub);margin-bottom:5px">OPTIONS PLAY</div>
            <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:4px">
              <span style="font-size:13px;font-weight:800;font-family:'JetBrains Mono',monospace;color:${_typeCol}">
                ${_icon} BUY ${_type}
              </span>
              <span style="font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--text)">
                $${_strike}
              </span>
              <span style="font-size:9px;padding:1px 6px;background:${_typeCol}20;color:${_typeCol};
                border-radius:3px;font-weight:700">${_dte}</span>
            </div>
            <div style="font-size:8px;color:var(--sub);line-height:1.4">${_why}</div>
          </div>`;
        }
        return `<div style="background:var(--bg3);border-radius:5px;padding:8px;margin-bottom:8px">
          <div style="font-size:7px;letter-spacing:1px;color:var(--sub);margin-bottom:4px">OPTIONS PLAY</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-bottom:3px">
            <div style="font-size:9px;color:var(--green);font-weight:700">📈 CALL $${_strike} ${_dte}</div>
            <div style="font-size:9px;color:var(--red);font-weight:700">📉 PUT $${_strike} ${_dte}</div>
          </div>
          <div style="font-size:8px;color:var(--sub)">${_why}</div>
        </div>`;
      })()}
      <!-- Bull / Bear scores -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
        <div style="background:var(--bg3);border-radius:5px;padding:8px;border-left:2px solid var(--green)">
          <div style="font-size:7px;color:var(--sub);margin-bottom:2px">BULL SCORE</div>
          <div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--green)">${_bullScore}</div>
          <div style="font-size:8px;color:var(--sub);margin-top:3px;line-height:1.3">
            ${_bullFact[0] || (direction === 'BULLISH' ? 'Bullish bias confirmed' : 'No bullish catalysts')}
          </div>
        </div>
        <div style="background:var(--bg3);border-radius:5px;padding:8px;border-left:2px solid var(--red)">
          <div style="font-size:7px;color:var(--sub);margin-bottom:2px">BEAR SCORE</div>
          <div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--red)">${_bearScore}</div>
          <div style="font-size:8px;color:var(--sub);margin-top:3px;line-height:1.3">
            ${_bearFact[0] || (direction === 'BEARISH' ? 'Bearish pressure present' : 'No major bear risk')}
          </div>
        </div>
      </div>
      <!-- Risk decision -->
      <div style="background:var(--bg3);border-radius:5px;padding:8px;margin-bottom:8px;border-left:2px solid ${_riskCol}">
        <div style="font-size:7px;color:var(--sub);margin-bottom:3px">RISK MANAGER</div>
        <div style="font-size:10px;font-weight:700;color:${_riskCol};margin-bottom:4px">${_riskDec}</div>
        <div style="font-size:8px;color:var(--sub);line-height:1.4">${_riskReason}</div>
      </div>
      <!-- GEX + modules grid -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;margin-bottom:8px">
        ${[
          ['GEX REGIME', gexRegime.replace(/_/g,' ') || '—', netGex < 0 ? 'var(--red)' : 'var(--green)'],
          ['CALL WALL',  callWall  ? '$'+callWall.toLocaleString()  : '—', 'var(--red)'],
          ['PUT WALL',   putWall   ? '$'+putWall.toLocaleString()   : '—', 'var(--green)'],
          ['P/C RATIO',  _pcRatio ? _pcRatio.toFixed(2) : '—', _pcCol],
          ['HMM REGIME', _hmmReg, _hmmReg === 'CRISIS' ? 'var(--red)' : _hmmReg.includes('TREND') ? 'var(--green)' : 'var(--sub)'],
          ['VRP STATE',  _vrpSt, _vrpSt === 'FEAR' ? 'var(--red)' : _vrpSt === 'GREED' ? 'var(--green)' : 'var(--sub)'],
        ].map(([l,v,c])=>`<div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
          <div style="font-size:7px;color:var(--sub);margin-bottom:2px">${l}</div>
          <div style="font-size:9px;font-weight:700;color:${c};font-family:'JetBrains Mono',monospace">${v}</div>
        </div>`).join('')}
      </div>
      <!-- Bull / Bear factors -->
      ${_bullFact.length ? `<div style="margin-bottom:7px">
        <div style="font-size:7px;color:var(--green);letter-spacing:.8px;margin-bottom:4px">BULL FACTORS</div>
        ${_bullFact.map(f=>`<div style="font-size:8px;color:var(--sub);padding:2px 0;border-bottom:1px solid var(--border)">✓ ${f}</div>`).join('')}
      </div>` : ''}
      ${_bearFact.length ? `<div style="margin-bottom:7px">
        <div style="font-size:7px;color:var(--red);letter-spacing:.8px;margin-bottom:4px">BEAR FACTORS</div>
        ${_bearFact.map(f=>`<div style="font-size:8px;color:var(--sub);padding:2px 0;border-bottom:1px solid var(--border)">✗ ${f}</div>`).join('')}
      </div>` : ''}
      <div style="text-align:center">
        <button onclick="_toggleDetail('${sym}')"
          style="font-size:8px;color:var(--sub);background:none;border:none;cursor:pointer">▲ COLLAPSE</button>
      </div>
    </div>`;
    })()}

    <!-- ⑫ FOOTER -->
    <div style="display:flex;justify-content:space-between;align-items:center;
        padding-top:6px;margin-top:4px;border-top:1px solid var(--border);font-size:9px;color:var(--sub)">
      <span style="color:${pctCol};font-family:'JetBrains Mono',monospace;font-weight:700">
        ${pct !== 0 ? (pct>=0?'+':'')+pct.toFixed(2)+'%' : '—'}
      </span>
      <span style="font-size:7px;color:${gexDirCol}">${gexDir} GEX</span>
      ${signal !== 'HOLD' ? `<span class="chip ${signal==='BUY'?'cg':'cr'}" style="font-size:7px">${signal}</span>` : ''}
    </div>
  </div>`;
}

// ── Manual Index Buy — contract picker modal ──────────────────
async function manualIndexBuy(ticker, direction) {
  // Show loading modal
  _showManualBuyModal(ticker, direction, null, 'loading');

  let data;
  try {
    const r = await fetch(`/api/options/contracts/picker?ticker=${ticker}&direction=${direction}&max_dte=5`);
    data = await r.json();
  } catch(e) {
    _showManualBuyModal(ticker, direction, null, 'error', 'Network error: ' + e.message);
    return;
  }
  if (!data.success) {
    _showManualBuyModal(ticker, direction, null, 'error', data.error);
    return;
  }
  _showManualBuyModal(ticker, direction, data, 'pick');
}

function _showManualBuyModal(ticker, direction, data, mode, errMsg) {
  // Remove existing modal
  const old = document.getElementById('_manualBuyModal');
  if (old) old.remove();

  const dirLabel = direction === 'call' ? '📈 CALL' : '📉 PUT';
  const dirColor = direction === 'call' ? 'var(--green)' : 'var(--red)';

  let body = '';
  if (mode === 'loading') {
    body = `<div style="text-align:center;padding:30px;color:var(--sub)">
      <div class="spin" style="margin:0 auto 12px"></div>Loading contracts…</div>`;
  } else if (mode === 'error') {
    body = `<div style="text-align:center;padding:20px;color:var(--red)">${errMsg || 'Error loading contracts'}</div>`;
  } else if (mode === 'pick') {
    const spot = data.spot || 0;
    const rows = (data.contracts || []).map((c, i) => {
      const otmLbl = Math.abs(c.otm_pct) < 0.3 ? '<span style="color:var(--gold);font-size:9px">ATM</span>'
        : `<span style="color:var(--sub);font-size:9px">${c.otm_pct > 0 ? '+' : ''}${c.otm_pct.toFixed(1)}%</span>`;
      const px = c.price > 0 ? `$${c.price.toFixed(2)}` : '—';
      const cost = c.price > 0 ? `($${(c.price * 100).toFixed(0)}/ct)` : '';
      const dteLbl = c.dte === 0 ? '<span style="color:var(--gold)">0DTE</span>'
        : c.dte === 1 ? '<span style="color:var(--green)">1DTE</span>'
        : `<span style="color:var(--sub)">${c.dte}DTE</span>`;
      return `<div id="mbc_${i}" onclick="_selectContract(${i})" style="
        display:grid;grid-template-columns:1fr 60px 60px 60px 80px;align-items:center;
        padding:8px 10px;cursor:pointer;border-radius:4px;margin-bottom:2px;
        border:1px solid var(--border);background:var(--bg2);gap:4px"
        data-sym="${c.symbol}" data-price="${c.price}" data-strike="${c.strike}" data-exp="${c.expiry}">
        <div>
          <div style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700">$${c.strike}</div>
          <div style="font-size:9px;color:var(--sub)">${c.expiry}</div>
        </div>
        <div style="text-align:center">${dteLbl}</div>
        <div style="text-align:center">${otmLbl}</div>
        <div style="text-align:right;font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:var(--text)">${px}</div>
        <div style="text-align:right;font-size:9px;color:var(--sub)">${cost}</div>
      </div>`;
    }).join('');

    body = `
      <div style="font-size:10px;color:var(--sub);margin-bottom:10px">
        ${ticker} spot: <span style="color:var(--text);font-weight:700">$${spot.toFixed(2)}</span>
        &nbsp;·&nbsp; Pick a contract, then set qty
      </div>
      <div id="_mbcList" style="max-height:260px;overflow-y:auto">${rows}</div>
      <div id="_mbcSelected" style="display:none;margin-top:12px;padding:10px;background:var(--bg3);border-radius:6px;border:1px solid var(--accent)">
        <div style="font-size:9px;color:var(--sub);margin-bottom:6px">SELECTED CONTRACT</div>
        <div id="_mbcSelLabel" style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;margin-bottom:8px"></div>
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:10px;color:var(--sub)">Contracts:</span>
          <button onclick="_mbcQty(-1)" style="width:28px;height:28px;border-radius:4px;border:1px solid var(--border);background:var(--bg2);color:var(--text);cursor:pointer;font-size:14px">−</button>
          <span id="_mbcQtyVal" style="font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:800;min-width:24px;text-align:center">1</span>
          <button onclick="_mbcQty(+1)" style="width:28px;height:28px;border-radius:4px;border:1px solid var(--border);background:var(--bg2);color:var(--text);cursor:pointer;font-size:14px">+</button>
          <span id="_mbcCostLabel" style="font-size:10px;color:var(--sub);margin-left:4px"></span>
        </div>
      </div>`;
  }

  const modal = document.createElement('div');
  modal.id = '_manualBuyModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = `
    <div style="background:var(--bg1);border:1px solid var(--border);border-radius:12px;
      padding:20px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto"
      onclick="event.stopPropagation()">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div>
          <span style="font-size:14px;font-weight:800;letter-spacing:1px">${ticker}</span>
          <span style="color:${dirColor};font-size:12px;font-weight:700;margin-left:8px">${dirLabel}</span>
        </div>
        <button onclick="document.getElementById('_manualBuyModal').remove()"
          style="background:none;border:none;color:var(--sub);cursor:pointer;font-size:18px;padding:0 4px">✕</button>
      </div>
      <div id="_mbcBody">${body}</div>
      ${mode === 'pick' ? `
      <div style="display:flex;gap:8px;margin-top:14px">
        <button onclick="document.getElementById('_manualBuyModal').remove()"
          style="flex:1;padding:10px;border-radius:6px;border:1px solid var(--border);
          background:var(--bg2);color:var(--sub);cursor:pointer;font-size:12px">Cancel</button>
        <button id="_mbcConfirmBtn" onclick="_confirmManualBuy('${ticker}','${direction}')"
          disabled style="flex:2;padding:10px;border-radius:6px;border:none;
          background:var(--accent);color:#000;cursor:pointer;font-size:12px;font-weight:700;
          opacity:0.4">Select a contract above</button>
      </div>` : `
      <div style="margin-top:14px">
        <button onclick="document.getElementById('_manualBuyModal').remove()"
          style="width:100%;padding:10px;border-radius:6px;border:1px solid var(--border);
          background:var(--bg2);color:var(--sub);cursor:pointer">Close</button>
      </div>`}
    </div>`;
  modal.onclick = () => modal.remove();
  document.body.appendChild(modal);
}

window._mbcSelectedSym   = null;
window._mbcSelectedPrice = 0;
window._mbcQtyVal        = 1;

function _selectContract(idx) {
  // Deselect all
  document.querySelectorAll('[id^="mbc_"]').forEach(el => {
    el.style.background = 'var(--bg2)';
    el.style.borderColor = 'var(--border)';
  });
  const el = document.getElementById('mbc_' + idx);
  if (!el) return;
  el.style.background = 'var(--bg3)';
  el.style.borderColor = 'var(--accent)';

  window._mbcSelectedSym   = el.dataset.sym;
  window._mbcSelectedPrice = parseFloat(el.dataset.price || 0);
  window._mbcQtyVal        = 1;

  const strike = el.dataset.strike;
  const exp    = el.dataset.exp;
  const selDiv = document.getElementById('_mbcSelected');
  const selLbl = document.getElementById('_mbcSelLabel');
  if (selDiv) selDiv.style.display = 'block';
  if (selLbl) selLbl.textContent = `${el.dataset.sym}  ·  $${strike}  ·  ${exp}`;

  const qv = document.getElementById('_mbcQtyVal');
  if (qv) qv.textContent = '1';
  _updateMbcCost();

  const btn = document.getElementById('_mbcConfirmBtn');
  if (btn) { btn.disabled = false; btn.style.opacity = '1'; btn.textContent = 'Place Order'; }
}

function _mbcQty(delta) {
  window._mbcQtyVal = Math.max(1, Math.min(5, (window._mbcQtyVal || 1) + delta));
  const qv = document.getElementById('_mbcQtyVal');
  if (qv) qv.textContent = window._mbcQtyVal;
  _updateMbcCost();
}

function _updateMbcCost() {
  const lbl = document.getElementById('_mbcCostLabel');
  if (!lbl) return;
  const px  = window._mbcSelectedPrice || 0;
  const qty = window._mbcQtyVal || 1;
  if (px > 0) lbl.textContent = `= $${(px * 100 * qty).toFixed(0)} total`;
  else lbl.textContent = '';
}

async function _confirmManualBuy(ticker, direction) {
  const sym = window._mbcSelectedSym;
  const qty = window._mbcQtyVal || 1;
  if (!sym) return;

  const btn = document.getElementById('_mbcConfirmBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Placing…'; }

  try {
    const r = await fetch('/api/swings/manual-entry', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ticker, direction, contract_sym: sym, qty, source: 'manual' })
    });
    const d = await r.json();
    document.getElementById('_manualBuyModal')?.remove();
    if (d.success) {
      toast(`✅ ${sym} x${qty} placed!`);
    } else {
      toast(`❌ Order failed: ${d.error || 'unknown'}`, 5000);
    }
  } catch(e) {
    document.getElementById('_manualBuyModal')?.remove();
    toast(`❌ Error: ${e.message}`, 5000);
  }
}

// ══════════════════════════════════════════════════════════════
//  ENRICHED STUB CARD — non-ARJUN tickers
//  Uses /api/ticker/analyze: conviction, direction, VWAP, reasons
// ══════════════════════════════════════════════════════════════
function _buildEnrichedStubCard(ticker, px, analyze, tier) {
  const sym    = ticker.toUpperCase();
  const lv     = px && px[sym] ? px[sym] : null;
  const price  = lv ? lv.price   : 0;
  const pct    = lv ? lv.chg_pct : 0;
  const pctCol = pct >= 0 ? 'var(--green)' : 'var(--red)';

  // From /api/ticker/analyze
  const sections   = analyze?.sections || {};
  const priceData  = sections.price    || {};
  const arjunData  = sections.arjun    || {};
  const conviction = parseInt(arjunData.conviction_score || 0);
  const direction  = (arjunData.direction || '').toUpperCase();
  const signal     = (arjunData.signal    || 'HOLD').toUpperCase();
  const reasons    = arjunData.reasons   || [];
  const threshold  = arjunData.threshold || 55;
  const wouldTrade = arjunData.would_trade ?? false;
  const vwapSig    = (priceData.vwap_signal || '').replace(/_/g,' ');
  const aboveVwap  = priceData.vwap_signal === 'ABOVE_VWAP';
  const vol        = priceData.volume || 0;

  const sentCol   = direction==='BULLISH'?'var(--green)':direction==='BEARISH'?'var(--red)':'var(--sub)';
  const confColor = conviction>=55?'var(--green)':conviction>=35?'var(--gold)':'var(--sub)';
  const alignCol  = conviction>=55?'var(--green)':conviction>=35?'var(--gold)':'var(--red)';
  const alignment = conviction>=55?'ALIGNED':conviction>=35?'MIXED':'WATCHING';
  const volLabel  = vol>20e6?'EXTREME':vol>5e6?'HIGH':vol>1e6?'NORMAL':vol>0?'LOW':'—';
  const volCol    = vol>20e6?'var(--red)':vol>5e6?'var(--gold)':'var(--sub)';
  const orbCol    = direction==='BULLISH'?'var(--green)':direction==='BEARISH'?'var(--red)':'var(--sub)';

  const tierBadge = {sector:'📊 SECTOR ETF', movers:'🔥 MOVER', chakra:'🌀 ARKA SWING', arjun:''}[tier]||'';
  const recColor  = signal==='BUY'?'var(--green)':signal==='SELL'?'var(--red)':'var(--gold)';
  const recLabel  = signal==='BUY'?'BUY CALLS':signal==='SELL'?'BUY PUTS':'WATCHING';
  const subLabel  = wouldTrade ? 'Conviction threshold met'
    : conviction > 0 ? 'Score '+conviction+' — threshold '+threshold
    : 'Runs 8am ET';

  return `<div class="sc george-card ${signal.toLowerCase()}" data-sym="${sym}" style="min-width:0">

    <!-- HEADER -->
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
      <div>
        <div style="font-size:16px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1.1">
          ${sym}
          ${price ? `<span style="font-size:10px;color:${pctCol};font-weight:600;margin-left:4px">
            ${pct>=0?'+':''}${pct.toFixed(2)}%</span>` : ''}
        </div>
        <div style="font-size:10px;color:var(--sub);font-family:'JetBrains Mono',monospace;margin-top:2px">
          ${price ? f$(price) : '—'}
          ${tierBadge ? `<span style="font-size:7px;color:var(--sub);margin-left:5px;opacity:.7">${tierBadge}</span>` : ''}
        </div>
      </div>
      <div style="text-align:right">
        <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">CROWD SENTIMENT</div>
        <span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;
          background:${sentCol}18;color:${sentCol};border:1px solid ${sentCol}35">
          ${direction || '—'}
        </span>
      </div>
    </div>

    <!-- CONVICTION · VOLUME · ORB -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;margin-bottom:8px">
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">CONVICTION</div>
        <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:${confColor}">
          ${conviction > 0 ? conviction : '—'}
        </div>
        ${conviction > 0 ? `<div style="height:2px;background:var(--bg2);border-radius:1px;margin-top:3px;overflow:hidden">
          <div style="width:${Math.min(conviction,100)}%;height:100%;background:${confColor}"></div>
        </div>` : ''}
      </div>
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">VOLUME</div>
        <div style="font-size:9px;font-weight:700;color:${volCol}">${volLabel}</div>
        ${vol > 0 ? `<div style="font-size:7px;color:var(--sub);margin-top:2px">${(vol/1e6).toFixed(1)}M</div>` : ''}
      </div>
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">ORB TREND</div>
        <div style="font-size:9px;font-weight:700;color:${orbCol}">${direction || '—'}</div>
      </div>
    </div>

    <!-- VWAP + REASONS -->
    ${(vwapSig || reasons.length) ? `<div style="background:var(--bg3);border-radius:5px;padding:7px 9px;margin-bottom:7px">
      ${vwapSig ? `<div style="display:flex;justify-content:space-between;align-items:center;
          ${reasons.length ? 'margin-bottom:5px;' : ''}">
        <span style="font-size:7px;color:var(--sub);text-transform:uppercase;letter-spacing:.8px">VWAP</span>
        <span style="font-size:9px;font-weight:700;color:${aboveVwap?'var(--green)':'var(--red)'}">
          ${vwapSig}
        </span>
      </div>` : ''}
      ${reasons.map(r => `<div style="font-size:8px;color:var(--sub);padding:1px 0">· ${r}</div>`).join('')}
    </div>` : ''}

    <!-- ALIGNMENT -->
    <div style="background:var(--bg3);border-radius:5px;padding:6px 9px;margin-bottom:7px;
        border-left:2px solid ${confColor}">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:9px;font-weight:700;color:${alignCol}">${alignment}</span>
        ${conviction > 0 ? `<span style="font-size:8px;color:${confColor};font-weight:700">${conviction}/100</span>` : ''}
      </div>
    </div>

    <!-- ARJUN RECOMMENDS -->
    <div style="border-top:1px solid var(--border);padding-top:8px">
      <div style="font-size:7px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;
          margin-bottom:5px;text-align:center">
        A R J U N &nbsp; R E C O M M E N D S
      </div>
      <div style="color:${recColor};font-size:${wouldTrade?'14':'12'}px;font-weight:${wouldTrade?'800':'700'};
          text-align:center;padding:5px 0">
        ${recLabel}
      </div>
      <div style="text-align:center;margin-top:3px">
        <span style="font-size:8px;color:var(--sub)">${subLabel}</span>
      </div>
    </div>

    <!-- FOOTER BUY BUTTON — only when ARJUN recommends action -->
    <div style="margin-bottom:6px">
      ${wouldTrade ? `
      <button onclick="manualIndexBuy('${sym}','${direction==='BEARISH'?'put':'call'}')"
        style="width:100%;padding:7px;border-radius:5px;cursor:pointer;font-size:10px;
          font-family:'JetBrains Mono',monospace;font-weight:700;border:none;
          background:${sentCol};color:#fff">
        ${direction==='BEARISH' ? '📉 BUY PUT' : direction==='BULLISH' ? '📈 BUY CALL' : '👁 WATCH'} — ${sym}
      </button>` : `
      <div style="text-align:center;padding:6px;font-size:9px;color:var(--sub);
        border:1px dashed var(--border);border-radius:5px;letter-spacing:.3px">
        ⏳ Building conviction — no trade yet
      </div>`}
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;
        padding-top:6px;margin-top:4px;border-top:1px solid var(--border);font-size:9px">
      <span style="color:${pctCol};font-family:'JetBrains Mono',monospace;font-weight:700">
        ${pct !== 0 ? (pct>=0?'+':'')+pct.toFixed(2)+'%' : '—'}
      </span>
      <span style="font-size:7px;color:var(--sub)">${tierBadge}</span>
      ${signal !== 'HOLD' ? `<span class="chip ${signal==='BUY'?'cg':'cr'}" style="font-size:7px">${signal}</span>` : ''}
    </div>
  </div>`;
}

// ══════════════════════════════════════════════════════════════
//  CHAKRA-STYLE SIGNAL CARD (full ARJUN signal)
// ══════════════════════════════════════════════════════════════
function _buildGeorgeCard(sig, px) {
  const sym  = (sig.ticker || sig.symbol || '').toUpperCase();
  const lv   = px && px[sym] ? px[sym] : null;
  const price = lv ? lv.price   : parseFloat(sig.price || 0);
  const pct   = lv ? lv.chg_pct : parseFloat(sig.indicators?.price_change_1d || 0);
  const pctCol = pct >= 0 ? 'var(--green)' : 'var(--red)';

  // Pre-market data (populated in loadSignals from /api/premarket)
  const pmPrice  = parseFloat(lv?.pm_price   || 0);
  const pmPct    = parseFloat(lv?.pm_chg_pct || 0);
  const pmChange = parseFloat(lv?.pm_change  || 0);
  const prevClose= parseFloat(lv?.prev_close || 0);
  const pmHigh   = parseFloat((pmTickers[sym] || {})?.levels?.pm_high || 0);
  const pmLow    = parseFloat((pmTickers[sym] || {})?.levels?.pm_low  || 0);
  const pmLast   = parseFloat((pmTickers[sym] || {})?.levels?.pm_last || pmPrice || 0);
  const inExtGeo = _isPreMkt || _isPostMkt;
  const sessLbl  = _isPreMkt ? 'PRE MKT' : _isPostMkt ? 'POST MKT' : 'LIVE';
  const sessCol  = _isPreMkt ? 'var(--gold)' : _isPostMkt ? '#8888ff' : 'var(--green)';
  const pmCol    = pmPct >= 0 ? 'var(--green)' : 'var(--red)';

  const st   = (sig.signal || 'HOLD').toUpperCase();
  const conf = parseFloat(sig.confidence || 50);

  const agents   = sig.agents || {};
  const bull     = agents.bull         || {};
  const bear     = agents.bear         || {};
  const risk     = agents.risk_manager || {};
  const analyst  = agents.analyst      || {};

  const bullScore = parseFloat(bull.score ?? sig.bull_score ?? 50);
  const bearScore = parseFloat(bear.score ?? sig.bear_score ?? 50);
  const riskDec   = risk.decision || '';
  const blocks    = risk.blocks   || [];

  const ind       = sig.indicators || {};
  const rsi       = parseFloat(ind.rsi          || 0);
  const volRatio  = parseFloat(ind.volume_ratio  || 0);
  const macdTrend = (ind.macd_trend || '').toLowerCase();
  const above20   = ind.above_ema20  ?? null;
  const above200  = ind.above_ema200 ?? true;
  const goldenX   = ind.golden_cross ?? false;
  const adx       = parseFloat(ind.adx || 0);
  const bbUpper   = parseFloat(ind.bb_upper || 0);
  const bbLower   = parseFloat(ind.bb_lower || 0);

  const gex        = sig.gex || {};
  const netGex     = parseFloat(gex.net_gex    || 0);
  const callWall   = parseFloat(gex.call_wall  || 0);
  const putWall    = parseFloat(gex.put_wall   || 0);
  const secondCall = parseFloat(gex.second_call || 0);
  const secondPut  = parseFloat(gex.second_put  || 0);
  const zeroGamma  = parseFloat(gex.zero_gamma  || 0);
  const gexRegime  = gex.regime    || '';
  const ivSkew     = parseFloat(gex.iv_skew    || 0);
  const bearBias   = gex.bearish_bias ?? false;
  const bullBias   = gex.bullish_bias ?? false;

  const gexDir     = netGex > 0 ? 'POS' : netGex < 0 ? 'NEG' : 'NEUT';
  const gexDirCol  = netGex > 0 ? 'var(--green)' : netGex < 0 ? 'var(--red)' : 'var(--sub)';
  const gexBias    = bearBias ? 'BEARISH' : bullBias ? 'BULLISH' : 'NEUTRAL';
  const gexBiasCol = bearBias ? 'var(--red)' : bullBias ? 'var(--green)' : 'var(--sub)';

  const entry  = parseFloat(sig.entry     || sig.entry_price || price);
  const target = parseFloat(sig.target    || sig.target_price || 0);
  const stop   = parseFloat(sig.stop_loss || sig.stop || 0);
  const rr     = parseFloat(sig.risk_reward || 0);

  const netScore  = bullScore - bearScore;
  const sentiment = netScore >= 15 ? 'BULLISH' : netScore <= -15 ? 'BEARISH' : 'NEUTRAL';
  const sentCol   = sentiment==='BULLISH'?'var(--green)':sentiment==='BEARISH'?'var(--red)':'var(--gold)';

  const orbTrend = macdTrend==='bullish'?'BULLISH':macdTrend==='bearish'?'BEARISH':'NEUTRAL';
  const orbCol   = orbTrend==='BULLISH'?'var(--green)':orbTrend==='BEARISH'?'var(--red)':'var(--sub)';

  const instVol    = _instVolLabel(volRatio);
  const instVolCol = volRatio >= 3 ? 'var(--red)' : volRatio >= 1.5 ? 'var(--gold)' : 'var(--sub)';

  const profile   = _deriveProfile(ind, gex, sentiment, orbTrend, goldenX, above200, bbUpper, bbLower);
  const confColor = conf>=70?'var(--green)':conf>=55?'var(--gold)':'var(--sub)';
  const alignment = conf>=70?'ALIGNED':conf>=55?'MIXED':'CONFLICTED';
  const alignCol  = conf>=70?'var(--green)':conf>=55?'var(--gold)':'var(--red)';

  const discomfort = _discomfortIndex(rsi, volRatio, netScore, gexDir, orbTrend, adx, ivSkew, gexRegime);
  const discColor  = discomfort>=80?'var(--red)':discomfort>=60?'var(--gold)':'var(--green)';

  const sweep    = _detectSweep(price, callWall, putWall, zeroGamma, secondCall, secondPut);
  const tags     = _buildStructureTags(orbTrend, goldenX, above200, above20, netGex, sentiment, ivSkew, gexRegime);
  const triggers = _buildTriggers(price, callWall, putWall, zeroGamma);
  const levels   = _buildLevels(target, stop, callWall, putWall, secondCall, secondPut, zeroGamma);
  const rec      = _buildArjunRec(sym, st, conf, riskDec, blocks, entry, target, stop, rr, discomfort, alignment);

  return `<div class="sc george-card ${st.toLowerCase()}" data-sym="${sym}" style="min-width:0${sig.stale ? ';opacity:0.82;border-color:rgba(255,179,71,0.25)' : ''}">

    <!-- ① HEADER -->
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
      <div>
        <div style="font-size:16px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1.1">
          ${sym}
          <span style="font-size:10px;color:${pctCol};font-weight:600;margin-left:4px">
            ${pct>=0?'+':''}${(pct||0).toFixed(2)}%
          </span>
        </div>
        <div style="font-size:11px;color:var(--sub);font-family:'JetBrains Mono',monospace;margin-top:2px;display:flex;align-items:center;gap:5px">
          ${f$(price)}
          ${inExtGeo ? `<span style="font-size:6px;font-weight:700;letter-spacing:.8px;padding:1px 4px;
            border-radius:2px;background:${sessCol}18;color:${sessCol};border:1px solid ${sessCol}35">${sessLbl}</span>` : ''}
        </div>
        ${Math.abs(pct) >= 0.3 ? `<div style="font-size:8px;padding:1px 6px;border-radius:3px;margin-top:3px;display:inline-block;
          background:${pct>0?'rgba(0,208,132,0.1)':'rgba(255,61,90,0.1)'};
          color:${pct>0?'var(--green)':'var(--red)'}">
          ${pct>0?'▲':'▼'} ${Math.abs(pct).toFixed(2)}% today${pct>0?' — BULLISH session':' — BEARISH session'}
        </div>` : ''}
      </div>
      <div style="text-align:right">
        <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">CROWD SENTIMENT</div>
        <span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;
          background:${sentCol}18;color:${sentCol};border:1px solid ${sentCol}40">${sentiment}</span>
      </div>
    </div>

    <!-- ② PRE/POST MARKET STRIP -->
    ${(inExtGeo || pmLast > 0) ? `
    <div style="background:var(--bg3);border-radius:5px;padding:6px 10px;margin-bottom:8px;
      border-left:3px solid ${sessCol}">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <div style="font-size:7px;letter-spacing:1px;color:${sessCol};font-weight:700;margin-bottom:2px">${sessLbl}</div>
          <div style="display:flex;align-items:baseline;gap:6px">
            <span style="font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:800;color:var(--text)">
              ${pmLast > 0 ? pmLast.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : (price > 0 ? f$(price) : '—')}
            </span>
            ${pmPct !== 0 ? `<span style="font-size:10px;font-weight:700;color:${pmCol}">
              ${pmPct >= 0 ? '+' : ''}${pmPct.toFixed(2)}%
              ${pmChange !== 0 ? `<span style="font-size:8px;opacity:.8">(${pmChange >= 0 ? '+' : ''}${pmChange.toFixed(2)})</span>` : ''}
            </span>` : ''}
          </div>
        </div>
        <div style="display:flex;gap:10px;text-align:center">
          ${pmHigh > 0 ? `<div><div style="font-size:7px;color:var(--sub);letter-spacing:.5px">PM HIGH</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;color:var(--green)">${pmHigh.toFixed(2)}</div></div>` : ''}
          ${pmLow > 0 ? `<div><div style="font-size:7px;color:var(--sub);letter-spacing:.5px">PM LOW</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;color:var(--red)">${pmLow.toFixed(2)}</div></div>` : ''}
          ${prevClose > 0 ? `<div><div style="font-size:7px;color:var(--sub);letter-spacing:.5px">PREV</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;color:var(--sub)">${prevClose.toFixed(2)}</div></div>` : ''}
        </div>
      </div>
    </div>` : ''}

    <!-- ③ SWEEP ALERT -->
    ${sweep ? `<div style="font-size:9px;color:var(--gold);margin-bottom:7px;padding:5px 8px;
        background:var(--gold)0d;border-radius:4px;border-left:2px solid var(--gold)">${sweep}</div>` : ''}

    <!-- ④ RSI · VOLUME · ORB -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;margin-bottom:8px">
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">RSI (14)</div>
        <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;
          color:${rsi>70?'var(--red)':rsi<30?'var(--green)':'var(--text)'}">
          ${rsi>0?rsi.toFixed(1):'—'}
        </div>
        <div style="height:2px;background:var(--bg2);border-radius:1px;margin-top:3px;overflow:hidden">
          <div style="width:${Math.min(rsi||50,100)}%;height:100%;
            background:${rsi>70?'var(--red)':rsi<30?'var(--green)':'var(--blue)'}"></div>
        </div>
      </div>
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">VOLUME</div>
        <div style="font-size:8px;font-weight:700;color:${instVolCol};line-height:1.3">${instVol}</div>
      </div>
      <div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
        <div style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase;margin-bottom:3px">ORB TREND</div>
        <div style="font-size:9px;font-weight:700;color:${orbCol}">${orbTrend}</div>
      </div>
    </div>

    <!-- ④ PROFILE -->
    <div style="background:var(--bg3);border-radius:5px;padding:7px 9px;margin-bottom:7px;border-left:2px solid ${confColor}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
        <span style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">Profile</span>
        <span style="font-size:8px;font-weight:700;color:var(--text);text-align:right">${profile}</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
        <span style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">Confidence</span>
        <span style="font-size:12px;font-weight:700;color:${confColor}">${conf.toFixed(0)}%</span>
      </div>
      ${sig.stale ? `
      <div style="margin-top:4px;margin-bottom:3px;padding:3px 8px;border-radius:4px;
        background:rgba(255,179,71,0.12);border:1px solid rgba(255,179,71,0.3);
        display:flex;align-items:center;gap:4px">
        <span style="font-size:9px">⚠️</span>
        <span style="font-size:8px;color:var(--gold)">Signal ${sig.stale_label || (sig.age_hours + 'h ago')} — click to refresh</span>
        <button onclick="refreshSignal('${sig.ticker || sym}')"
          style="font-size:7px;padding:1px 6px;border-radius:3px;background:var(--gold);
          color:#000;border:none;cursor:pointer;margin-left:auto;font-weight:700">REFRESH</button>
      </div>` : ''}
      ${(() => {
        const _conflict = sig.stale && (
          (sentiment === 'BEARISH' && pct >  1.0) ||
          (sentiment === 'BULLISH' && pct < -1.0)
        );
        if (!_conflict) return '';
        const _dir = pct > 0 ? `market +${pct.toFixed(1)}% but signal BEARISH` : `market ${pct.toFixed(1)}% but signal BULLISH`;
        return `<div style="margin-top:4px;padding:3px 8px;border-radius:4px;
          background:rgba(255,61,90,0.12);border:1px solid rgba(255,61,90,0.3);
          font-size:8px;color:var(--red)">
          ⚡ Stale signal conflicts with price action — ${_dir} — refresh recommended
        </div>`;
      })()}
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:9px;font-weight:700;color:${alignCol}">${alignment}</span>
        <span style="font-size:8px;color:var(--sub)">0 POOLS</span>
      </div>
    </div>

    <!-- ⑤ INSTITUTIONAL VOL -->
    <div style="display:flex;justify-content:space-between;align-items:center;
        padding:4px 9px;background:var(--bg3);border-radius:4px;margin-bottom:5px">
      <span style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">INSTITUTIONAL VOL</span>
      <span style="font-size:8px;font-weight:700;padding:2px 7px;border-radius:3px;
        background:${instVolCol}18;color:${instVolCol};border:1px solid ${instVolCol}35">${instVol}</span>
    </div>

    <!-- ⑥ GEX -->
    <div style="display:flex;justify-content:space-between;align-items:center;
        padding:4px 9px;background:var(--bg3);border-radius:4px;margin-bottom:7px">
      <span style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">GEX</span>
      <div style="display:flex;gap:4px">
        <span style="font-size:8px;font-weight:700;padding:1px 6px;border-radius:3px;
          background:${gexDirCol}18;color:${gexDirCol};border:1px solid ${gexDirCol}35">${gexDir}</span>
        <span style="font-size:8px;font-weight:700;padding:1px 6px;border-radius:3px;
          background:${gexBiasCol}18;color:${gexBiasCol};border:1px solid ${gexBiasCol}35">${gexBias}</span>
      </div>
    </div>

    <!-- ⑦ STRUCTURE TAGS -->
    ${tags.length ? `<div style="display:flex;gap:3px;flex-wrap:wrap;margin-bottom:7px">
      ${tags.map(t=>`<span style="font-size:7px;padding:2px 6px;border-radius:3px;
        background:${t.c}15;color:${t.c};border:1px solid ${t.c}30;font-weight:700;white-space:nowrap">${t.t}</span>`).join('')}
    </div>` : ''}

    <!-- ⑧ LEVEL TRIGGERS -->
    ${triggers.length ? `<div style="margin-bottom:7px">
      ${triggers.map(tr=>`<div style="display:flex;justify-content:space-between;align-items:center;
          padding:3px 0;border-bottom:1px solid var(--border)">
        <span style="font-size:8px;color:var(--gold)">${tr.label}
          <span style="font-size:7px;color:var(--sub);margin-left:3px">FRESH</span></span>
        <span style="font-size:7px;color:var(--sub)">${tr.when}</span>
      </div>`).join('')}
    </div>` : ''}

    <!-- ⑨ LEVELS TO WATCH (collapsible) -->
    ${levels.length ? `<div style="margin-bottom:8px">
      <div onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none';this.querySelector('.lvl-arrow').textContent=this.nextElementSibling.style.display==='none'?'▶':'▼'"
        style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;
          padding:4px 0;border-bottom:1px solid var(--border);margin-bottom:3px">
        <span style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">LEVELS TO WATCH</span>
        <span class="lvl-arrow" style="font-size:8px;color:var(--sub)">▼</span>
      </div>
      <div>
      ${levels.map(lv=>`<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--border)">
        <span style="font-size:9px;color:var(--sub)">${lv.l}</span>
        <span style="font-size:9px;font-weight:700;font-family:'JetBrains Mono',monospace;color:${lv.c}">${lv.v}</span>
      </div>`).join('')}
      </div>
    </div>` : ''}

    <!-- ⑩ DISCOMFORT INDEX -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:9px">
      <span style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase">DISCOMFORT INDEX</span>
      <div style="position:relative;width:40px;height:40px">
        <svg viewBox="0 0 40 40" style="transform:rotate(-90deg);width:40px;height:40px">
          <circle cx="20" cy="20" r="16" fill="none" stroke="var(--bg3)" stroke-width="3.5"/>
          <circle cx="20" cy="20" r="16" fill="none" stroke="${discColor}" stroke-width="3.5"
            stroke-dasharray="${(discomfort/100*100.53).toFixed(1)} 100.53" stroke-linecap="round"/>
        </svg>
        <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
          font-size:8px;font-weight:800;color:${discColor};font-family:'JetBrains Mono',monospace">
          ${discomfort}%
        </div>
      </div>
    </div>

    <!-- ⑪ ARJUN RECOMMENDS -->
    <div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:6px">
      <div style="font-size:7px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;margin-bottom:6px;text-align:center">
        A R J U N &nbsp; R E C O M M E N D S
      </div>
      ${rec}
    </div>

    <!-- BUY BUTTON — only when conviction threshold met -->
    ${(() => {
      const _wt = sig.would_trade === true || st === 'BUY' || st === 'SELL' || (conf >= 55 && st !== 'HOLD');
      if (_wt) return `<div style="margin-bottom:8px">
        <button onclick="manualIndexBuy('${sym}','${st==='BUY'?'call':st==='SELL'?'put':sentiment==='BULLISH'?'call':'put'}')"
          style="width:100%;padding:8px;border-radius:5px;cursor:pointer;font-size:11px;
            font-family:'JetBrains Mono',monospace;font-weight:700;border:none;
            background:${sentCol};color:#fff;letter-spacing:.5px">
          ${st==='SELL'||sentiment==='BEARISH' ? '📉 BUY PUT' : '📈 BUY CALL'} — ${sym}
        </button>
      </div>`;
      return `<div style="margin-bottom:8px;text-align:center;padding:7px;font-size:9px;
        color:var(--sub);border:1px dashed var(--border);border-radius:5px;letter-spacing:.3px">
        ⏳ Building conviction — no trade yet
      </div>`;
    })()}

    <!-- ⑫ ARKA SCAN STATUS -->
    ${(() => {
      const _arkaWatched = ['SPX','RUT','SPY','QQQ','IWM','DIA'].includes(sym);
      if (!_arkaWatched) return '';
      const _arkaScans   = window._arkaLastScans || {};
      const _lastScan    = _arkaScans[sym];
      const _scanScore   = _lastScan?.score ?? null;
      const _scanDecision= _lastScan?.decision ?? '—';
      const _scanTime    = _lastScan?.time ?? '—';
      const _scanCol     = _scanScore >= 55 ? 'var(--green)' : _scanScore >= 35 ? 'var(--gold)' : 'var(--sub)';
      return `<div style="background:var(--bg3);border-radius:5px;padding:6px 9px;margin-bottom:7px;
          border-left:2px solid var(--accent)">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="font-size:7px;letter-spacing:.8px;color:var(--sub);text-transform:uppercase">ARKA SCAN</span>
          <span style="font-size:7px;color:var(--sub)">${_scanTime}</span>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:3px">
          <span style="font-size:9px;font-weight:700;color:${_scanCol}">${_scanScore !== null ? 'conv ' + _scanScore.toFixed(0) : '👁 Watching'}</span>
          <span style="font-size:8px;color:var(--sub)">${_scanDecision}</span>
        </div>
      </div>`;
    })()}

    <!-- ⑬ FULL ANALYSIS (expandable) -->
    <div style="text-align:center;margin-bottom:4px">
      <button onclick="event.stopPropagation();_toggleDetail('${sym}')"
        style="font-size:8px;color:var(--sub);background:none;border:1px solid var(--border);
          cursor:pointer;letter-spacing:.5px;padding:3px 12px;border-radius:3px">
        ▼ FULL ANALYSIS
      </button>
    </div>
    <div id="detail-${sym}" style="display:none;border-top:1px solid var(--border);padding-top:8px;margin-top:4px">

      <!-- OPTIONS PLAY -->
      ${(() => {
        const _isCall = st === 'BUY' || sentiment === 'BULLISH';
        const _isPut  = st === 'SELL' || sentiment === 'BEARISH';
        const _rnd    = sym === 'SPX' || sym === 'NDX' ? 5 : 1;
        const _strike = Math.round(price / _rnd) * _rnd;
        const _dte    = conf >= 55 ? '0DTE' : '1DTE';
        const _nearCW = callWall && price > 0 && Math.abs(price - callWall) / price < 0.005;
        const _nearPW = putWall  && price > 0 && Math.abs(price - putWall)  / price < 0.005;
        const _why    = _nearCW  ? `Near call wall $${callWall} — rejection play`
                      : _nearPW  ? `Near put wall $${putWall} — bounce play`
                      : zeroGamma && zeroGamma !== 500 && price > 0 && Math.abs(price - zeroGamma) / price < 0.012
                        ? `Near gamma flip $${zeroGamma} — explosive move setup`
                      : _isCall  ? `Bullish conviction ${conf.toFixed(0)}/100`
                      : _isPut   ? `Bearish conviction ${conf.toFixed(0)}/100`
                      : `Watching — no directional edge yet`;
        if (_isCall || _isPut) {
          const _type   = _isCall ? 'CALL' : 'PUT';
          const _typeCol = _isCall ? 'var(--green)' : 'var(--red)';
          const _icon   = _isCall ? '📈' : '📉';
          return `<div style="background:var(--bg3);border-radius:5px;padding:10px;margin-bottom:8px;
              border-left:3px solid ${_typeCol}">
            <div style="font-size:7px;letter-spacing:1px;color:var(--sub);margin-bottom:5px">OPTIONS PLAY</div>
            <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:4px">
              <span style="font-size:13px;font-weight:800;font-family:'JetBrains Mono',monospace;color:${_typeCol}">
                ${_icon} BUY ${_type}
              </span>
              <span style="font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--text)">
                $${_strike}
              </span>
              <span style="font-size:9px;padding:1px 6px;background:${_typeCol}20;color:${_typeCol};
                border-radius:3px;font-weight:700">${_dte}</span>
            </div>
            <div style="font-size:8px;color:var(--sub);line-height:1.4">${_why}</div>
          </div>`;
        }
        return `<div style="background:var(--bg3);border-radius:5px;padding:8px;margin-bottom:8px">
          <div style="font-size:7px;letter-spacing:1px;color:var(--sub);margin-bottom:4px">OPTIONS PLAY</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:5px">
            <div style="font-size:9px;color:var(--green);font-weight:700">📈 CALL $${_strike} ${_dte}</div>
            <div style="font-size:9px;color:var(--red);font-weight:700">📉 PUT $${_strike} ${_dte}</div>
          </div>
          <div style="font-size:8px;color:var(--sub);margin-top:3px">${_why}</div>
        </div>`;
      })()}

      <!-- BULL / BEAR AGENT SCORES -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
        <div style="background:var(--bg3);border-radius:5px;padding:8px;border-left:2px solid var(--green)">
          <div style="font-size:7px;color:var(--sub);margin-bottom:2px">BULL AGENT</div>
          <div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--green)">${bullScore.toFixed(0)}</div>
          <div style="font-size:8px;color:var(--sub);margin-top:3px;line-height:1.3">
            ${bull.key_catalyst
              ? bull.key_catalyst.slice(0,90)+'…'
              : (sig.reasons||[]).filter(r=>!r.includes('-')).slice(0,2).join(' · ') || '—'}
          </div>
        </div>
        <div style="background:var(--bg3);border-radius:5px;padding:8px;border-left:2px solid var(--red)">
          <div style="font-size:7px;color:var(--sub);margin-bottom:2px">BEAR AGENT</div>
          <div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--red)">${bearScore.toFixed(0)}</div>
          <div style="font-size:8px;color:var(--sub);margin-top:3px;line-height:1.3">
            ${bear.key_risk
              ? bear.key_risk.slice(0,90)+'…'
              : (sig.reasons||[]).filter(r=>r.includes('-')||r.toLowerCase().includes('below')||r.toLowerCase().includes('weak')).slice(0,2).join(' · ') || '—'}
          </div>
        </div>
      </div>
      <div style="background:var(--bg3);border-radius:5px;padding:8px;margin-bottom:8px;
          border-left:2px solid ${riskDec==='APPROVE'?'var(--green)':'var(--red)'}">
        <div style="font-size:7px;color:var(--sub);margin-bottom:3px">RISK MANAGER</div>
        <div style="font-size:10px;font-weight:700;color:${riskDec==='APPROVE'?'var(--green)':'var(--red)'};margin-bottom:4px">${riskDec}</div>
        <div style="font-size:8px;color:var(--sub);line-height:1.4">${risk.reason || '—'}</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;margin-bottom:8px">
        ${[
          ['IV SKEW',    ivSkew.toFixed(4), ivSkew<-0.05?'var(--red)':ivSkew>0.05?'var(--green)':'var(--sub)'],
          ['GEX REGIME', gexRegime.replace('_',' '), netGex<0?'var(--red)':'var(--green)'],
          ['ADX',        adx.toFixed(1),    adx>25?'var(--green)':'var(--sub)'],
          ['CALL WALL',  f$(callWall),      'var(--red)'],
          ['PUT WALL',   f$(putWall),       'var(--green)'],
          ['NET GEX',    netGex.toFixed(3)+'B', netGex<0?'var(--red)':'var(--green)'],
        ].map(([l,v,c])=>`<div style="background:var(--bg3);border-radius:4px;padding:6px 7px">
          <div style="font-size:7px;color:var(--sub);margin-bottom:2px">${l}</div>
          <div style="font-size:9px;font-weight:700;color:${c};font-family:'JetBrains Mono',monospace">${v}</div>
        </div>`).join('')}
      </div>
      ${analyst.summary ? `<div style="background:var(--bg3);border-radius:5px;padding:8px;margin-bottom:8px">
        <div style="font-size:7px;color:var(--sub);margin-bottom:4px">ANALYST SUMMARY</div>
        <div style="font-size:9px;color:var(--text);line-height:1.5">${analyst.summary}</div>
      </div>` : ''}
      ${analyst.bull_factors?.length ? `<div style="margin-bottom:7px">
        <div style="font-size:7px;color:var(--green);letter-spacing:.8px;margin-bottom:4px">BULL FACTORS</div>
        ${analyst.bull_factors.map(f=>`<div style="font-size:8px;color:var(--sub);padding:2px 0;border-bottom:1px solid var(--border)">✓ ${f}</div>`).join('')}
      </div>` : ''}
      ${analyst.bear_factors?.length ? `<div style="margin-bottom:7px">
        <div style="font-size:7px;color:var(--red);letter-spacing:.8px;margin-bottom:4px">BEAR FACTORS</div>
        ${analyst.bear_factors.map(f=>`<div style="font-size:8px;color:var(--sub);padding:2px 0;border-bottom:1px solid var(--border)">✗ ${f}</div>`).join('')}
      </div>` : ''}
      <div style="background:var(--bg3);border-radius:5px;padding:8px;margin-bottom:8px">
        <div style="font-size:7px;color:var(--sub);letter-spacing:.8px;margin-bottom:6px">TRADE PLAN</div>
        ${[['Entry',f$(entry),'var(--text)'],['Target',f$(target),'var(--green)'],['Stop',f$(stop),'var(--red)'],['R/R',rr?'1:'+rr.toFixed(2):'—','var(--text)']].map(([l,v,c])=>`<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--border)">
          <span style="font-size:9px;color:var(--sub)">${l}</span>
          <span style="font-size:9px;font-weight:700;font-family:'JetBrains Mono',monospace;color:${c}">${v}</span>
        </div>`).join('')}
      </div>
      <div style="text-align:center">
        <button onclick="_toggleDetail('${sym}')"
          style="font-size:8px;color:var(--sub);background:none;border:none;cursor:pointer">▲ COLLAPSE</button>
      </div>
    </div>

    <!-- ⑬ FOOTER -->
    <div style="display:flex;justify-content:space-between;align-items:center;
        padding-top:6px;margin-top:4px;border-top:1px solid var(--border);font-size:9px;color:var(--sub)">
      <span>🐂 <span style="font-family:'JetBrains Mono',monospace;color:var(--green)">${bullScore.toFixed(0)}</span></span>
      <span>🐻 <span style="font-family:'JetBrains Mono',monospace;color:var(--red)">${bearScore.toFixed(0)}</span></span>
      <span class="chip ${riskDec==='APPROVE'?'cg':'cr'}" style="font-size:7px">${riskDec}</span>
      <span style="font-family:'JetBrains Mono',monospace;font-size:8px">${rr?'1:'+rr.toFixed(2):'—'}</span>
    </div>
  </div>`;
}

function _toggleDetail(sym) {
  const el = document.getElementById('detail-' + sym);
  if (!el) return;
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

// ══════════════════════════════════════════════════════════════
//  HELPERS
// ══════════════════════════════════════════════════════════════
function _deriveProfile(ind, gex, sentiment, orbTrend, goldenX, above200, bbUpper, bbLower) {
  const rsi    = parseFloat(ind.rsi || 50);
  const macd   = (ind.macd_trend || '').toLowerCase();
  const isWed  = new Date().getDay() === 3;
  const bbMid  = (bbUpper + bbLower) / 2;
  const bbTight = bbMid > 0 && (bbUpper - bbLower) / bbMid < 0.03;
  if (isWed && bbTight && sentiment === 'BEARISH') return 'WEDNESDAY HIGH BEARISH';
  if (isWed && bbTight)  return 'WEDNESDAY REVERSAL BULLISH';
  if (isWed && rsi < 32) return 'WEDNESDAY LOW BULLISH';
  if (goldenX && above200 && macd === 'bullish')  return 'TREND BULLISH';
  if (!above200 && macd === 'bearish')             return 'TREND BEARISH';
  if (sentiment === 'BULLISH' && orbTrend === 'BULLISH') return 'TREND BULLISH';
  if (sentiment === 'BEARISH' || orbTrend === 'BEARISH') return 'TREND BEARISH';
  return 'NONE';
}

function _discomfortIndex(rsi, volRatio, netScore, gexDir, orbTrend, adx, ivSkew, gexRegime) {
  let s = 0;
  if (rsi > 75 || rsi < 25) s += 25; else if (rsi > 65 || rsi < 35) s += 12;
  if (volRatio >= 3) s += 20; else if (volRatio >= 1.5) s += 8;
  if ((netScore > 0) !== (orbTrend === 'BULLISH')) s += 18;
  if (gexDir === 'NEG') s += 15;
  if (gexRegime === 'NEGATIVE_GAMMA') s += 10;
  if (netScore < -20) s += 15; else if (netScore < 0) s += 7;
  if (ivSkew < -0.10) s += 10;
  if (adx < 15) s += 8;
  return Math.min(100, Math.round(s));
}

function _detectSweep(price, callWall, putWall, zeroGamma, secondCall, secondPut) {
  const near = (a, b) => b > 0 && Math.abs(a - b) / b < 0.003;
  if (near(price, callWall))   return `CALL_WALL swept ${f$(callWall)} — expecting bearish reversal`;
  if (near(price, putWall))    return `PUT_WALL swept ${f$(putWall)} — expecting bullish reversal`;
  if (near(price, zeroGamma) && zeroGamma !== 500)
                               return `GAMMA_FLIP swept ${f$(zeroGamma)} — expecting bullish reversal`;
  if (near(price, secondCall)) return `2nd CALL_WALL swept ${f$(secondCall)} — expecting bearish reversal`;
  if (near(price, secondPut))  return `2nd PUT_WALL swept ${f$(secondPut)} — expecting bullish reversal`;
  return null;
}

function _buildStructureTags(orbTrend, goldenX, above200, above20, netGex, sentiment, ivSkew, gexRegime) {
  const tags = [];
  const G='var(--green)',R='var(--red)',Gold='var(--gold)',B='var(--blue)';
  if (orbTrend==='BULLISH')      tags.push({t:'Structure: BULLISH',c:G});
  else if(orbTrend==='BEARISH')  tags.push({t:'Structure: BEARISH',c:R});
  if (goldenX)                   tags.push({t:'OB: BULLISH',c:G});
  else if(!above200)             tags.push({t:'OB: BEARISH',c:R});
  if (above20===true)            tags.push({t:'Liquidity: BUY_SIDE',c:G});
  else if(above20===false)       tags.push({t:'Liquidity: SELL_SIDE',c:R});
  if (netGex>0)                  tags.push({t:'Sweep: BUY_SIDE',c:G});
  else if(netGex<0)              tags.push({t:'Sweep: SELL_SIDE',c:R});
  if (sentiment==='BULLISH'&&orbTrend==='BEARISH') tags.push({t:'Breaker: BULLISH +1',c:G});
  if (sentiment==='BEARISH'&&orbTrend==='BULLISH') tags.push({t:'Breaker: BEARISH +1',c:R});
  if (ivSkew<-0.05)              tags.push({t:'IV: PUT SKEW',c:R});
  else if(ivSkew>0.05)           tags.push({t:'IV: CALL SKEW',c:G});
  if (gexRegime==='NEGATIVE_GAMMA') tags.push({t:'NEGATIVE GAMMA',c:R});
  const h=new Date().getHours();
  if(h===9)       tags.push({t:'NY_OPEN',c:Gold});
  else if(h>=15)  tags.push({t:'LONDON_CLOSE',c:B});
  return tags.slice(0,7);
}

function _buildTriggers(price, callWall, putWall, zeroGamma) {
  const triggers=[]; const ago='2m ago';
  if(callWall&&price<callWall)  triggers.push({label:'CALL_WALL → Bearish Reversal',when:ago});
  if(putWall&&price>putWall)    triggers.push({label:'PUT_WALL → Bullish Reversal',when:ago});
  if(zeroGamma&&zeroGamma!==500&&Math.abs(price-zeroGamma)/zeroGamma<0.015)
    triggers.push({label:'GAMMA_FLIP → Bullish Reversal',when:ago});
  return triggers.slice(0,3);
}

function _buildLevels(target, stop, callWall, putWall, secondCall, secondPut, zeroGamma) {
  const lvls=[];
  if(putWall)   lvls.push({l:'PUT_WALL',  v:f$(putWall),   c:'var(--green)'});
  if(callWall)  lvls.push({l:'CALL_WALL', v:f$(callWall),  c:'var(--red)'});
  if(zeroGamma&&zeroGamma!==500) lvls.push({l:'ZERO GAMMA',v:f$(zeroGamma),c:'var(--gold)'});
  if(secondCall)lvls.push({l:'2nd CALL',  v:f$(secondCall),c:'var(--red)'});
  if(secondPut) lvls.push({l:'2nd PUT',   v:f$(secondPut), c:'var(--green)'});
  if(target)    lvls.push({l:'Target',    v:f$(target),    c:'var(--green)'});
  if(stop)      lvls.push({l:'Stop',      v:f$(stop),      c:'var(--red)'});
  return lvls.slice(0,4);
}

function _buildArjunRec(sym, st, conf, riskDec, blocks, entry, target, stop, rr, discomfort, alignment) {
  const isBlocked = riskDec==='BLOCK'||riskDec==='REJECT';
  const blockMsg  = blocks.length ? blocks[0] : (isBlocked?'Risk limits exceeded':'');
  const contract  = entry ? _suggestContract(sym,st,entry,target,stop,rr) : null;
  const contractEl = contract
    ? `<div style="text-align:center;margin-top:5px">
        <span style="font-size:9px;padding:3px 12px;background:var(--bg3);border-radius:4px;
          font-family:'JetBrains Mono',monospace;color:var(--sub)">${contract}</span>
      </div>` : '';
  if(isBlocked&&blockMsg) {
    return `<div style="color:var(--gold);font-size:11px;font-weight:800;text-align:center;
        padding:6px 4px;letter-spacing:.3px;line-height:1.5">
        BLOCKED: ${blockMsg.replace(/_/g,' ')}
      </div>${contractEl}`;
  }
  if(alignment==='CONFLICTED')
    return `<div style="color:var(--gold);font-size:12px;font-weight:800;text-align:center;
        padding:6px 0">BLOCKED: Critical Conflict Cluster</div>${contractEl}`;
  if(discomfort>=85)
    return `<div style="color:var(--gold);font-size:12px;font-weight:800;text-align:center;
        padding:6px 0">BLOCKED: Discomfort ${discomfort}%</div>`;
  if(st==='BUY')
    return `<div style="color:var(--green);font-size:14px;font-weight:800;text-align:center;
        padding:6px 0">BUY CALLS</div>${contractEl}`;
  if(st==='SELL')
    return `<div style="color:var(--red);font-size:14px;font-weight:800;text-align:center;
        padding:6px 0">BUY PUTS</div>${contractEl}`;
  if(conf<55)
    return `<div style="color:var(--gold);font-size:12px;font-weight:700;text-align:center;padding:6px 0">WATCHING</div>
      <div style="text-align:center"><span style="font-size:8px;color:var(--sub)">Building conviction…</span></div>`;
  return `<div style="color:var(--gold);font-size:12px;font-weight:700;text-align:center;padding:6px 0">WAITING FOR TRIGGER</div>
    <div style="text-align:center"><span style="font-size:8px;color:var(--sub)">Waiting for entry trigger…</span></div>`;
}

function _suggestContract(sym, st, entry, target, stop, rr) {
  const dte=rr<=1.5?'0DTE':rr<=2.5?'2DTE':rr<=3.5?'7DTE':'9DTE';
  const isCall=st==='BUY';
  const interval=entry>500?5:entry>200?2.5:entry>100?1:0.5;
  const strike=isCall?Math.ceil(entry/interval)*interval:Math.floor(entry/interval)*interval;
  const dteNum={'0DTE':0.25,'2DTE':2,'7DTE':7,'9DTE':9}[dte]||1;
  const prem=Math.max(0.05,
    (isCall?Math.max(0,entry-strike):Math.max(0,strike-entry))
    +entry*0.18*Math.sqrt(dteNum/365)*0.4
  ).toFixed(2);
  return `${dte} ${f$(strike)} ${isCall?'C':'P'} ~${f$(prem)}`;
}

function _instVolLabel(v) {
  if(v>=10)  return `EXTREME ${v.toFixed(1)}X +2`;
  if(v>=3)   return `EXTREME ${v.toFixed(1)}X +1`;
  if(v>=1.5) return `CLIMAX NEUTRAL +${v>=2?3:1}`;
  if(v>=1.1) return `SPIKE ${v.toFixed(1)}X +1`;
  return 'DELTA';
}

function showSignalDetail(sym) {}

// ══════════════════════════════════════════════════════════════
//  BACKTEST + VALIDATION
// ══════════════════════════════════════════════════════════════
function _loadBacktest(bt) {
  const btb=$('btBody'); if(!btb)return;
  btb.innerHTML=`<tr><td colspan="6"><div class="empty">Backtest data loads after 30-day validation</div></td></tr>`;
}

function _loadValidation() {
  const start=new Date('2026-02-24'),now=new Date();
  const dn=Math.floor((now-start)/864e5)+1;
  const dayB=$('dayB'); if(dayB) dayB.textContent='DAY '+Math.min(dn,30)+'/30';
  const weeks=[
    {w:'Week 1',d:'Feb 24–28',s:'2026-02-24',e:'2026-02-28'},
    {w:'Week 2',d:'Mar 3–7',  s:'2026-03-03',e:'2026-03-07'},
    {w:'Week 3',d:'Mar 10–14',s:'2026-03-10',e:'2026-03-14'},
    {w:'Week 4',d:'Mar 17–21',s:'2026-03-17',e:'2026-03-21'},
  ];
  const today=now.toISOString().split('T')[0];
  const vb=$('valBody'); if(!vb)return;
  vb.innerHTML=weeks.map(w=>{
    const cur=today>=w.s&&today<=w.e,done=today>w.e;
    const st=cur?'<span class="chip cg">LIVE</span>':!done?'<span style="color:var(--sub);font-size:9px">WAITING</span>':'<span style="color:var(--sub);font-size:9px">DONE</span>';
    return `<tr><td class="mn" style="font-weight:700">${w.w}</td>
      <td style="color:var(--sub);font-size:10px">${w.d}</td>
      <td class="mn">—</td><td class="mn">—</td><td class="mn">—</td><td>${st}</td></tr>`;
  }).join('')+'<tr><td colspan="6" style="padding:7px 10px;font-size:8px;color:var(--sub)">PASS: Win% ≥60% · Max DD &lt;25% · 30 days stable</td></tr>';
}

// ══════════════════════════════════════════════════════════════
//  PERFORMANCE PAGE
// ══════════════════════════════════════════════════════════════
async function loadPerformance() {
  const el = $('performanceContent');
  if (!el) return;
  el.innerHTML = '<div class="ld"><div class="spin"></div>Loading…</div>';

  const [acct, perfData] = await Promise.all([
    getCached('/api/account', 15000),
    getCached('/api/arka/performance', 30000),
  ]);

  const equity      = parseFloat(acct?.portfolio_value || 100000);
  const startEquity = 100000;
  // Paired trades: each row = one complete round-trip (BUY matched with SELL)
  const allTrades   = (perfData?.trades || []);
  const closedT     = allTrades.filter(t => t.status === 'CLOSED');
  const liveT       = allTrades.filter(t => t.status === 'LIVE');
  const dailyPnl    = (perfData?.daily_pnl || []).filter(d => d.trades > 0)
                       .sort((a, b) => new Date(b.date) - new Date(a.date));
  const totalPnl    = perfData?.total_pnl ?? 0;
  const totalTrades = perfData?.total_trades ?? closedT.length;
  const wins        = perfData?.wins ?? closedT.filter(t => (t.pnl || 0) > 0).length;
  const losses      = perfData?.losses ?? closedT.filter(t => (t.pnl || 0) < 0).length;
  const winRate     = (wins + losses) > 0 ? ((wins / (wins + losses)) * 100).toFixed(1) : '0.0';
  const roi         = (((equity - startEquity) / startEquity) * 100).toFixed(1);
  const pnlCol      = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
  const byCat       = perfData?.by_category || {};

  // ── Stats strip ──────────────────────────────────────────────────────────
  const liveCount  = liveT.length;
  const indexPnl   = byCat['INDEX']?.pnl ?? 0;
  const swingPnl   = byCat['SWING']?.pnl ?? 0;
  const statsHtml = [
    ['TOTAL P&L',    (totalPnl >= 0 ? '+' : '') + '$' + Math.abs(totalPnl).toFixed(2), pnlCol],
    ['WIN RATE',     winRate + '%',      parseFloat(winRate) >= 50 ? 'var(--green)' : 'var(--red)'],
    ['TOTAL TRADES', totalTrades + (liveCount ? ' +' + liveCount + ' LIVE' : ''), 'var(--text)'],
    ['ROI',          (parseFloat(roi) >= 0 ? '+' : '') + roi + '%', parseFloat(roi) >= 0 ? 'var(--green)' : 'var(--red)'],
  ].map(([l, v, c]) => `
    <div style="background:var(--bg2);padding:16px 20px">
      <div style="font-size:8px;letter-spacing:1.5px;color:var(--sub);text-transform:uppercase;margin-bottom:8px">${l}</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:800;color:${c}">${v}</div>
    </div>`).join('');

  // ── Category summary row ──────────────────────────────────────────────────
  const catSummaryHtml = Object.keys(byCat).length ? `
    <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
      ${Object.entries(byCat).map(([cat, s]) => {
        const pc = s.pnl >= 0 ? 'var(--green)' : 'var(--red)';
        const wr = s.trades ? Math.round(s.wins / s.trades * 100) : 0;
        return `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;
          padding:8px 14px;min-width:110px">
          <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">${cat}</div>
          <div style="font-size:16px;font-weight:800;font-family:'JetBrains Mono',monospace;color:${pc}">
            ${s.pnl >= 0 ? '+' : ''}$${Math.abs(s.pnl).toFixed(0)}
          </div>
          <div style="font-size:9px;color:var(--sub);margin-top:2px">${s.trades}T · ${wr}% WR</div>
        </div>`;
      }).join('')}
    </div>` : '';

  // ── Daily P&L cards (CHAKRA-style) ────────────────────────────────────────
  const dayCards = dailyPnl.slice(0, 7).map(d => {
    const p = parseFloat(d.pnl || 0);
    const c = p >= 0 ? 'var(--green)' : 'var(--red)';
    const label = new Date(d.date + 'T12:00:00').toLocaleDateString('en-US',
      { month: 'short', day: 'numeric', year: '2-digit' });
    const pctStr = d.roi ? d.roi.toFixed(1) + '%' : '';
    return `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;
      padding:14px 16px;min-width:120px;flex-shrink:0;cursor:pointer"
      onclick="filterByDate('${d.date}')">
      <div style="font-size:9px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:6px">${label}</div>
      <div style="font-size:22px;font-weight:800;color:${c};font-family:'JetBrains Mono',monospace">
        ${p >= 0 ? '+' : ''}$${Math.abs(p).toFixed(0)}
      </div>
      <div style="font-size:9px;color:var(--sub);margin-top:4px">${d.trades} position${d.trades !== 1 ? 's' : ''}${pctStr ? ' · ' + pctStr : ''}</div>
    </div>`;
  }).join('');

  // ── Trade rows — one row per round-trip (BUY→SELL paired) ─────────────────
  const tradeRows = allTrades.map((t, i) => {
    const pnl     = t.pnl;
    const isLive  = t.status === 'LIVE';
    const pnlStr  = pnl == null ? '—' : (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2);
    const pnlCol2 = pnl == null ? 'var(--sub)' : pnl >= 0 ? 'var(--green)' : 'var(--red)';

    // Date: entry date (exit date if different)
    const entryDt  = t.entry_date ? new Date(t.entry_date + 'T12:00:00') : null;
    const exitDt   = t.exit_date  ? new Date(t.exit_date  + 'T12:00:00') : null;
    const dateLabel = entryDt
      ? entryDt.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' })
      : '—';
    const sameDayExit = !exitDt || (t.exit_date === t.entry_date);
    const timeRange = sameDayExit
      ? (t.entry_time || '—')
      : `${t.entry_time || '—'} → ${t.exit_time || '—'}`;

    // Type badge: CALL/PUT/EQUITY
    const ctype   = t.type || 'EQUITY';
    const isCall  = ctype === 'CALL';
    const isPut   = ctype === 'PUT';
    const typeBg  = isCall ? 'rgba(0,208,132,0.12)' : isPut ? 'rgba(255,61,90,0.12)' : 'rgba(61,142,255,0.12)';
    const typeCol = isCall ? 'var(--green)' : isPut ? 'var(--red)' : 'var(--blue)';

    // Category badge: INDEX/SWING/EQUITY
    const cat    = t.category || '—';
    const catBg  = cat === 'INDEX' ? 'rgba(255,140,0,0.12)' : 'rgba(150,100,255,0.12)';
    const catCol = cat === 'INDEX' ? 'var(--gold)' : 'var(--purple)';

    // Status badge
    const isExpired   = t.status === 'EXPIRED';
    const isEodClosed = t.status === 'EOD_CLOSED';
    let statusBg, statusCol, statusTxt, rowBorder;
    if (isLive) {
      statusBg = 'rgba(255,215,0,0.15)'; statusCol = 'var(--gold)'; statusTxt = 'LIVE';
      rowBorder = '';
    } else if (isExpired) {
      statusBg = 'rgba(150,100,255,0.12)'; statusCol = 'var(--purple)'; statusTxt = 'EXPIRED';
      rowBorder = '';
    } else if (isEodClosed) {
      statusBg = 'rgba(255,140,0,0.12)'; statusCol = 'var(--gold)'; statusTxt = 'EOD CLOSED';
      rowBorder = '';
    } else if (pnl == null) {
      statusBg = 'rgba(61,142,255,0.1)'; statusCol = 'var(--blue)'; statusTxt = 'PENDING';
      rowBorder = '';
    } else {
      statusBg = pnl >= 0 ? 'rgba(0,208,132,0.1)' : 'rgba(255,61,90,0.1)';
      statusCol = pnl >= 0 ? 'var(--green)' : 'var(--red)';
      statusTxt = pnl >= 0 ? 'WIN' : 'LOSS';
      rowBorder = '';
    }
    const rowBg = isLive ? 'background:rgba(255,215,0,0.06);' : '';
    const firstTdBorder = isLive ? 'border-left:3px solid var(--gold);' : 'border-left:3px solid transparent;';

    const entryStr = t.entry != null ? '$' + parseFloat(t.entry).toFixed(2) : '—';
    const exitStr  = t.exit  != null ? '$' + parseFloat(t.exit).toFixed(2)  : '—';

    return `<tr style="border-bottom:1px solid var(--border);cursor:pointer;${rowBg}"
      onclick="showTradeDetail(${i})" class="trade-row${isLive ? ' live-row' : ''}">
      <td style="padding:10px 12px;color:var(--sub);font-size:10px;white-space:nowrap;${firstTdBorder}">${dateLabel}</td>
      <td style="padding:10px 4px;font-size:10px;color:var(--sub);white-space:nowrap">${timeRange}</td>
      <td style="padding:10px 12px">
        <span style="font-family:'JetBrains Mono',monospace;font-weight:700;font-size:13px">${t.ticker || '—'}</span>
      </td>
      <td style="padding:10px 4px">
        <span style="font-size:8px;font-weight:700;padding:2px 5px;border-radius:3px;
          background:${catBg};color:${catCol};border:1px solid ${catCol}40">${cat}</span>
      </td>
      <td style="padding:10px 4px">
        <span style="font-size:8px;font-weight:700;padding:2px 5px;border-radius:3px;
          background:${typeBg};color:${typeCol};border:1px solid ${typeCol}40">${ctype}</span>
      </td>
      <td style="padding:10px 8px;font-family:'JetBrains Mono',monospace;font-size:11px">${t.qty || '—'}</td>
      <td style="padding:10px 8px;font-family:'JetBrains Mono',monospace;font-size:11px">${entryStr}</td>
      <td style="padding:10px 8px;font-family:'JetBrains Mono',monospace;font-size:11px">${exitStr}</td>
      <td style="padding:10px 8px;font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:${pnlCol2}">${pnlStr}</td>
      <td style="padding:10px 4px">
        <span style="font-size:8px;font-weight:700;padding:2px 5px;border-radius:3px;
          background:${statusBg};color:${statusCol};border:1px solid ${statusCol}40">${statusTxt}</span>
      </td>
      <td style="padding:10px 8px">
        <span style="font-size:9px;color:var(--accent);cursor:pointer"
          onclick="showTradeDetail(${i});event.stopPropagation()">View →</span>
      </td>
    </tr>`;
  }).join('');

  // ── Cumulative P&L chart ─────────────────────────────────────────────────
  // Use paired closed trades sorted oldest-exit first
  const closedTrades = [...closedT]
    .sort((a, b) => new Date((a.exit_date||a.entry_date)+'T'+(a.exit_time||'00:00'))
                  - new Date((b.exit_date||b.entry_date)+'T'+(b.exit_time||'00:00')));
  let cum = 0;
  const cumPts = [0, ...closedTrades.map(t => { cum += (t.pnl || 0); return cum; })];
  const cumMin  = Math.min(...cumPts, 0);
  const cumMax  = Math.max(...cumPts, 0.01);
  const cumRange = cumMax - cumMin || 1;
  const cW = 600, cH = 80;
  const ptX = (i) => (i / Math.max(cumPts.length - 1, 1)) * cW;
  const ptY = (v) => cH - ((v - cumMin) / cumRange) * cH;
  const pathD = cumPts.map((v, i) => `${i === 0 ? 'M' : 'L'}${ptX(i).toFixed(1)},${ptY(v).toFixed(1)}`).join(' ');
  const zeroY = ptY(0).toFixed(1);
  const chartColor = totalPnl >= 0 ? '#00d084' : '#ff3d5a';
  const cumChartHtml = cumPts.length > 1 ? `
    <div class="panel" style="margin-bottom:16px">
      <div class="ph"><span class="pt">Cumulative P&L</span>
        <span style="font-size:10px;color:${chartColor};font-family:'JetBrains Mono',monospace">
          ${totalPnl >= 0 ? '+' : ''}$${Math.abs(totalPnl).toFixed(2)}
        </span>
      </div>
      <svg viewBox="0 0 ${cW} ${cH}" style="width:100%;height:80px;display:block;overflow:visible">
        <line x1="0" y1="${zeroY}" x2="${cW}" y2="${zeroY}" stroke="var(--border)" stroke-width="1" stroke-dasharray="4,4"/>
        <polyline points="${cumPts.map((v,i)=>`${ptX(i).toFixed(1)},${ptY(v).toFixed(1)}`).join(' ')}"
          fill="none" stroke="${chartColor}" stroke-width="2" stroke-linejoin="round"/>
        <circle cx="${ptX(cumPts.length-1).toFixed(1)}" cy="${ptY(cumPts[cumPts.length-1]).toFixed(1)}"
          r="3" fill="${chartColor}"/>
      </svg>
    </div>` : '';

  // ── Win/Loss by ticker ────────────────────────────────────────────────────
  const byTicker = {};
  for (const t of closedTrades) {
    const tk = t.ticker || '?';
    if (!byTicker[tk]) byTicker[tk] = { wins: 0, losses: 0, pnl: 0, trades: 0 };
    byTicker[tk].trades++;
    byTicker[tk].pnl += t.pnl || 0;
    if ((t.pnl || 0) > 0) byTicker[tk].wins++;
    else byTicker[tk].losses++;
  }
  const tickerRows = Object.entries(byTicker)
    .sort((a, b) => Math.abs(b[1].pnl) - Math.abs(a[1].pnl))
    .slice(0, 8)
    .map(([tk, s]) => {
      const wr  = s.trades ? ((s.wins / s.trades) * 100).toFixed(0) : 0;
      const pc  = s.pnl >= 0 ? 'var(--green)' : 'var(--red)';
      const pStr = (s.pnl >= 0 ? '+' : '') + '$' + Math.abs(s.pnl).toFixed(0);
      return `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:7px 10px;font-weight:700;font-family:'JetBrains Mono',monospace">${tk}</td>
        <td style="padding:7px 6px;color:var(--sub)">${s.trades}</td>
        <td style="padding:7px 6px"><span style="color:var(--green)">${s.wins}W</span> / <span style="color:var(--red)">${s.losses}L</span></td>
        <td style="padding:7px 6px;color:var(--sub)">${wr}%</td>
        <td style="padding:7px 10px;font-weight:700;color:${pc};font-family:'JetBrains Mono',monospace">${pStr}</td>
      </tr>`;
    }).join('');

  // ── Best / Worst trades ───────────────────────────────────────────────────
  const sorted = [...closedTrades].sort((a, b) => (b.pnl || 0) - (a.pnl || 0));
  const bestWorst = (trades, label, col) => trades.slice(0, 3).map(t =>
    `<div style="display:flex;justify-content:space-between;align-items:center;
      padding:6px 0;border-bottom:1px solid var(--border)">
      <div>
        <span style="font-weight:700;font-size:11px">${t.ticker}</span>
        <span style="font-size:9px;color:var(--sub);margin-left:6px">${t.date} · ${t.type||''}</span>
      </div>
      <span style="font-family:'JetBrains Mono',monospace;font-weight:700;font-size:12px;color:${col}">
        ${(t.pnl||0) >= 0 ? '+' : ''}$${Math.abs(t.pnl||0).toFixed(2)}
      </span>
    </div>`).join('');

  const bestWorstHtml = sorted.length ? `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px">
      <div class="panel">
        <div class="ph"><span class="pt">🏆 Best Trades</span></div>
        <div style="padding:8px 12px">${bestWorst(sorted, 'Best', 'var(--green)') || '<div class="empty">No closed trades</div>'}</div>
      </div>
      <div class="panel">
        <div class="ph"><span class="pt">💀 Worst Trades</span></div>
        <div style="padding:8px 12px">${bestWorst([...sorted].reverse(), 'Worst', 'var(--red)') || '<div class="empty">No closed trades</div>'}</div>
      </div>
    </div>` : '';

  // ── Daily bar chart (SVG) ────────────────────────────────────────────────
  const barDays = dailyPnl.slice(0, 14).reverse();
  const barMax  = Math.max(...barDays.map(d => Math.abs(d.pnl)), 1);
  const barH = 48, barW = 28, barGap = 4;
  const barsSvgW = barDays.length * (barW + barGap);
  const barsSvg = barDays.map((d, i) => {
    const h   = Math.max(2, (Math.abs(d.pnl) / barMax) * barH);
    const col = d.pnl >= 0 ? '#00d084' : '#ff3d5a';
    const x   = i * (barW + barGap);
    const y   = barH - h;
    const lbl = new Date(d.date + 'T12:00').toLocaleDateString('en-US', {month:'numeric',day:'numeric'});
    return `<rect x="${x}" y="${y}" width="${barW}" height="${h}" fill="${col}" rx="2"/>
      <text x="${x + barW/2}" y="${barH + 12}" text-anchor="middle"
        fill="var(--sub)" font-size="7">${lbl}</text>`;
  }).join('');
  const barChartHtml = barDays.length > 1 ? `
    <div class="panel" style="margin-bottom:16px">
      <div class="ph"><span class="pt">Daily P&L — Last ${barDays.length} Days</span></div>
      <div style="padding:8px 12px;overflow-x:auto">
        <svg width="${barsSvgW}" height="${barH + 20}" style="display:block;min-width:100%">
          ${barsSvg}
        </svg>
      </div>
    </div>` : '';

  el.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);
      border-radius:8px;overflow:hidden;margin-bottom:12px">${statsHtml}</div>

    ${catSummaryHtml}
    ${cumChartHtml}
    ${barChartHtml}
    ${bestWorstHtml}

    ${Object.keys(byTicker).length ? `
    <div class="panel" style="margin-bottom:16px">
      <div class="ph"><span class="pt">Win/Loss by Ticker</span></div>
      <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:11px">
          <thead><tr style="border-bottom:1px solid var(--border)">
            ${['TICKER','TRADES','W/L','WIN%','P&L'].map(h =>
              `<th style="padding:7px 10px;text-align:left;font-size:8px;letter-spacing:1px;color:var(--sub)">${h}</th>`
            ).join('')}
          </tr></thead>
          <tbody>${tickerRows}</tbody>
        </table>
      </div>
    </div>` : ''}

    <div style="font-size:9px;letter-spacing:2px;color:var(--sub);text-transform:uppercase;margin-bottom:10px">
      Daily P&L (Last ${Math.min(dailyPnl.length, 7)} Trading Days)
      <span style="color:var(--sub);font-size:9px;margin-left:8px;text-transform:none;letter-spacing:0">
        Click a day to filter
      </span>
    </div>
    <div style="display:flex;gap:10px;overflow-x:auto;padding-bottom:12px;margin-bottom:20px">
      ${dayCards || '<div class="empty">No daily data yet</div>'}
    </div>

    <div class="panel">
      <div class="ph">
        <span class="pt">All Positions</span>
        <span style="font-size:10px;color:var(--sub)">${totalTrades} trades</span>
        <button id="perfFilterClear" onclick="filterByDate(null)"
          style="display:none;margin-left:8px;font-size:9px;padding:2px 8px;border-radius:3px;
          border:1px solid var(--border);background:var(--bg3);color:var(--sub);cursor:pointer">
          Clear filter ×
        </button>
      </div>
      <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:12px" id="perfTable">
          <thead>
            <tr style="border-bottom:1px solid var(--border)" id="perfTableHeader">
              ${[
                ['DATE',     'entry_date'],
                ['TIME',     ''],
                ['TICKER',   'ticker'],
                ['CATEGORY', 'category'],
                ['TYPE',     'type'],
                ['SIZE',     'qty'],
                ['ENTRY',    'entry'],
                ['EXIT',     'exit'],
                ['P&L',      'pnl'],
                ['STATUS',   'status'],
                ['',         ''],
              ].map(([h, key]) =>
                key
                  ? `<th data-sort="${key}" style="padding:8px 12px;text-align:left;font-size:8px;letter-spacing:1.5px;
                      color:var(--sub);font-weight:600;white-space:nowrap;cursor:pointer;user-select:none">
                      ${h} <span class="perf-sort-arrow"></span></th>`
                  : `<th style="padding:8px 12px;text-align:left;font-size:8px;letter-spacing:1.5px;
                      color:var(--sub);font-weight:600;white-space:nowrap">${h}</th>`
              ).join('')}
            </tr>
          </thead>
          <tbody id="perfTbody">${tradeRows || '<tr><td colspan="11"><div class="empty">No trades yet</div></td></tr>'}</tbody>
        </table>
      </div>
    </div>

    <!-- Swing Positions Section -->
    <div class="panel" style="margin-top:16px">
      <div class="ph">
        <span class="pt">🌀 CHAKRA Swing Positions</span>
        <span id="swingsBadge" style="font-size:10px;color:var(--sub)">Loading…</span>
      </div>
      <div id="swingsContent" style="padding:12px">
        <div class="ld"><div class="spin"></div></div>
      </div>
    </div>

    <!-- Trade detail modal -->
    <div id="tradeDetailModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);
      z-index:1000;display:none;align-items:center;justify-content:center" onclick="closeTradeDetail()">
      <div id="tradeDetailContent" style="background:var(--bg1);border:1px solid var(--border);
        border-radius:12px;padding:24px;max-width:500px;width:90%;max-height:80vh;overflow-y:auto"
        onclick="event.stopPropagation()"></div>
    </div>
  `;

  // Store paired trades for detail view + sorting
  window._perfTrades   = allTrades;
  window._perfSortCol  = null;
  window._perfSortDir  = 1;
  _initPerfSortHeaders();
  // Load swing positions
  _loadSwingsInPerformance();
}

function _buildPerfRow(t, i) {
  // Re-generate a single trade row (used when re-sorting)
  const pnl     = t.pnl;
  const isLive  = t.status === 'LIVE';
  const pnlStr  = pnl == null ? '—' : (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2);
  const pnlCol2 = pnl == null ? 'var(--sub)' : pnl >= 0 ? 'var(--green)' : 'var(--red)';
  const entryDt  = t.entry_date ? new Date(t.entry_date + 'T12:00:00') : null;
  const exitDt   = t.exit_date  ? new Date(t.exit_date  + 'T12:00:00') : null;
  const dateLabel = entryDt
    ? entryDt.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' }) : '—';
  const sameDayExit = !exitDt || (t.exit_date === t.entry_date);
  const timeRange = sameDayExit ? (t.entry_time || '—') : `${t.entry_time || '—'} → ${t.exit_time || '—'}`;
  const ctype   = t.type || 'EQUITY';
  const isCall  = ctype === 'CALL', isPut = ctype === 'PUT';
  const typeBg  = isCall ? 'rgba(0,208,132,0.12)' : isPut ? 'rgba(255,61,90,0.12)' : 'rgba(61,142,255,0.12)';
  const typeCol = isCall ? 'var(--green)' : isPut ? 'var(--red)' : 'var(--blue)';
  const cat = t.category || '—';
  const catBg  = cat === 'INDEX' ? 'rgba(255,140,0,0.12)' : 'rgba(150,100,255,0.12)';
  const catCol = cat === 'INDEX' ? 'var(--gold)' : 'var(--purple)';
  const isExpired = t.status === 'EXPIRED', isEod = t.status === 'EOD_CLOSED';
  let statusBg, statusCol, statusTxt, rowBorder;
  if (isLive)     { statusBg='rgba(255,215,0,0.15)';     statusCol='var(--gold)';   statusTxt='LIVE'; }
  else if (isExpired) { statusBg='rgba(150,100,255,0.12)'; statusCol='var(--purple)'; statusTxt='EXPIRED'; }
  else if (isEod)     { statusBg='rgba(255,140,0,0.12)';  statusCol='var(--gold)';   statusTxt='EOD CLOSED'; }
  else if (pnl==null) { statusBg='rgba(61,142,255,0.1)';  statusCol='var(--blue)';   statusTxt='PENDING'; }
  else { statusBg=pnl>=0?'rgba(0,208,132,0.1)':'rgba(255,61,90,0.1)'; statusCol=pnl>=0?'var(--green)':'var(--red)'; statusTxt=pnl>=0?'WIN':'LOSS'; }
  const rowBg2 = isLive ? 'background:rgba(255,215,0,0.06);' : '';
  const firstTdBorder2 = isLive ? 'border-left:3px solid var(--gold);' : 'border-left:3px solid transparent;';
  const entryStr = t.entry != null ? '$' + parseFloat(t.entry).toFixed(2) : '—';
  const exitStr  = t.exit  != null ? '$' + parseFloat(t.exit).toFixed(2)  : '—';
  return `<tr style="border-bottom:1px solid var(--border);cursor:pointer;${rowBg2}" onclick="showTradeDetail(${i})" class="trade-row${isLive ? ' live-row' : ''}">
    <td style="padding:10px 12px;color:var(--sub);font-size:10px;white-space:nowrap;${firstTdBorder2}">${dateLabel}</td>
    <td style="padding:10px 4px;font-size:10px;color:var(--sub);white-space:nowrap">${timeRange}</td>
    <td style="padding:10px 12px"><span style="font-family:'JetBrains Mono',monospace;font-weight:700;font-size:13px">${t.ticker||'—'}</span></td>
    <td style="padding:10px 4px"><span style="font-size:8px;font-weight:700;padding:2px 5px;border-radius:3px;background:${catBg};color:${catCol};border:1px solid ${catCol}40">${cat}</span></td>
    <td style="padding:10px 4px"><span style="font-size:8px;font-weight:700;padding:2px 5px;border-radius:3px;background:${typeBg};color:${typeCol};border:1px solid ${typeCol}40">${ctype}</span></td>
    <td style="padding:10px 8px;font-family:'JetBrains Mono',monospace;font-size:11px">${t.qty||'—'}</td>
    <td style="padding:10px 8px;font-family:'JetBrains Mono',monospace;font-size:11px">${entryStr}</td>
    <td style="padding:10px 8px;font-family:'JetBrains Mono',monospace;font-size:11px">${exitStr}</td>
    <td style="padding:10px 8px;font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:${pnlCol2}">${pnlStr}</td>
    <td style="padding:10px 4px"><span style="font-size:8px;font-weight:700;padding:2px 5px;border-radius:3px;background:${statusBg};color:${statusCol};border:1px solid ${statusCol}40">${statusTxt}</span></td>
    <td style="padding:10px 8px"><span style="font-size:9px;color:var(--accent);cursor:pointer" onclick="showTradeDetail(${i});event.stopPropagation()">View →</span></td>
  </tr>`;
}

function _sortPerfTable(col) {
  const trades = window._perfTrades;
  if (!trades || !trades.length) return;
  if (window._perfSortCol === col) window._perfSortDir *= -1;
  else { window._perfSortCol = col; window._perfSortDir = col === 'pnl' ? -1 : 1; }
  const d = window._perfSortDir;
  trades.sort((a, b) => {
    // LIVE rows always float to top regardless of sort column
    const aLive = a.status === 'LIVE' ? 0 : 1;
    const bLive = b.status === 'LIVE' ? 0 : 1;
    if (aLive !== bLive) return aLive - bLive;

    let av = a[col], bv = b[col];
    if (col === 'entry_date') {
      const at = new Date(((a.entry_date || '1970-01-01') + 'T' + (a.entry_time || '00:00:00')).replace(' ', 'T')).getTime();
      const bt = new Date(((b.entry_date || '1970-01-01') + 'T' + (b.entry_time || '00:00:00')).replace(' ', 'T')).getTime();
      return d * (at - bt);
    } else if (col === 'status') {
      const order = { LIVE: 0, PENDING: 1, WIN: 2, LOSS: 3, EOD_CLOSED: 4, EXPIRED: 5, CLOSED: 6 };
      return d * ((order[a.status] ?? 9) - (order[b.status] ?? 9));
    } else if (col === 'entry' || col === 'exit' || col === 'qty') {
      av = parseFloat(av || 0); bv = parseFloat(bv || 0);
    } else if (col === 'pnl') {
      av = parseFloat(av ?? 99999); bv = parseFloat(bv ?? 99999);
    } else {
      av = (av || '').toString(); bv = (bv || '').toString();
      return d * av.localeCompare(bv);
    }
    return d * (av - bv);
  });
  const tbody = $('perfTbody');
  if (tbody) tbody.innerHTML = trades.map((t, i) => _buildPerfRow(t, i)).join('') ||
    '<tr><td colspan="11"><div class="empty">No trades yet</div></td></tr>';
  _updatePerfSortArrows();
}

function _updatePerfSortArrows() {
  const hdr = $('perfTableHeader');
  if (!hdr) return;
  hdr.querySelectorAll('[data-sort]').forEach(th => {
    const arrow = th.querySelector('.perf-sort-arrow');
    if (!arrow) return;
    if (th.dataset.sort === window._perfSortCol) {
      arrow.textContent = window._perfSortDir === 1 ? ' ▲' : ' ▼';
      th.style.color = 'var(--blue)';
    } else {
      arrow.textContent = ''; th.style.color = '';
    }
  });
}

function _initPerfSortHeaders() {
  const hdr = $('perfTableHeader');
  if (!hdr || hdr.dataset.sortBound) return;
  hdr.dataset.sortBound = '1';
  hdr.querySelectorAll('[data-sort]').forEach(th => {
    th.addEventListener('click', () => _sortPerfTable(th.dataset.sort));
  });
}

async function _loadSwingsInPerformance() {
  const el     = $('swingsContent');
  const badge  = $('swingsBadge');
  if (!el) return;
  try {
    const data  = await getCached('/api/swings/positions', 30000);
    const open  = data?.open  || [];
    const closed= data?.closed|| [];
    if (badge) badge.textContent = `${open.length} open · ${closed.length} recent closed`;

    if (!open.length && !closed.length) {
      el.innerHTML = '<div class="empty">No swing positions yet</div>';
      return;
    }

    const rowStyle = 'padding:10px 12px;border-bottom:1px solid var(--border);display:grid;' +
                     'grid-template-columns:80px 80px 1fr 80px 80px 80px 80px 80px;gap:8px;align-items:center;font-size:11px';
    const hdr = `<div style="${rowStyle};font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;font-weight:600">
      <span>DATE</span><span>TICKER</span><span>CONTRACT</span><span>ENTRY</span>
      <span>TARGET</span><span>STOP</span><span>P&L</span><span>STATUS</span></div>`;

    const openRows = open.map(p => {
      const pnl    = p.realized_pnl || 0;
      const pnlCol = pnl > 0 ? 'var(--green)' : pnl < 0 ? 'var(--red)' : 'var(--sub)';
      const tp1    = p.tp1_hit ? '✅ TP1' : '⏳';
      return `<div style="${rowStyle};background:rgba(0,208,132,0.03)">
        <span style="color:var(--sub)">${(p.entry_date||'').slice(5)}</span>
        <span style="font-weight:700;font-family:JetBrains Mono,monospace">${p.ticker}</span>
        <span style="font-size:9px;color:var(--sub)">${p.contract||'—'}</span>
        <span>$${parseFloat(p.entry_price||0).toFixed(2)}</span>
        <span style="color:var(--green)">$${parseFloat(p.target||0).toFixed(2)}</span>
        <span style="color:var(--red)">$${parseFloat(p.stop||0).toFixed(2)}</span>
        <span style="color:${pnlCol};font-weight:700">${pnl ? (pnl>0?'+':'')+pnl.toFixed(2) : tp1}</span>
        <span style="color:var(--green);font-size:9px;font-weight:700">OPEN</span>
      </div>`;
    }).join('');

    const closedRows = closed.map(p => {
      const pnl    = p.realized_pnl || 0;
      const pnlCol = pnl > 0 ? 'var(--green)' : 'var(--red)';
      const outcome= pnl > 0 ? '✅ WIN' : '❌ LOSS';
      return `<div style="${rowStyle}">
        <span style="color:var(--sub)">${(p.entry_date||'').slice(5)}</span>
        <span style="font-weight:700;font-family:JetBrains Mono,monospace">${p.ticker}</span>
        <span style="font-size:9px;color:var(--sub)">${p.contract||'—'}</span>
        <span>$${parseFloat(p.entry_price||0).toFixed(2)}</span>
        <span style="color:var(--green)">$${parseFloat(p.target||0).toFixed(2)}</span>
        <span style="color:var(--red)">$${parseFloat(p.stop||0).toFixed(2)}</span>
        <span style="color:${pnlCol};font-weight:700">${(pnl>0?'+':'')+pnl.toFixed(2)}</span>
        <span style="font-size:9px;color:${pnlCol}">${outcome}</span>
      </div>`;
    }).join('');

    el.innerHTML = hdr + openRows +
      (closed.length ? `<div style="font-size:8px;letter-spacing:1px;color:var(--sub);
        text-transform:uppercase;padding:8px 12px;margin-top:8px">Recent Closed</div>` + closedRows : '');
  } catch(e) {
    if (el) el.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}

window.filterByDate = function(date) {
  const rows = document.querySelectorAll('#perfTbody tr');
  const btn  = $('perfFilterClear');
  if (!date) {
    rows.forEach(r => r.style.display = '');
    if (btn) btn.style.display = 'none';
    return;
  }
  const label = new Date(date + 'T12:00:00').toLocaleDateString('en-US',
    { month: 'short', day: 'numeric', year: 'numeric' });
  rows.forEach(r => {
    const td = r.querySelector('td');
    r.style.display = (td && td.textContent.includes(label.replace(',', ''))) ? '' : 'none';
  });
  if (btn) btn.style.display = 'inline-block';
};

window.showTradeDetail = function(idx) {
  const t   = (window._perfTrades || [])[idx];
  if (!t) return;
  const modal   = $('tradeDetailModal');
  const content = $('tradeDetailContent');
  if (!modal || !content) return;

  const pnl     = t.pnl;
  const pnlStr  = pnl == null ? '—' : (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2);
  const pnlCol  = pnl == null ? 'var(--sub)' : pnl >= 0 ? 'var(--green)' : 'var(--red)';
  const isWin   = (pnl || 0) > 0;
  const side    = (t.side || '').toUpperCase();
  const held    = t.hold_time || '—';

  content.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <div>
        <div style="font-size:9px;letter-spacing:2px;color:var(--sub);text-transform:uppercase">Trade Details</div>
        <div style="font-size:22px;font-weight:800;font-family:'JetBrains Mono',monospace;margin-top:4px">
          ${t.ticker || '—'}
          <span style="font-size:12px;color:var(--sub);margin-left:8px">${side}</span>
        </div>
      </div>
      <div style="text-align:right">
        <div style="font-size:28px;font-weight:800;font-family:'JetBrains Mono',monospace;color:${pnlCol}">${pnlStr}</div>
        <div style="font-size:10px;color:var(--sub)">${isWin ? '✅ WIN' : pnl == null ? '⏳ OPEN' : '❌ LOSS'}</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
      ${[
        ['Date',         t.date || '—'],
        ['Time',         t.time || '—'],
        ['Entry Price',  t.price ? '$' + parseFloat(t.price).toFixed(2) : '—'],
        ['Exit Price',   t.exit_price ? '$' + parseFloat(t.exit_price).toFixed(2) : '—'],
        ['Size',         t.qty ? t.qty + ' shares' : '—'],
        ['Strategy',     t.strategy || (side === 'BUY' ? '0DTE Buy Call' : side === 'SHORT' ? '0DTE Buy Put' : 'ARKA Swing')],
        ['Stop Loss',    t.stop ? '$' + parseFloat(t.stop).toFixed(2) : '—'],
        ['Target',       t.target ? '$' + parseFloat(t.target).toFixed(2) : '—'],
        ['Conviction',   t.conviction ? t.conviction + '/100' : '—'],
        ['Time Held',    held],
      ].map(([l, v]) => `
        <div style="background:var(--bg2);border-radius:6px;padding:10px 12px">
          <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:4px">${l}</div>
          <div style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600">${v}</div>
        </div>`).join('')}
    </div>
    ${t.reasons ? `<div style="background:var(--bg2);border-radius:6px;padding:12px;margin-bottom:12px">
      <div style="font-size:8px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:6px">Entry Rationale</div>
      <div style="font-size:11px;color:var(--text);line-height:1.6">${Array.isArray(t.reasons) ? t.reasons.join(' · ') : t.reasons}</div>
    </div>` : ''}
    <div id="arjunAnalysisSection" style="background:var(--bg2);border:1px solid var(--border2);border-radius:8px;padding:14px;margin-bottom:12px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div style="font-size:8px;letter-spacing:1px;color:var(--purple);text-transform:uppercase;font-weight:700">🤖 Arjun AI Analysis</div>
        <button onclick="_runArjunAnalysis()" id="arjunAnalysisBtn"
          style="font-size:9px;padding:3px 10px;background:var(--purple);color:#fff;border:none;border-radius:4px;cursor:pointer;font-family:'JetBrains Mono',monospace">
          Analyze
        </button>
      </div>
      <div id="arjunAnalysisResult" style="font-size:11px;color:var(--sub);line-height:1.7">
        Click Analyze to get Arjun's assessment of this trade.
      </div>
    </div>
    <button onclick="closeTradeDetail()"
      style="width:100%;padding:10px;background:var(--accent);color:#fff;border:none;
      border-radius:6px;cursor:pointer;font-family:'JetBrains Mono',monospace;font-weight:700">
      Close
    </button>
  `;

  // Store trade data for analysis
  window._currentTradeForAnalysis = t;
  modal.style.display = 'flex';
};

window.closeTradeDetail = function() {
  const modal = $('tradeDetailModal');
  if (modal) modal.style.display = 'none';
};


async function sendChat(mode) {
  const boxId=mode==='app'?'appChatBox':mode==='trade'?'tradeChatBox':'chatBox';
  const msgsId=mode==='app'?'appChatMsgs':mode==='trade'?'tradeChatMsgs':'chatMsgs';
  const input=$(boxId); const msg=(input?.value||'').trim(); if(!msg)return;
  input.value='';
  const msgsEl=$(msgsId);
  msgsEl.innerHTML+=`<div class="cmsg user"><div class="cname">You</div>${msg}</div>`;
  msgsEl.scrollTop=msgsEl.scrollHeight;
  const thinkId='think-'+Date.now();
  msgsEl.innerHTML+=`<div class="cmsg arjun" id="${thinkId}"><div class="cname">Arjun</div><div class="spin"></div></div>`;
  msgsEl.scrollTop=msgsEl.scrollHeight;
  try {
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,mode:mode||'main'})});
    const data=await r.json(); const reply=data.reply||'Sorry, I had trouble responding.';
    const el=document.getElementById(thinkId);
    if(el)el.innerHTML=`<div class="cname">Arjun</div>`+reply.replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>').replace(/\n/g,'<br>');
  } catch {
    const el=document.getElementById(thinkId);
    if(el)el.innerHTML=`<div class="cname">Arjun</div>Connection error — is the backend running?`;
  }
  msgsEl.scrollTop=msgsEl.scrollHeight;
}

window._runArjunAnalysis = async function() {
  const t   = window._currentTradeForAnalysis;
  const btn = document.getElementById('arjunAnalysisBtn');
  const out = document.getElementById('arjunAnalysisResult');
  if (!t || !out) return;

  btn.disabled = true;
  btn.textContent = 'Analyzing…';
  out.innerHTML = '<div style="color:var(--purple)">⏳ Arjun is reviewing this trade…</div>';

  const pnl    = t.pnl == null ? 'open' : (t.pnl >= 0 ? '+$' + t.pnl.toFixed(2) : '-$' + Math.abs(t.pnl).toFixed(2));
  const prompt = `You are Arjun, an elite AI trading analyst for the CHAKRA neural trading system.

Analyze this trade and give a sharp, specific assessment:

TRADE SUMMARY:
- Ticker: ${t.ticker || '—'}
- Side: ${(t.side || '').toUpperCase()}
- Strategy: ${t.strategy || 'ARKA-SCALP'}
- Entry: $${parseFloat(t.price || 0).toFixed(2)}
- Exit: ${t.exit_price ? '$' + parseFloat(t.exit_price).toFixed(2) : 'still open'}
- P&L: ${pnl}
- Size: ${t.qty || '—'} shares/contracts
- Conviction: ${t.conviction || '—'}/100
- Time Held: ${t.hold_time || '—'}
- Stop Loss: ${t.stop ? '$' + parseFloat(t.stop).toFixed(2) : '—'}
- Target: ${t.target ? '$' + parseFloat(t.target).toFixed(2) : '—'}
- Entry Rationale: ${Array.isArray(t.reasons) ? t.reasons.join(', ') : (t.reasons || 'none recorded')}
- Exit Reason: ${t.exit_reason || '—'}

Give a structured analysis with these sections:
1. **Trade Quality** — Was this a good setup? Rate 1-10 and explain why briefly.
2. **What Went Right / Wrong** — Be specific based on the data above.
3. **Risk Management** — Assess stop placement and sizing.
4. **Key Lesson** — One actionable takeaway for future trades.

Be direct, concise, and brutally honest. No fluff. Max 200 words total.`;

  try {
    const resp = await fetch('http://localhost:5001/api/arjun/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'claude-sonnet-4-20250514',
        max_tokens: 1000,
        messages: [{ role: 'user', content: prompt }]
      })
    });
    const data = await resp.json();
    const text = data?.content?.[0]?.text || 'No analysis returned.';

    // Render with basic markdown bold support
    const html = text
      .replace(/\*\*(.*?)\*\*/g, '<span style="color:var(--fg);font-weight:700">$1</span>')
      .replace(/\n\n/g, '</p><p style="margin:6px 0">')
      .replace(/\n/g, '<br>');

    out.innerHTML = `<p style="margin:0">${html}</p>`;
  } catch(e) {
    out.innerHTML = `<span style="color:var(--red)">Error: ${e.message}</span>`;
  }

  btn.disabled = false;
  btn.textContent = 'Re-analyze';
};

async function _enrichIndexCards(tickers) {
  // Index cards built via _buildIndexCard already contain GEX levels inline.
  // This function now only enriches cards that were built from a full ARJUN signal
  // (i.e. _buildGeorgeCard) which may be missing live GEX wall data.
  await new Promise(r => setTimeout(r, 300));
  for (const sym of tickers) {
    try {
      // If the card already has an index-gex-block or built-in GEX levels, skip
      const card = document.querySelector(`.george-card[data-sym="${sym}"]`);
      if (!card) continue;
      if (card.querySelector('.index-gex-block')) continue;

      const gex = await getCached(`/api/options/gex?ticker=${sym}`, 60000).catch(() => null);
      if (!gex) continue;
      const cw     = gex.call_wall || gex.top_call_wall || 0;
      const pw     = gex.put_wall  || gex.top_put_wall  || 0;
      const regime = (gex.regime || '').replace(/_/g,' ');
      const net    = parseFloat(gex.net_gex || 0);
      const regCol = regime.includes('NEG') ? 'var(--red)' : regime.includes('LOW') ? 'var(--blue)' : 'var(--gold)';
      const block  = `<div class="index-gex-block" style="background:var(--bg3);border-radius:5px;padding:8px 10px;margin:6px 0">
        <div style="font-size:7px;letter-spacing:1px;color:var(--sub);text-transform:uppercase;margin-bottom:5px">GEX LEVELS</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px">
          ${cw ? `<span style="font-size:8px;padding:1px 6px;border-radius:3px;background:rgba(0,208,132,0.1);color:var(--green)">CW $${cw.toLocaleString()}</span>` : ''}
          ${pw ? `<span style="font-size:8px;padding:1px 6px;border-radius:3px;background:rgba(255,45,85,0.1);color:var(--red)">PW $${pw.toLocaleString()}</span>` : ''}
          ${regime ? `<span style="font-size:8px;padding:1px 6px;border-radius:3px;color:${regCol};border:1px solid ${regCol}40">${regime}</span>` : ''}
        </div>
        ${net !== 0 ? `<div style="font-size:8px;color:var(--sub)">Net GEX <span style="color:${net>=0?'var(--green)':'var(--red)'}">${net>=0?'+':''}${net.toFixed(2)}B</span></div>` : ''}
      </div>`;

      // Find VWAP section to insert before it, or fallback
      const vwapDiv = Array.from(card.querySelectorAll('div')).find(d => d.textContent.includes('BELOW VWAP') || d.textContent.includes('ABOVE VWAP'));
      if (vwapDiv) {
        vwapDiv.insertAdjacentHTML('beforebegin', block);
      } else {
        card.lastElementChild?.insertAdjacentHTML('beforebegin', block);
      }
    } catch(e) {
      console.warn('Index enrichment failed for', sym, e);
    }
  }
}

// ── ARJUN single-ticker signal refresh ───────────────────────────────────────
async function refreshSignal(ticker) {
  if (!ticker) return;
  const sym = ticker.toUpperCase();
  try {
    document.querySelectorAll(`[data-sym="${sym}"]`).forEach(c => {
      c.style.opacity = '0.5';
      c.style.transition = 'opacity 0.3s';
      c.style.pointerEvents = 'none';
    });
    toast(`⏳ Refreshing ${sym}…`, 60000);

    const r = await fetch(`/api/signals/refresh/${sym}`, { method: 'POST' });
    const d = await r.json();

    if (d.success && d.signal) {
      const s = d.signal;
      const conf = parseFloat(s.confidence || 0).toFixed(0);
      toast(`✅ ${sym} → ${s.signal} ${conf}% (${s.vwap_bias || ''} VWAP, RSI ${s.rsi || ''})`);
      // Clear ALL signal-related cache entries so loadSignals fetches fresh
      if (window._cache) {
        Object.keys(window._cache).forEach(k => {
          if (k.includes('signal') || k.includes('signals')) delete window._cache[k];
        });
      }
      setTimeout(() => loadSignals(), 300);
    } else {
      toast(`⚠️ ${sym}: ${d.error || 'refresh failed'}`, 4000);
      document.querySelectorAll(`[data-sym="${sym}"]`).forEach(c => {
        c.style.opacity = '1';
        c.style.pointerEvents = '';
      });
    }
  } catch(e) {
    toast(`❌ Refresh error: ${e.message}`, 4000);
    document.querySelectorAll(`[data-sym="${sym}"]`).forEach(c => {
      c.style.opacity = '1';
      c.style.pointerEvents = '';
    });
  }
}

// ══════════════════════════════════════════════════════════════
//  ARJUN ACCURACY WIDGET
// ══════════════════════════════════════════════════════════════

async function loadArjunAccuracy() {
  const el = $('arjunAccuracyWidget');
  if (!el) return;
  try {
    const data = await fetchJSON('/api/arjun/accuracy?days=30');
    if (!data || data.total === 0) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    el.innerHTML = _buildAccuracyWidget(data);
  } catch(e) {
    el.style.display = 'none';
  }
}

function _buildAccuracyWidget(d) {
  const wr      = d.win_rate_pct != null ? d.win_rate_pct : (d.win_rate * 100);
  const wrColor = wr >= 60 ? 'var(--green)' : wr >= 50 ? 'var(--yellow, #f7b731)' : '#ff4466';
  const wrBar   = Math.round(wr);
  const bySig   = d.by_signal || {};
  const buyWR   = bySig.BUY  ? Math.round(bySig.BUY.win_rate  * 100) : null;
  const sellWR  = bySig.SELL ? Math.round(bySig.SELL.win_rate * 100) : null;

  // Best/worst tickers
  let tickerHTML = '';
  if (d.best_ticker) {
    const bt = d.by_ticker[d.best_ticker];
    tickerHTML += `<span style="color:var(--green);font-size:10px">▲ ${d.best_ticker} ${Math.round(bt.win_rate*100)}%</span>`;
  }
  if (d.worst_ticker && d.worst_ticker !== d.best_ticker) {
    const wt = d.by_ticker[d.worst_ticker];
    tickerHTML += `<span style="color:#ff4466;font-size:10px;margin-left:12px">▼ ${d.worst_ticker} ${Math.round(wt.win_rate*100)}%</span>`;
  }

  return `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px 16px;
    display:flex;align-items:center;gap:20px;flex-wrap:wrap">
    <div style="font-size:8px;letter-spacing:2px;color:var(--sub);text-transform:uppercase;flex-basis:100%">
      ARJUN ACCURACY — LAST ${d.period_days} DAYS
    </div>
    <div style="flex:0 0 auto">
      <div style="font-size:26px;font-weight:800;color:${wrColor};line-height:1">${wr.toFixed(1)}%</div>
      <div style="font-size:9px;color:var(--sub)">WIN RATE</div>
    </div>
    <div style="flex:0 0 auto">
      <div style="display:flex;gap:6px;align-items:center">
        <span style="color:var(--green);font-weight:700;font-size:13px">${d.wins}W</span>
        <span style="color:var(--sub)">/</span>
        <span style="color:#ff4466;font-weight:700;font-size:13px">${d.losses}L</span>
        <span style="color:var(--sub);font-size:10px">· ${d.total} total</span>
      </div>
      <div style="height:4px;width:100px;background:var(--bg3);border-radius:2px;margin-top:4px">
        <div style="height:4px;width:${wrBar}%;background:${wrColor};border-radius:2px"></div>
      </div>
    </div>
    <div style="flex:0 0 auto;font-size:10px;color:var(--sub)">
      ${buyWR  != null ? `<div>BUY signals: <b style="color:${buyWR>=60?'var(--green)':'#ff4466'}">${buyWR}%</b></div>` : ''}
      ${sellWR != null ? `<div>SELL signals: <b style="color:${sellWR>=60?'var(--green)':'#ff4466'}">${sellWR}%</b></div>` : ''}
    </div>
    <div style="flex:1;text-align:right;font-size:10px">${tickerHTML}</div>
    <button onclick="runArjunFeedback()" style="background:var(--bg3);border:1px solid var(--border);
      border-radius:4px;padding:4px 10px;cursor:pointer;color:var(--sub);font-size:9px;flex:0 0 auto">
      ↺ Score Now
    </button>
  </div>`;
}

async function runArjunFeedback() {
  const el = $('arjunAccuracyWidget');
  if (el) el.innerHTML += '<span style="font-size:10px;color:var(--sub);margin-left:8px">Scoring…</span>';
  try {
    await fetch('/api/arjun/feedback/run', { method: 'POST' });
    await loadArjunAccuracy();
    toast('✅ ARJUN feedback scored', 3000);
  } catch(e) {
    toast(`❌ Feedback error: ${e.message}`, 4000);
  }
}

// ══════════════════════════════════════════════════════════════
//  CHAKRA AGENTIC PIPELINE WIDGET
// ══════════════════════════════════════════════════════════════
async function loadPipelineStatus() {
  const el = $('pipelineStatus');
  if (!el) return;

  try {
    const [status, memory] = await Promise.all([
      fetch('/api/arjun/pipeline/status').then(r => r.json()).catch(() => ({})),
      fetch('/api/arjun/memory/stats').then(r => r.json()).catch(() => ({})),
    ]);

    const lastCycle = status.last_cycle
      ? new Date(status.last_cycle).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'})
      : 'Not run yet';
    const memTotal   = memory.summary?.total || 0;
    const memOutcome = memory.summary?.with_outcomes || 0;
    const regimeCall = (status.regime_call || '—').replace(/_/g,' ');
    const sigsCount  = status.signals_count || 0;
    const placedCount= status.placed_count  || 0;
    const vix        = status.vix ? status.vix.toFixed(1) : '—';
    const pulse      = status.neural_pulse ? status.neural_pulse.toFixed(0) : '—';

    const regimeCol = regimeCall.includes('DIPS') ? 'var(--green)'
                    : regimeCall.includes('POPS') ? 'var(--red)'
                    : regimeCall.includes('MOMENTUM') ? 'var(--gold)'
                    : 'var(--sub)';

    el.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 14px;
        background:var(--bg2);border:1px solid rgba(155,89,182,0.25);border-radius:8px;
        margin-bottom:12px">

        <div style="display:flex;align-items:center;gap:5px">
          <span style="font-size:8px;letter-spacing:1px;color:var(--purple);font-weight:700">🤖 PIPELINE</span>
          <span style="font-size:9px;color:var(--text);font-weight:600">${sigsCount} signals</span>
          ${placedCount ? `<span style="font-size:8px;background:rgba(0,208,132,0.15);color:var(--green);padding:1px 5px;border-radius:3px">${placedCount} placed</span>` : ''}
          <span style="font-size:8px;color:var(--sub)">${lastCycle}</span>
        </div>

        <div style="width:1px;height:14px;background:var(--border)"></div>

        <div style="display:flex;align-items:center;gap:5px">
          <span style="font-size:8px;letter-spacing:1px;color:var(--purple);font-weight:700">🧠 MEMORY</span>
          <span style="font-size:9px;color:var(--text);font-weight:600">${memTotal} signals</span>
          <span style="font-size:8px;color:var(--sub)">${memOutcome} scored</span>
        </div>

        <div style="width:1px;height:14px;background:var(--border)"></div>

        <div style="display:flex;align-items:center;gap:5px">
          <span style="font-size:8px;color:var(--sub)">REGIME</span>
          <span style="font-size:9px;font-weight:700;color:${regimeCol}">${regimeCall}</span>
        </div>

        <div style="display:flex;align-items:center;gap:5px">
          <span style="font-size:8px;color:var(--sub)">VIX</span>
          <span style="font-size:9px;font-family:'JetBrains Mono',monospace;font-weight:700;
            color:${parseFloat(vix)>25?'var(--red)':'var(--text)'}">${vix}</span>
        </div>

        <button onclick="runPipelineNow()"
          style="margin-left:auto;font-size:8px;letter-spacing:0.5px;font-weight:700;
            padding:4px 12px;border-radius:5px;background:rgba(155,89,182,0.2);
            color:var(--purple);border:1px solid rgba(155,89,182,0.4);cursor:pointer">
          ▶ RUN NOW
        </button>
      </div>
    `;
  } catch(e) {
    // silent fail — pipeline widget is non-critical
  }
}

async function runPipelineNow() {
  const btn = document.querySelector('[onclick="runPipelineNow()"]');
  if (btn) { btn.textContent = '⏳ Running…'; btn.disabled = true; }
  toast('🤖 Running CHAKRA pipeline…');
  try {
    const r = await fetch('/api/arjun/pipeline/run', { method: 'POST' });
    const d = await r.json();
    if (d.success) {
      toast(`✅ Pipeline: ${d.signals} signals, ${d.placed} placed — ${d.regime}`);
      setTimeout(() => loadSignals(), 2000);
      setTimeout(() => loadPipelineStatus(), 1500);
    } else {
      toast(`❌ Pipeline error: ${d.error}`, 4000);
    }
  } catch(e) {
    toast(`❌ ${e.message}`, 3000);
  }
  if (btn) { btn.textContent = '▶ RUN NOW'; btn.disabled = false; }
}
