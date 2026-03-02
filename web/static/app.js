/* ============================================================
   DRONE DETECT — Frontend Application
   GPS: browser → IP-geolocation → click-to-set
   Map: ALL devices shown with RSSI-based positioning
   ============================================================ */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────
let devices        = {};
let ws             = null;
let wsRetries      = 0;
let showDronesOnly = false;
let selectedMac    = null;
let map            = null;
let mapMarkers     = {};        // mac → Leaflet marker
let mapLines       = {};        // mac → Leaflet polyline to observer
let observerMarker = null;
let observerAccuracyCircle = null;
let startTime      = Date.now();
let uiConfig       = {};
let gpsWatchId     = null;
let observerPos    = null;      // {lat, lon}
let gpsSource      = 'none';   // 'hardware' | 'browser' | 'ip' | 'manual'
let settingLocation = false;   // map-click-to-set mode

// ── Boot ──────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  await loadConfig();
  initMap();
  connectWS();
  setInterval(updateUptime, 1000);
  setInterval(pruneStaleRows, 10_000);
  // GPS cascade: browser → IP → prompt user
  await startGPSCascade();
});

// ── Config ────────────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    uiConfig = await r.json();
    applyTheme(uiConfig.theme || {});
  } catch (e) { console.warn('Config load failed', e); }
}

function applyTheme(t) {
  if (t.primary_color) document.documentElement.style.setProperty('--amber', t.primary_color);
  if (t.scanlines === false) {
    const s = document.querySelector('.scanlines');
    if (s) s.style.display = 'none';
  }
  if (t.flicker === false)
    document.querySelectorAll('.flicker').forEach(el => el.style.animation = 'none');
}

// ── GPS Cascade ───────────────────────────────────────────────────────────
/**
 * GPS acquisition strategy:
 *  1. Start watchPosition immediately (high-accuracy, prompts permission).
 *  2. Wait up to 3 s for first fix; meanwhile fall through to IP geoloc
 *     as a temporary city-level placeholder.
 *  3. Once browser GPS arrives it auto-upgrades (watch stays active).
 *
 * HTTPS REQUIRED FOR PHONE GPS OVER LAN:
 *  Mobile browsers block navigator.geolocation on plain http:// when the
 *  host is not localhost.  Restart the server with --ssl and open
 *  https://<your-ip>:8443 on the phone — GPS will then work.
 */
async function startGPSCascade() {
  setGPSStatus('⌛ GPS: requesting permission…', false);

  // Start persistent high-accuracy watch (triggers permission prompt)
  startBrowserGPSWatch();

  // Wait up to 3 s for a browser GPS fix before trying the IP fallback
  await new Promise(resolve => {
    const poll = setInterval(() => {
      if (gpsSource === 'browser' || gpsSource === 'hardware') {
        clearInterval(poll); resolve();
      }
    }, 200);
    setTimeout(() => { clearInterval(poll); resolve(); }, 3000);
  });

  if (gpsSource === 'browser' || gpsSource === 'hardware') return;

  // No GPS yet — use IP geolocation as a temporary placeholder
  await tryIPGeolocation();

  if (gpsSource === 'none') showSetLocationPrompt();
}

/**
 * Start (or restart) a persistent high-accuracy GPS watch.
 * On HTTPS pages  → uses device GPS chip (~3 m accuracy).
 * On plain HTTP   → browser blocks this; we show a --ssl tip.
 */
function startBrowserGPSWatch() {
  if (!navigator.geolocation) {
    setGPSStatus('GPS: not supported by browser', false);
    _showSSLTip();
    return;
  }

  if (gpsWatchId !== null) {
    navigator.geolocation.clearWatch(gpsWatchId);
    gpsWatchId = null;
  }

  gpsWatchId = navigator.geolocation.watchPosition(
    pos => {
      onGPSFix(
        pos.coords.latitude,
        pos.coords.longitude,
        pos.coords.altitude || 0,
        'browser',
        pos.coords.accuracy
      );
    },
    err => {
      console.warn('GPS watch error:', err.message, '(code:', err.code, ')');
      if (err.code === 1) {           // PERMISSION_DENIED
        if (gpsSource === 'none') setGPSStatus('GPS permission denied', false);
        _showSSLTip();
      }
      // code 2 = POSITION_UNAVAILABLE, code 3 = TIMEOUT — keep waiting
    },
    { enableHighAccuracy: true, timeout: 30000, maximumAge: 0 }
  );
}

