"""
rf_engine/frame_parser.py
Parse raw 802.11 frames (via Scapy) into structured ParsedFrame objects.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("drone_detect.frame_parser")

# ─── 802.11 frame type constants ────────────────────────────────────────────
MGMT = 0
CTRL = 1
DATA = 2

# Management subtypes
BEACON       = 8
PROBE_REQ    = 4
PROBE_RESP   = 5
ASSOC_REQ    = 0
ASSOC_RESP   = 1
REASSOC_REQ  = 2
REASSOC_RESP = 3
AUTH         = 11
DEAUTH       = 12
DISASSOC     = 10


@dataclass
class ParsedFrame:
    timestamp: float
    mac_src: str
    mac_dst: str
    mac_bssid: Optional[str]
    ssid: Optional[str]
    channel: int
    rssi: int
    frame_type: int
    frame_subtype: int
    frame_type_str: str
    seq_num: Optional[int]
    raw_length: int


# ─── Public API ─────────────────────────────────────────────────────────────

def parse_frame(packet) -> Optional[ParsedFrame]:
    """Parse a Scapy packet. Returns None if the packet is not 802.11 or parse fails."""
    try:
        from scapy.layers.dot11 import Dot11
        if not packet.haslayer(Dot11):
            return None

        dot11 = packet[Dot11]

        mac_src   = _norm_mac(dot11.addr2)
        mac_dst   = _norm_mac(dot11.addr1)
        mac_bssid = _norm_mac(dot11.addr3) if dot11.addr3 else None

        frame_type    = int(dot11.type)
        frame_subtype = int(dot11.subtype)
        frame_type_str = _type_str(frame_type, frame_subtype)

        rssi    = _extract_rssi(packet)
        channel = _extract_channel(packet)
        ssid    = _extract_ssid(packet, frame_type, frame_subtype)
        seq_num = (int(dot11.SC) >> 4) if hasattr(dot11, "SC") and dot11.SC is not None else None

        return ParsedFrame(
            timestamp=time.time(),
            mac_src=mac_src,
            mac_dst=mac_dst,
            mac_bssid=mac_bssid,
            ssid=ssid,
            channel=channel,
            rssi=rssi,
            frame_type=frame_type,
            frame_subtype=frame_subtype,
            frame_type_str=frame_type_str,
            seq_num=seq_num,
            raw_length=len(packet),
        )

    except Exception as exc:
        logger.debug("Frame parse error: %s", exc)
        return None


# ─── Helpers ────────────────────────────────────────────────────────────────

def _norm_mac(mac: Optional[str]) -> str:
    if not mac:
        return "00:00:00:00:00:00"
    return mac.upper()


def _extract_rssi(packet) -> int:
    try:
        from scapy.layers.dot11 import RadioTap  # type: ignore
        if packet.haslayer(RadioTap):
            rt = packet[RadioTap]
            if hasattr(rt, "dBm_AntSignal") and rt.dBm_AntSignal is not None:
                return int(rt.dBm_AntSignal)
    except Exception:
        pass
    return -100


def _extract_channel(packet) -> int:
    try:
        from scapy.layers.dot11 import RadioTap  # type: ignore
        if packet.haslayer(RadioTap):
            rt = packet[RadioTap]
            if hasattr(rt, "ChannelFrequency") and rt.ChannelFrequency:
                return freq_to_channel(int(rt.ChannelFrequency))
    except Exception:
        pass
    return 0


def _extract_ssid(packet, frame_type: int, frame_subtype: int) -> Optional[str]:
    if frame_type != MGMT or frame_subtype not in (BEACON, PROBE_REQ, PROBE_RESP):
        return None
    try:
        from scapy.layers.dot11 import Dot11Elt
        elt = packet.getlayer(Dot11Elt)
        while elt:
            if elt.ID == 0:  # SSID element
                raw = elt.info
                if raw:
                    ssid = raw.decode("utf-8", errors="replace").strip()
                    if ssid:
                        return ssid
            if elt.payload:
                elt = elt.payload.getlayer(Dot11Elt)
            else:
                break
    except Exception:
        pass
    return None


def freq_to_channel(freq: int) -> int:
    """Convert frequency (MHz) to 802.11 channel number."""
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2412) // 5 + 1
    if 5180 <= freq <= 5825:
        return (freq - 5000) // 5
    if 5955 <= freq <= 7115:       # Wi-Fi 6E
        return (freq - 5955) // 5 + 1
    return 0


_MGMT_SUBTYPES = {
    0: "Assoc-Req", 1: "Assoc-Resp", 2: "Reassoc-Req", 3: "Reassoc-Resp",
    4: "Probe-Req", 5: "Probe-Resp", 8: "Beacon",
    10: "Disassoc", 11: "Auth", 12: "Deauth",
}
_CTRL_SUBTYPES = {8: "BAR", 9: "BA", 10: "PS-Poll", 11: "RTS", 12: "CTS", 13: "ACK"}
_DATA_SUBTYPES = {0: "Data", 4: "Null", 8: "QoS-Data", 12: "QoS-Null"}
_TYPE_NAMES    = {MGMT: "Mgmt", CTRL: "Ctrl", DATA: "Data"}

_SUBTYPE_MAPS = {MGMT: _MGMT_SUBTYPES, CTRL: _CTRL_SUBTYPES, DATA: _DATA_SUBTYPES}


def _type_str(frame_type: int, frame_subtype: int) -> str:
    type_name    = _TYPE_NAMES.get(frame_type, f"T{frame_type}")
    subtype_name = _SUBTYPE_MAPS.get(frame_type, {}).get(frame_subtype, f"S{frame_subtype}")
    return f"{type_name}/{subtype_name}"
