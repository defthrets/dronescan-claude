#!/usr/bin/env bash
# =============================================================================
# Drone Detection System — Raspberry Pi Setup Script
# =============================================================================
#
# Tested on: Raspberry Pi OS Bookworm / Bullseye (64-bit and 32-bit)
#            Also works on Pi OS Lite (headless)
#
# What this script does:
#   1.  Installs system packages (Python, libpcap, aircrack-ng, git, etc.)
#   2.  Creates a Python virtual-environment at /opt/drone-detect/.venv
#   3.  Installs all Python requirements
#   4.  Detects your Wi-Fi USB dongle and offers to install extra drivers
#       (RTL8812AU for Alfa AWUS036ACH/AWUS036ACHM — the most common choice)
#   5.  Sets up monitor mode (uses airmon-ng)
#   6.  Detects your GPS module (USB or GPIO UART) and configures it
#   7.  Writes a ready-to-use config.yaml under /opt/drone-detect/config/
#   8.  Installs a systemd service so the scanner starts on every boot
#
# Usage:
#   sudo bash setup_pi.sh [--install-dir /opt/drone-detect] [--iface wlan1]
#
# After setup:
#   sudo systemctl start  drone-detect
#   sudo systemctl status drone-detect
#   # Open http://<pi-ip>:8080  or  https://<pi-ip>:8443 (with --ssl)
# =============================================================================

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/drone-detect"
VENV_DIR="$INSTALL_DIR/.venv"
SERVICE_NAME="drone-detect"
REPO_URL="https://github.com/defthrets/dronescan-claude.git"

# Colour helpers
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'

info()    { echo -e "${CYN}[INFO]${RST}  $*"; }
success() { echo -e "${GRN}[OK]${RST}    $*"; }
warn()    { echo -e "${YLW}[WARN]${RST}  $*"; }
error()   { echo -e "${RED}[ERR]${RST}   $*"; exit 1; }
header()  { echo -e "\n${BLD}${CYN}══ $* ══${RST}"; }

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --iface)       FORCED_IFACE="$2"; shift 2 ;;
        *) warn "Unknown arg: $1"; shift ;;
    esac
done

# ── Must run as root ──────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Run this script with sudo: sudo bash setup_pi.sh"

# =============================================================================
header "1/8  System packages"
# =============================================================================
info "Updating package lists…"
apt-get update -qq

info "Installing system dependencies…"
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev \
    libpcap-dev libssl-dev \
    aircrack-ng \
    git curl wget \
    iw wireless-tools network-manager \
    gpsd gpsd-clients \
    build-essential dkms linux-headers-$(uname -r) \
    2>/dev/null || true

success "System packages installed"

# =============================================================================
header "2/8  Fetch / update source code"
# =============================================================================
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Updating existing repo at $INSTALL_DIR …"
    git -C "$INSTALL_DIR" pull --ff-only
else
    if [[ -d "$INSTALL_DIR" && "$(ls -A $INSTALL_DIR)" ]]; then
        warn "$INSTALL_DIR exists and is not empty — assuming files are already in place"
    else
        info "Cloning repo to $INSTALL_DIR …"
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
fi
success "Source code ready at $INSTALL_DIR"

# =============================================================================
header "3/8  Python virtual environment"
# =============================================================================
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment at $VENV_DIR …"
    python3 -m venv "$VENV_DIR"
fi

info "Installing Python requirements…"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
success "Python environment ready"

# =============================================================================
header "4/8  Wi-Fi adapter detection + driver installation"
# =============================================================================
info "Detecting wireless interfaces…"
WIFI_IFACES=$(iw dev 2>/dev/null | awk '/Interface/{print $2}' | head -20)
if [[ -z "$WIFI_IFACES" ]]; then
    warn "No wireless interfaces found — plug in your USB Wi-Fi dongle and rerun"
else
    info "Found wireless interfaces:"
    for iface in $WIFI_IFACES; do
        chipset=$(iw dev "$iface" info 2>/dev/null | awk '/wiphy/{print $2}' || echo "?")
        driver=$(cat /sys/class/net/"$iface"/device/uevent 2>/dev/null | grep DRIVER | cut -d= -f2 || echo "unknown")
        echo "    $iface  (driver: $driver)"
    done
fi

# Detect RTL8812AU / RTL8812BU (Alfa AWUS036ACH / AWUS036ACHM)
if lsusb 2>/dev/null | grep -qiE "0bda:8812|0bda:881[23]|0bda:b812"; then
    echo ""
    warn "RTL8812AU/BU chipset detected (Alfa AWUS036ACH or similar)"
    warn "The in-kernel driver for this chip does NOT support monitor mode."
    read -rp "    Install rtl8812au driver from aircrack-ng? [Y/n] " INSTALL_DRV
    if [[ "${INSTALL_DRV:-Y}" =~ ^[Yy]$ ]]; then
        info "Cloning rtl8812au driver…"
        TMP_DRV=$(mktemp -d)
        git clone --depth=1 https://github.com/aircrack-ng/rtl8812au.git "$TMP_DRV/rtl8812au"
        make -C "$TMP_DRV/rtl8812au" -j"$(nproc)"
        make -C "$TMP_DRV/rtl8812au" install
        rm -rf "$TMP_DRV"
        # Reload driver
        modprobe -r 88XXau 2>/dev/null || true
        modprobe 88XXau 2>/dev/null || true
        success "RTL8812AU driver installed"
    fi
