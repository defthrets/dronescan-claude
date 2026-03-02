#!/usr/bin/env python3
"""
Drone Detection System — Main Entry Point
=========================================
Usage:
    sudo python main.py web           # Launch web dashboard (default)
    sudo python main.py terminal      # Rich live terminal display
    sudo python main.py dashboard     # Full-screen curses dashboard
    sudo python main.py scan          # Simple scrolling scan output
    sudo python main.py --help

All modes share the same detection pipeline.  The web mode also exposes
a REST API and WebSocket stream usable by external clients.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import click
import uvicorn

from utils.config_loader import load_config
from utils.logging_config import setup_logging
from rf_engine.capture import PacketCapture, ChannelHopper, resolve_interface
from rf_engine.frame_parser import parse_frame
from rf_engine.device_table import DeviceTable, DroneDevice
from detection.oui_lookup import OUILookup
from detection.ssid_patterns import match_ssid
from detection.confidence import ConfidenceScorer
from detection.brand_profiles import BrandProfiler
from gps.nmea_parser import GPSReader
from gps.tracker import LocationTracker
from web.app import create_app
from web.websocket_manager import WebSocketManager
from cli.display import CLIDisplay
# CursesDashboard imported lazily inside the dashboard command (curses unavailable on Windows)

logger = logging.getLogger("drone_detect")

# ─────────────────────────────────────────────────────────────────────────────
# Core Detection System
# ─────────────────────────────────────────────────────────────────────────────

class DroneDetectionSystem:
    """
    Orchestrates all subsystems:
      PacketCapture → frame_parser → OUILookup + SSID match → ConfidenceScorer
      → DeviceTable → WebSocket broadcast / CLI render
    """

    def __init__(self, config: dict):
        self.config = config
        self._running = False
        self._start_time = time.time()
        self._total_packets = 0
        self._alerted_macs: set = set()

        # ── Shared state ─────────────────────────────────────────────────────
        self.device_table    = DeviceTable(
            device_timeout=config["tracking"]["device_timeout"],
            history_length=config["tracking"]["history_length"],
        )
        self.location_tracker = LocationTracker()
        self.ws_manager       = WebSocketManager()

        # ── Intelligence ─────────────────────────────────────────────────────
        self.oui_lookup    = OUILookup()
        self.scorer        = ConfidenceScorer(config)
        self.brand_profiler = BrandProfiler()

        # ── RF engine ────────────────────────────────────────────────────────
        self.capture = PacketCapture(
            interface=config["interface"],
            pcap_enabled=config["pcap"]["enabled"],
            pcap_dir=config["pcap"]["output_dir"],
        )

        all_channels = (
            config["channels"].get("bands_2_4ghz", [1, 6, 11])
            + config["channels"].get("bands_5ghz", [36, 40, 44, 48])
        )
        fixed = config["channels"].get("fixed_channel")
        channels = [fixed] if fixed else all_channels

        self.hopper = ChannelHopper(
            interface=config["interface"],
            channels=channels,
            hop_interval=config["channels"].get("hop_interval", 0.5),
        )

        # ── GPS ──────────────────────────────────────────────────────────────
        self.gps: Optional[GPSReader] = None
        if config["gps"]["enabled"]:
            self.gps = GPSReader(
                port=config["gps"]["port"],
                baud_rate=config["gps"]["baud_rate"],
                timeout=config["gps"]["timeout"],
            )
            self.gps.set_fix_callback(self._on_gps_fix)

        # ── Background tasks ─────────────────────────────────────────────────
        self._broadcast_task: Optional[asyncio.Task] = None
        self._cleanup_task:   Optional[asyncio.Task] = None

    # ── GPS callback ─────────────────────────────────────────────────────────

    async def _on_gps_fix(self, fix):
        self.location_tracker.update_observer(fix.latitude, fix.longitude, fix.altitude)

    # ── Packet processing pipeline ───────────────────────────────────────────

    async def _process_packet(self, packet):
        """
        Full detection pipeline for a single captured packet.
        Called by the capture bridge task for every 802.11 frame.
        Target latency: <200ms end-to-end.
        """
        self._total_packets += 1

        frame = parse_frame(packet)
        if not frame:
            return

        mac = frame.mac_src
        # Skip broadcast/multicast and null addresses
        if not mac or mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
            return
        if mac[:2] in ("01", "03", "05", "07"):  # multicast prefix
            return

        # ── Intelligence layer ────────────────────────────────────────────────
        oui_result  = self.oui_lookup.lookup(mac)
        vendor      = self.oui_lookup.get_vendor_string(mac)

        # Preserve previously seen SSID if current frame has none
        existing = self.device_table.get_device(mac)
        ssid = frame.ssid or (existing.ssid if existing else None)

        ssid_brand, ssid_pts = match_ssid(ssid)
        pps = self.device_table.get_packet_rate(mac, window_seconds=5.0)

        score = self.scorer.score(
            oui_result=oui_result,
            ssid=ssid,
            ssid_brand=ssid_brand,
            ssid_points=ssid_pts,
            channel=frame.channel,
            packet_rate_pps=pps,
        )

        model = self.brand_profiler.identify_model(score["brand"], ssid)

        # ── Device table update ───────────────────────────────────────────────
        self.device_table.update_device(
            mac=mac,
            vendor=vendor,
            ssid=ssid,
            channel=frame.channel or (existing.channel if existing else 0),
            rssi=frame.rssi,
            confidence=score["total"],
            confidence_label=score["label"],
            brand=model,
            is_drone=score["is_drone"],
            frame_type=frame.frame_type_str,
        )

        # ── RSSI tracking ─────────────────────────────────────────────────────
        if frame.rssi and frame.rssi != -100:
            self.location_tracker.update_drone_rssi(mac, frame.rssi)

        # ── Alert on new high-confidence detection ────────────────────────────
        if score["label"] in ("HIGH", "MEDIUM") and mac not in self._alerted_macs:
            self._alerted_macs.add(mac)
            logger.warning(
                "DRONE ALERT ▶ %s | %s | SSID: %s | CH: %s | %.0f%% %s",
                mac, model, ssid or "—", frame.channel, score["total"], score["label"],
            )

    # ── Background tasks ──────────────────────────────────────────────────────

    async def _broadcast_loop(self):
        """Push device state to all WebSocket clients every second."""
        while self._running:
            try:
                await asyncio.sleep(1.0)
                await self.ws_manager.broadcast({
                    "type": "update",
                    "devices": self.device_table.to_json_list(),
                    "observer": self.location_tracker.get_observer_dict(),
                    "stats": self.get_stats(),
                    "ts": time.time(),
                })
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Broadcast error: %s", exc)

    async def _cleanup_loop(self):
        """Periodically evict stale devices."""
        while self._running:
            try:
                await asyncio.sleep(60)
                removed = self.device_table.cleanup_stale_devices()
                if removed:
                    logger.info("Evicted %d stale device(s)", removed)
                    self._alerted_macs = {
                        m for m in self._alerted_macs
                        if self.device_table.get_device(m) is not None
                    }
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Cleanup error: %s", exc)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True

        # ── Resolve the actual monitor-mode interface ──────────────────────────
        # Handles the common case where airmon-ng renamed wlan1 → wlan1mon
        # but config still says wlan0mon (or any other mismatch).
        try:
            resolved = resolve_interface(self.config["interface"])
        except RuntimeError as exc:
            logger.error("Interface error: %s", exc)
            raise SystemExit(1)

        if resolved != self.config["interface"]:
            logger.warning(
                "Interface override: '%s' → '%s'  "
                "(update config.yaml to silence this warning)",
                self.config["interface"], resolved,
            )
            self.config["interface"] = resolved
            self.capture.interface   = resolved
            self.hopper.interface    = resolved

        logger.info("Starting drone detection on interface '%s'", self.config["interface"])

        await self.capture.start(self._process_packet)
        await self.hopper.start()

        if self.gps:
            await self.gps.start()

        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        self._cleanup_task   = asyncio.create_task(self._cleanup_loop())

        logger.info("Detection system ready")

    async def stop(self):
        self._running = False

        tasks = [t for t in [self._broadcast_task, self._cleanup_task] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await self.capture.stop()
        await self.hopper.stop()

        if self.gps:
            await self.gps.stop()

        logger.info("Detection system stopped")

    def get_stats(self) -> dict:
        return {
            "interface":     self.config["interface"],
            "uptime":        time.time() - self._start_time,
            "total_packets": self._total_packets,
            "total_devices": len(self.device_table),
            "drone_devices": len(self.device_table.get_drone_devices()),
            "current_channel": self.hopper.current_channel,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Click CLI
# ─────────────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--config", "-c", default="config/config.yaml",
              help="Path to YAML configuration file", show_default=True)
@click.option("--interface", "-i", default=None,
              help="Override Wi-Fi interface (e.g. wlan0mon)")
@click.pass_context
def cli(ctx, config, interface):
    """
    DRONE DETECT — Real-time Wi-Fi based drone detection system.
    Run as root for monitor-mode packet capture.
    """
    ctx.ensure_object(dict)
    cfg = load_config(config)
    if interface:
        cfg["interface"] = interface

    setup_logging(
        level=cfg["logging"]["level"],
        log_file=cfg["logging"]["file"],
        max_bytes=cfg["logging"]["max_bytes"],
        backup_count=cfg["logging"]["backup_count"],
    )
    ctx.obj["config"] = cfg


# ── Web mode ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--host", default=None, help="Override web host")
@click.option("--port", "-p", default=None, type=int, help="Override web port")
@click.pass_context
def web(ctx, host, port):
    """Launch the web dashboard + REST API + WebSocket stream."""
    cfg = ctx.obj["config"]
    if host:
        cfg["web"]["host"] = host
    if port:
        cfg["web"]["port"] = port

    asyncio.run(_run_web(cfg))


async def _run_web(config: dict):
    system = DroneDetectionSystem(config)
    app    = create_app(
        device_table=system.device_table,
        location_tracker=system.location_tracker,
        ws_manager=system.ws_manager,
        config=config,
    )

    web_cfg = config["web"]
    uv_config = uvicorn.Config(
        app,
        host=web_cfg["host"],
        port=web_cfg["port"],
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)

    loop = asyncio.get_event_loop()

    def _shutdown(*_):
        logger.info("Shutdown signal received")
        loop.create_task(system.stop())
        server.should_exit = True

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    await system.start()
    logger.info("Web UI: http://%s:%d", web_cfg["host"], web_cfg["port"])
    await server.serve()
    await system.stop()


# ── Terminal (Rich live) mode ─────────────────────────────────────────────────

@cli.command()
@click.pass_context
def terminal(ctx):
    """Live terminal display using Rich (scrolling, color-coded)."""
    asyncio.run(_run_terminal(ctx.obj["config"]))


async def _run_terminal(config: dict):
    system  = DroneDetectionSystem(config)
    display = CLIDisplay()

    loop = asyncio.get_event_loop()
    _stop_flag = asyncio.Event()

    def _shutdown(*_):
        _stop_flag.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    await system.start()
    live = display.start_live()

    try:
        while not _stop_flag.is_set():
            stats = system.get_stats()
            display.update_stats(**stats)
            devices = system.device_table.get_all_devices()
            display.update_live(devices)
            await asyncio.sleep(0.5)
    finally:
        display.stop_live()
        await system.stop()


# ── Curses dashboard mode ─────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def dashboard(ctx):
    """Full-screen curses terminal dashboard. Press Q to quit."""
    try:
        from cli.dashboard import CursesDashboard
    except ModuleNotFoundError:
        click.echo("ERROR: curses is not available on this platform (requires Linux/macOS).", err=True)
        raise SystemExit(1)
    config = ctx.obj["config"]
    system = DroneDetectionSystem(config)
    dash   = CursesDashboard()

    async def _bg():
        await system.start()
        # Keep background tasks alive while curses runs
        while system._running:
            stats = system.get_stats()
            dash.update_stats(**stats)
            await asyncio.sleep(0.5)

    # Run system in background thread-compatible way
    import threading

    loop    = asyncio.new_event_loop()
    bg_task = None

    def _run_loop():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_bg())

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()

    try:
        dash.run(lambda: system.device_table.get_all_devices())
    finally:
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(system.stop(), loop=loop))
        t.join(timeout=3)


# ── Simple scan mode ──────────────────────────────────────────────────────────

@cli.command()
@click.option("--drones-only", is_flag=True, default=False,
              help="Print only drone-classified devices")
@click.pass_context
def scan(ctx, drones_only):
    """Scrolling real-time scan output (minimal, script-friendly)."""
    asyncio.run(_run_scan(ctx.obj["config"], drones_only))


async def _run_scan(config: dict, drones_only: bool):
    from rich.console import Console
    con = Console()
    system = DroneDetectionSystem(config)
    seen: set = set()

    _stop = asyncio.Event()
    signal.signal(signal.SIGINT,  lambda *_: _stop.set())
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())

    await system.start()
    con.print("[bold orange1]DRONE DETECT — SCAN MODE[/bold orange1]")
    con.print(f"Interface: [cyan]{config['interface']}[/cyan]\n")

    try:
        while not _stop.is_set():
            devs = system.device_table.get_all_devices()
            if drones_only:
                devs = [d for d in devs if d.is_drone or d.confidence >= 30]

            for d in devs:
                if d.mac not in seen:
                    seen.add(d.mac)
                    label_color = {
                        "HIGH": "bold red", "MEDIUM": "yellow",
                        "LOW": "cyan", "NONE": "dim",
                    }.get(d.confidence_label, "dim")
                    con.print(
                        f"[{label_color}]▶ {d.confidence_label:6}[/{label_color}] "
                        f"[cyan]{d.mac}[/cyan]  "
                        f"[white]{(d.brand or d.vendor or 'Unknown'):20}[/white]  "
                        f"[yellow]{(d.ssid or '—'):24}[/yellow]  "
                        f"CH:{d.channel or '?':3}  "
                        f"RSSI:{d.rssi} dBm  "
                        f"[bold]{d.confidence:.0f}%[/bold]"
                    )
            await asyncio.sleep(0.5)
    finally:
        await system.stop()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Default to web mode if no subcommand given
    if len(sys.argv) == 1:
        sys.argv.append("web")
    cli(obj={})
