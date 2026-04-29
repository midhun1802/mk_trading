/* ═══════════════════════════════════════════════════════════
   CHAKRA — taraka.js
   TARAKA channel management · alert feed · summary
   ═══════════════════════════════════════════════════════════ */

let _tarakaChannels = [];

async function loadTaraka() {
  await Promise.all([loadTarakaChannels(), loadTarakaSummary(), loadTarakaFeed()]);
}

async function loadTarakaChannels() {
  try {
    const data = await get('/api/taraka/channels');
    _tarakaChannels = data.chChannels || [];
    renderChannelChips(); renderChannelTable();
    const active = _tarakaChannels.filter(c => c.active).length;
    $('tStatus').textContent = `${active} of ${_tarakaChannels.length} channels active — TARAKA monitoring`;
    $('tarakaBotBadge').textContent = 'LIVE'; $('tarakaBotBadge').className = 'chip cg';
  } catch {
    $('tStatus').textContent = 'Could not load channel config';
    $('tarakaBotBadge').textContent = 'ERROR'; $('tarakaBotBadge').className = 'chip cr';
  }
}

function renderChannelChips() {
  $('tarakaChannelChips').innerHTML = _tarakaChannels.map(ch =>
    `<span class="chip ${ch.active ? 'cg' : 'cm'}" style="cursor:pointer" onclick="toggleChannel('${ch.id}')">
      ${ch.active ? '●' : '○'} ${ch.name}
    </span>`
  ).join('');
}