/** Show --ssl tip when browsing over plain HTTP on a LAN address. */
function _showSSLTip() {
  if (location.protocol !== 'https:' &&
      !['localhost', '127.0.0.1'].includes(location.hostname)) {
    triggerAlert(
      '📍 Exact GPS needs HTTPS — restart the server with --ssl, ' +
      'then open https://' + location.hostname + ':8443 on your phone'
    );
  }
}

async function tryIPGeolocation() {
  try {
    setGPSStatus('Locating via IP… (city-level only)', false);
    const r = await fetch('https://ipapi.co/json/', { signal: AbortSignal.timeout(5000) });
    const d = await r.json();
    if (d.latitude && d.longitude) {
      onGPSFix(d.latitude, d.longitude, 0, 'ip', null);
      return true;
    }
  } catch (e) { /* silent */ }
  return false;
}

function showSetLocationPrompt() {
  setGPSStatus('CLICK MAP TO SET LOCATION', false);
  const el = document.getElementById('gpsStatus');
  el.style.cursor = 'pointer';
  el.onclick = activateClickToSet;
  triggerAlert('GPS unavailable — click anywhere on the map to set your location');
}

function activateClickToSet() {
  settingLocation = true;
  setGPSStatus('CLICK ON MAP…', false);
  map.getContainer().style.cursor = 'crosshair';
  triggerAlert('Click your location on the map');
}

/**
 * Called when a GPS fix arrives from any source.
 * Source priority (higher number wins):
 *   hardware / browser = 4  >  manual = 2  >  ip = 1  >  none = 0
 * This prevents an IP-geoloc result from overwriting a real GPS fix.
 */
const GPS_PRIORITY = { hardware: 4, browser: 4, manual: 2, ip: 1, none: 0 };

function onGPSFix(lat, lon, alt = 0, source = 'browser', accuracy = null) {
  // Don't downgrade from a higher-priority source
  if ((GPS_PRIORITY[gpsSource] || 0) > (GPS_PRIORITY[source] || 0)) return;

  observerPos = { lat, lon };
  gpsSource   = source;

  // Push to server
  fetch('/api/gps/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lat, lon, alt, accuracy, source }),
  }).catch(() => {});

  // Build status label
  const accTxt = accuracy ? ` ±${Math.round(accuracy)}m` : '';
  let statusText, statusActive;
  switch (source) {
    case 'hardware':
    case 'browser':
      statusText   = `GPS ✓  ${lat.toFixed(5)}, ${lon.toFixed(5)}${accTxt}`;
      statusActive = true;
      break;
    case 'ip':
      statusText   = `⚠ CITY APPROX  ${lat.toFixed(3)}, ${lon.toFixed(3)}  — use --ssl for exact GPS`;
      statusActive = false;
      break;
    case 'manual':
      statusText   = `MANUAL  ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
      statusActive = true;
      break;
    default:
      statusText   = `${source.toUpperCase()}  ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
      statusActive = true;
  }
  setGPSStatus(statusText, statusActive);

  // Update observer marker and device positions
  placeObserverMarker(lat, lon, accuracy);
  updateMapMarkers();
}

// ── Map init ──────────────────────────────────────────────────────────────
function initMap() {
  map = L.map('map', {
    center: [51.505, -0.09],
    zoom: 14,
    zoomControl: true,
    attributionControl: false,
  });

  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    subdomains: 'abcd', maxZoom: 19,
  }).addTo(map);

  L.control.scale({ imperial: false, metric: true, position: 'bottomright' }).addTo(map);

  // Click to set location
  map.on('click', e => {
    if (!settingLocation) return;
    settingLocation = false;
    map.getContainer().style.cursor = '';
    onGPSFix(e.latlng.lat, e.latlng.lng, 0, 'manual', null);
  });
}

// ── GPS Button ────────────────────────────────────────────────────────────
async function requestBrowserGPS() {
  const btn = document.getElementById('gpsBtn');
  if (btn) btn.textContent = '⊕ GPS…';

  // (Re-)start the watch — will re-prompt for permission if previously denied
  startBrowserGPSWatch();

  // Wait up to 8 s for a GPS fix
  await new Promise(resolve => {
    const poll = setInterval(() => {
      if (gpsSource === 'browser') { clearInterval(poll); resolve(); }
    }, 200);
    setTimeout(() => { clearInterval(poll); resolve(); }, 8000);
  });

  if (gpsSource !== 'browser') {
    const ipOk = await tryIPGeolocation();
    if (!ipOk) activateClickToSet();
  }

  if (btn) {
    btn.textContent = (gpsSource === 'browser' || gpsSource === 'hardware')
      ? '⊕ GPS ✓' : '⊕ GPS ✗';
  }
}

