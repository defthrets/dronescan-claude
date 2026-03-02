# Drone Detect v1.0

> Real-time Wi-Fi based drone detection and monitoring system.
> **Defensive / monitoring use only.**

---

## Table of Contents

- [How It Works](#how-it-works)
- [Detection Logic](#detection-logic)
- [Requirements](#requirements)
- [Installation](#installation)
- [Monitor Mode Setup](#monitor-mode-setup)
- [Usage](#usage)
- [Web Dashboard](#web-dashboard)
- [Configuration](#configuration)
- [Project Structure](#project-structure)
- [Extending the OUI Database](#extending-the-oui-database)
- [Docker](#docker)

---

## How It Works

```
Wi-Fi Adapter (monitor mode)
        │
        ▼
[ Packet Capture ]  ← separate process (Scapy)
        │  raw bytes via multiprocessing.Queue
        ▼
[ Frame Parser ]   ← 802.11 MAC/SSID/RSSI/channel extraction
        │
        ▼
[ OUI Lookup ]   ← MAC prefix → known drone manufacturer?
[ SSID Match ]   ← regex patterns for known drone SSIDs
[ Channel Score ]  ← drone-typical 2.4/5.8 GHz channels?
[ Traffic Score ]  ← high packet rate = telemetry link?
        │
        ▼
[ Confidence Engine ]  → 0–100%  LOW / MEDIUM / HIGH
        │
        ├──→ DeviceTable (in-memory, thread-safe)
        │
        ├──→ WebSocket broadcast → Browser UI
        └──→ CLI / curses dashboard
```

---

## Detection Logic

Four independent evidence sources contribute to a 0–100 confidence score:

| Component | Max Points | Evidence |
|-----------|-----------|---------|
| **OUI Match** | 40 | MAC prefix belongs to DJI, Autel, Parrot, Skydio, etc. |
| **SSID Match** | 30 | Network name matches known patterns (`DJI_XXXX`, `ANAFI-...`) |
| **Channel Match** | 15 | Device uses drone-typical channels (1/6/11, 36/40/44/48, 149–165) |
| **Traffic Behavior** | 15 | Packet rate ≥ 20 pps (consistent with telemetry) |

**Labels:**
- `NONE` — < 30 pts — not classified
- `LOW` — 30–59 pts — worth monitoring
- `MEDIUM` — 60–79 pts — probable drone
- `HIGH` — ≥ 80 pts — very likely drone

Supported brands: **DJI, Autel, Parrot, Skydio, Yuneec, Hubsan** (extensible via JSON database).

---

## Requirements

- **Linux** (monitor mode is not available on Windows/macOS without patches)
- Python 3.10+
- A Wi-Fi adapter that supports monitor mode (e.g. Alfa AWUS036ACH, TP-Link AC600)
- Root / sudo privileges for raw packet capture
- `iw` and `aircrack-ng` utilities

---

## Installation

```bash
git clone <repo>
cd drone-detect

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## Monitor Mode Setup

```bash
# Find your adapter name
ip link show

# Option A — using iw
sudo ip link set wlan1 down
sudo iw dev wlan1 set type monitor
sudo ip link set wlan1 up

# Option B — using airmon-ng (kills conflicting processes)
sudo airmon-ng check kill
sudo airmon-ng start wlan1
# → creates wlan1mon

# Verify
sudo iw dev wlan1mon info
```

---

## Usage

```bash
# Web dashboard (recommended) — open http://localhost:8080
sudo python main.py web

# Override interface and port
sudo python main.py --interface wlan0mon web --port 9090

# Rich live terminal display
sudo python main.py terminal

# Full-screen curses dashboard (press Q to quit)
sudo python main.py dashboard

# Minimal scrolling scan (script-friendly)
sudo python main.py scan
sudo python main.py scan --drones-only

# Help
python main.py --help
python main.py web --help
```

---

## Web Dashboard

The dashboard is served at `http://<host>:8080` and provides:

- **Live device table** — all detected Wi-Fi devices sorted by confidence
- **RSSI sparkline** — click any row to see the signal history graph
- **Satellite/dark map** — drone markers colour-coded by confidence
  (CartoDB Dark Matter tiles by default, Google Maps optional)
- **WebSocket stream** — updates every 1 second, reconnects automatically
- **Export button** — download full JSON report
- **Filter toggle** — show only drone-classified devices

### Fallout terminal aesthetic
- Black background, amber `#FF8C00` text
- CRT scanline overlay, subtle flicker animation
- Configurable via `config.yaml → theme`

---

## Configuration

Edit `config/config.yaml`:

```yaml
interface: wlan0mon     # monitor-mode adapter name

channels:
  fixed_channel: null   # null = hop; set to e.g. 6 to lock

gps:
  enabled: false        # set true if USB GPS dongle attached
  port: /dev/ttyUSB0

web:
  port: 8080
  map_provider: openstreetmap   # or google (set google_maps_api_key)

pcap:
  enabled: false        # toggle live PCAP recording

theme:
  primary_color: "#FF8C00"
  scanlines: true
  flicker: true
```

---

## Project Structure

```
drone-detect/
├── config/
│   ├── config.yaml           ← main configuration
│   └── oui_database.json     ← drone manufacturer OUI/SSID database
├── rf_engine/
│   ├── capture.py            ← monitor-mode packet capture (multiprocess)
│   ├── frame_parser.py       ← 802.11 frame parsing (Scapy)
│   └── device_table.py       ← thread-safe in-memory device store
├── detection/
│   ├── oui_lookup.py         ← MAC→manufacturer lookup
│   ├── ssid_patterns.py      ← regex SSID matching
│   ├── confidence.py         ← 4-component scoring engine
│   └── brand_profiles.py     ← model identification + metadata
├── gps/
│   ├── nmea_parser.py        ← NMEA sentence parser + async serial reader
│   └── tracker.py            ← observer position + RSSI trend analysis
├── web/
│   ├── app.py                ← FastAPI app (REST + WebSocket)
│   ├── websocket_manager.py  ← broadcast manager
│   └── static/
│       ├── index.html        ← dashboard HTML (Fallout aesthetic)
│       ├── style.css         ← CRT/amber/scanline styling
│       └── app.js            ← WebSocket client, table, map, sparkline
├── cli/
│   ├── display.py            ← Rich live terminal display
│   └── dashboard.py          ← curses full-screen dashboard
├── utils/
│   ├── config_loader.py      ← YAML config loader with defaults
│   └── logging_config.py     ← rotating log setup
├── main.py                   ← Click CLI entry point + orchestrator
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Extending the OUI Database

Edit `config/oui_database.json` to add new drone brands or models:

```json
{
  "manufacturers": {
    "NewBrand": {
      "full_name": "New Brand Inc.",
      "country": "US",
      "confidence_boost": 35,
      "ouis": ["AA:BB:CC"],
      "ssid_patterns": ["^NewBrand[-_].*"],
      "known_channels": [36, 40, 149, 153],
      "frequency_bands": ["5GHz"]
    }
  }
}
```

No code changes needed — the system reloads the database on startup.

---

## Docker

```bash
# Build
docker build -t drone-detect .

# Run (requires host networking + privileged for monitor mode)
docker run --rm --privileged --network=host \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/pcap_recordings:/app/pcap_recordings \
  drone-detect web

# Or with docker-compose
docker-compose up --build
```

> **Note:** The container must run as privileged (or with `NET_RAW` + `NET_ADMIN` capabilities) and use `--network=host` so it can access the physical Wi-Fi adapter.

---

## Advanced / Stubbed Features

| Feature | Status |
|---------|--------|
| Direction finding (manual antenna sweep) | Stub — RSSI trend only |
| Historical replay from PCAP | Stub — recording works, replay coming |
| PCAP recording | ✅ Configurable toggle |
| JSON export | ✅ `/api/export` endpoint + UI button |
| GPS dongle integration | ✅ Full NMEA parsing |
| Multi-adapter support | Planned |
| ML classification layer | Planned |

---

## Legal & Ethics

This tool is intended for **authorized monitoring** only — event security, facility protection, research, and hobbyist situational awareness. Always ensure you have legal authority to monitor RF transmissions in your jurisdiction. Passive Wi-Fi monitoring laws vary by country.