function renderChannelTable() {
  if (!_tarakaChannels.length) {
    $('tarakaChannelList').innerHTML = '<div class="empty" style="padding:12px 0">No channels configured</div>';
    return;
  }
  $('tarakaChannelList').innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:11px">
    <thead><tr style="border-bottom:1px solid var(--border)">
      <th style="text-align:left;padding:5px 8px;font-size:9px;color:var(--sub);letter-spacing:1px;font-weight:400">NAME</th>
      <th style="text-align:left;padding:5px 8px;font-size:9px;color:var(--sub);letter-spacing:1px;font-weight:400">CHANNEL ID</th>
      <th style="text-align:center;padding:5px 8px;font-size:9px;color:var(--sub);letter-spacing:1px;font-weight:400">STATUS</th>
      <th style="text-align:center;padding:5px 8px;font-size:9px;color:var(--sub);letter-spacing:1px;font-weight:400">ACTIONS</th>
    </tr></thead>
    <tbody>${_tarakaChannels.map(ch => `
      <tr style="border-bottom:1px solid var(--border)">
        <td style="padding:8px;font-weight:700">${ch.name}</td>
        <td style="padding:8px;color:var(--sub);font-size:10px;font-family:'JetBrains Mono',monospace">${ch.id}</td>
        <td style="padding:8px;text-align:center">
          <span class="chip ${ch.active ? 'cg' : 'cm'}" style="cursor:pointer" onclick="toggleChannel('${ch.id}')">
            ${ch.active ? '● ACTIVE' : '○ PAUSED'}
          </span>
        </td>
        <td style="padding:8px;text-align:center">
          <button onclick="deleteChannel('${ch.id}','${ch.name}')"
            style="padding:2px 8px;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--red);font-size:9px;cursor:pointer;font-family:'JetBrains Mono',monospace">REMOVE</button>
        </td>
      </tr>`).join('')}
    </tbody>
  </table>`;
}

function toggleChannelManager() {
  const form = $('tarakaAddForm'), btn = $('chMgrToggleBtn');
  const open = form.style.display === 'none';
  form.style.display = open ? 'block' : 'none';
  btn.textContent = open ? '✕ CLOSE' : '+ ADD CHANNEL';
  if (open) $('chNewName').focus();
}

async function toggleChannel(channelId) {
  const ch = _tarakaChannels.find(c => c.id === channelId); if (!ch) return;
  ch.active = !ch.active;
  await saveTarakaChannels(); renderChannelChips(); renderChannelTable();
  const active = _tarakaChannels.filter(c => c.active).length;
  $('tStatus').textContent = `${active} of ${_tarakaChannels.length} channels active — changes apply within 60s`;
}

async function addTarakaChannel() {
  const name = $('chNewName').value.trim().toUpperCase();
  const id   = $('chNewId').value.trim().replace(/\D/g, '');
  if (!name) { $('chAddStatus').textContent = '⚠️ Enter a channel name'; $('chAddStatus').style.color = 'var(--gold)'; return; }
  if (!id || id.length < 15) { $('chAddStatus').textContent = '⚠️ Enter a valid Discord channel ID (17-19 digits)'; $('chAddStatus').style.color = 'var(--gold)'; return; }
  if (_tarakaChannels.find(c => c.id === id)) { $('chAddStatus').textContent = '⚠️ Channel ID already exists'; $('chAddStatus').style.color = 'var(--gold)'; return; }
  _tarakaChannels.push({ name, id, active: true });
  const result = await saveTarakaChannels();
  if (result && result.ok) {
    $('chNewName').value = ''; $('chNewId').value = '';
    $('chAddStatus').textContent = `✅ ${name} added — TARAKA will monitor within 60s`;
    $('chAddStatus').style.color = 'var(--green)';
    renderChannelChips(); renderChannelTable();
  } else {
    $('chAddStatus').textContent = '❌ Failed to save — check API';
    $('chAddStatus').style.color = 'var(--red)';
    _tarakaChannels.pop();
  }
}

async function deleteChannel(channelId, channelName) {
  if (!confirm(`Remove "${channelName}" from TARAKA monitoring?`)) return;
  _tarakaChannels = _tarakaChannels.filter(c => c.id !== channelId);
  const result = await saveTarakaChannels();
  if (result && result.ok) { renderChannelChips(); renderChannelTable(); }
}

async function saveTarakaChannels() {
  try {
    return await fetch('/api/taraka/channels', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chChannels: _tarakaChannels }),
    }).then(r => r.json());
  } catch { return { ok: false }; }
}

async function loadTarakaSummary() {
  try {
    const s = await get('/api/taraka/summary');
    if (!s || !Object.keys(s).length) { $('tSum').innerHTML = '<div class="empty">Monitoring…</div>'; return; }
    const alerts = s.alerts || s.messages || [];
    const real = alerts.filter(a => a.mode === 'REAL').length;
    const paper = alerts.filter(a => a.mode === 'PAPER').length;
    const channels = _tarakaChannels.filter(c => c.active).length;
    $('tSum').innerHTML = `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
      <div><div style="font-size:7px;color:var(--sub);letter-spacing:1px">ALERTS</div><div class="mn" style="font-size:18px;font-weight:700">${alerts.length}</div></div>
      <div><div style="font-size:7px;color:var(--sub);letter-spacing:1px">REAL</div><div class="mn" style="font-size:18px;font-weight:700;color:var(--green)">${real}</div></div>
      <div><div style="font-size:7px;color:var(--sub);letter-spacing:1px">PAPER</div><div class="mn" style="font-size:18px;font-weight:700;color:var(--gold)">${paper}</div></div>
      <div><div style="font-size:7px;color:var(--sub);letter-spacing:1px">CHANNELS</div><div class="mn" style="font-size:18px;font-weight:700">${channels}</div></div>
    </div>`;
  } catch { $('tSum').innerHTML = '<div class="empty">Summary unavailable</div>'; }
}

async function loadTarakaFeed() {
  try {
    const alerts = await get('/api/taraka/alerts') || [];
    $('tAlertCount').textContent = `${alerts.length} today`;
    if (!alerts.length) { $('tFeed').innerHTML = '<div class="empty">No alerts today — watching channels…</div>'; return; }
    $('tFeed').innerHTML = alerts.slice(-30).reverse().map(a => {
      const parsed = a.parsed || {}, score = a.score || 0, mode = a.mode || 'PAPER';
      const isCall = (parsed.direction || '').toUpperCase() === 'CALL';
      const dirColor = isCall ? 'var(--green)' : 'var(--red)';
      const modeChip = mode === 'REAL'
        ? '<span class="chip cg" style="font-size:8px">✅ REAL</span>'
        : '<span class="chip cm" style="font-size:8px">📋 PAPER</span>';
      const scoreColor = score >= 65 ? 'var(--green)' : score >= 45 ? 'var(--gold)' : 'var(--red)';
      return `<div style="padding:10px 0;border-bottom:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">
          <div style="display:flex;gap:6px;align-items:center">
            <span class="chip cb">${a.channel || '—'}</span>
            ${parsed.ticker ? `<span style="font-weight:700;color:${dirColor}">${isCall ? '📈' : '📉'} ${parsed.ticker} ${parsed.direction || ''}</span>` : ''}
            ${modeChip}
          </div>
          <div style="display:flex;gap:6px;align-items:center">
            <span style="font-size:10px;font-weight:700;color:${scoreColor}">${score}/100</span>
            <span style="font-size:9px;color:var(--sub)">${ft(a.timestamp)}</span>
          </div>
        </div>
        <div style="font-size:10px;color:var(--sub);margin-bottom:4px">👤 ${a.analyst || '—'}</div>
        <div style="font-size:11px;line-height:1.5">${(a.raw || '').slice(0, 140)}${(a.raw || '').length > 140 ? '…' : ''}</div>
        ${parsed.entry || parsed.target || parsed.stop ? `<div style="display:flex;gap:8px;margin-top:6px">
          ${parsed.entry  ? `<span class="chip cm" style="font-size:8px">Entry $${parsed.entry}</span>` : ''}
          ${parsed.target ? `<span class="chip cg" style="font-size:8px">Target $${parsed.target}</span>` : ''}
          ${parsed.stop   ? `<span class="chip cr" style="font-size:8px">Stop $${parsed.stop}</span>` : ''}
        </div>` : ''}
      </div>`;
    }).join('');
  } catch { $('tFeed').innerHTML = '<div class="empty">Alert feed unavailable</div>'; }
}
