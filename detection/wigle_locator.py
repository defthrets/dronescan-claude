"""
detection/wigle_locator.py
Wi-Fi-based observer position estimation via the WiGLE crowdsourced database.

How it works
────────────
  1. The scanner already captures BSSIDs + RSSI from every nearby access point.
  2. WiGLE has GPS coordinates for millions of APs (wardriving crowdsource).
  3. We query WiGLE for the stored positions of each detected BSSID.
  4. RSSI-weighted centroid of the matched positions → observer location.

Accuracy: ~20–100 m in urban/suburban areas; less accurate in rural areas
          where WiGLE coverage is thin.

Setup
─────
  1. Create a free account at https://wigle.net
  2. Go to https://wigle.net/account → copy "API Name" and "API Token"
  3. Add to config.yaml:
       wigle:
         enabled:         true
         api_name:        "AID..."   # shown as "API Name" on your account page
         api_token:       "..."      # shown as "API Token"
         update_interval: 60        # seconds between position refreshes
         min_refs:        2         # minimum matched APs needed for a fix

Rate limits
───────────
  Free WiGLE tier: ~10 API calls / minute.
  All results are cached for 24 h — AP locations don't change.
  The locator automatically backs off on HTTP 429 responses.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("drone_detect.wigle")

# RSSI floor below which signals are ignored (too noisy to be useful)
_RSSI_FLOOR: float = -92.0


def _rssi_weight(rssi: float) -> float:
    """
    Convert RSSI (dBm) to a linear power weight.
    Signals closer to 0 dBm (stronger) receive exponentially higher weight,
    pulling the centroid towards the nearest / most reliable reference APs.
    """
    return 10.0 ** ((max(rssi, _RSSI_FLOOR) - _RSSI_FLOOR) / 10.0)


class WiGLELocator:
    """
    Maps observed BSSIDs to GPS coordinates via WiGLE REST API v2,
    then computes an RSSI-weighted centroid as the observer's position.
    """

    API_BASE = "https://api.wigle.net/api/v2"

    def __init__(
        self,
        api_name:  str,
        api_token: str,
        cache_ttl: int = 86_400,   # 24 h
    ):
        """
        api_name  : WiGLE "API Name" (shown on account page, starts with AID…)
        api_token : WiGLE API Token
        cache_ttl : how long to reuse cached AP positions (seconds)
        """
        self._auth:      Tuple[str, str]                          = (api_name, api_token)
        self._cache:     Dict[str, Optional[Tuple[float, float]]] = {}
        self._cache_ts:  Dict[str, float]                         = {}
        self._cache_ttl: int                                      = cache_ttl
        self._sem:       asyncio.Semaphore                        = asyncio.Semaphore(1)
        self._last_req:  float                                    = 0.0
        self._min_gap:   float                                    = 6.5   # ~9 req/min
        self._ok:        bool                                     = True  # False on 401

    # ── Public API ─────────────────────────────────────────────────────────────

    async def lookup_bssid(self, mac: str) -> Optional[Tuple[float, float]]:
        """Return (lat, lon) for a BSSID from the WiGLE database, or None."""
        if not self._ok:
            return None

        now = time.time()
        # Fast path: valid cache entry
        if mac in self._cache and now - self._cache_ts.get(mac, 0) < self._cache_ttl:
            return self._cache[mac]

        # Serialise all HTTP calls through one semaphore (rate-limiting)
        async with self._sem:
            # Re-check after acquiring lock (another coroutine may have fetched it)
            now = time.time()
            if mac in self._cache and now - self._cache_ts.get(mac, 0) < self._cache_ttl:
                return self._cache[mac]

            # Enforce minimum gap between successive requests
            wait = self._min_gap - (time.time() - self._last_req)
            if wait > 0:
                await asyncio.sleep(wait)

            result              = await self._fetch_bssid(mac)
            self._cache[mac]    = result
            self._cache_ts[mac] = time.time()
            self._last_req      = time.time()
            return result

    async def estimate_position(
        self,
        devices:     List[dict],
        min_refs:    int = 2,
        max_lookups: int = 15,
    ) -> Optional[Tuple[float, float, float]]:
        """
        Estimate the observer's position from nearby access points.

        devices     : list of device dicts (need 'mac', 'rssi', 'confidence')
        min_refs    : minimum WiGLE-matched APs needed for a valid fix
        max_lookups : cap on API queries per call (to respect rate limits)

        Returns (lat, lon, accuracy_metres) or None.
        """
        if not self._ok:
            return None

        # Filter: exclude probable drones and very weak / noisy signals
        candidates = [
            d for d in devices
            if d.get("rssi", -100) > _RSSI_FLOOR
            and d.get("confidence", 0) < 60
        ]

        # Sort strongest signal first — closer APs give tighter position constraints
        candidates.sort(key=lambda d: d.get("rssi", -100), reverse=True)
        candidates = candidates[:max_lookups]

        if not candidates:
            return None

        # Kick off all lookups concurrently (serialised internally via semaphore)
        positions = await asyncio.gather(
            *[self.lookup_bssid(d["mac"]) for d in candidates],
            return_exceptions=True,
        )

        # Collect (lat, lon, rssi) for every matched AP
        known: List[Tuple[float, float, float]] = []
        for d, pos in zip(candidates, positions):
            if isinstance(pos, tuple) and pos is not None:
                known.append((pos[0], pos[1], d.get("rssi", -80)))

        if len(known) < min_refs:
            logger.debug(
                "WiGLE: only %d/%d APs found in database (need ≥%d)",
                len(known), len(candidates), min_refs,
            )
            return None

        # RSSI-weighted centroid
        total_w = 0.0
        lat_sum = 0.0
        lon_sum = 0.0
        for lat, lon, rssi in known:
            w        = _rssi_weight(rssi)
            lat_sum += lat * w
            lon_sum += lon * w
            total_w += w

        est_lat  = lat_sum / total_w
        est_lon  = lon_sum / total_w

        # Rough accuracy estimate: improves with more reference APs
        accuracy = max(20.0, 250.0 / len(known))

        logger.info(
            "WiGLE fix: %.5f, %.5f  ±%d m  (%d/%d APs matched)",
            est_lat, est_lon, int(accuracy), len(known), len(candidates),
        )
        return (est_lat, est_lon, accuracy)

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _fetch_bssid(self, mac: str) -> Optional[Tuple[float, float]]:
        """Single WiGLE API request for one BSSID."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"{self.API_BASE}/network/search",
                    params={"netid": mac},
                    auth=self._auth,
                )

            if r.status_code == 401:
                logger.error(
                    "WiGLE: authentication failed — "
                    "check wigle.api_name / wigle.api_token in config.yaml"
                )
                self._ok = False
                return None

            if r.status_code == 429:
                logger.warning("WiGLE: rate-limit hit — backing off 60 s")
                self._last_req = time.time() + 54   # force ~60 s gap
                return None

            if r.status_code != 200:
                logger.debug("WiGLE: HTTP %d for %s", r.status_code, mac)
                return None

            results = r.json().get("results", [])
            if results:
                lat = results[0].get("trilat")
                lon = results[0].get("trilong")
                if lat is not None and lon is not None:
                    return (float(lat), float(lon))

        except ImportError:
            logger.error(
                "WiGLE: httpx not installed — run: pip install httpx"
            )
            self._ok = False
        except Exception as exc:
            logger.debug("WiGLE: lookup error for %s: %s", mac, exc)

        return None
