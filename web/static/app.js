/* ============================================================
   DRONE DETECT — Frontend Application
   WebSocket client, device table, map rendering, sparklines.
   ============================================================ */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────
let devices     = {};          // mac → device object
let ws          = null;
let wsRetries   = 0;
let showDronesOnly = false;
let selectedMac = null;
let map         = null;
let mapMarkers  = {};          // mac → Leaflet marker
let observerMarker = null;
let startTime   = Date.now();
let uiConfig    = {};

// ── Boot ──────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  await loadConfig();
  initMap();
  connectWS();
  setInterval(updateUptime, 1000);
  setInterval(pruneStaleRows, 10_000);
});

// ── Config ────────────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    uiConfig  = await res.json();
    applyTheme(uiConfig.theme || {});
  } catch (e) {
    console.warn('Config load failed', e);
  }
}

function applyTheme(theme) {
  if (theme.primary_color) {
    document.documentElement.style.setProperty('--amber', theme.primary_color);
  }
  const scanlines = document.querySelector('.scanlines');
  if (scanlines && theme.scanlines === false) scanlines.style.display = 'none';
  if (theme.flicker === false) {
    document.querySelectorAll('.flicker').forEach(el => el.style.animation = 'none');
  }
}

// ── WebSocket ─────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url   = `${proto}://${location.host}/ws`;

  setStatus('connecting');

  ws = new WebSocket(url);

  ws.onopen = () => {
    wsRetries = 0;
    setStatus('online');
  };

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }

    switch (msg.type) {
      case 'init':
        handleInit(msg);
        break;
      case 'update':
        handleUpdate(msg);
        break;
      case 'pong':
      case 'keepalive':
        break;
    }
  };

  ws.onclose = () => {
    setStatus('offline');
    const delay = Math.min(30_000, 1_000 * Math.pow(2, wsRetries++));
    setTimeout(connectWS, delay);
  };

  ws.onerror = () => ws.close();

  // Ping every 20s
  setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'ping' }));
    }
  }, 20_000);
}

// ── Message handlers ──────────────────────────────────────────────────────
function handleInit(msg) {
  devices = {};
  if (msg.devices) {
    msg.devices.forEach(d => { devices[d.mac] = d; });
  }
  if (msg.observer) updateObserver(msg.observer);
  renderTable();
  updateStats();
}

function handleUpdate(msg) {
  if (msg.devices) {
    const prev = Object.keys(devices);
    const newMacs = new Set();

    msg.devices.forEach(d => {
      const isNew = !devices[d.mac];
      devices[d.mac] = d;
      newMacs.add(d.mac);

      if (isNew && d.is_drone) {
        triggerAlert(`NEW DRONE DETECTED: ${d.brand || 'Unknown'} — ${d.mac}`);
      }
    });

    // Mark removed devices
    prev.forEach(mac => { if (!newMacs.has(mac)) delete devices[mac]; });
  }

  if (msg.observer) updateObserver(msg.observer);

  renderTable();
  updateStats(msg.stats);
  updateMapMarkers();

  if (selectedMac && devices[selectedMac]) {
    updateDetailPanel(devices[selectedMac]);
  }
}

// ── Table rendering ───────────────────────────────────────────────────────
function renderTable() {
  const tbody = document.getElementById('deviceTableBody');
  let devList  = Object.values(devices);

  if (showDronesOnly) {
    devList = devList.filter(d => d.is_drone || d.confidence >= 30);
  }

  // Sort: highest confidence → most recently seen
  devList.sort((a, b) => b.confidence - a.confidence || b.last_seen - a.last_seen);

  if (devList.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">— NO DEVICES DETECTED —</td></tr>';
    return;
  }

  tbody.innerHTML = devList.map(d => `
    <tr onclick="selectDevice('${escHtml(d.mac)}')"
        class="${selectedMac === d.mac ? 'selected-row' : ''}">
      <td style="color:var(--amber-bright)">${escHtml(d.mac)}</td>
      <td>${escHtml(d.brand || d.vendor || '—')}</td>
      <td title="${escHtml(d.ssid || '')}">${escHtml(truncate(d.ssid || '—', 18))}</td>
      <td>${d.channel || '—'}</td>
      <td>${rssiBar(d.rssi)} <small>${d.rssi} dBm</small></td>
      <td>${confidenceBadge(d.confidence_label, d.confidence)}</td>
      <td style="color:#888">${fmtNum(d.packet_count)}</td>
      <td style="color:#888">${timeAgo(d.last_seen)}</td>
    </tr>
  `).join('');
}

