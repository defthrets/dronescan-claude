"""
cli/display.py
Rich-based live terminal display — Fallout terminal aesthetic.
"""
from __future__ import annotations

import time
from typing import List, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from rf_engine.device_table import DroneDevice


console = Console()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_CONF_STYLES = {
    "HIGH":   "bold bright_red",
    "MEDIUM": "bold yellow",
    "LOW":    "bold cyan",
    "NONE":   "dim",
}


def rssi_bar(rssi: int, width: int = 12) -> str:
    pct    = max(0, min(100, (rssi + 100) * 100 // 70))
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def _time_ago(ts: float) -> str:
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"


# ─────────────────────────────────────────────────────────────────────────────
# Header (ASCII art)
# ─────────────────────────────────────────────────────────────────────────────

ASCII_HEADER = """\
 ██████╗ ██████╗  ██████╗ ███╗   ██╗███████╗    ██████╗ ███████╗████████╗███████╗ ██████╗████████╗
 ██╔══██╗██╔══██╗██╔═══██╗████╗  ██║██╔════╝    ██╔══██╗██╔════╝╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝
 ██║  ██║██████╔╝██║   ██║██╔██╗ ██║█████╗      ██║  ██║█████╗     ██║   █████╗  ██║        ██║
 ██║  ██║██╔══██╗██║   ██║██║╚██╗██║██╔══╝      ██║  ██║██╔══╝     ██║   ██╔══╝  ██║        ██║
 ██████╔╝██║  ██║╚██████╔╝██║ ╚████║███████╗    ██████╔╝███████╗   ██║   ███████╗╚██████╗   ██║
 ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝   ╚═════╝ ╚══════╝   ╚═╝   ╚══════╝ ╚═════╝   ╚═╝"""


def make_header() -> Text:
    t = Text(ASCII_HEADER + "\n", style="orange1", justify="center")
    return t


# ─────────────────────────────────────────────────────────────────────────────
# Status bar
# ─────────────────────────────────────────────────────────────────────────────

def make_status(stats: dict) -> Panel:
    up = int(stats.get("uptime", 0))
    h, rem = divmod(up, 3600)
    m, s = divmod(rem, 60)

    content = (
        f"[orange1]IFACE:[/orange1] {stats.get('interface', '—')}  "
        f"[orange1]UPTIME:[/orange1] {h:02d}:{m:02d}:{s:02d}  "
        f"[orange1]DEVICES:[/orange1] {stats.get('total_devices', 0)}  "
        f"[orange1]DRONES:[/orange1] [bold red]{stats.get('drone_devices', 0)}[/bold red]  "
        f"[orange1]CHANNEL:[/orange1] {stats.get('current_channel', 'hopping')}  "
        f"[orange1]PACKETS:[/orange1] {stats.get('total_packets', 0):,}"
    )
    return Panel(content, border_style="orange1", padding=(0, 1))


# ─────────────────────────────────────────────────────────────────────────────
# Device table
# ─────────────────────────────────────────────────────────────────────────────

def make_device_table(devices: List[DroneDevice]) -> Table:
    tbl = Table(
        title="[bold orange1]◈ DETECTED DEVICES ◈[/bold orange1]",
        style="orange1",
        border_style="dark_orange",
        header_style="bold orange1",
        show_header=True,
        expand=True,
        highlight=True,
    )

    tbl.add_column("MAC ADDRESS",    style="cyan",   min_width=17, no_wrap=True)
    tbl.add_column("VENDOR / MODEL", style="white",  min_width=18, no_wrap=True)
    tbl.add_column("SSID",           style="yellow", min_width=18, no_wrap=True)
    tbl.add_column("CH",             style="cyan",   min_width=4,  justify="right")
    tbl.add_column("RSSI",           min_width=22)
    tbl.add_column("CONFIDENCE",     min_width=14)
    tbl.add_column("PKTS",           style="dim",    min_width=6,  justify="right")
    tbl.add_column("LAST SEEN",      style="dim",    min_width=8,  justify="right")

    sorted_devs = sorted(devices, key=lambda d: (-d.confidence, -d.last_seen))

    for dev in sorted_devs:
        label  = dev.confidence_label or "NONE"
        style  = _CONF_STYLES.get(label, "dim")
        badge  = f"[{style}]{label} {dev.confidence:.0f}%[/{style}]"

        bar_color = "green" if dev.rssi >= -65 else "yellow" if dev.rssi >= -80 else "red"
        rssi_cell = (
            f"[{bar_color}]{rssi_bar(dev.rssi)}[/{bar_color}] "
            f"[dim]{dev.rssi}[/dim]"
        )

        ssid = (dev.ssid or "—")[:18]
        vendor = (dev.brand or dev.vendor or "—")[:18]

        tbl.add_row(
            dev.mac,
            vendor,
            ssid,
            str(dev.channel) if dev.channel else "—",
            rssi_cell,
            badge,
            f"{dev.packet_count:,}",
            _time_ago(dev.last_seen),
        )

    return tbl


# ─────────────────────────────────────────────────────────────────────────────
# Live display manager
# ─────────────────────────────────────────────────────────────────────────────

class CLIDisplay:

    def __init__(self):
        self._stats: dict = {}
        self._live: Optional[Live] = None

    def update_stats(self, **kwargs):
        self._stats.update(kwargs)

    def render(self, devices: List[DroneDevice]) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(make_header(),            name="header",  size=9),
            Layout(make_status(self._stats), name="status",  size=3),
            Layout(make_device_table(devices), name="table"),
        )
        return layout

    def start_live(self) -> Live:
        self._live = Live(
            console=console,
            refresh_per_second=2,
            screen=True,
        )
        self._live.start()
        return self._live

    def update_live(self, devices: List[DroneDevice]):
        if self._live:
            self._live.update(self.render(devices))

    def stop_live(self):
        if self._live:
            self._live.stop()
