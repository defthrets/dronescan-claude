"""
rf_engine/device_table.py
Thread-safe in-memory store for all observed Wi-Fi devices.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional


# ─────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────

@dataclass
class PacketRecord:
    timestamp: float
    rssi: int
    channel: int
    frame_type: str


@dataclass
class DroneDevice:
    mac: str
    vendor: str = "Unknown"
    ssid: Optional[str] = None
    channel: int = 0
    rssi: int = -100
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    packet_count: int = 0
    confidence: float = 0.0
    confidence_label: str = "NONE"
    brand: str = "Unknown"
    is_drone: bool = False

    # Rolling histories (not serialised in full — sliced on export)
    packet_history: Deque[PacketRecord] = field(
        default_factory=lambda: deque(maxlen=100)
    )
    rssi_history: Deque[int] = field(default_factory=lambda: deque(maxlen=60))

    def to_dict(self) -> dict:
        return {
            "mac": self.mac,
            "vendor": self.vendor,
            "ssid": self.ssid,
            "channel": self.channel,
            "rssi": self.rssi,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "packet_count": self.packet_count,
            "confidence": round(self.confidence, 1),
            "confidence_label": self.confidence_label,
            "brand": self.brand,
            "is_drone": self.is_drone,
            # Last 30 RSSI readings for sparkline
            "rssi_history": list(self.rssi_history)[-30:],
        }


# ─────────────────────────────────────────────
# Device table
# ─────────────────────────────────────────────

class DeviceTable:
    """Thread-safe, in-memory device tracking table."""

    def __init__(self, device_timeout: int = 300, history_length: int = 100):
        self._devices: Dict[str, DroneDevice] = {}
        self._lock = threading.RLock()
        self._device_timeout = device_timeout
        self._history_length = history_length

    # ── writes ───────────────────────────────

    def update_device(self, mac: str, **kwargs) -> DroneDevice:
        """
        Upsert a device by MAC address.
        All keyword arguments map to DroneDevice fields.
        """
        with self._lock:
            if mac not in self._devices:
                self._devices[mac] = DroneDevice(mac=mac)

            device = self._devices[mac]
            device.last_seen = time.time()
            device.packet_count += 1

            for key, value in kwargs.items():
                if value is None:
                    continue
                if key == "ssid" and device.ssid and not value:
                    continue  # don't overwrite a known SSID with None
                if hasattr(device, key):
                    setattr(device, key, value)

            # Rolling RSSI history
            rssi = kwargs.get("rssi")
            if rssi and rssi != -100:
                device.rssi_history.append(rssi)

            # Rolling packet history
            record = PacketRecord(
                timestamp=time.time(),
                rssi=kwargs.get("rssi", -100),
                channel=kwargs.get("channel", 0),
                frame_type=kwargs.get("frame_type", "unknown"),
            )
            device.packet_history.append(record)

            return device

    def cleanup_stale_devices(self) -> int:
        """Remove devices not seen within timeout window. Returns count removed."""
        cutoff = time.time() - self._device_timeout
        with self._lock:
            stale = [mac for mac, dev in self._devices.items() if dev.last_seen < cutoff]
            for mac in stale:
                del self._devices[mac]
        return len(stale)

    # ── reads ────────────────────────────────

    def get_device(self, mac: str) -> Optional[DroneDevice]:
        with self._lock:
            return self._devices.get(mac)

    def get_all_devices(self) -> List[DroneDevice]:
        with self._lock:
            return list(self._devices.values())

    def get_drone_devices(self) -> List[DroneDevice]:
        """Return devices classified as probable drones (confidence >= low threshold)."""
        with self._lock:
            return [d for d in self._devices.values() if d.confidence >= 30]

    def get_packet_rate(self, mac: str, window_seconds: float = 5.0) -> float:
        """Packets per second over the last *window_seconds* for a device."""
        with self._lock:
            device = self._devices.get(mac)
            if not device:
                return 0.0
            cutoff = time.time() - window_seconds
            recent = sum(1 for p in device.packet_history if p.timestamp >= cutoff)
            return recent / window_seconds

    def to_json_list(self) -> List[dict]:
        with self._lock:
            return [d.to_dict() for d in self._devices.values()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._devices)