// ── Observer marker ───────────────────────────────────────────────────────
function placeObserverMarker(lat, lon, accuracy) {
  const icon = L.divIcon({
    html: `<div class="observer-marker"></div>`,
    className: '',
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });

  if (observerMarker) {
    observerMarker.setLatLng([lat, lon]);
    observerMarker.getPopup()?.setContent(observerPopupHtml(lat, lon));
  } else {
    observerMarker = L.marker([lat, lon], { icon, zIndexOffset: 1000 })
      .addTo(map)
      .bindPopup(observerPopupHtml(lat, lon));
    map.setView([lat, lon], 16);
  }

  if (accuracy) {
    if (observerAccuracyCircle) {
      observerAccuracyCircle.setLatLng([lat, lon]).setRadius(accuracy);
    } else {
      observerAccuracyCircle = L.circle([lat, lon], {
        radius: accuracy, color: '#39FF14', fillColor: '#39FF14',
        fillOpacity: 0.04, weight: 1, dashArray: '4 4',
      }).addTo(map);
    }
  }
}

function observerPopupHtml(lat, lon) {
  const src = { hardware: 'Hardware GPS', browser: 'Browser GPS',
                ip: 'IP Geolocation', manual: 'Manual (map click)' }[gpsSource] || gpsSource;
  return `<b style="color:#39FF14">◉ OBSERVER</b><br/>
          <small>${lat.toFixed(6)}, ${lon.toFixed(6)}</small><br/>
          <small style="color:#666">Source: ${src}</small>`;
}

// ── Device map positioning ────────────────────────────────────────────────
/**
 * Derive a stable bearing from a MAC address.
 * Same MAC always returns the same direction so markers don't jump.
 */
function macToBearing(mac) {
  let h = 5381;
  for (const c of mac.replace(/:/g, '')) h = ((h << 5) + h) ^ c.charCodeAt(0);
  return ((h >>> 0) % 360);
}

/** RSSI → estimated distance in metres (free-space path-loss approximation) */
function rssiToMetres(rssi) {
  if (rssi >= -45) return 8;
  if (rssi >= -55) return 20;
  if (rssi >= -65) return 60;
  if (rssi >= -75) return 180;
  if (rssi >= -85) return 450;
  return 900;
}

/**
 * Return [lat, lon] for a device marker.
 * Places the device at RSSI-derived distance, in a consistent direction based on MAC.
 * If no observer pos, cluster around map centre.
 */
function deviceLatLon(d) {
  if (d.lat && d.lon) return [d.lat, d.lon];   // real GPS on device (future)

  const centre = observerPos || { lat: map.getCenter().lat, lon: map.getCenter().lng };
  const bearing  = macToBearing(d.mac) * (Math.PI / 180);
  const distM    = rssiToMetres(d.rssi || -85);
  const distDeg  = distM / 111_320;

  const lat = centre.lat + distDeg * Math.cos(bearing);
  const lon = centre.lon + distDeg * Math.sin(bearing) /
              Math.cos(centre.lat * Math.PI / 180);
  return [lat, lon];
}

// ── Device confidence → colour ─────────────────────────────────────────
const CONF_COLOR = { HIGH: '#FF3C00', MEDIUM: '#FFB800', LOW: '#00CED1', NONE: '#444' };
function confColor(label) { return CONF_COLOR[label] || '#444'; }

