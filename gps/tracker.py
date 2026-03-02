"""
gps/tracker.py
Tracks observer GPS position and derives coarse drone position estimates
from RSSI trend analysis (approaching / stable / departing).

True direction finding requires a directional antenna sweep — that mode
is stubbed here and designed for future extension.
"""
from __future__ import annotations

import math
import time
import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("drone_detect.tracker")

# Same priority table as the JS frontend (higher = better source)
_SOURCE_PRIORITY: dict = {
    "hardware": 4,
    "browser":  4,
    "wigle":    3,
    "manual":   2,
    "ip":       1,
    "none":     0,
}


@dataclass
class Location:
    lat:       float
    lon:       float
    alt:       float = 0.0
    timestamp: float = 0.0


@dataclass
class DroneEstimate:
    mac:               str
    distance_class:    str           # e.g. "close (<50m)"
    rssi_trend:        str           # "approaching" | "stable" | "departing"
    avg_rssi:          float
    bearing_degrees:   Optional[float] = None  # None until DF mode implemented
    bearing_confidence: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────

def _rssi_to_distance_class(rssi: float) -> str:
    if rssi >= -45:
        return "very close (<10 m)"
    if rssi >= -60:
        return "close (<50 m)"
    if rssi >= -70:
        return "medium (50–200 m)"
    if rssi >= -80:
        return "far (200–500 m)"
    return "distant (>500 m)"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in metres."""
    R = 6_371_000.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)
    a = math.sin(Δφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class LocationTracker:
    """
    Maintains observer GPS history and per-device RSSI history.
    Derives distance class and trend from RSSI deltas.
    """

    def __init__(self, history_len: int = 30):
        self._observer:        Optional[Location]                    = None
        self._observer_source: str                                    = "none"
        self._observer_history: Deque[Location]                      = deque(maxlen=history_len)
        # mac → deque of (timestamp, rssi)
        self._rssi_history: Dict[str, Deque[Tuple[float, int]]]      = {}
        self._estimates:    Dict[str, DroneEstimate]                  = {}

    # ── writes ───────────────────────────────────────────────────────────────

    def update_observer(
        self,
        lat:    float,
        lon:    float,
        alt:    float = 0.0,
        source: str   = "hardware",
    ):
        """
        Update observer position.  Higher-priority sources overwrite lower ones;
        e.g. a real GPS fix will not be replaced by a WiGLE estimate.
        Priority: hardware = browser (4) > wigle (3) > manual (2) > ip (1)
        """
        current_pri = _SOURCE_PRIORITY.get(self._observer_source, 0)
        new_pri     = _SOURCE_PRIORITY.get(source, 0)
        if current_pri > new_pri:
            logger.debug(
                "Observer update from '%s' ignored (current source '%s' has higher priority)",
                source, self._observer_source,
            )
            return
        loc = Location(lat=lat, lon=lon, alt=alt, timestamp=time.time())
        self._observer        = loc
        self._observer_source = source
        self._observer_history.append(loc)

    def update_drone_rssi(self, mac: str, rssi: int):
        if mac not in self._rssi_history:
            self._rssi_history[mac] = deque(maxlen=30)
        self._rssi_history[mac].append((time.time(), rssi))
        self._recalc(mac)

    # ── reads ────────────────────────────────────────────────────────────────

    def get_estimate(self, mac: str) -> Optional[DroneEstimate]:
        return self._estimates.get(mac)

    def get_observer(self) -> Optional[Location]:
        return self._observer

    def get_observer_dict(self) -> Optional[dict]:
        if not self._observer:
            return None
        return {
            "lat":       self._observer.lat,
            "lon":       self._observer.lon,
            "alt":       self._observer.alt,
            "timestamp": self._observer.timestamp,
            "source":    self._observer_source,
        }

    # ── internals ────────────────────────────────────────────────────────────

    def _recalc(self, mac: str):
        history = self._rssi_history.get(mac)
        if not history or len(history) < 3:
            return

        rssi_vals = [r for _, r in history]
        avg_rssi  = sum(rssi_vals) / len(rssi_vals)

        # Trend from last N readings
        recent = rssi_vals[-6:]
        if len(recent) >= 3:
            delta = recent[-1] - recent[0]
            if delta > 2:
                trend = "approaching"
            elif delta < -2:
                trend = "departing"
            else:
                trend = "stable"
        else:
            trend = "unknown"

        self._estimates[mac] = DroneEstimate(
            mac=mac,
            distance_class=_rssi_to_distance_class(avg_rssi),
            rssi_trend=trend,
            avg_rssi=avg_rssi,
        )
