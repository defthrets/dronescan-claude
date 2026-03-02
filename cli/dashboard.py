"""
cli/dashboard.py
Curses-based full-screen terminal dashboard.
Press Q or ESC to quit.
"""
from __future__ import annotations

import curses
import time
from typing import Callable, List

from rf_engine.device_table import DroneDevice

# Color pair indices
_AMBER   = 1
_RED     = 2
_GREEN   = 3
_CYAN    = 4
_DIM     = 5
_YELLOW  = 6


def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_AMBER,  curses.COLOR_YELLOW,  -1)
    curses.init_pair(_RED,    curses.COLOR_RED,     -1)
    curses.init_pair(_GREEN,  curses.COLOR_GREEN,   -1)
    curses.init_pair(_CYAN,   curses.COLOR_CYAN,    -1)
    curses.init_pair(_DIM,    curses.COLOR_WHITE,   -1)
    curses.init_pair(_YELLOW, curses.COLOR_YELLOW,  -1)


def _rssi_bar(rssi: int, width: int = 12) -> str:
    pct    = max(0, min(100, (rssi + 100) * 100 // 70))
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def _time_ago(ts: float) -> str:
    s = int(time.time() - ts)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s // 60}m"
    return f"{s // 3600}h"


def _safe_addstr(stdscr, y: int, x: int, text: str, attr=0):
    try:
        h, w = stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0:
            return
        clipped = text[: max(0, w - x - 1)]
        if clipped:
            stdscr.addstr(y, x, clipped, attr)
    except curses.error:
        pass


class CursesDashboard:
    """Full-screen curses dashboard for drone detection."""

    def __init__(self):
        self._stats: dict = {}
        self._running = True

    def update_stats(self, **kwargs):
        self._stats.update(kwargs)

    def run(self, get_devices: Callable[[], List[DroneDevice]]):
        """Blocking call — runs until user presses Q or ESC."""
        curses.wrapper(self._main, get_devices)

    # ── internals ────────────────────────────────────────────────────────────

    def _main(self, stdscr, get_devices: Callable):
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(500)
        _init_colors()

        while self._running:
            key = stdscr.getch()
            if key in (ord('q'), ord('Q'), 27):   # Q / ESC
                break

            devices = get_devices()
            self._draw(stdscr, devices)

    def _draw(self, stdscr, devices: List[DroneDevice]):
        stdscr.erase()
        H, W = stdscr.getmaxyx()

        amber      = curses.color_pair(_AMBER)  | curses.A_BOLD
        amber_dim  = curses.color_pair(_AMBER)
        red        = curses.color_pair(_RED)    | curses.A_BOLD
        green      = curses.color_pair(_GREEN)
        cyan       = curses.color_pair(_CYAN)
        dim        = curses.color_pair(_DIM)

        # ── Title ────────────────────────────────────────────────────────────
        title = "◈  DRONE DETECT v1.0  —  RF MONITORING SYSTEM  ◈"
        _safe_addstr(stdscr, 0, max(0, (W - len(title)) // 2), title, amber)
        _safe_addstr(stdscr, 1, 0, "═" * W, amber_dim)

        # ── Status line ───────────────────────────────────────────────────────
        up = int(self._stats.get("uptime", 0))
        h, rem = divmod(up, 3600)
        m, s   = divmod(rem, 60)
        status = (
            f" IFACE: {self._stats.get('interface', '—')}"
            f"  UP: {h:02d}:{m:02d}:{s:02d}"
            f"  DEVICES: {self._stats.get('total_devices', 0)}"
            f"  DRONES: {self._stats.get('drone_devices', 0)}"
            f"  CH: {self._stats.get('current_channel', 'hop')}"
            f"  PKTS: {self._stats.get('total_packets', 0):,}"
        )
        _safe_addstr(stdscr, 2, 0, status, amber_dim)
        _safe_addstr(stdscr, 3, 0, "─" * W, dim)

        # ── Column headers ────────────────────────────────────────────────────
        cols = [
            ("MAC ADDRESS",       17),
            ("VENDOR / MODEL",    18),
            ("SSID",              18),
            ("CH",                 4),
            ("SIGNAL",            16),
            ("CONFIDENCE",        14),
            ("PKTS",               7),
            ("SEEN",               6),
        ]
        x = 1
        for label, width in cols:
            _safe_addstr(stdscr, 4, x, label[:width].ljust(width), amber | curses.A_UNDERLINE)
            x += width + 1

        _safe_addstr(stdscr, 5, 0, "─" * W, dim)

        # ── Device rows ───────────────────────────────────────────────────────
        sorted_devs = sorted(devices, key=lambda d: (-d.confidence, -d.last_seen))
        row = 6

        for dev in sorted_devs:
            if row >= H - 2:
                break

            label = dev.confidence_label or "NONE"
            if label == "HIGH":
                row_color = red
            elif label == "MEDIUM":
                row_color = amber
            elif label == "LOW":
                row_color = cyan
            else:
                row_color = dim

            bar   = _rssi_bar(dev.rssi)
            ssid  = (dev.ssid or "—")[:17]
            brand = (dev.brand or dev.vendor or "—")[:17]
            conf  = f"{label} {dev.confidence:.0f}%"

            x = 1
            cell_vals = [
                (dev.mac[:17],          17, row_color),
                (brand,                 18, row_color),
                (ssid,                  18, row_color),
                (str(dev.channel or "—"), 4, row_color),
                (bar,                   16, green if dev.rssi >= -65 else amber_dim),
                (conf[:13],             14, row_color),
                (f"{dev.packet_count:,}"[:6], 7, dim),
                (_time_ago(dev.last_seen)[:5], 6, dim),
            ]

            for text, width, color in cell_vals:
                _safe_addstr(stdscr, row, x, text.ljust(width), color)
                x += width + 1

            row += 1

        # ── Footer ────────────────────────────────────────────────────────────
        footer = " [ Q ] Quit  [ R ] Refresh  [ E ] Export JSON "
        _safe_addstr(stdscr, H - 1, 0, "─" * W, dim)
        _safe_addstr(stdscr, H - 1, max(0, (W - len(footer)) // 2), footer, amber_dim)

        stdscr.refresh()
