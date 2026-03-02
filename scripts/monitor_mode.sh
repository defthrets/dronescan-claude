#!/usr/bin/env bash
# =============================================================================
# monitor_mode.sh — Quick helper to start/stop monitor mode
# =============================================================================
# Usage:
#   sudo bash scripts/monitor_mode.sh start [iface]   # start monitor mode
#   sudo bash scripts/monitor_mode.sh stop  [iface]   # return to managed mode
#   sudo bash scripts/monitor_mode.sh check           # show current state
# =============================================================================

set -euo pipefail

CMD="${1:-check}"
IFACE="${2:-}"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; CYN='\033[0;36m'; RST='\033[0m'

[[ $EUID -eq 0 ]] || { echo -e "${RED}Run as root: sudo bash $0${RST}"; exit 1; }

# Auto-detect physical interface if not given
if [[ -z "$IFACE" ]]; then
    IFACE=$(iw dev 2>/dev/null | awk '/Interface/{print $2}' | grep -v 'mon$' | head -1 || true)
    [[ -n "$IFACE" ]] || { echo -e "${RED}No wireless interface found${RST}"; exit 1; }
fi

case "$CMD" in

    start)
        echo -e "${CYN}Killing interfering processes…${RST}"
        airmon-ng check kill 2>/dev/null || true

        echo -e "${CYN}Starting monitor mode on $IFACE …${RST}"
        airmon-ng start "$IFACE"

        MON=$(iw dev 2>/dev/null | awk '/Interface/{print $2}' \
              | grep -E "^${IFACE}mon$|^${IFACE}$" | head -1 || echo "${IFACE}mon")

        if iw dev "$MON" info 2>/dev/null | grep -q "type monitor"; then
            echo -e "${GRN}✓ Monitor mode active: $MON${RST}"
            echo ""
            echo "  Update config.yaml:  interface: $MON"
            echo "  Then start scanner:  sudo python main.py --config config/config.yaml web"
        else
            echo -e "${YLW}Warning: could not confirm monitor mode on $MON${RST}"
        fi
        ;;

    stop)
        MON_IFACE="${IFACE}mon"
        echo -e "${CYN}Stopping monitor mode on $MON_IFACE …${RST}"
        airmon-ng stop "$MON_IFACE" 2>/dev/null || true

        echo -e "${CYN}Restarting NetworkManager…${RST}"
        systemctl start NetworkManager 2>/dev/null || true
        systemctl start wpa_supplicant 2>/dev/null || true

        echo -e "${GRN}✓ Interface returned to managed mode${RST}"
        ;;

    check)
        echo "── Wireless interfaces ──"
        iw dev 2>/dev/null || echo "(iw not available)"
        echo ""
        echo "── Monitor-mode interfaces ──"
        iw dev 2>/dev/null | awk '/Interface/{iface=$2} /type monitor/{print iface, "→ MONITOR MODE"}' \
            || echo "(none)"
        echo ""
        echo "── Supported monitor-mode interfaces ──"
        for iface in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do
            if iw phy "$(cat /sys/class/net/$iface/phy80211/name 2>/dev/null)" \
               info 2>/dev/null | grep -q "monitor"; then
                echo "  $iface  ✓ supports monitor mode"
            fi
        done 2>/dev/null || true
        ;;

    *)
        echo "Usage: sudo bash $0 [start|stop|check] [interface]"
        exit 1
        ;;
esac
