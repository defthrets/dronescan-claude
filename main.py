#!/usr/bin/env python3
"""
Drone Detection System — Main Entry Point
=========================================
Usage:
    sudo python main.py web           # Launch web dashboard (default)
    sudo python main.py web --ssl     # HTTPS (required for phone GPS over LAN)
    sudo python main.py terminal      # Rich live terminal display
    sudo python main.py dashboard     # Full-screen curses dashboard
    sudo python main.py scan          # Simple scrolling scan output
    sudo python main.py diag          # Diagnostic capture mode
    sudo python main.py --help

All modes share the same detection pipeline.  The web mode also exposes
a REST API and WebSocket stream usable by external clients.

HTTPS NOTE: Phone/tablet browsers block GPS on plain http:// over LAN.
Run with --ssl to enable HTTPS with an auto-generated self-signed cert.
Then open https://<ip>:8443 on your phone and accept the cert warning.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

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
from detection.wigle_locator import WiGLELocator
from gps.nmea_parser import GPSReader
from gps.tracker import LocationTracker
from web.app import create_app
from web.websocket_manager import WebSocketManager
from cli.display import CLIDisplay
# CursesDashboard imported lazily inside the dashboard command (curses unavailable on Windows)

logger = logging.getLogger("drone_detect")


# ─────────────────────────────────────────────────────────────────────────────
# SSL Certificate (self-signed, for HTTPS over LAN)
# ─────────────────────────────────────────────────────────────────────────────

def _get_local_ips() -> list:
    """Return all local IPv4 addresses for the SAN extension."""
    ips = {"127.0.0.1"}
    try:
        hostname = socket.gethostname()
        ips.add(socket.gethostbyname(hostname))
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.check_output(["hostname", "-I"], text=True, timeout=3)
        for ip in out.strip().split():
            if "." in ip:
                ips.add(ip)
    except Exception:
        pass
    return list(ips)


def generate_ssl_cert(cert_dir: Path = Path("C:/drone-detect/ssl")) -> Tuple[str, str]:
    """
    Generate (or reuse) a self-signed TLS certificate for HTTPS.
    Returns (cert_path, key_path).
    Required so phone browsers allow GPS over LAN (https://<ip>:8443).
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_file = cert_dir / "cert.pem"
    key_file  = cert_dir / "key.pem"

    if cert_file.exists() and key_file.exists():
        logger.info("Reusing existing SSL cert: %s", cert_file)
        return str(cert_file), str(key_file)

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME,        "drone-detect"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME,  "DroneDetect"),
        ])

        # SAN: include all local IPs so the cert is valid on the LAN
        san_entries = [x509.DNSName("localhost"), x509.DNSName("drone-detect.local")]
        for ip in _get_local_ips():
            try:
                san_entries.append(x509.IPAddress(ipaddress.IPv4Address(ip)))
            except Exception:
                pass

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
            .sign(key, hashes.SHA256(), default_backend())
        )

        with open(cert_file, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        with open(key_file, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))

        logger.info("Generated self-signed SSL cert: %s", cert_file)
        logger.info("Local IPs in cert SAN: %s", _get_local_ips())
        return str(cert_file), str(key_file)

    except ImportError:
        logger.error("cryptography package not installed — run: pip install cryptography")
        raise
    except Exception as exc:
        logger.error("SSL cert generation failed: %s", exc)
        raise


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

        # ── WiGLE Wi-Fi positioning ───────────────────────────────────────────
        self.wigle: Optional[WiGLELocator] = None
        wigle_cfg = config.get("wigle", {})
        if (wigle_cfg.get("enabled")
                and wigle_cfg.get("api_name")
                and wigle_cfg.get("api_token")):
            self.wigle = WiGLELocator(
                api_name=wigle_cfg["api_name"],
                api_token=wigle_cfg["api_token"],
            )
            logger.info("WiGLE Wi-Fi positioning enabled")

        # ── Background tasks ─────────────────────────────────────────────────
        self._broadcast_task: Optional[asyncio.Task] = None
        self._cleanup_task:   Optional[asyncio.Task] = None
        self._wigle_task:     Optional[asyncio.Task] = None

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

    async def _wigle_loop(self):
        """
        Periodically estimate observer position from WiGLE-known access points.
        Runs only when wigle is enabled in config.
        Waits 20 s on startup to let the device table fill up first.
        """
        wigle_cfg  = self.config.get("wigle", {})
        interval   = wigle_cfg.get("update_interval", 60)
        min_refs   = wigle_cfg.get("min_refs", 2)

        await asyncio.sleep(20)   # let packet capture collect some APs first

        while self._running:
            try:
                devices = self.device_table.to_json_list()
                result  = await self.wigle.estimate_position(
                    devices, min_refs=min_refs
                )
                if result:
                    lat, lon, accuracy = result
                    self.location_tracker.update_observer(lat, lon, 0.0, "wigle")
                    # Push an immediate observer update to all connected clients
                    await self.ws_manager.broadcast({
                        "type":     "observer_update",
                        "observer": self.location_tracker.get_observer_dict(),
                        "ts":       time.time(),
                    })
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("WiGLE loop error: %s", exc)
                await asyncio.sleep(interval)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        demo_mode = self.config.get("_demo_mode", False)

        if demo_mode:
            logger.warning(
                "DEMO MODE — capture disabled (no wireless adapter required). "
                "Web UI is fully functional; use on Linux with adapter for live detection."
            )
        else:
            # ── Resolve the actual monitor-mode interface ──────────────────────
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
        if self.wigle:
            self._wigle_task = asyncio.create_task(self._wigle_loop())

        logger.info("Detection system ready")

    async def stop(self):
        self._running = False

        tasks = [t for t in [self._broadcast_task, self._cleanup_task, self._wigle_task] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        if not self.config.get("_demo_mode", False):
            await self.capture.stop()
            await self.hopper.stop()

        if self.gps:
            await self.gps.stop()

        logger.info("Detection system stopped")

    def get_stats(self) -> dict:
        elapsed = time.time() - self._start_time
        return {
            "interface":       self.config["interface"],
            "uptime":          elapsed,
            "total_packets":   self._total_packets,
            "pps":             round(self._total_packets / max(elapsed, 1), 1),
            "total_devices":   len(self.device_table),
            "drone_devices":   len(self.device_table.get_drone_devices()),
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
@click.option("--demo", is_flag=True, default=False,
              help="Start web UI only (no capture) — useful without a wireless adapter")
@click.option("--ssl", "use_ssl", is_flag=True, default=False,
              help="Enable HTTPS with auto-generated self-signed cert "
                   "(required for phone GPS over LAN). "
                   "Opens on port 8443 unless --port is set.")
@click.pass_context
def web(ctx, host, port, demo, use_ssl):
    """Launch the web dashboard + REST API + WebSocket stream.

    \b
    GPS NOTE:
      Plain HTTP blocks GPS on mobile browsers over LAN.
      Use --ssl to enable HTTPS, then open https://<ip>:8443 on your phone
      and accept the self-signed cert warning — GPS will then work.
    """
    cfg = ctx.obj["config"]
    if host:
        cfg["web"]["host"] = host
    if port:
        cfg["web"]["port"] = port
    elif use_ssl and not port:
        cfg["web"]["ssl_port"] = cfg["web"].get("ssl_port", 8443)
    if demo:
        cfg["_demo_mode"] = True
    if use_ssl:
        cfg["_use_ssl"] = True

    asyncio.run(_run_web(cfg))


async def _run_web(config: dict):
    demo_mode = config.get("_demo_mode", False)
    use_ssl   = config.get("_use_ssl", False)

    system = DroneDetectionSystem(config)
    app    = create_app(
        device_table=system.device_table,
        location_tracker=system.location_tracker,
        ws_manager=system.ws_manager,
        config=config,
    )

    web_cfg  = config["web"]
    ssl_port = web_cfg.get("ssl_port", 8443)

    if use_ssl:
        cert_file, key_file = generate_ssl_cert()
        port = ssl_port
        scheme = "https"
    else:
        cert_file = key_file = None
        port = web_cfg["port"]
        scheme = "http"

    uv_kwargs: dict = dict(
        host=web_cfg["host"],
        port=port,
        log_level="warning",
        access_log=False,
    )
    if use_ssl:
        uv_kwargs["ssl_certfile"] = cert_file
        uv_kwargs["ssl_keyfile"]  = key_file

    uv_config = uvicorn.Config(app, **uv_kwargs)
    server = uvicorn.Server(uv_config)

    loop = asyncio.get_event_loop()

    def _shutdown(*_):
        logger.info("Shutdown signal received")
        loop.create_task(system.stop())
        server.should_exit = True

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    await system.start()

    local_ips = _get_local_ips()
    logger.info("Web UI: %s://localhost:%d", scheme, port)
    for ip in local_ips:
        if ip != "127.0.0.1":
            logger.info("       %s://%s:%d  ← open this on your phone", scheme, ip, port)
    if use_ssl:
        logger.info("NOTE: Accept the browser's self-signed cert warning — GPS will work after that")
    else:
        logger.info("TIP: Run with --ssl to enable HTTPS and phone GPS over LAN")

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


# ── Diagnostic mode ───────────────────────────────────────────────────────────

@cli.command()
@click.option("--duration", "-d", default=30, type=int,
              help="Seconds to capture for diagnostics", show_default=True)
@click.pass_context
def diag(ctx, duration):
    """
    Diagnostic mode: capture packets for N seconds and show ALL seen MACs.
    Helps verify the capture pipeline is working before deploying in the field.
    Use this first to confirm packets are being received.
    """
    asyncio.run(_run_diag(ctx.obj["config"], duration))


async def _run_diag(config: dict, duration: int):
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    import collections

    con = Console()
    con.print("[bold orange1]═══ DRONE DETECT — DIAGNOSTIC MODE ═══[/bold orange1]")
    con.print(f"Interface : [cyan]{config['interface']}[/cyan]")
    con.print(f"Duration  : [cyan]{duration}s[/cyan]")
    con.print(f"OUI DB    : [cyan]{len(OUILookup()._db)} entries[/cyan]\n")

    system = DroneDetectionSystem(config)

    # Widen thresholds to catch everything for diagnostics
    config["thresholds"]["low"]    = 0
    config["thresholds"]["medium"] = 30
    config["thresholds"]["high"]   = 60

    _stop = asyncio.Event()

    await system.start()
    con.print("[green]✓ Capture started — showing ALL devices seen...[/green]\n")

    start = time.time()
    last_pkt_count = 0

    try:
        while not _stop.is_set() and (time.time() - start) < duration:
            await asyncio.sleep(2.0)

            elapsed     = time.time() - start
            pkts_now    = system._total_packets
            pkt_delta   = pkts_now - last_pkt_count
            pps         = pkt_delta / 2.0
            last_pkt_count = pkts_now

            all_devs    = system.device_table.get_all_devices()
            drone_devs  = [d for d in all_devs if d.is_drone or d.confidence >= 30]
            ch          = system.hopper.current_channel

            # Build diagnostic table
            tbl = Table(title=f"[orange1]t={elapsed:.0f}s  pkts={pkts_now}  pps={pps:.1f}  CH={ch}[/orange1]",
                        style="dim", header_style="bold orange1", min_width=90)
            tbl.add_column("MAC",       width=19)
            tbl.add_column("VENDOR",    width=20)
            tbl.add_column("SSID",      width=22)
            tbl.add_column("CH",        width=4)
            tbl.add_column("RSSI",      width=8)
            tbl.add_column("CONF",      width=8)
            tbl.add_column("PKTS",      width=7)
            tbl.add_column("LABEL",     width=8)

            # Sort by confidence desc
            for d in sorted(all_devs, key=lambda x: -x.confidence):
                label_style = {
                    "HIGH":   "bold red",
                    "MEDIUM": "yellow",
                    "LOW":    "cyan",
                    "NONE":   "dim",
                }.get(d.confidence_label, "dim")

                tbl.add_row(
                    d.mac,
                    (d.brand or d.vendor or "—")[:20],
                    (d.ssid or "—")[:22],
                    str(d.channel or "?"),
                    f"{d.rssi} dBm",
                    f"{d.confidence:.0f}%",
                    str(d.packet_count),
                    f"[{label_style}]{d.confidence_label}[/{label_style}]",
                )

            con.print(tbl)

            if pkts_now == 0 and elapsed > 5:
                con.print("[bold red]⚠ NO PACKETS received — check interface is in monitor mode:[/bold red]")
                con.print(f"  sudo airmon-ng start wlan0")
                con.print(f"  sudo airmon-ng check kill")
            elif pps < 1 and elapsed > 10:
                con.print(f"[yellow]⚠ Low packet rate ({pps:.1f} pps) — try moving closer to devices[/yellow]")
            else:
                con.print(f"[green]✓ {pkts_now} packets captured, {len(all_devs)} devices, "
                          f"{len(drone_devs)} potential drones[/green]")

    except KeyboardInterrupt:
        pass
    finally:
        await system.stop()
        con.print(f"\n[bold orange1]═══ DIAG COMPLETE ═══[/bold orange1]")
        con.print(f"Total packets : [cyan]{system._total_packets}[/cyan]")
        con.print(f"Total devices : [cyan]{len(system.device_table.get_all_devices())}[/cyan]")
        con.print(f"Drones found  : [cyan]{len(system.device_table.get_drone_devices())}[/cyan]")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Default to web mode if no subcommand given
    if len(sys.argv) == 1:
        sys.argv.append("web")
    cli(obj={})
