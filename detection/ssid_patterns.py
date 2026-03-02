"""
detection/ssid_patterns.py
Regex-based SSID matching against known drone network name patterns.
All patterns are compiled once at import time for performance.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple


# (compiled_pattern, brand, confidence_points)
# Points are scaled relative to the ssid_match weight in config (default 30).
_PATTERNS: list[Tuple[re.Pattern, str, int]] = [
    # ── DJI ─────────────────────────────────────────────────────────────────
    (re.compile(r"^DJI[-_\s]",          re.I), "DJI", 30),
    (re.compile(r"^MAVIC[-_\s]",        re.I), "DJI", 28),
    (re.compile(r"^PHANTOM[-_\s]",      re.I), "DJI", 28),
    (re.compile(r"^SPARK[-_\s]?",       re.I), "DJI", 27),
    (re.compile(r"^DJI[-_\s]?MINI",     re.I), "DJI", 30),
    (re.compile(r"^MINI[-_\s]\d",       re.I), "DJI", 25),
    (re.compile(r"^AIR[-_\s]2",         re.I), "DJI", 25),
    (re.compile(r"^DJI[-_\s]?FPV",      re.I), "DJI", 30),
    (re.compile(r"^Inspire[-_\s]",      re.I), "DJI", 28),
    (re.compile(r"^Matrice[-_\s]",      re.I), "DJI", 28),
    (re.compile(r"^Agras[-_\s]",        re.I), "DJI", 28),

    # ── Autel ────────────────────────────────────────────────────────────────
    (re.compile(r"^Autel[-_\s]",        re.I), "Autel", 30),
    (re.compile(r"^EVO[-_\s]",          re.I), "Autel", 25),
    (re.compile(r"^EVO_\d",             re.I), "Autel", 25),
    (re.compile(r"^Dragonfish[-_\s]",   re.I), "Autel", 28),

    # ── Parrot ───────────────────────────────────────────────────────────────
    (re.compile(r"^Parrot[-_\s]",       re.I), "Parrot", 30),
    (re.compile(r"^Bebop[-_\s]",        re.I), "Parrot", 28),
    (re.compile(r"^ANAFI[-_\s]?",       re.I), "Parrot", 28),
    (re.compile(r"^Disco[-_\s]",        re.I), "Parrot", 25),
    (re.compile(r"^Mambo[-_\s]?",       re.I), "Parrot", 25),
    (re.compile(r"^Swing[-_\s]?",       re.I), "Parrot", 22),

    # ── Skydio ───────────────────────────────────────────────────────────────
    (re.compile(r"^SKYDIO[-_\s]",       re.I), "Skydio", 30),
    (re.compile(r"^Skydio[-_\s]",       re.I), "Skydio", 30),
    (re.compile(r"^S2[-_\s]",           re.I), "Skydio", 20),
    (re.compile(r"^X2[-_\s]",           re.I), "Skydio", 20),

    # ── Yuneec ───────────────────────────────────────────────────────────────
    (re.compile(r"^YUNEEC[-_\s]",       re.I), "Yuneec", 28),
    (re.compile(r"^Typhoon[-_\s]",      re.I), "Yuneec", 25),
    (re.compile(r"^Mantis[-_\s]",       re.I), "Yuneec", 25),
    (re.compile(r"^H520[-_\s]?",        re.I), "Yuneec", 25),

    # ── Hubsan ───────────────────────────────────────────────────────────────
    (re.compile(r"^Zino[-_\s]",         re.I), "Hubsan", 22),
    (re.compile(r"^HUBSAN[-_\s]",       re.I), "Hubsan", 25),

    # ── Generic drone keywords (lowest priority, lower points) ────────────────
    (re.compile(r"\bdrone\b",           re.I), "Generic",  10),
    (re.compile(r"\bUAV\b",             re.I), "Generic",  10),
    (re.compile(r"\bUAS\b",             re.I), "Generic",  10),
    (re.compile(r"\bquadcopter\b",      re.I), "Generic",  12),
    (re.compile(r"\bFPV\b",             re.I), "Generic",   8),
    (re.compile(r"\baerial\b",          re.I), "Generic",   6),
]


def match_ssid(ssid: Optional[str]) -> Tuple[str, int]:
    """
    Match *ssid* against known drone patterns.

    Returns (brand, confidence_points) where points are 0-30,
    aligned with the default ssid_match weight.
    """
    if not ssid:
        return "Unknown", 0

    for pattern, brand, points in _PATTERNS:
        if pattern.search(ssid):
            return brand, points

    return "Unknown", 0


def is_drone_ssid(ssid: Optional[str]) -> bool:
    _, pts = match_ssid(ssid)
    return pts > 0