// ── Update all device map markers ─────────────────────────────────────────
function updateMapMarkers() {
  const activeMacs = new Set(Object.keys(devices));

  Object.values(devices).forEach(d => {
    const [lat, lon] = deviceLatLon(d);
    const color      = confColor(d.confidence_label);
    const isDrone    = d.is_drone || d.confidence >= 60;
    const popup      = buildDevicePopup(d, color);

    if (mapMarkers[d.mac]) {
      mapMarkers[d.mac].setLatLng([lat, lon]);
      mapMarkers[d.mac].setPopupContent(popup);
      mapMarkers[d.mac].setIcon(deviceIcon(d, color));
    } else {
      mapMarkers[d.mac] = L.marker([lat, lon], {
        icon: deviceIcon(d, color),
        zIndexOffset: isDrone ? 500 : 0,
      }).addTo(map).bindPopup(popup);
    }

    // Line from observer to device (only when observer pos known)
    if (observerPos) {
      const pts = [[observerPos.lat, observerPos.lon], [lat, lon]];
      if (mapLines[d.mac]) {
        mapLines[d.mac].setLatLngs(pts);
      } else {
        mapLines[d.mac] = L.polyline(pts, {
          color, weight: 1, opacity: 0.3, dashArray: '4 4',
        }).addTo(map);
      }
    }
  });

  // Remove stale markers and lines
  Object.keys(mapMarkers).forEach(mac => {
    if (!activeMacs.has(mac)) {
      map.removeLayer(mapMarkers[mac]);
      delete mapMarkers[mac];
    }
  });
  Object.keys(mapLines).forEach(mac => {
    if (!activeMacs.has(mac)) {
      map.removeLayer(mapLines[mac]);
      delete mapLines[mac];
    }
  });
}

function deviceIcon(d, color) {
  const isDrone = d.is_drone || d.confidence >= 60;
  if (isDrone) {
    return L.divIcon({
      html: `<div class="drone-pulse" style="--dc:${color}">
               <div class="drone-dot" style="background:${color}"></div>
             </div>`,
      className: '', iconSize: [20, 20], iconAnchor: [10, 10],
    });
  }
  return L.divIcon({
    html: `<div style="width:10px;height:10px;border-radius:50%;
                       background:${color};border:1px solid rgba(255,255,255,0.4);
                       box-shadow:0 0 4px ${color};"></div>`,
    className: '', iconSize: [10, 10], iconAnchor: [5, 5],
  });
}

function buildDevicePopup(d, color) {
  const dist   = rssiToMetres(d.rssi || -85);
  const band   = d.band || '?';
  const frames = (d.frame_types || []).join(', ') || '—';
  const age    = timeAgo(d.last_seen);
  const isDrone = d.is_drone || d.confidence >= 60;
  const droneTag = isDrone
    ? `<div style="color:${color};font-weight:bold;margin-bottom:4px">
         ✦ ${d.confidence_label} CONFIDENCE DRONE
       </div>` : '';

  return `
    <div style="font-family:monospace;font-size:11px;min-width:200px">
      ${droneTag}
      <table style="border-collapse:collapse;width:100%">
        <tr><td style="color:#666;padding:1px 6px 1px 0">MAC</td>
            <td style="color:${color}">${d.mac}</td></tr>
        <tr><td style="color:#666;padding:1px 6px 1px 0">OUI</td>
            <td>${d.oui || '—'}</td></tr>
        <tr><td style="color:#666;padding:1px 6px 1px 0">VENDOR</td>
            <td>${d.vendor || '—'}</td></tr>
        <tr><td style="color:#666;padding:1px 6px 1px 0">BRAND</td>
            <td style="color:${color}">${d.brand !== 'Unknown' ? d.brand : '—'}</td></tr>
        <tr><td style="color:#666;padding:1px 6px 1px 0">SSID</td>
            <td style="color:#FFB347">${d.ssid || '—'}</td></tr>
        <tr><td style="color:#666;padding:1px 6px 1px 0">CHANNEL</td>
            <td>CH ${d.channel || '?'} &nbsp;<small style="color:#888">${band}</small></td></tr>
        <tr><td style="color:#666;padding:1px 6px 1px 0">RSSI</td>
            <td>${d.rssi} dBm &nbsp;<small style="color:#888">~${dist}m est.</small></td></tr>
        <tr><td style="color:#666;padding:1px 6px 1px 0">PACKETS</td>
            <td>${fmtNum(d.packet_count)} &nbsp;<small style="color:#888">${d.pps || 0} pps</small></td></tr>
        <tr><td style="color:#666;padding:1px 6px 1px 0">FRAMES</td>
            <td style="color:#888;font-size:10px">${frames}</td></tr>
        <tr><td style="color:#666;padding:1px 6px 1px 0">CONFIDENCE</td>
            <td><span style="color:${color}">${d.confidence_label} ${Math.round(d.confidence)}%</span></td></tr>
        <tr><td style="color:#666;padding:1px 6px 1px 0">LAST SEEN</td>
            <td style="color:#888">${age}</td></tr>
      </table>
      <div style="color:#666;font-size:9px;margin-top:4px">
        ⚠ Position estimated from RSSI — no GPS direction-finding
      </div>
    </div>`;
}

