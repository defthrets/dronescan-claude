/* ============================================================
   DRONE DETECT — Frontend Application
   WebSocket client, device table, map rendering, sparklines,
   browser GPS integration.
   ============================================================ */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────
let devices        = {};          // mac → device object
let ws             = null;
let wsRetries      = 0;
let showDronesOnly = false;
let selectedMac    = null;
let map            = null;
let mapMarkers     = {};          // mac → Leaflet marker
let droneCircles   = {};          // mac → Leaflet circle (RSSI range estimate)
let observerMarker = null;
let observerAccuracyCircle = null;
let startTime      = Date.now();
let uiConfig       = {};
let gpsWatchId     = null;
let observerPos    = null;        // {lat, lon} current observer
let lastStats      = {};

// ── Boot ──────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  await loadConfig();
  initMap();
  connectWS();
  setInterval(updateUptime, 1000);
  setInterval(pruneStaleRows, 10_000);
  // Try browser GPS automatically on load
  tryAutoGPS();
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
      case 'init':   handleInit(msg);   break;
      case 'update': handleUpdate(msg); break;
      case 'pong':
      case 'keepalive': break;
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
  // If server has hardware GPS observer, use it; else keep browser GPS
  if (msg.observer) updateObserver(msg.observer, 'hardware');
  renderTable();
  updateStats();
}

function handleUpdate(msg) {
  if (msg.devices) {
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
    Object.keys(devices).forEach(mac => { if (!newMacs.has(mac)) delete devices[mac]; });
  }

  if (msg.observer) updateObserver(msg.observer, 'hardware');
  if (msg.stats)    lastStats = msg.stats;

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

  // Pan map to drone circle or observer
  const m = mapMarkers[mac];
  if (m) map.panTo(m.getLatLng());
  else if (observerPos) map.panTo([observerPos.lat, observerPos.lon]);
}

function updateDetailPanel(d) {
  document.getElementById('detailMac').textContent     = d.mac;
  document.getElementById('detailBrand').textContent   = d.brand || d.vendor || '—';
  document.getElementById('detailSsid').textContent    = d.ssid  || '—';
  document.getElementById('detailChannel').textContent = d.channel || '—';
  document.getElementById('detailConf').textContent    = `${d.confidence_label} (${Math.round(d.confidence)}%)`;
  document.getElementById('detailFirst').textContent   = new Date(d.first_seen * 1000).toLocaleTimeString();
  document.getElementById('detailPkts').textContent    = fmtNum(d.packet_count);

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

  // Map scale control
  L.control.scale({ imperial: false, metric: true, position: 'bottomright' }).addTo(map);
}

function droneMarkerStyle(label) {
  const colors = { HIGH: '#FF3C00', MEDIUM: '#FFB800', LOW: '#00CED1', NONE: '#555' };
  return colors[label] || '#555';
}

/** Estimate drone range in metres from RSSI using free-space path loss approximation */
function rssiToRangeMetres(rssi) {
  // Rough model: -45 → ~10m, -60 → ~50m, -70 → ~150m, -80 → ~400m, -90 → ~800m
  if (rssi >= -45) return 10;
  if (rssi >= -60) return 50;
  if (rssi >= -70) return 150;
  if (rssi >= -80) return 400;
  if (rssi >= -90) return 800;
  return 1500;
}

