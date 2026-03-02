"""
gps/nmea_parser.py
Async reader for a USB GPS dongle emitting NMEA 0183 sentences.
Parses GGA (fix quality, altitude, HDOP) and RMC (speed, heading) sentences.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("drone_detect.gps")


@dataclass
class GPSFix:
    timestamp: float
    latitude:  float
    longitude: float
    altitude:  float        # metres above sea level
    speed_kph: float        # kilometres per hour
    heading:   float        # true north, degrees
    hdop:      float        # horizontal dilution of precision
    satellites: int
    fix_quality: int        # 0=none, 1=GPS, 2=DGPS, 4=RTK


class NMEAParser:
    """Stateful parser that accumulates info from multiple sentence types."""

    def __init__(self):
        self._fix: Optional[GPSFix] = None

    def parse(self, sentence: str) -> Optional[GPSFix]:
        """
        Feed one NMEA sentence.  Returns a GPSFix if enough data is available,
        otherwise None.  Requires *pynmea2* to be installed.
        """
        try:
            import pynmea2  # type: ignore

            msg = pynmea2.parse(sentence.strip())

            if msg.sentence_type == "GGA":
                qual = int(msg.gps_qual) if msg.gps_qual else 0
                if qual > 0:
                    self._fix = GPSFix(
                        timestamp=time.time(),
                        latitude=float(msg.latitude)    if msg.latitude    else 0.0,
                        longitude=float(msg.longitude)  if msg.longitude   else 0.0,
                        altitude=float(msg.altitude)    if msg.altitude    else 0.0,
                        speed_kph=0.0,
                        heading=0.0,
                        hdop=float(msg.horizontal_dil)  if msg.horizontal_dil else 99.0,
                        satellites=int(msg.num_sats)    if msg.num_sats    else 0,
                        fix_quality=qual,
                    )
                    return self._fix

            elif msg.sentence_type == "RMC" and msg.status == "A":
                if self._fix:
                    if msg.spd_over_grnd:
                        self._fix.speed_kph = float(msg.spd_over_grnd) * 1.852  # knots → kph
                    if msg.true_course:
                        self._fix.heading = float(msg.true_course)
                    return self._fix

        except Exception:
            pass

        return None

    @property
    def latest_fix(self) -> Optional[GPSFix]:
        return self._fix


class GPSReader:
    """
    Asynchronously reads NMEA sentences from a serial GPS device.
    Calls *fix_callback* with each valid GPSFix.
    """

    def __init__(self, port: str, baud_rate: int = 9600, timeout: float = 5.0):
        self.port      = port
        self.baud_rate = baud_rate
        self.timeout   = timeout

        self._parser   = NMEAParser()
        self._callback: Optional[Callable] = None
        self._running  = False
        self._task: Optional[asyncio.Task] = None

    def set_fix_callback(self, callback: Callable):
        self._callback = callback

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._read_loop())
        logger.info("GPS reader started on %s @ %d baud", self.port, self.baud_rate)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _read_loop(self):
        try:
            import serial.asyncio  # type: ignore
        except ImportError:
            logger.error("pyserial-asyncio not installed — GPS disabled")
            return

        while self._running:
            try:
                reader, _ = await serial.asyncio.open_serial_connection(
                    url=self.port, baudrate=self.baud_rate
                )
                logger.info("GPS connected on %s", self.port)

                while self._running:
                    try:
                        raw = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
                    except asyncio.TimeoutError:
                        logger.warning("GPS: no data received (timeout)")
                        continue

                    sentence = raw.decode("ascii", errors="ignore")
                    fix = self._parser.parse(sentence)
                    if fix and self._callback:
                        try:
                            await self._callback(fix)
                        except Exception as exc:
                            logger.debug("GPS callback error: %s", exc)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("GPS read error: %s — retrying in 5s", exc)
                await asyncio.sleep(5)

    @property
    def latest_fix(self) -> Optional[GPSFix]:
        return self._parser.latest_fix