// ── WebSocket ─────────────────────────────────────────────────────────────
function connectWS() {
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
  setStatus('connecting');
  ws = new WebSocket(url);

  ws.onopen  = () => { wsRetries = 0; setStatus('online'); };
  ws.onclose = () => {
    setStatus('offline');
    setTimeout(connectWS, Math.min(30_000, 1_000 * 2 ** wsRetries++));
  };
  ws.onerror = () => ws.close();

  ws.onmessage = evt => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }
    switch (msg.type) {
      case 'init':      handleInit(msg);   break;
      case 'update':    handleUpdate(msg); break;
    }
  };

  setInterval(() => {
    if (ws?.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({ type: 'ping' }));
  }, 20_000);
}

function handleInit(msg) {
  devices = {};
  (msg.devices || []).forEach(d => { devices[d.mac] = d; });
  if (msg.observer) handleHardwareObserver(msg.observer);
  renderTable();
  updateStats();
  updateMapMarkers();
}

function handleUpdate(msg) {
  const newMacs = new Set();
  (msg.devices || []).forEach(d => {
    const isNew = !devices[d.mac];
    devices[d.mac] = d;
    newMacs.add(d.mac);
    if (isNew && (d.is_drone || d.confidence >= 60))
      triggerAlert(`✦ DRONE DETECTED: ${d.brand || d.vendor} — ${d.mac}${d.ssid ? ' — ' + d.ssid : ''}`);
  });
  Object.keys(devices).forEach(m => { if (!newMacs.has(m)) delete devices[m]; });

  if (msg.observer) handleHardwareObserver(msg.observer);
  renderTable();
  updateStats(msg.stats);
  updateMapMarkers();

  if (selectedMac && devices[selectedMac]) updateDetailPanel(devices[selectedMac]);
}

/** Hardware GPS from server — overrides IP/manual but not browser GPS. */
function handleHardwareObserver(obs) {
  if (!obs?.lat || !obs?.lon) return;
  // browser GPS (phone) and hardware GPS share the same priority level (4),
  // so onGPSFix's priority guard handles the tie correctly — first-writer wins.
  onGPSFix(obs.lat, obs.lon, obs.alt || 0, 'hardware', null);
}

// ── Table rendering ───────────────────────────────────────────────────────
function renderTable() {
  const tbody  = document.getElementById('deviceTableBody');
  let devList  = Object.values(devices);
  if (showDronesOnly) devList = devList.filter(d => d.is_drone || d.confidence >= 30);
  devList.sort((a, b) => b.confidence - a.confidence || b.last_seen - a.last_seen);

  if (!devList.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">— NO DEVICES DETECTED —</td></tr>';
    return;
  }

  tbody.innerHTML = devList.map(d => `
    <tr onclick="selectDevice('${escHtml(d.mac)}')"
        class="${selectedMac === d.mac ? 'selected-row' : ''}">
      <td style="color:var(--amber-bright)">${escHtml(d.mac)}</td>
      <td>${escHtml(d.brand !== 'Unknown' ? d.brand : d.vendor || '—')}</td>
      <td title="${escHtml(d.ssid || '')}">${escHtml(truncate(d.ssid || '—', 18))}</td>
      <td>${d.channel || '—'} <small style="color:#555">${d.band || ''}</small></td>
      <td>${rssiBar(d.rssi)} <small>${d.rssi} dBm</small></td>
      <td>${confidenceBadge(d.confidence_label, d.confidence)}</td>
      <td style="color:#888">${fmtNum(d.packet_count)}</td>
      <td style="color:#888">${timeAgo(d.last_seen)}</td>
    </tr>`).join('');
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
  return `<span class="badge badge-${label || 'NONE'}">${label || 'NONE'} ${Math.round(score || 0)}%</span>`;
}

// ── Device detail panel ───────────────────────────────────────────────────
function selectDevice(mac) {
  selectedMac = mac;
  const d = devices[mac];
  if (!d) return;

  document.getElementById('detailPanel').classList.remove('hidden');
  updateDetailPanel(d);

  // Pan map to marker
  if (mapMarkers[mac]) map.panTo(mapMarkers[mac].getLatLng());
  else if (observerPos) map.panTo([observerPos.lat, observerPos.lon]);
}