function updateMapMarkers() {
  Object.values(devices).forEach(d => {
    const color = droneMarkerStyle(d.confidence_label);

    // ── If device has a real GPS-derived location, place precise marker ──
    if (d.lat && d.lon) {
      const icon = L.divIcon({
        html: `<div style="width:14px;height:14px;border-radius:50%;
                           background:${color};border:2px solid #fff;
                           box-shadow:0 0 8px ${color};"></div>`,
        className: '',
        iconSize: [14, 14],
        iconAnchor: [7, 7],
      });

      const popup = buildDronePopup(d, color);

      if (mapMarkers[d.mac]) {
        mapMarkers[d.mac].setLatLng([d.lat, d.lon]);
        mapMarkers[d.mac].setPopupContent(popup);
      } else {
        mapMarkers[d.mac] = L.marker([d.lat, d.lon], { icon })
          .addTo(map)
          .bindPopup(popup);
      }
      return;
    }

    // ── No GPS on drone — draw RSSI-range circle around observer ──
    if (!observerPos) return;  // need at least observer position

    const rangeM = rssiToRangeMetres(d.rssi || -90);
    const popup  = buildDronePopup(d, color);

    if (droneCircles[d.mac]) {
      droneCircles[d.mac].setRadius(rangeM);
      droneCircles[d.mac].setStyle({ color });
      droneCircles[d.mac].setPopupContent(popup);
    } else {
      droneCircles[d.mac] = L.circle([observerPos.lat, observerPos.lon], {
        radius: rangeM,
        color,
        fillColor: color,
        fillOpacity: 0.06,
        weight: 1.5,
        dashArray: d.confidence_label === 'HIGH' ? null : '6 4',
      }).addTo(map).bindPopup(popup);

      // Pulse icon at observer location for confirmed drones
      if (d.is_drone || d.confidence >= 60) {
        const icon = L.divIcon({
          html: `<div class="drone-pulse" style="--dc:${color}">
                   <div class="drone-dot" style="background:${color}"></div>
                 </div>`,
          className: '',
          iconSize: [20, 20],
          iconAnchor: [10, 10],
        });
        mapMarkers[d.mac] = L.marker([observerPos.lat, observerPos.lon], { icon })
          .addTo(map)
          .bindPopup(popup);
      }
    }
  });

  // Remove markers / circles for gone devices
  Object.keys(mapMarkers).forEach(mac => {
    if (!devices[mac]) {
      map.removeLayer(mapMarkers[mac]);
      delete mapMarkers[mac];
    }
  });
  Object.keys(droneCircles).forEach(mac => {
    if (!devices[mac]) {
      map.removeLayer(droneCircles[mac]);
      delete droneCircles[mac];
    }
  });
}

function buildDronePopup(d, color) {
  const rangeM  = rssiToRangeMetres(d.rssi || -90);
  const ranging = observerPos
    ? `<br/><small style="color:#888">Est. range: ~${rangeM}m (RSSI-based)</small>`
    : '';
  return `
    <b style="color:${color}">${d.mac}</b><br/>
    ${d.brand || d.vendor || '—'}<br/>
    SSID: ${d.ssid || '—'}<br/>
    CH: ${d.channel || '?'} &nbsp; RSSI: ${d.rssi} dBm<br/>
    <span style="color:${color}">${d.confidence_label} — ${Math.round(d.confidence)}%</span>
    ${ranging}
  `;
}

// ── Observer / GPS ─────────────────────────────────────────────────────────
/**
 * Update the observer position on the map.
 * source: 'hardware' | 'browser'
 * accuracy: metres (browser only)
 */
