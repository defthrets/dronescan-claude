"""
detection/brand_profiles.py
Brand-specific model identification and contextual metadata.

After OUI + SSID matching produces a brand, this module attempts to narrow
down to a specific model, provides operational context, and is designed to
be easily extended as new drone models appear.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Model-level SSID patterns per brand
# ─────────────────────────────────────────────────────────────────────────────
_MODEL_PATTERNS: dict[str, list[Tuple[re.Pattern, str]]] = {
    "DJI": [
        (re.compile(r"MINI[-_\s]?4",     re.I), "DJI Mini 4 Pro"),
        (re.compile(r"MINI[-_\s]?3",     re.I), "DJI Mini 3 Pro"),
        (re.compile(r"MINI[-_\s]?2",     re.I), "DJI Mini 2"),
        (re.compile(r"MINI[-_\s]?SE",    re.I), "DJI Mini SE"),
        (re.compile(r"MINI",             re.I), "DJI Mavic Mini"),
        (re.compile(r"MAVIC[-_\s]?3",    re.I), "DJI Mavic 3"),
        (re.compile(r"MAVIC[-_\s]?AIR[-_\s]?2S", re.I), "DJI Air 2S"),
        (re.compile(r"MAVIC[-_\s]?AIR[-_\s]?2",  re.I), "DJI Mavic Air 2"),
        (re.compile(r"MAVIC[-_\s]?AIR",  re.I), "DJI Mavic Air"),
        (re.compile(r"MAVIC[-_\s]?PRO",  re.I), "DJI Mavic Pro"),
        (re.compile(r"MAVIC[-_\s]?2",    re.I), "DJI Mavic 2"),
        (re.compile(r"PHANTOM[-_\s]?4",  re.I), "DJI Phantom 4"),
        (re.compile(r"PHANTOM[-_\s]?3",  re.I), "DJI Phantom 3"),
        (re.compile(r"SPARK",            re.I), "DJI Spark"),
        (re.compile(r"FPV",              re.I), "DJI FPV"),
        (re.compile(r"INSPIRE[-_\s]?2",  re.I), "DJI Inspire 2"),
        (re.compile(r"INSPIRE",          re.I), "DJI Inspire"),
        (re.compile(r"MATRICE[-_\s]?300",re.I), "DJI Matrice 300"),
        (re.compile(r"MATRICE",          re.I), "DJI Matrice"),
        (re.compile(r"AGRAS",            re.I), "DJI Agras"),
    ],
    "Autel": [
        (re.compile(r"EVO[-_\s]?MAX",    re.I), "Autel EVO Max 4T"),
        (re.compile(r"EVO[-_\s]?II",     re.I), "Autel EVO II"),
        (re.compile(r"EVO[-_\s]?LITE\+", re.I), "Autel EVO Lite+"),
        (re.compile(r"EVO[-_\s]?LITE",   re.I), "Autel EVO Lite"),
        (re.compile(r"EVO[-_\s]?NANO\+", re.I), "Autel EVO Nano+"),
        (re.compile(r"EVO[-_\s]?NANO",   re.I), "Autel EVO Nano"),
        (re.compile(r"DRAGONFISH",       re.I), "Autel Dragonfish"),
    ],
    "Parrot": [
        (re.compile(r"ANAFI[-_\s]?AI",   re.I), "Parrot ANAFI Ai"),
        (re.compile(r"ANAFI[-_\s]?USA",  re.I), "Parrot ANAFI USA"),
        (re.compile(r"ANAFI[-_\s]?FPV",  re.I), "Parrot ANAFI FPV"),
        (re.compile(r"ANAFI",            re.I), "Parrot ANAFI"),
        (re.compile(r"BEBOP[-_\s]?2",    re.I), "Parrot Bebop 2"),
        (re.compile(r"BEBOP",            re.I), "Parrot Bebop"),
        (re.compile(r"MAMBO",            re.I), "Parrot Mambo"),
        (re.compile(r"SWING",            re.I), "Parrot Swing"),
        (re.compile(r"DISCO",            re.I), "Parrot Disco"),
    ],
    "Skydio": [
        (re.compile(r"X2[-_\s]?D",       re.I), "Skydio X2D"),
        (re.compile(r"X2",               re.I), "Skydio X2"),
        (re.compile(r"2\+",              re.I), "Skydio 2+"),
        (re.compile(r"2",                re.I), "Skydio 2"),
        (re.compile(r"R1",               re.I), "Skydio R1"),
    ],
    "Yuneec": [
        (re.compile(r"TYPHOON[-_\s]?H[-_\s]?PRO", re.I), "Yuneec Typhoon H Pro"),
        (re.compile(r"TYPHOON[-_\s]?H",  re.I), "Yuneec Typhoon H"),
        (re.compile(r"MANTIS[-_\s]?G",   re.I), "Yuneec Mantis G"),
        (re.compile(r"MANTIS[-_\s]?Q",   re.I), "Yuneec Mantis Q"),
        (re.compile(r"H520E",            re.I), "Yuneec H520E"),
        (re.compile(r"H520",             re.I), "Yuneec H520"),
        (re.compile(r"H480",             re.I), "Yuneec H480"),
    ],
}

# Typical operational ranges (km) per brand
_RANGES: dict[str, str] = {
    "DJI":    "Up to 12 km (OcuSync 3.0 / O3+)",
    "Autel":  "Up to 9 km (SkyLink 2.0)",
    "Parrot": "Up to 4 km",
    "Skydio": "Up to 3.5 km",
    "Yuneec": "Up to 1.5 km",
    "Hubsan": "Up to 1 km",
}


class BrandProfiler:
    """Refines brand-level detection to specific models and provides context."""

    def identify_model(self, brand: str, ssid: Optional[str]) -> str:
        """
        Attempt to identify a specific drone model from the SSID string.
        Falls back to brand name if no model pattern matches.
        """
        if not ssid:
            return brand

        for pattern, model_name in _MODEL_PATTERNS.get(brand, []):
            if pattern.search(ssid):
                return model_name

        return brand

    def get_risk_note(self, brand: str, confidence: float) -> str:
        if confidence >= 80:
            return f"Confirmed {brand} — high confidence"
        if confidence >= 60:
            return f"Probable {brand} — recommend monitoring"
        if confidence >= 30:
            return f"Possible drone ({brand}) — low confidence"
        return "Insufficient evidence"

    def get_typical_range(self, brand: str) -> str:
        return _RANGES.get(brand, "Range unknown")