function updateDetailPanel(d) {
  document.getElementById('detailMac').textContent     = d.mac;
  document.getElementById('detailBrand').textContent   = d.brand || d.vendor || '—';
  document.getElementById('detailSsid').textContent    = d.ssid  || '—';
  document.getElementById('detailChannel').textContent = `${d.channel || '—'} ${d.band || ''}`;
  document.getElementById('detailConf').textContent    = `${d.confidence_label} (${Math.round(d.confidence)}%)`;
  document.getElementById('detailFirst').textContent   = new Date(d.first_seen * 1000).toLocaleTimeString();
  document.getElementById('detailPkts').textContent    = `${fmtNum(d.packet_count)} (${d.pps || 0} pps)`;
  drawSparkline(d.rssi_history || []);
}

function closeDetail() {
  selectedMac = null;
  document.getElementById('detailPanel').classList.add('hidden');
}

// ── RSSI Sparkline ────────────────────────────────────────────────────────
function drawSparkline(history) {
  const canvas = document.getElementById('rssiCanvas');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  if (!history?.length || history.length < 2) {
    ctx.fillStyle = '#333'; ctx.font = '11px monospace';
    ctx.fillText('— INSUFFICIENT DATA —', W / 2 - 70, H / 2 + 4);
    return;
  }

  const min = Math.min(...history) - 5;
  const max = Math.max(...history) + 5;
  const rng = max - min || 1;
  const toY = v => H - ((v - min) / rng) * (H - 8) - 4;
  const toX = i => (i / (history.length - 1)) * W;

  ctx.strokeStyle = '#1a1a1a'; ctx.lineWidth = 1;
  for (let y = 0; y < H; y += 15) { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke(); }

  ctx.beginPath();
  ctx.moveTo(toX(0), H);
  history.forEach((v,i) => ctx.lineTo(toX(i), toY(v)));
  ctx.lineTo(toX(history.length-1), H);
  ctx.closePath();
  ctx.fillStyle = 'rgba(255,140,0,0.12)'; ctx.fill();

  ctx.beginPath();
  history.forEach((v,i) => i === 0 ? ctx.moveTo(toX(i),toY(v)) : ctx.lineTo(toX(i),toY(v)));
  ctx.strokeStyle = '#FF8C00'; ctx.lineWidth = 1.5; ctx.stroke();

  const last = history[history.length-1];
  ctx.beginPath(); ctx.arc(toX(history.length-1), toY(last), 3, 0, Math.PI*2);
  ctx.fillStyle = '#FFB347'; ctx.fill();

  ctx.fillStyle = '#995200'; ctx.font = '9px monospace';
  ctx.fillText(`${max} dBm`, 2, 10);
  ctx.fillText(`${min} dBm`, 2, H - 2);
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
  const s = Math.floor((Date.now() - startTime) / 1000);
  const h = String(Math.floor(s / 3600)).padStart(2,'0');
  const m = String(Math.floor((s % 3600) / 60)).padStart(2,'0');
  const sec = String(s % 60).padStart(2,'0');
  document.getElementById('statUptime').textContent = `${h}:${m}:${sec}`;
}

// ── UI controls ───────────────────────────────────────────────────────────
function filterDrones() {
  showDronesOnly = !showDronesOnly;
  document.getElementById('filterBtn').textContent = showDronesOnly ? 'SHOW ALL' : 'DRONES ONLY';
  renderTable();
}

async function exportJSON() {
  try {
    const r = await fetch('/api/export');
    const b = await r.blob();
    const u = URL.createObjectURL(b);
    const a = document.createElement('a');
    a.href = u; a.download = `drone_report_${Date.now()}.json`; a.click();
    URL.revokeObjectURL(u);
  } catch (e) { console.error('Export failed', e); }
}

function setStatus(state) {
  const dot  = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  dot.className = `status-dot ${state}`;
  text.textContent = { connecting:'CONNECTING…', online:'ONLINE', offline:'RECONNECTING…' }[state] || state.toUpperCase();
}

function setGPSStatus(text, active) {
  const el = document.getElementById('gpsStatus');
  el.textContent = text;
  el.classList.toggle('active', active);
}

function triggerAlert(msg) {
  const bar = document.getElementById('alertBar');
  bar.textContent = `⚠ ${msg}`;
  bar.classList.remove('hidden');
  setTimeout(() => bar.classList.add('hidden'), 7000);
}

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
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 60)   return `${s}s`;
  if (s < 3600) return `${Math.floor(s/60)}m`;
  return `${Math.floor(s/3600)}h`;
}
function truncate(str, n) { return str?.length > n ? str.slice(0,n-1)+'…' : str; }
function fmtNum(n) { return n?.toLocaleString() ?? '0'; }
function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
                  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