function updateObserver(obs, source = 'hardware', accuracy = null) {
  if (!obs || !obs.lat || !obs.lon) return;

  observerPos = { lat: obs.lat, lon: obs.lon };

  const label   = source === 'browser' ? 'BROWSER GPS' : 'GPS';
  const accText = accuracy ? ` ±${Math.round(accuracy)}m` : '';
  document.getElementById('gpsStatus').textContent =
    `${label}: ${obs.lat.toFixed(5)}, ${obs.lon.toFixed(5)}${accText}`;
  document.getElementById('gpsStatus').classList.add('active');

  const icon = L.divIcon({
    html: `<div style="width:14px;height:14px;border-radius:50%;
                       background:#39FF14;border:2px solid #fff;
                       box-shadow:0 0 10px #39FF14;"></div>`,
    className: '',
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });

  if (observerMarker) {
    observerMarker.setLatLng([obs.lat, obs.lon]);
  } else {
    observerMarker = L.marker([obs.lat, obs.lon], { icon })
      .addTo(map)
      .bindPopup(`<b>OBSERVER</b><br/>${label}<br/>${obs.lat.toFixed(5)}, ${obs.lon.toFixed(5)}${accText}`);
    // First fix — center the map here
    map.setView([obs.lat, obs.lon], 16);
  }

  // Accuracy circle (browser GPS only)
  if (accuracy) {
    if (observerAccuracyCircle) {
      observerAccuracyCircle.setLatLng([obs.lat, obs.lon]);
      observerAccuracyCircle.setRadius(accuracy);
    } else {
      observerAccuracyCircle = L.circle([obs.lat, obs.lon], {
        radius: accuracy,
        color: '#39FF14',
        fillColor: '#39FF14',
        fillOpacity: 0.05,
        weight: 1,
        dashArray: '4 4',
      }).addTo(map);
    }
  }

  // Update drone circles to new observer location
  updateDroneCirclePositions();
}

function updateDroneCirclePositions() {
  if (!observerPos) return;
  Object.keys(droneCircles).forEach(mac => {
    droneCircles[mac].setLatLng([observerPos.lat, observerPos.lon]);
  });
  Object.keys(mapMarkers).forEach(mac => {
    const d = devices[mac];
    if (d && !d.lat && !d.lon) {
      // This marker is an RSSI-proximity marker at observer pos
      mapMarkers[mac].setLatLng([observerPos.lat, observerPos.lon]);
    }
  });
}

// ── Browser GPS ───────────────────────────────────────────────────────────

/** Called automatically on page load — tries to get GPS without a prompt click */
function tryAutoGPS() {
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    pos => onBrowserGPS(pos),
    () => {},  // silent fail — user can click GPS button manually
    { timeout: 5000, maximumAge: 30_000 }
  );
}

/** Called when user clicks the ⊕ GPS button */
function requestBrowserGPS() {
  if (!navigator.geolocation) {
    triggerAlert('Geolocation not supported in this browser');
    return;
  }

  const btn = document.getElementById('gpsBtn');
  if (btn) btn.textContent = '⊕ GPS…';

  if (gpsWatchId !== null) {
    navigator.geolocation.clearWatch(gpsWatchId);
    gpsWatchId = null;
  }

  gpsWatchId = navigator.geolocation.watchPosition(
    pos => {
      onBrowserGPS(pos);
      if (btn) btn.textContent = '⊕ GPS ✓';
    },
    err => {
      console.warn('GPS error:', err.message);
      triggerAlert(`GPS error: ${err.message}`);
      if (btn) btn.textContent = '⊕ GPS ✗';
    },
    { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 }
  );
}

function onBrowserGPS(pos) {
  const { latitude: lat, longitude: lon, altitude, accuracy } = pos.coords;
  const alt = altitude || 0;

  // Push to server so it can track observer position
  fetch('/api/gps/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lat, lon, alt, accuracy, source: 'browser' }),
  }).catch(e => console.warn('GPS push failed:', e));

  // Update map immediately (no waiting for WS roundtrip)
  updateObserver({ lat, lon, alt }, 'browser', accuracy);
}

// ── Stats bar ─────────────────────────────────────────────────────────────
function updateStats(stats) {
  const total  = Object.keys(devices).length;
  const drones = Object.values(devices).filter(d => d.is_drone || d.confidence >= 30).length;

  document.getElementById('statTotal').textContent  = total;
  document.getElementById('statDrones').textContent = drones;

  if (stats) {
    const ppsEl = document.getElementById('statPps');
    const chEl  = document.getElementById('statChannel');
    if (ppsEl) ppsEl.textContent = stats.pps != null ? stats.pps.toFixed(1) : '0';
    if (chEl)  chEl.textContent  = stats.current_channel || '—';
  }
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
  if (s < 60)   return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
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