function rssiBar(rssi) {
  const pct  = Math.max(0, Math.min(100, (rssi + 100) * 100 / 70));
  const fill = Math.round(pct / 10);
  const bar  = '█'.repeat(fill) + '░'.repeat(10 - fill);
  let color  = '#39FF14';
  if (pct < 40) color = '#FF8C00';
  if (pct < 20) color = '#FF3C00';
  return `<span class="rssi-bar" style="color:${color}">${bar}</span>`;
}

function confidenceBadge(label, score) {
  const cls = `badge-${label || 'NONE'}`;
  return `<span class="badge ${cls}">${label || 'NONE'} ${Math.round(score || 0)}%</span>`;
}

// ── Device selection & detail panel ──────────────────────────────────────
function selectDevice(mac) {
  selectedMac = mac;
  const d = devices[mac];
  if (!d) return;

  document.querySelectorAll('tbody tr').forEach(r => r.classList.remove('selected-row'));

  const panel = document.getElementById('detailPanel');
  panel.classList.remove('hidden');
  updateDetailPanel(d);

  // Pan map to marker
  const m = mapMarkers[mac];
  if (m) map.panTo(m.getLatLng());
}

function updateDetailPanel(d) {
  document.getElementById('detailMac').textContent   = d.mac;
  document.getElementById('detailBrand').textContent = d.brand || d.vendor || '—';
  document.getElementById('detailSsid').textContent  = d.ssid  || '—';
  document.getElementById('detailChannel').textContent = d.channel || '—';
  document.getElementById('detailConf').textContent  = `${d.confidence_label} (${Math.round(d.confidence)}%)`;
  document.getElementById('detailFirst').textContent = new Date(d.first_seen * 1000).toLocaleTimeString();
  document.getElementById('detailPkts').textContent  = fmtNum(d.packet_count);

  drawSparkline(d.rssi_history || []);
}

function closeDetail() {
  selectedMac = null;
  document.getElementById('detailPanel').classList.add('hidden');
}

// ── Sparkline canvas ──────────────────────────────────────────────────────
function drawSparkline(history) {
  const canvas = document.getElementById('rssiCanvas');
  const ctx    = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;

  ctx.clearRect(0, 0, W, H);

  if (!history || history.length < 2) {
    ctx.fillStyle = '#333';
    ctx.font = '11px monospace';
    ctx.fillText('— INSUFFICIENT DATA —', W / 2 - 70, H / 2 + 4);
    return;
  }

  const min = Math.min(...history) - 5;
  const max = Math.max(...history) + 5;
  const range = max - min || 1;

  const toY = v => H - ((v - min) / range) * (H - 8) - 4;
  const toX = (i) => (i / (history.length - 1)) * W;

  // Grid lines
  ctx.strokeStyle = '#1a1a1a';
  ctx.lineWidth = 1;
  for (let y = 0; y < H; y += 15) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }

  // Fill area
  ctx.beginPath();
  ctx.moveTo(toX(0), H);
  history.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
  ctx.lineTo(toX(history.length - 1), H);
  ctx.closePath();
  ctx.fillStyle = 'rgba(255,140,0,0.12)';
  ctx.fill();

  // Line
  ctx.beginPath();
  history.forEach((v, i) => {
    i === 0 ? ctx.moveTo(toX(i), toY(v)) : ctx.lineTo(toX(i), toY(v));
  });
  ctx.strokeStyle = '#FF8C00';
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // Latest value dot
  const last = history[history.length - 1];
  ctx.beginPath();
  ctx.arc(toX(history.length - 1), toY(last), 3, 0, Math.PI * 2);
  ctx.fillStyle = '#FFB347';
  ctx.fill();

  // Labels
  ctx.fillStyle = '#995200';
  ctx.font = '9px monospace';
  ctx.fillText(`${max} dBm`, 2, 10);
  ctx.fillText(`${min} dBm`, 2, H - 2);
}

// ── Map ───────────────────────────────────────────────────────────────────
function initMap() {
  map = L.map('map', {
    center: [51.505, -0.09],
    zoom: 15,
    zoomControl: true,
    attributionControl: false,
  });

  // Dark tile layer (CartoDB Dark Matter)
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    subdomains: 'abcd',
    maxZoom: 19,
  }).addTo(map);
}

function droneMarkerStyle(label) {
  const colors = { HIGH: '#FF3C00', MEDIUM: '#FFB800', LOW: '#00CED1', NONE: '#555' };
  return colors[label] || '#555';
}

