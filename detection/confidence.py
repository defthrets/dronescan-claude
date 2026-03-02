"""
detection/confidence.py
Confidence scoring engine.

Combines four independent evidence sources into a 0-100 score:
    OUI match          (0-40 pts)  — known drone manufacturer MAC prefix
    SSID match         (0-30 pts)  — known drone network name pattern
    Channel match      (0-15 pts)  — drone-typical 2.4/5.8 GHz channel usage
    Traffic behaviour  (0-15 pts)  — high packet rate typical of telemetry links

Labels:  NONE (<30)  |  LOW (30-59)  |  MEDIUM (60-79)  |  HIGH (>=80)
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger("drone_detect.confidence")

# Channels most commonly used by consumer drones
_DRONE_CH_24  = frozenset({1, 6, 11})
_DRONE_CH_5   = frozenset({36, 40, 44, 48})
_DRONE_CH_58  = frozenset({149, 153, 157, 161, 165})


class ConfidenceScorer:

    def __init__(self, config: dict):
        w = config.get("confidence_weights", {})
        self.w_oui      = w.get("oui_match",        40)
        self.w_ssid     = w.get("ssid_match",        30)
        self.w_channel  = w.get("channel_match",     15)
        self.w_traffic  = w.get("traffic_behavior",  15)

        t = config.get("thresholds", {})
        self.t_low    = t.get("low",    30)
        self.t_medium = t.get("medium", 60)
        self.t_high   = t.get("high",   80)

    # ── public API ───────────────────────────────────────────────────────────

    def score(
        self,
        oui_result:       Optional[dict],
        ssid:             Optional[str],
        ssid_brand:       str,
        ssid_points:      int,
        channel:          int,
        packet_rate_pps:  float,
    ) -> Dict:
        """
        Compute confidence score.

        Returns dict:
            total           — 0-100 float
            label           — "NONE" | "LOW" | "MEDIUM" | "HIGH"
            brand           — best-guess brand string
            is_drone        — bool (total >= low threshold)
            breakdown       — per-component scores
        """
        scores: Dict[str, int] = {}
        brand = "Unknown"

        # ── OUI ──────────────────────────────────────────────────────────────
        if oui_result:
            boost = oui_result.get("confidence_boost", 30)
            # Scale manufacturer boost (0-100) to our weight slot (0-w_oui)
            scores["oui"] = min(self.w_oui, int(boost * self.w_oui / 100))
            brand = oui_result.get("brand", "Unknown")
        else:
            scores["oui"] = 0

        # ── SSID ─────────────────────────────────────────────────────────────
        if ssid_points > 0:
            # ssid_points are 0-30 relative to w_ssid default of 30
            scores["ssid"] = min(self.w_ssid, int(ssid_points * self.w_ssid / 30))
            if brand == "Unknown" and ssid_brand not in ("Unknown", "Generic"):
                brand = ssid_brand
        else:
            scores["ssid"] = 0

        # ── Channel ──────────────────────────────────────────────────────────
        scores["channel"] = self._score_channel(channel, oui_result)

        # ── Traffic ──────────────────────────────────────────────────────────
        scores["traffic"] = self._score_traffic(packet_rate_pps)

        total = min(100.0, float(sum(scores.values())))
        label = self._label(total)

        return {
            "total":     total,
            "label":     label,
            "brand":     brand,
            "is_drone":  total >= self.t_low,
            "breakdown": scores,
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    def _score_channel(self, ch: int, oui_result: Optional[dict]) -> int:
        if not ch:
            return 0

        # Use brand's known channels if available
        if oui_result:
            known = set(oui_result.get("known_channels", []))
            if known:
                return self.w_channel if ch in known else 0

        # Generic drone channel scoring
        if ch in _DRONE_CH_58:
            return self.w_channel            # 5.8 GHz — strongly preferred by drones
        if ch in _DRONE_CH_5:
            return int(self.w_channel * 0.8)
        if ch in _DRONE_CH_24:
            return int(self.w_channel * 0.6)
        return 0

    def _score_traffic(self, pps: float) -> int:
        if pps <= 0:
            return 0
        if pps >= 50:
            return self.w_traffic
        if pps >= 20:
            return int(self.w_traffic * 0.8)
        if pps >= 10:
            return int(self.w_traffic * 0.4)
        return 0

    def _label(self, score: float) -> str:
        if score >= self.t_high:
            return "HIGH"
        if score >= self.t_medium:
            return "MEDIUM"
        if score >= self.t_low:
            return "LOW"
        return "NONE"