fi

# Detect RT5572 / MT7612U (common cheap adapters that work OOB)
if lsusb 2>/dev/null | grep -qiE "148f:5572|148f:761[02]"; then
    success "MT/RT chipset detected — supports monitor mode out of the box"
fi

# AR9271 (Alfa AWUS036NHA, TP-Link TL-WN722N v1) — fully supported in kernel
if lsusb 2>/dev/null | grep -qi "0cf3:9271"; then
    success "AR9271 chipset detected — excellent monitor-mode support"
fi

# =============================================================================
header "5/8  Monitor mode setup"
# =============================================================================

# Determine which interface to put in monitor mode
if [[ -n "${FORCED_IFACE:-}" ]]; then
    PHYS_IFACE="$FORCED_IFACE"
else
    # Pick the first wireless interface that isn't already wlanXmon
    PHYS_IFACE=$(iw dev 2>/dev/null | awk '/Interface/{print $2}' \
                 | grep -v 'mon$' | head -1 || true)
fi

if [[ -z "$PHYS_IFACE" ]]; then
    warn "Could not auto-detect a wireless interface."
    read -rp "    Enter interface name manually (e.g. wlan1): " PHYS_IFACE
fi

info "Using interface: $PHYS_IFACE"

# Kill processes that fight for the interface
info "Stopping NetworkManager / wpa_supplicant on $PHYS_IFACE …"
airmon-ng check kill 2>/dev/null || true

# Start monitor mode
info "Starting monitor mode…"
airmon-ng start "$PHYS_IFACE" 2>/dev/null || true

# Detect the resulting monitor interface name (could be wlan1mon or wlan1)
MON_IFACE=$(iw dev 2>/dev/null | awk '/Interface/{print $2}' \
            | grep -E "^${PHYS_IFACE}mon$|^${PHYS_IFACE}$" | head -1 || echo "${PHYS_IFACE}mon")

# Verify
if iw dev "$MON_IFACE" info 2>/dev/null | grep -q "type monitor"; then
    success "Monitor mode active on interface: $MON_IFACE"
else
    warn "Could not confirm monitor mode on $MON_IFACE — you may need to set it up manually"
    warn "Run: sudo airmon-ng start $PHYS_IFACE"
    MON_IFACE="${PHYS_IFACE}mon"
fi

# =============================================================================
header "6/8  GPS detection and configuration"
# =============================================================================
GPS_PORT=""
GPS_BAUD=9600

# Look for USB GPS modules
USB_GPS=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -1 || true)
if [[ -n "$USB_GPS" ]]; then
    success "USB GPS detected: $USB_GPS"
    GPS_PORT="$USB_GPS"
# Look for GPIO UART GPS (Pi 4: /dev/ttyAMA0, Pi 3 with overlay: /dev/serial0)
elif [[ -e /dev/serial0 ]]; then
    success "GPIO UART GPS detected: /dev/serial0"
    GPS_PORT="/dev/serial0"
    # Pi UART is typically 9600 for u-blox modules
    GPS_BAUD=9600
    info "NOTE: If the GPS doesn't fix, check that serial console is disabled:"
    info "      sudo raspi-config → Interface Options → Serial Port"
    info "      Disable serial LOGIN shell, but ENABLE serial hardware port"
elif [[ -e /dev/ttyAMA0 ]]; then
    success "GPIO UART detected: /dev/ttyAMA0"
    GPS_PORT="/dev/ttyAMA0"
    GPS_BAUD=9600
fi

if [[ -z "$GPS_PORT" ]]; then
    warn "No GPS module detected — GPS will be disabled in config"
    warn "Connect a GPS module (USB or GPIO) and rerun to enable it"
    GPS_ENABLED="false"
else
    GPS_ENABLED="true"
    info "GPS port: $GPS_PORT  baud: $GPS_BAUD"
    # Enable gpsd (optional — drone-detect reads GPS directly via pyserial)
    if systemctl list-unit-files 2>/dev/null | grep -q gpsd; then
        info "Stopping gpsd (drone-detect reads GPS directly)…"
        systemctl stop gpsd 2>/dev/null || true
        systemctl disable gpsd 2>/dev/null || true
    fi
fi

# =============================================================================
header "7/8  Write config.yaml"
# =============================================================================
PI_CONFIG="$INSTALL_DIR/config/config.yaml"
BACKUP="$INSTALL_DIR/config/config.yaml.bak.$(date +%Y%m%d%H%M%S)"

if [[ -f "$PI_CONFIG" ]]; then
    info "Backing up existing config to $BACKUP"
    cp "$PI_CONFIG" "$BACKUP"