function updateMapMarkers() {
  Object.values(devices).forEach(d => {
    // Only place markers for devices with a valid GPS-derived location.
    // Without DF hardware we don't have coords — show all at observer if GPS active.
    // For now, markers are only placed if the device has lat/lon injected (future DF).
    if (!d.lat || !d.lon) return;

    const color = droneMarkerStyle(d.confidence_label);
    const icon  = L.divIcon({
      html: `<div style="width:14px;height:14px;border-radius:50%;
                         background:${color};border:2px solid #fff;
                         box-shadow:0 0 8px ${color};"></div>`,
      className: '',
      iconSize: [14, 14],
      iconAnchor: [7, 7],
    });

    const popup = `
      <b>${d.mac}</b><br/>
      ${d.brand || d.vendor || '—'}<br/>
      SSID: ${d.ssid || '—'}<br/>
      CH: ${d.channel}  RSSI: ${d.rssi} dBm<br/>
      <span style="color:${color}">${d.confidence_label} — ${Math.round(d.confidence)}%</span>
    `;

    if (mapMarkers[d.mac]) {
      mapMarkers[d.mac].setLatLng([d.lat, d.lon]);
      mapMarkers[d.mac].setPopupContent(popup);
    } else {
      mapMarkers[d.mac] = L.marker([d.lat, d.lon], { icon })
        .addTo(map)
        .bindPopup(popup);
    }
  });

  // Remove markers for gone devices
  Object.keys(mapMarkers).forEach(mac => {
    if (!devices[mac]) {
      map.removeLayer(mapMarkers[mac]);
      delete mapMarkers[mac];
    }
  });
}

function updateObserver(obs) {
  if (!obs || !obs.lat || !obs.lon) return;

  document.getElementById('gpsStatus').textContent = `GPS: ${obs.lat.toFixed(5)}, ${obs.lon.toFixed(5)}`;
  document.getElementById('gpsStatus').classList.add('active');

  const icon = L.divIcon({
    html: `<div style="width:12px;height:12px;border-radius:50%;
                       background:#39FF14;border:2px solid #fff;
                       box-shadow:0 0 10px #39FF14;"></div>`,
    className: '',
    iconSize: [12, 12],
    iconAnchor: [6, 6],
  });

  if (observerMarker) {
    observerMarker.setLatLng([obs.lat, obs.lon]);
  } else {
    observerMarker = L.marker([obs.lat, obs.lon], { icon })
      .addTo(map)
      .bindPopup('<b>OBSERVER</b><br/>Your GPS position');
    map.setView([obs.lat, obs.lon], 15);
  }
}

// ── Stats bar ─────────────────────────────────────────────────────────────
function updateStats(stats) {
  const total  = Object.keys(devices).length;
  const drones = Object.values(devices).filter(d => d.is_drone || d.confidence >= 30).length;

  document.getElementById('statTotal').textContent  = total;
  document.getElementById('statDrones').textContent = drones;
}

function updateUptime() {
  const secs = Math.floor((Date.now() - startTime) / 1000);
  const h = String(Math.floor(secs / 3600)).padStart(2, '0');
  const m = String(Math.floor((secs % 3600) / 60)).padStart(2, '0');
  const s = String(secs % 60).padStart(2, '0');
  document.getElementById('statUptime').textContent = `${h}:${m}:${s}`;
}

// ── Filter toggle ─────────────────────────────────────────────────────────
function filterDrones() {
  showDronesOnly = !showDronesOnly;
  document.getElementById('filterBtn').textContent = showDronesOnly ? 'SHOW ALL' : 'DRONES ONLY';
  renderTable();
}

// ── Export ────────────────────────────────────────────────────────────────
async function exportJSON() {
  try {
    const res  = await fetch('/api/export');
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `drone_report_${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    console.error('Export failed', e);
  }
}

// ── Alert ─────────────────────────────────────────────────────────────────
function triggerAlert(msg) {
  const bar = document.getElementById('alertBar');
  bar.textContent = `⚠ ${msg}`;
  bar.classList.remove('hidden');
  setTimeout(() => bar.classList.add('hidden'), 6000);
}

// ── Status indicator ──────────────────────────────────────────────────────
function setStatus(state) {
  const dot  = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  dot.className = `status-dot ${state}`;
  const labels = { connecting: 'CONNECTING...', online: 'ONLINE', offline: 'RECONNECTING...' };
  text.textContent = labels[state] || state.toUpperCase();
}

// ── Stale cleanup ─────────────────────────────────────────────────────────
function pruneStaleRows() {
  const now = Date.now() / 1000;
  Object.keys(devices).forEach(mac => {
    if (now - devices[mac].last_seen > 320) delete devices[mac];
  });
  renderTable();
}

// ── Utilities ─────────────────────────────────────────────────────────────
function timeAgo(ts) {
  if (!ts) return '—';
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60)  return `${s}s`;
  if (s < 3600) return `${Math.floor(s/60)}m`;
  return `${Math.floor(s/3600)}h`;
}

function truncate(str, n) {
  return str && str.length > n ? str.slice(0, n - 1) + '…' : str;
}

function fmtNum(n) {
  return n?.toLocaleString() ?? '0';
}

function escHtml(s) {
  if (!s) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
