"""
detection/oui_lookup.py
Fast OUI (Organizationally Unique Identifier) lookup against the drone manufacturer database.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("drone_detect.oui_lookup")


class OUILookup:
    """
    Loads *config/oui_database.json* and provides O(1) MAC-to-brand lookups.

    The OUI is the first 3 octets of a MAC address (XX:XX:XX).
    """

    def __init__(self, db_path: str = "config/oui_database.json"):
        self._oui_map: Dict[str, dict] = {}
        self._raw_db: dict = {}
        self._load(db_path)

    # ── loading ──────────────────────────────────────────────────────────────

    def _load(self, path: str):
        p = Path(path)
        if not p.exists():
            logger.warning("OUI database not found at '%s'", path)
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                self._raw_db = json.load(f)

            for brand, data in self._raw_db.get("manufacturers", {}).items():
                for raw_oui in data.get("ouis", []):
                    key = self._normalise_oui(raw_oui)
                    if key:
                        self._oui_map[key] = {
                            "brand":            brand,
                            "full_name":        data.get("full_name", brand),
                            "confidence_boost": data.get("confidence_boost", 30),
                            "ssid_patterns":    data.get("ssid_patterns", []),
                            "known_channels":   data.get("known_channels", []),
                            "frequency_bands":  data.get("frequency_bands", []),
                            "models":           data.get("models", {}),
                        }

            logger.info(
                "OUI database loaded: %d OUI entries across %d brands",
                len(self._oui_map),
                len(self._raw_db.get("manufacturers", {})),
            )
        except Exception as exc:
            logger.error("Failed to load OUI database: %s", exc)

    # ── public API ───────────────────────────────────────────────────────────

    def lookup(self, mac: str) -> Optional[dict]:
        """Return brand dict for a MAC address, or None if not a known drone OUI."""
        oui = self._oui_from_mac(mac)
        return self._oui_map.get(oui) if oui else None

    def get_vendor_string(self, mac: str) -> str:
        result = self.lookup(mac)
        if result:
            return result.get("full_name", result.get("brand", "Unknown"))
        return "Unknown"

    def is_drone_oui(self, mac: str) -> bool:
        return self.lookup(mac) is not None

    def get_generic_indicators(self) -> dict:
        return self._raw_db.get("generic_drone_indicators", {})

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_oui(raw: str) -> Optional[str]:
        """Normalise any OUI format to 'XX:XX:XX' uppercase."""
        cleaned = raw.upper().replace("-", ":").replace(".", ":")
        parts = cleaned.split(":")
        if len(parts) >= 3:
            return ":".join(parts[:3])
        return None

    @staticmethod
    def _oui_from_mac(mac: str) -> Optional[str]:
        if not mac or len(mac) < 8:
            return None
        cleaned = mac.upper().replace("-", ":").replace(".", ":")
        parts = cleaned.split(":")
        if len(parts) >= 3:
            return ":".join(parts[:3])
        return None