fi

info "Writing Pi config to $PI_CONFIG …"
# Detect local IP for logging
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")

cat > "$PI_CONFIG" <<YAML
# =============================================================================
# Drone Detection System — Raspberry Pi Configuration
# Generated by setup_pi.sh on $(date)
# Pi hostname: $(hostname)   Local IP: $LOCAL_IP
# =============================================================================

# Wi-Fi interface in monitor mode
interface: $MON_IFACE

# Channel hopping
channels:
  bands_2_4ghz: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
  bands_5ghz:   [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112,
                  116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161, 165]
  hop_interval: 0.5       # seconds per channel (reduce to 0.3 with Yagi in sweep mode)
  fixed_channel: null     # set to e.g. 6 to lock channel (useful with Yagi direction-finding)

# Detection confidence weights
confidence_weights:
  oui_match:        40
  ssid_match:       30
  channel_match:    15
  traffic_behavior: 15

# Confidence thresholds
thresholds:
  low:    30
  medium: 60
  high:   80

# Hardware GPS module
gps:
  enabled:   $GPS_ENABLED
  port:      ${GPS_PORT:-/dev/ttyUSB0}
  baud_rate: $GPS_BAUD
  timeout:   5.0

# WiGLE Wi-Fi positioning (optional — fills in position if GPS not fixed yet)
# Get credentials at https://wigle.net/account
wigle:
  enabled:         false
  api_name:        ""
  api_token:       ""
  update_interval: 60
  min_refs:        2

# Web dashboard
web:
  host: "0.0.0.0"
  port: 8080
  ssl_port: 8443          # used with --ssl flag (needed for phone GPS over LAN)
  map_provider: openstreetmap

# PCAP recording (useful for post-analysis)
pcap:
  enabled: false
  output_dir: "/opt/drone-detect/pcap_recordings"
  rotate_size_mb: 100

# Logging
logging:
  level: INFO
  file:  "/opt/drone-detect/drone_detect.log"
  max_bytes: 10485760   # 10 MB
  backup_count: 5

# UI theme
theme:
  style: fallout
  primary_color: "#FF8C00"
  scanlines: true
  flicker: true
  flicker_intensity: 0.02

# Device lifecycle
tracking:
  device_timeout: 300
  history_length: 100
  min_packets_for_traffic_analysis: 10
YAML

success "Config written: $PI_CONFIG"

# =============================================================================
header "8/8  Systemd service"
# =============================================================================
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

info "Writing systemd service to $SERVICE_FILE …"
cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=Drone Detection System
Documentation=https://github.com/defthrets/dronescan-claude
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStartPre=/bin/bash -c 'airmon-ng check kill || true'
ExecStartPre=/bin/bash -c 'airmon-ng start $PHYS_IFACE || true'
ExecStart=$VENV_DIR/bin/python $INSTALL_DIR/main.py \\
          --config $PI_CONFIG \\
          web --port 8080
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=drone-detect

# Give it a moment for the monitor interface to come up after ExecStartPre
ExecStartPost=/bin/sleep 2

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
success "Service installed and enabled on boot"

# =============================================================================
echo ""
echo -e "${BLD}${GRN}╔══════════════════════════════════════════════════════════╗"
echo -e "║           SETUP COMPLETE — DRONE DETECT IS READY         ║"
echo -e "╚══════════════════════════════════════════════════════════╝${RST}"
echo ""
echo -e "  Monitor interface : ${CYN}$MON_IFACE${RST}"
echo -e "  GPS               : ${CYN}${GPS_PORT:-disabled}${RST}"
echo -e "  Install dir       : ${CYN}$INSTALL_DIR${RST}"
echo -e "  Config            : ${CYN}$PI_CONFIG${RST}"
echo -e "  Service           : ${CYN}${SERVICE_NAME}.service${RST}"
echo ""
echo -e "  ${BLD}Start now:${RST}"
echo -e "    sudo systemctl start $SERVICE_NAME"
echo ""
echo -e "  ${BLD}Open in browser (same network):${RST}"
echo -e "    http://$LOCAL_IP:8080"
echo ""
echo -e "  ${BLD}For phone GPS (exact location over LAN):${RST}"
echo -e "    sudo systemctl stop $SERVICE_NAME"
echo -e "    sudo $VENV_DIR/bin/python $INSTALL_DIR/main.py --config $PI_CONFIG web --ssl"
echo -e "    Then open: https://$LOCAL_IP:8443 on your phone"
echo ""
echo -e "  ${BLD}View live logs:${RST}"
echo -e "    sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo -e "  ${YLW}Yagi antenna tip:${RST}"
echo -e "    Set fixed_channel in config.yaml to lock the scanner on one channel,"
echo -e "    then rotate your Yagi while watching the RSSI bars in the UI —"
echo -e "    peak RSSI = drone direction. Set hop_interval: 0.3 for faster"
echo -e "    sweeping, or null for full-band scanning."
echo ""
