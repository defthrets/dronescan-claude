#!/usr/bin/env bash
# ============================================================
# Drone Detect -- Debian/Ubuntu Setup Script
# Run once as root on the target machine:  sudo bash setup_debian.sh
# Installs dependencies, then launches the interactive wizard.
# ============================================================
set -euo pipefail

RED='\033[0;31m'; AMBER='\033[0;33m'; GREEN='\033[0;32m'; NC='\033[0m'
info() { echo -e "${AMBER}[*] $*${NC}"; }
ok()   { echo -e "${GREEN}[ok] $*${NC}"; }
err()  { echo -e "${RED}[!] $*${NC}"; exit 1; }

[[ $EUID -eq 0 ]] || err "Run as root:  sudo bash setup_debian.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# -- System packages --------------------------------------------------
info "Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    iw wireless-tools aircrack-ng \
    libpcap-dev libpcap0.8 \
    gcc libc-dev \
    usbutils
ok "System packages installed"

# -- Python virtualenv ------------------------------------------------
info "Creating Python virtual environment (.venv)..."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
ok "Python dependencies installed"

# -- Show detected Wi-Fi interfaces -----------------------------------
info "Detected Wi-Fi interfaces:"
iw dev 2>/dev/null | grep -E 'Interface|type' \
    || echo "  (none found -- plug in USB adapter and re-run)"

echo ""
ok "Dependencies ready -- launching interactive setup wizard..."
echo ""

# -- Interactive wizard -----------------------------------------------
.venv/bin/python wizard.py
