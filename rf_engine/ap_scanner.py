"""
rf_engine/ap_scanner.py
Dedicated access-point beacon harvester for Wi-Fi-based positioning.

Two complementary data sources feed a shared reference AP table:

  1. In-process beacon harvester
     Every 802.11 Beacon / ProbeResponse frame from the existing
     monitor-mode capture is recorded here (BSSID, SSID, channel, RSSI).
     No extra tools or interfaces needed — runs on the shared interface.

  2. System Wi-Fi scanner (supplemental)
     Periodically calls OS tools to collect APs that the fast-hopping
     monitor channel may have missed.  Tried in order:
       • nmcli  (NetworkManager — common on Linux desktops)
       • iw dev <iface> scan  (kernel wireless, needs a managed iface)
       • airport -s  (macOS)
     Falls back gracefully if none are available or the interface is
     already in monitor mode with no managed alternative.

The combined AP table is consumed by WiGLELocator to estimate the
observer's position via the WiGLE crowdsourced AP database.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

logger = logging.getLogger("drone_detect.ap_scanner")

# Import frame-type constants (avoids magic numbers)
try:
    from rf_engine.frame_parser import MGMT, BEACON, PROBE_RESP
except ImportError:
    MGMT, BEACON, PROBE_RESP = 0, 8, 5


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class APRecord:
    """One observed access point."""
    mac:        str
    ssid:       str
    channel:    int
    rssi:       int
    last_seen:  float = field(default_factory=time.time)
    first_seen: float = field(default_factory=time.time)
    seen_count: int   = 0
    source:     str   = "beacon"    # 'beacon' | 'system'


# ─────────────────────────────────────────────────────────────────────────────
# AP Scanner
# ─────────────────────────────────────────────────────────────────────────────

class APScanner:
    """
    Maintains a reference table of nearby access points for WiGLE positioning.

    Usage
    ─────
    # In the packet-processing pipeline, for every parsed frame:
    ap_scanner.record_frame(frame)

    # Periodically (e.g. every 5 min):
    await ap_scanner.scan_system_aps()

    # For WiGLE lookup:
    candidates = ap_scanner.get_candidates(exclude_macs={...})
    """

    MAX_AGE: float = 600.0   # seconds before a record is considered stale

    def __init__(self) -> None:
        self._aps: Dict[str, APRecord] = {}    # mac.upper() → APRecord

    # ── In-process beacon feed ────────────────────────────────────────────────

    def record_frame(self, frame) -> None:
        """
        Feed a ParsedFrame from the capture pipeline.
        Only Beacon and ProbeResponse frames are processed (both prove
        the sender is an access point broadcasting its BSSID).
        """
        if frame.frame_type != MGMT:
            return
        if frame.frame_subtype not in (BEACON, PROBE_RESP):
            return

        mac = frame.mac_src   # addr2 == BSSID for beacons / probe responses
        if not mac or mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
            return

        self._upsert(mac, frame.ssid, frame.channel, frame.rssi, source="beacon")

    def record_raw(
        self,
        mac:     str,
        ssid:    Optional[str],
        channel: Optional[int],
        rssi:    int,
        source:  str = "beacon",
    ) -> None:
        """Low-level insert — use record_frame() when you have a ParsedFrame."""
        if not mac or mac.upper() in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
            return
        self._upsert(mac.upper(), ssid, channel, rssi, source)

    # ── System Wi-Fi scanner (supplemental) ───────────────────────────────────

    async def scan_system_aps(self) -> int:
        """
        Collect supplemental AP data via OS-level tools.
        Runs the synchronous scan in a thread pool to avoid blocking the loop.
        Returns number of NEW APs added to the table.
        """
        results = await asyncio.get_event_loop().run_in_executor(
            None, self._run_system_scan
        )
        added = 0
        for ap in results:
            mac = ap.get("mac", "").upper()
            if not mac:
                continue
            if mac not in self._aps:
                added += 1
            self._upsert(
                mac,
                ap.get("ssid"),
                ap.get("channel"),
                ap.get("rssi", -80),
                source="system",
            )
        if added:
            logger.info("System Wi-Fi scan added %d new APs (total: %d)", added, len(self._aps))
        return added

    def _run_system_scan(self) -> List[dict]:
        """
        Synchronous system scan — tried in order: nmcli → iw → airport.
        Returns list of dicts with 'mac', 'ssid', 'channel', 'rssi'.
        """
        # ── nmcli (NetworkManager) ─────────────────────────────────────────────
        if shutil.which("nmcli"):
            try:
                out = subprocess.check_output(
                    ["nmcli", "-g", "BSSID,SSID,CHAN,SIGNAL", "-t",
                     "device", "wifi", "list", "--rescan", "yes"],
                    text=True, timeout=12, stderr=subprocess.DEVNULL,
                )
                results = _parse_nmcli(out)
                if results:
                    logger.debug("nmcli: %d APs", len(results))
                    return results
            except Exception as exc:
                logger.debug("nmcli scan: %s", exc)

        # ── iw dev <iface> scan ────────────────────────────────────────────────
        if shutil.which("iw"):
            for iface in _find_managed_ifaces():
                try:
                    out = subprocess.check_output(
                        ["iw", "dev", iface, "scan"],
                        text=True, timeout=15, stderr=subprocess.DEVNULL,
                    )
                    results = _parse_iw_scan(out)
                    if results:
                        logger.debug("iw scan on '%s': %d APs", iface, len(results))
                        return results
                except Exception as exc:
                    logger.debug("iw scan on '%s': %s", iface, exc)

        # ── airport (macOS) ────────────────────────────────────────────────────
        import os
        airport_paths = [
            "/System/Library/PrivateFrameworks/Apple80211.framework"
            "/Versions/Current/Resources/airport",
            shutil.which("airport") or "",
        ]
        for ap_path in airport_paths:
            if ap_path and os.path.exists(ap_path):
                try:
                    out = subprocess.check_output(
                        [ap_path, "-s"],
                        text=True, timeout=10, stderr=subprocess.DEVNULL,
                    )
                    results = _parse_airport(out)
                    if results:
                        logger.debug("airport: %d APs", len(results))
                        return results
                except Exception as exc:
                    logger.debug("airport scan: %s", exc)

        return []

    # ── Query interface ────────────────────────────────────────────────────────

    def get_candidates(
        self,
        max_age:      float = MAX_AGE,
        min_rssi:     float = -92.0,
        exclude_macs: Optional[Set[str]] = None,
    ) -> List[dict]:
        """
        Return AP records suitable for WiGLE lookup.

        exclude_macs : set of MAC strings to skip (e.g. confirmed drones).
        Returns list of dicts with 'mac', 'ssid', 'rssi', 'channel', 'confidence'.
        The 'confidence' key is always 0 so WiGLELocator's filter passes them all.
        """
        now  = time.time()
        excl = {m.upper() for m in (exclude_macs or set())}
        return [
            {
                "mac":        ap.mac,
                "ssid":       ap.ssid,
                "rssi":       ap.rssi,
                "channel":    ap.channel,
                "confidence": 0,          # not a drone candidate
            }
            for ap in self._aps.values()
            if (now - ap.last_seen) <= max_age
            and ap.rssi           >= min_rssi
            and ap.mac            not in excl
        ]

    def get_all(self) -> List[dict]:
        """Return all known APs as dicts (for the /api/aps endpoint)."""
        return [
            {
                "mac":        ap.mac,
                "ssid":       ap.ssid,
                "channel":    ap.channel,
                "rssi":       ap.rssi,
                "seen_count": ap.seen_count,
                "first_seen": ap.first_seen,
                "last_seen":  ap.last_seen,
                "source":     ap.source,
            }
            for ap in sorted(self._aps.values(), key=lambda a: a.rssi, reverse=True)
        ]

    def ap_count(self) -> int:
        return len(self._aps)

    def clear_stale(self) -> int:
        """Remove records older than MAX_AGE. Returns count removed."""
        cutoff = time.time() - self.MAX_AGE
        stale  = [m for m, a in self._aps.items() if a.last_seen < cutoff]
        for m in stale:
            del self._aps[m]
        return len(stale)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _upsert(
        self,
        mac:     str,
        ssid:    Optional[str],
        channel: Optional[int],
        rssi:    int,
        source:  str,
    ) -> None:
        if mac in self._aps:
            ap             = self._aps[mac]
            ap.rssi        = rssi
            ap.last_seen   = time.time()
            ap.seen_count += 1
            if ssid:
                ap.ssid = ssid
            if channel:
                ap.channel = channel
        else:
            self._aps[mac] = APRecord(
                mac=mac,
                ssid=ssid or "",
                channel=channel or 0,
                rssi=rssi,
                source=source,
            )
            if ssid:
                logger.debug(
                    "New AP [%s]: %s  '%s'  CH%d  %d dBm",
                    source, mac, ssid, channel or 0, rssi,
                )


# ─────────────────────────────────────────────────────────────────────────────
# Output parsers for system tools
# ─────────────────────────────────────────────────────────────────────────────

def _safe_int(s: str, default: int = 0) -> int:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return default


def _signal_pct_to_rssi(pct: int) -> int:
    """Convert nmcli 0-100 signal percentage to approximate dBm."""
    return max(-100, -90 + int(pct * 60 / 100))


def _find_managed_ifaces() -> List[str]:
    """List wireless interfaces currently in managed (station) mode."""
    try:
        out = subprocess.check_output(
            ["iw", "dev"], text=True, timeout=3, stderr=subprocess.DEVNULL
        )
        ifaces, current = [], None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Interface"):
                current = line.split()[-1]
            elif line.startswith("type managed") and current:
                ifaces.append(current)
                current = None
        return ifaces
    except Exception:
        return []


def _parse_nmcli(output: str) -> List[dict]:
    """
    Parse: nmcli -g BSSID,SSID,CHAN,SIGNAL -t device wifi list
    Fields are colon-separated; literal colons in BSSID are escaped as \\:
    """
    results = []
    for line in output.splitlines():
        # Unescape BSSID colons first, then split on remaining ':'
        # nmcli escapes field-separator colons in values as '\:'
        # We split conservatively on unescaped colons
        parts = line.replace("\\:", "\x00").split(":")
        parts = [p.replace("\x00", ":") for p in parts]
        if len(parts) < 4:
            continue
        bssid = parts[0].strip().upper()
        ssid  = parts[1].strip()
        chan  = _safe_int(parts[2])
        sig   = _safe_int(parts[3])
        rssi  = _signal_pct_to_rssi(sig)
        if bssid and bssid != "--":
            results.append({"mac": bssid, "ssid": ssid, "channel": chan, "rssi": rssi})
    return results


def _parse_iw_scan(output: str) -> List[dict]:
    """Parse `iw dev <iface> scan` output."""
    results, current = [], {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("BSS "):
            if current.get("mac"):
                results.append(current)
            mac     = line.split()[1].split("(")[0].upper()
            current = {"mac": mac, "ssid": "", "channel": 0, "rssi": -80}
        elif line.startswith("SSID:"):
            current["ssid"] = line[5:].strip()
        elif "signal:" in line.lower():
            try:
                current["rssi"] = int(float(
                    line.lower().split("signal:")[1].split("dbm")[0].strip()
                ))
            except Exception:
                pass
        elif "DS Parameter set: channel" in line:
            try:
                current["channel"] = int(line.split("channel")[-1].strip())
            except Exception:
                pass
    if current.get("mac"):
        results.append(current)
    return results


def _parse_airport(output: str) -> List[dict]:
    """Parse macOS `airport -s` output (space-aligned columns)."""
    results = []
    for line in output.splitlines()[1:]:   # skip header
        parts = line.split()
        if len(parts) < 3:
            continue
        ssid  = parts[0]
        bssid = parts[1].upper()
        try:
            rssi = int(parts[2])
        except ValueError:
            rssi = -80
        chan  = _safe_int(parts[3]) if len(parts) > 3 else 0
        results.append({"mac": bssid, "ssid": ssid, "rssi": rssi, "channel": chan})
    return results
