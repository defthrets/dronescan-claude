"""
rf_engine/capture.py
Packet capture (monitor mode) and channel hopping.

Capture runs in a *separate process* (to avoid Scapy's GIL issues) and
feeds raw packet bytes into a multiprocessing Queue.  The async bridge
task reads from that queue and hands deserialized packets to a coroutine
callback in the main asyncio event loop.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import signal
import time
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger("drone_detect.capture")


# ─────────────────────────────────────────────────────────────────────────────
# Channel hopper
# ─────────────────────────────────────────────────────────────────────────────

class ChannelHopper:
    """Hops channels on a monitor-mode interface using `iw`."""

    def __init__(self, interface: str, channels: List[int], hop_interval: float):
        self.interface = interface
        self.channels = channels if channels else [1, 6, 11]
        self.hop_interval = hop_interval
        self._running = False
        self._idx = 0
        self._task: Optional[asyncio.Task] = None
        self.current_channel: int = self.channels[0]

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Channel hopper started — %d channels, %.1fs interval",
                    len(self.channels), self.hop_interval)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while self._running:
            ch = self.channels[self._idx % len(self.channels)]
            await self._set_channel(ch)
            self.current_channel = ch
            self._idx += 1
            await asyncio.sleep(self.hop_interval)

    async def _set_channel(self, channel: int):
        try:
            proc = await asyncio.create_subprocess_exec(
                "iw", "dev", self.interface, "set", "channel", str(channel),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except FileNotFoundError:
            logger.warning("'iw' not found — channel hopping disabled")
            self._running = False
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            logger.debug("Channel hop error: %s", exc)

    def fix_channel(self, channel: int):
        """Stop hopping and lock to a specific channel."""
        self.channels = [channel]
        self._running = False
        asyncio.ensure_future(self._set_channel(channel))
        logger.info("Fixed channel: %d", channel)


# ─────────────────────────────────────────────────────────────────────────────
# Capture worker (runs in a separate Process)
# ─────────────────────────────────────────────────────────────────────────────

def _capture_worker(
    interface: str,
    pkt_queue: multiprocessing.Queue,
    stop_evt: multiprocessing.Event,
    pcap_enabled: bool,
    pcap_dir: str,
):
    """
    Blocking Scapy sniff loop — runs in a daemon process.
    Pushes raw packet bytes into *pkt_queue*.
    """
    # Ignore SIGINT; let the main process handle shutdown.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    try:
        from scapy.all import sniff, wrpcap           # type: ignore
        from scapy.layers.dot11 import Dot11          # type: ignore
    except ImportError as exc:
        print(f"[CAPTURE] Scapy import error: {exc}")
        return

    packet_buf: list = []
    pcap_path: Optional[str] = None

    if pcap_enabled:
        Path(pcap_dir).mkdir(parents=True, exist_ok=True)
        pcap_path = str(Path(pcap_dir) / f"capture_{int(time.time())}.pcap")
        print(f"[CAPTURE] PCAP recording: {pcap_path}")

    def _handle(pkt):
        if stop_evt.is_set():
            return
        if not pkt.haslayer(Dot11):
            return
        try:
            pkt_queue.put_nowait(bytes(pkt))
        except Exception:
            pass  # drop on full queue

        if pcap_enabled and pcap_path:
            packet_buf.append(pkt)
            if len(packet_buf) >= 200:
                wrpcap(pcap_path, packet_buf, append=True)
                packet_buf.clear()

    try:
        sniff(
            iface=interface,
            prn=_handle,
            store=False,
            stop_filter=lambda _: stop_evt.is_set(),
            monitor=True,
        )
    except PermissionError:
        print(f"[CAPTURE] Permission denied on '{interface}'. Run as root.")
    except OSError as exc:
        print(f"[CAPTURE] Interface error: {exc}")
    except Exception as exc:
        print(f"[CAPTURE] Unexpected error: {exc}")
    finally:
        if pcap_enabled and packet_buf and pcap_path:
            try:
                from scapy.all import wrpcap  # type: ignore
                wrpcap(pcap_path, packet_buf, append=True)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Async wrapper
# ─────────────────────────────────────────────────────────────────────────────

class PacketCapture:
    """
    Spawns a capture Process, bridges its multiprocessing Queue to an
    async callback via an executor-based bridge task.
    """

    def __init__(self, interface: str, pcap_enabled: bool = False, pcap_dir: str = "./pcap"):
        self.interface = interface
        self.pcap_enabled = pcap_enabled
        self.pcap_dir = pcap_dir

        self._mp_queue: Optional[multiprocessing.Queue] = None
        self._stop_evt: Optional[multiprocessing.Event] = None
        self._process: Optional[multiprocessing.Process] = None
        self._bridge: Optional[asyncio.Task] = None
        self._running = False

    async def start(self, callback: Callable):
        """Start capture; *callback* is an async coroutine called per packet."""
        self._mp_queue = multiprocessing.Queue(maxsize=20_000)
        self._stop_evt = multiprocessing.Event()

        self._process = multiprocessing.Process(
            target=_capture_worker,
            args=(self.interface, self._mp_queue, self._stop_evt,
                  self.pcap_enabled, self.pcap_dir),
            daemon=True,
        )
        self._process.start()
        self._running = True
        logger.info("Capture process started (PID %d) on %s",
                    self._process.pid, self.interface)

        self._bridge = asyncio.create_task(self._bridge_task(callback))

    async def _bridge_task(self, callback: Callable):
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                raw = await loop.run_in_executor(None, self._dequeue)
                if raw:
                    pkt = self._deserialize(raw)
                    if pkt is not None:
                        try:
                            await callback(pkt)
                        except Exception as exc:
                            logger.debug("Callback error: %s", exc)
                else:
                    await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Bridge error: %s", exc)
                await asyncio.sleep(0.1)

    def _dequeue(self):
        try:
            return self._mp_queue.get(timeout=0.05)
        except Exception:
            return None

    @staticmethod
    def _deserialize(raw: bytes):
        try:
            from scapy.layers.radiohead import RadioTap  # type: ignore
            return RadioTap(raw)
        except Exception:
            return None

    async def stop(self):
        self._running = False

        if self._stop_evt:
            self._stop_evt.set()

        if self._bridge:
            self._bridge.cancel()
            try:
                await self._bridge
            except asyncio.CancelledError:
                pass

        if self._process and self._process.is_alive():
            self._process.join(timeout=3)
            if self._process.is_alive():
                self._process.terminate()

        logger.info("Packet capture stopped")
