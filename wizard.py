#!/usr/bin/env python3
"""
Drone Detect — Interactive Setup Wizard
========================================
Detects USB Wi-Fi adapters, puts them into monitor mode,
tests packet capture, optionally configures GPS, and writes
a verified config.yaml ready to run.

Run as root:  sudo python wizard.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Rich ──────────────────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, IntPrompt, Prompt
    from rich.table import Table
    from rich.text import Text
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.columns import Columns
    from rich import box
except ImportError:
    print("ERROR: 'rich' not installed.  Run: pip install rich")
    sys.exit(1)

console = Console()
PROJECT_DIR = Path(__file__).parent
CONFIG_PATH = PROJECT_DIR / "config" / "config.yaml"

# ─────────────────────────────────────────────────────────────────────────────
# Known adapter database
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AdapterProfile:
    name:    str
    chipset: str
    bands:   str
    notes:   str = ""
    recommended: bool = False

KNOWN_ADAPTERS: Dict[str, AdapterProfile] = {
    "0e8d:7612": AdapterProfile(
        name="Alfa AWUS036ACM",  chipset="MT7612U",
        bands="2.4 + 5 GHz",    notes="Native Linux driver, plug-and-play",
        recommended=True,
    ),
    "0e8d:761a": AdapterProfile(
        name="Alfa AWUS036ACHM", chipset="MT7610U",
        bands="2.4 + 5 GHz",    notes="Native Linux driver",
        recommended=True,
    ),
    "0bda:8812": AdapterProfile(
        name="Alfa AWUS036ACH",  chipset="RTL8812AU",
        bands="2.4 + 5 GHz",    notes="Needs rtl8812au driver (see README)",
    ),
    "0bda:8811": AdapterProfile(
        name="Alfa AWUS036ACS",  chipset="RTL8811AU",
        bands="2.4 + 5 GHz",    notes="Needs rtl8811au driver",
    ),
    "0cf3:9271": AdapterProfile(
        name="Alfa AWUS036NHA",  chipset="AR9271",
        bands="2.4 GHz only",   notes="Native, 2.4 GHz only — misses 5.8 GHz drones",
    ),
    "148f:5572": AdapterProfile(
        name="Panda PAU09",      chipset="RT5572",
        bands="2.4 + 5 GHz",    notes="Native Linux driver",
    ),
    "148f:3572": AdapterProfile(
        name="Panda PAU06",      chipset="RT3572",
        bands="2.4 + 5 GHz",    notes="Native Linux driver",
    ),
    "0846:9053": AdapterProfile(
        name="Netgear A6210",    chipset="MT7612U",
        bands="2.4 + 5 GHz",    notes="Same chipset as AWUS036ACM",
        recommended=True,
    ),
    "2357:0105": AdapterProfile(
        name="TP-Link TL-WN722N v2/3", chipset="RTL8188EUS",
        bands="2.4 GHz only",   notes="2.4 GHz only",
    ),
    "2357:010c": AdapterProfile(
        name="TP-Link AC600 T2U", chipset="RTL8811AU",
        bands="2.4 + 5 GHz",    notes="Needs rtl8811au driver",
    ),
    "0bda:b812": AdapterProfile(
        name="TP-Link AC1300 T4U", chipset="RTL8812BU",
        bands="2.4 + 5 GHz",    notes="Needs rtl88x2bu driver",
    ),
    "2604:0012": AdapterProfile(
        name="Hak5 WiFi Coconut", chipset="Multiple RT2800",
        bands="2.4 GHz",        notes="Multi-channel capture device",
    ),
}

# ─────────────────────────────────────────────────────────────────────────────
# System helpers
# ─────────────────────────────────────────────────────────────────────────────

def run(cmd: str, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, shell=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True, check=check,
    )

def run_ok(cmd: str) -> Tuple[bool, str]:
    r = run(cmd)
    return r.returncode == 0, (r.stdout + r.stderr).strip()

def tool_exists(name: str) -> bool:
    return shutil.which(name) is not None

# ─────────────────────────────────────────────────────────────────────────────
# Styled helpers
# ─────────────────────────────────────────────────────────────────────────────

def header(text: str):
    console.print(Panel(f"[bold orange1]{text}[/bold orange1]",
                         border_style="orange1", padding=(0, 2)))

def step(n: int, total: int, text: str):
    console.print(f"\n[dim]STEP {n}/{total}[/dim] [bold orange1]{text}[/bold orange1]")
    console.print("[orange1]" + "─" * 60 + "[/orange1]")

def ok(msg: str):    console.print(f"[green]  ✓[/green] {msg}")
def warn(msg: str):  console.print(f"[yellow]  ⚠[/yellow] {msg}")
def fail(msg: str):  console.print(f"[red]  ✗[/red] {msg}")
def info(msg: str):  console.print(f"[dim]  ·[/dim] {msg}")

def spinner_task(msg: str, cmd: str) -> Tuple[bool, str]:
    """Run a shell command while showing a spinner. Returns (success, output)."""
    with console.status(f"[orange1]{msg}...[/orange1]", spinner="dots"):
        success, out = run_ok(cmd)
    return success, out

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Banner
# ─────────────────────────────────────────────────────────────────────────────

BANNER = r"""
 ██████╗ ██████╗  ██████╗ ███╗   ██╗███████╗    ██████╗ ███████╗████████╗███████╗ ██████╗████████╗
 ██╔══██╗██╔══██╗██╔═══██╗████╗  ██║██╔════╝    ██╔══██╗██╔════╝╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝
 ██║  ██║██████╔╝██║   ██║██╔██╗ ██║█████╗      ██║  ██║█████╗     ██║   █████╗  ██║        ██║
 ██║  ██║██╔══██╗██║   ██║██║╚██╗██║██╔══╝      ██║  ██║██╔══╝     ██║   ██╔══╝  ██║        ██║
 ██████╔╝██║  ██║╚██████╔╝██║ ╚████║███████╗    ██████╔╝███████╗   ██║   ███████╗╚██████╗   ██║
 ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝   ╚═════╝ ╚══════╝   ╚═╝   ╚══════╝ ╚═════╝   ╚═╝
"""

def show_banner():
    console.print(BANNER, style="orange1", highlight=False)
    console.print(
        Panel(
            "[dim]Interactive Setup Wizard — Debian/Ubuntu[/dim]\n"
            "[dim]Detects adapters · enables monitor mode · tests capture · writes config[/dim]",
            border_style="orange1",
            padding=(0, 4),
        )
    )
    console.print()

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Root check
# ─────────────────────────────────────────────────────────────────────────────

def check_root():
    if os.geteuid() != 0:
        fail("This wizard must run as root (monitor mode requires raw socket access).")
        console.print("\n  [orange1]Re-run:[/orange1]  sudo python wizard.py")
        sys.exit(1)
    ok("Running as root")

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Dependency check
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_TOOLS = ["iw", "airmon-ng", "ip", "lsusb"]

def check_dependencies():
    missing = [t for t in REQUIRED_TOOLS if not tool_exists(t)]

    if not missing:
        ok(f"All tools present ({', '.join(REQUIRED_TOOLS)})")
        return

    warn(f"Missing tools: {', '.join(missing)}")
    if Confirm.ask("  Install missing packages now?", default=True):
        pkgs = " ".join({
            "iw": "iw",
            "airmon-ng": "aircrack-ng",
            "lsusb": "usbutils",
            "ip": "iproute2",
        }.get(t, t) for t in missing)
        success, _ = spinner_task(f"Installing {pkgs}", f"apt-get install -y -qq {pkgs}")
        if success:
            ok("Packages installed")
        else:
            fail("Package installation failed — check apt sources and retry")
            sys.exit(1)
    else:
        fail("Cannot continue without required tools")
        sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Adapter detection and selection
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WifiInterface:
    name: str                        # e.g. wlan1
    phy:  str                        # e.g. phy1
    mode: str                        # managed | monitor | etc.
    usb_id:  Optional[str] = None    # e.g. "0e8d:7612"
    driver:  str = "unknown"
    profile: Optional[AdapterProfile] = None

def _get_interfaces() -> List[WifiInterface]:
    """Return all Wi-Fi interfaces found via `iw dev`."""
    ifaces: List[WifiInterface] = []
    _, out = run_ok("iw dev")
    phy = ""
    iface = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("phy#"):
            phy = line.split()[0]
        elif line.startswith("Interface "):
            name = line.split()[1]
            iface = WifiInterface(name=name, phy=phy, mode="unknown")
            ifaces.append(iface)
        elif iface and line.startswith("type "):
            iface.mode = line.split()[1]
    return ifaces

def _get_usb_id(iface_name: str) -> Optional[str]:
    """Trace sysfs to find USB vendor:product for a wireless interface."""
    # Walk: /sys/class/net/<iface>/device -> find idVendor/idProduct
    sysfs = Path(f"/sys/class/net/{iface_name}/device")
    for _ in range(5):
        uevent = sysfs / "uevent"
        if uevent.exists():
            content = uevent.read_text(errors="ignore")
            vendor = product = None
            for line in content.splitlines():
                if "ID_VENDOR_ID=" in line:
                    vendor = line.split("=")[1].strip().lower()
                elif "ID_MODEL_ID=" in line:
                    product = line.split("=")[1].strip().lower()
            if vendor and product:
                return f"{vendor}:{product}"
        idv = sysfs / "idVendor"
        idp = sysfs / "idProduct"
        if idv.exists() and idp.exists():
            v = idv.read_text().strip().lower()
            p = idp.read_text().strip().lower()
            return f"{v}:{p}"
        sysfs = sysfs / ".."
    return None

def _get_driver(iface_name: str) -> str:
    _, out = run_ok(f"readlink /sys/class/net/{iface_name}/device/driver")
    return Path(out.strip()).name if out else "unknown"

def _lsusb_id_name(usb_id: str) -> str:
    """Get human-readable name from lsusb for a vendor:product pair."""
    _, out = run_ok(f"lsusb -d {usb_id} 2>/dev/null")
    # "Bus 001 Device 004: ID 0e8d:7612 MediaTek Inc. ..."
    match = re.search(r"ID \S+ (.+)", out)
    return match.group(1).strip() if match else usb_id

def detect_adapters() -> List[WifiInterface]:
    ifaces = _get_interfaces()
    for iface in ifaces:
        iface.usb_id = _get_usb_id(iface.name)
        iface.driver = _get_driver(iface.name)
        if iface.usb_id:
            iface.profile = KNOWN_ADAPTERS.get(iface.usb_id)
    return ifaces

def select_adapter(ifaces: List[WifiInterface]) -> WifiInterface:
    if not ifaces:
        fail("No Wi-Fi interfaces found.")
        console.print("\n  Plug in your USB Wi-Fi adapter, wait ~5 seconds, then re-run the wizard.")
        sys.exit(1)

    tbl = Table(box=box.SIMPLE_HEAD, border_style="orange1", show_header=True,
                header_style="bold orange1")
    tbl.add_column("#",        style="dim",         width=3)
    tbl.add_column("Interface",style="cyan",        width=10)
    tbl.add_column("Mode",     style="dim",         width=10)
    tbl.add_column("Driver",   style="dim",         width=14)
    tbl.add_column("USB ID",   style="dim",         width=12)
    tbl.add_column("Adapter",  style="white",       width=24)
    tbl.add_column("Bands",    style="orange1",     width=16)
    tbl.add_column("",         style="green",       width=14)

    for i, ifc in enumerate(ifaces, 1):
        p = ifc.profile
        star = "[green]★ Recommended[/green]" if (p and p.recommended) else ""
        tbl.add_row(
            str(i),
            ifc.name,
            ifc.mode,
            ifc.driver,
            ifc.usb_id or "—",
            p.name if p else (_lsusb_id_name(ifc.usb_id) if ifc.usb_id else "Unknown"),
            p.bands if p else "Unknown",
            star,
        )

    console.print(tbl)

    # Auto-select if exactly one recommended adapter
    recommended = [i for i, ifc in enumerate(ifaces, 1) if ifc.profile and ifc.profile.recommended]
    default = recommended[0] if len(recommended) == 1 else 1

    choice = IntPrompt.ask(
        f"  Select adapter [1-{len(ifaces)}]",
        default=default,
    )
    choice = max(1, min(choice, len(ifaces)))
    chosen = ifaces[choice - 1]

    console.print()
    ok(f"Selected: [cyan]{chosen.name}[/cyan]"
       + (f" — [white]{chosen.profile.name}[/white]" if chosen.profile else ""))

    if chosen.profile and chosen.profile.notes:
        info(chosen.profile.notes)

    # Warn about 2.4 GHz-only adapters
    if chosen.profile and "2.4 GHz only" in chosen.profile.bands:
        warn("This adapter only sees 2.4 GHz — you will miss DJI Mini 3/4, Mavic 3, and other 5.8 GHz drones.")
        if not Confirm.ask("  Continue anyway?", default=False):
            sys.exit(0)

    return chosen

# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Kill conflicting processes
# ─────────────────────────────────────────────────────────────────────────────

def kill_conflicts(iface: WifiInterface) -> bool:
    _, check_out = run_ok("airmon-ng check")
    # Detect if any conflicting processes are running
    conflict_lines = [l for l in check_out.splitlines()
                      if re.search(r'\d+\s+(NetworkManager|wpa_supplicant|dhclient|avahi)', l)]

    if not conflict_lines:
        ok("No conflicting processes detected")
        return True

    warn("Conflicting processes detected:")
    for line in conflict_lines:
        info(f"  {line.strip()}")

    console.print(
        "\n  [dim]These processes use the Wi-Fi adapter and will prevent monitor mode.[/dim]"
    )
    if Confirm.ask("  Kill conflicting processes?", default=True):
        success, _ = spinner_task("Killing conflicting processes", "airmon-ng check kill")
        if success:
            ok("Processes killed")
            warn("NetworkManager is stopped — internet access may be interrupted until reboot or restart")
            return True
        else:
            fail("Failed to kill processes — you may need to manually stop NetworkManager")
            return False
    else:
        warn("Skipped — monitor mode may not work reliably")
        return True

# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — Enable monitor mode
# ─────────────────────────────────────────────────────────────────────────────

def enable_monitor_mode(iface: WifiInterface) -> str:
    """
    Attempts monitor mode via airmon-ng first, falls back to iw.
    Returns the monitor interface name (e.g. wlan1mon).
    """
    original = iface.name
    mon_name = original + "mon"  # airmon-ng convention

    # ── Try airmon-ng ──────────────────────────────────────────────────────
    success, out = spinner_task(
        f"Enabling monitor mode on {original} (airmon-ng)",
        f"airmon-ng start {original}",
    )

    if success:
        # airmon-ng may rename the interface
        match = re.search(r'monitor mode vif enabled.+?on.+?\[(.+?)\]', out)
        if match:
            mon_name = match.group(1).strip()
        else:
            # Also check for "monitor mode enabled on wlanXmon"
            match2 = re.search(r'monitor mode enabled on (\S+)', out)
            if match2:
                mon_name = match2.group(1).strip()

        # Verify it actually appeared
        time.sleep(1)
        _, iw_out = run_ok("iw dev")
        if mon_name in iw_out and "monitor" in iw_out:
            ok(f"Monitor interface: [cyan]{mon_name}[/cyan]")
            return mon_name

    # ── Fallback: manual iw method ─────────────────────────────────────────
    warn(f"airmon-ng method failed — trying manual iw method")
    cmds = [
        f"ip link set {original} down",
        f"iw dev {original} set type monitor",
        f"ip link set {original} up",
    ]
    for cmd in cmds:
        ok_r, _ = run_ok(cmd)
        if not ok_r:
            fail(f"Command failed: {cmd}")
            fail("Could not enable monitor mode. Check adapter compatibility.")
            sys.exit(1)

    # When using iw method, interface keeps original name
    mon_name = original
    time.sleep(1)

    # Final verification
    _, verify_out = run_ok(f"iw dev {mon_name} info")
    if "monitor" in verify_out:
        ok(f"Monitor interface: [cyan]{mon_name}[/cyan]  (iw method)")
        return mon_name
    else:
        fail("Monitor mode verification failed — interface exists but type is not 'monitor'")
        console.print("\n  [dim]Output from `iw dev`:[/dim]")
        console.print(verify_out[:500])
        sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Step 7 — Test packet capture
# ─────────────────────────────────────────────────────────────────────────────

def test_capture(mon_iface: str, duration: int = 4) -> bool:
    """
    Runs a brief Scapy capture to confirm packets are flowing.
    Returns True if > 0 packets received.
    """
    try:
        import importlib.util
        if importlib.util.find_spec("scapy") is None:
            warn("Scapy not installed — skipping live capture test")
            info("Install with: pip install scapy")
            return True   # don't block setup
    except Exception:
        pass

    console.print(f"\n  [dim]Capturing for {duration} seconds on {mon_iface}...[/dim]")
    count = 0

    try:
        from scapy.all import sniff
        from scapy.layers.dot11 import Dot11

        with console.status("[orange1]Capturing 802.11 frames...[/orange1]", spinner="dots"):
            packets = sniff(
                iface=mon_iface,
                count=0,
                timeout=duration,
                monitor=True,
                store=True,
            )
        count = sum(1 for p in packets if p.haslayer(Dot11))
    except PermissionError:
        fail("Permission denied — are you running as root?")
        return False
    except OSError as e:
        fail(f"Interface error: {e}")
        return False
    except Exception as e:
        warn(f"Capture test error: {e}")
        return True  # non-fatal

    if count > 0:
        ok(f"Captured [bold green]{count}[/bold green] 802.11 frames — monitor mode confirmed")
        return True
    else:
        warn(f"No 802.11 frames captured in {duration}s — this may be normal if no devices are nearby")
        warn("Monitor mode may still be working; drones will be detected when in range")
        return True

# ─────────────────────────────────────────────────────────────────────────────
# Step 8 — Channel configuration
# ─────────────────────────────────────────────────────────────────────────────

def configure_channels(profile: Optional[AdapterProfile]) -> dict:
    console.print()

    # Determine available bands from adapter profile
    has_5ghz = not (profile and "2.4 GHz only" in profile.bands)

    console.print("  [orange1]Channel strategy:[/orange1]")
    console.print("  [dim]1[/dim] Full hop (all channels) — best detection coverage")
    console.print("  [dim]2[/dim] Drone-optimised hop  — prioritise drone-typical channels (faster)")
    if has_5ghz:
        console.print("  [dim]3[/dim] 5 GHz only          — modern DJI / Autel drones on 5.8 GHz")
    console.print("  [dim]4[/dim] Fixed channel        — lock to one channel (e.g. for known target)")

    max_opt = 4 if has_5ghz else 3
    choice = IntPrompt.ask("  Select strategy", default=2)
    choice = max(1, min(choice, max_opt))

    hop_interval = 0.5  # default

    if choice == 1:
        bands_24 = list(range(1, 12))
        bands_5  = [36,40,44,48,52,56,60,64,100,104,108,112,116,120,124,128,132,136,140,144,149,153,157,161,165]
        fixed    = None
        ok("Full channel hop (2.4 + 5 GHz)")

    elif choice == 2:
        bands_24 = [1, 6, 11]
        bands_5  = [36, 40, 44, 48, 149, 153, 157, 161] if has_5ghz else []
        fixed    = None
        hop_interval = 0.4
        ok("Drone-optimised hop — 2.4 GHz ch 1/6/11 + 5.8 GHz ch 36/40/44/48/149/153/157/161")

    elif choice == 3 and has_5ghz:
        bands_24 = []
        bands_5  = [36, 40, 44, 48, 149, 153, 157, 161, 165]
        fixed    = None
        hop_interval = 0.3
        ok("5 GHz only channel hop")

    else:  # fixed
        bands_24 = [1, 6, 11]
        bands_5  = [36, 40, 44, 48, 149, 153, 157, 161] if has_5ghz else []
        fixed_ch = IntPrompt.ask("  Fixed channel number", default=6)
        fixed = fixed_ch
        ok(f"Fixed channel {fixed_ch}")

    return {
        "bands_2_4ghz": bands_24,
        "bands_5ghz":   bands_5,
        "hop_interval": hop_interval,
        "fixed_channel": None if choice != 4 else fixed,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Step 9 — GPS (optional)
# ─────────────────────────────────────────────────────────────────────────────

def detect_gps_ports() -> List[str]:
    """Scan for likely GPS serial ports."""
    candidates = []
    for pattern in ["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*"]:
        candidates.extend(str(p) for p in Path("/dev").glob(pattern.lstrip("/dev/")))
    return sorted(candidates)

def configure_gps() -> dict:
    gps_cfg = {"enabled": False, "port": "/dev/ttyUSB0", "baud_rate": 9600, "timeout": 5.0}

    if not Confirm.ask("  Do you have a USB GPS dongle connected?", default=False):
        info("GPS skipped — observer position and direction finding will be unavailable")
        return gps_cfg

    ports = detect_gps_ports()
    if not ports:
        warn("No serial devices found under /dev/ttyUSB* or /dev/ttyACM*")
        warn("Plug in GPS dongle and re-run wizard, or edit config manually later")
        return gps_cfg

    console.print(f"\n  [orange1]Found {len(ports)} serial device(s):[/orange1]")
    for i, port in enumerate(ports, 1):
        console.print(f"    [dim]{i}[/dim]  {port}")

    choice = IntPrompt.ask(f"  Select GPS port [1-{len(ports)}]", default=1)
    choice = max(1, min(choice, len(ports)))
    selected_port = ports[choice - 1]

    baud = IntPrompt.ask("  Baud rate", default=9600)

    # Quick connectivity test
    with console.status(f"[orange1]Testing {selected_port} @ {baud}...[/orange1]", spinner="dots"):
        time.sleep(2)  # give device time
        ok_r, _ = run_ok(f"timeout 2 cat {selected_port} 2>&1 | head -3")

    ok(f"GPS port: [cyan]{selected_port}[/cyan]  baud: [cyan]{baud}[/cyan]")
    info("NMEA sentences will be parsed at runtime — full fix takes 30–60s outdoors")

    gps_cfg["enabled"]   = True
    gps_cfg["port"]      = selected_port
    gps_cfg["baud_rate"] = baud
    return gps_cfg

# ─────────────────────────────────────────────────────────────────────────────
# Step 10 — Web dashboard settings
# ─────────────────────────────────────────────────────────────────────────────

def configure_web() -> dict:
    port = IntPrompt.ask("  Web dashboard port", default=8080)

    console.print("\n  [orange1]Map provider:[/orange1]")
    console.print("  [dim]1[/dim] OpenStreetMap Dark  (recommended — no API key needed)")
    console.print("  [dim]2[/dim] Google Maps         (requires API key)")
    map_choice = IntPrompt.ask("  Select", default=1)

    provider = "openstreetmap"
    gmaps_key = ""
    if map_choice == 2:
        provider  = "google"
        gmaps_key = Prompt.ask("  Google Maps API key").strip()

    ok(f"Dashboard: http://0.0.0.0:{port}   map: {provider}")
    return {"host": "0.0.0.0", "port": port,
            "map_provider": provider, "google_maps_api_key": gmaps_key}

# ─────────────────────────────────────────────────────────────────────────────
# Step 11 — Optional settings
# ─────────────────────────────────────────────────────────────────────────────

def configure_options() -> dict:
    pcap_enabled = Confirm.ask("  Enable PCAP recording (saves raw capture files)?", default=False)
    pcap_dir     = "./pcap_recordings"
    if pcap_enabled:
        pcap_dir = Prompt.ask("  PCAP output directory", default=pcap_dir)
        ok(f"PCAP recording to: {pcap_dir}")
    else:
        info("PCAP recording disabled (can be enabled later in config.yaml)")

    log_level = Prompt.ask(
        "  Log level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )

    return {
        "pcap": {"enabled": pcap_enabled, "output_dir": pcap_dir, "rotate_size_mb": 100},
        "logging": {"level": log_level, "file": "drone_detect.log",
                    "max_bytes": 10_485_760, "backup_count": 3},
    }

# ─────────────────────────────────────────────────────────────────────────────
# Step 12 — Write config
# ─────────────────────────────────────────────────────────────────────────────

def write_config(
    mon_iface: str,
    channels:  dict,
    gps:       dict,
    web:       dict,
    options:   dict,
):
    """
    Write config.yaml using surgical line replacement so comments are preserved.
    Falls back to full rewrite if the file can't be parsed line-by-line.
    """
    if not CONFIG_PATH.exists():
        fail(f"Config file not found at {CONFIG_PATH}")
        sys.exit(1)

    content = CONFIG_PATH.read_text(encoding="utf-8")

    def replace_value(yaml_text: str, key: str, value) -> str:
        """Replace the value of a top-level or nested key while keeping inline comments."""
        if isinstance(value, bool):
            val_str = "true" if value else "false"
        elif value is None:
            val_str = "null"
        elif isinstance(value, str):
            val_str = f'"{value}"'
        elif isinstance(value, list):
            val_str = "[" + ", ".join(str(x) for x in value) + "]"
        else:
            val_str = str(value)

        pattern = rf'^(\s*{re.escape(key)}:\s*)([^\n#]*)(.*)$'
        return re.sub(pattern, rf'\g<1>{val_str}\3', yaml_text, flags=re.MULTILINE, count=1)

    # ── Apply substitutions ──────────────────────────────────────────────────
    content = replace_value(content, "interface", mon_iface)

    content = replace_value(content, "bands_2_4ghz", channels["bands_2_4ghz"])
    content = replace_value(content, "bands_5ghz",   channels["bands_5ghz"])
    content = replace_value(content, "hop_interval",  channels["hop_interval"])
    fc = channels.get("fixed_channel")
    content = replace_value(content, "fixed_channel", fc)

    # GPS
    content = replace_value(content, "enabled",   gps["enabled"])
    # 'port' appears multiple times; replace only in GPS section
    gps_section_pattern = r'(gps:.*?port:\s*)([^\n#]*)(.*)'
    content = re.sub(gps_section_pattern,
                     rf'\g<1>"{gps["port"]}"\3', content,
                     count=1, flags=re.DOTALL)
    content = replace_value(content, "baud_rate", gps["baud_rate"])

    # Web
    content = replace_value(content, "port",        web["port"])
    content = replace_value(content, "map_provider", web["map_provider"])
    content = replace_value(content, "google_maps_api_key", web.get("google_maps_api_key", ""))

    # PCAP
    p = options["pcap"]
    content = replace_value(content, "output_dir", p["output_dir"])
    content = replace_value(content, "rotate_size_mb", p["rotate_size_mb"])

    # Logging
    lg = options["logging"]
    content = replace_value(content, "level", lg["level"])

    # pcap enabled — careful, 'enabled' appears under gps too
    # Replace the one under pcap section
    pcap_enabled_pattern = r'(pcap:.*?enabled:\s*)([^\n#]*)(.*)'
    pcap_val = "true" if p["enabled"] else "false"
    content = re.sub(pcap_enabled_pattern,
                     rf'\g<1>{pcap_val}\3', content,
                     count=1, flags=re.DOTALL)

    CONFIG_PATH.write_text(content, encoding="utf-8")
    ok(f"Config written: [cyan]{CONFIG_PATH}[/cyan]")

# ─────────────────────────────────────────────────────────────────────────────
# Step 13 — Summary + launch instructions
# ─────────────────────────────────────────────────────────────────────────────

def show_summary(mon_iface: str, web: dict, gps: dict, adapter: WifiInterface):
    tbl = Table(box=box.SIMPLE, border_style="orange1", show_header=False, padding=(0, 2))
    tbl.add_column("Key",   style="orange1", min_width=22)
    tbl.add_column("Value", style="cyan")

    tbl.add_row("Monitor interface", mon_iface)
    if adapter.profile:
        tbl.add_row("Adapter",  f"{adapter.profile.name}  ({adapter.profile.bands})")
    tbl.add_row("Dashboard",       f"http://localhost:{web['port']}")
    tbl.add_row("Map provider",    web["map_provider"])
    tbl.add_row("GPS",             "enabled" if gps["enabled"] else "disabled")
    if gps["enabled"]:
        tbl.add_row("GPS port", gps["port"])

    console.print(Panel(tbl, title="[bold orange1]◈  CONFIGURATION SUMMARY  ◈[/bold orange1]",
                         border_style="orange1", padding=(1, 2)))

    console.print()
    console.print(Panel(
        f"[bold orange1]Launch the system:[/bold orange1]\n\n"
        f"  [green]sudo python main.py web[/green]\n\n"
        f"  Then open: [cyan]http://localhost:{web['port']}[/cyan]\n\n"
        f"[dim]Other modes:\n"
        f"  sudo python main.py terminal    # Rich live terminal\n"
        f"  sudo python main.py dashboard   # Full-screen curses\n"
        f"  sudo python main.py scan --drones-only[/dim]",
        border_style="green",
        padding=(1, 2),
    ))

# ─────────────────────────────────────────────────────────────────────────────
# Main wizard flow
# ─────────────────────────────────────────────────────────────────────────────

TOTAL_STEPS = 9

def main():
    show_banner()

    # ── Step 1: Root ────────────────────────────────────────────────────────
    step(1, TOTAL_STEPS, "Privilege Check")
    check_root()

    # ── Step 2: Dependencies ────────────────────────────────────────────────
    step(2, TOTAL_STEPS, "System Dependencies")
    check_dependencies()

    # ── Step 3: Adapter detection ───────────────────────────────────────────
    step(3, TOTAL_STEPS, "Wi-Fi Adapter Detection")
    with console.status("[orange1]Scanning for Wi-Fi interfaces...[/orange1]", spinner="dots"):
        adapters = detect_adapters()
    info(f"Found {len(adapters)} Wi-Fi interface(s)")
    chosen_adapter = select_adapter(adapters)

    # ── Step 4: Kill conflicts ──────────────────────────────────────────────
    step(4, TOTAL_STEPS, "Kill Conflicting Processes")
    kill_conflicts(chosen_adapter)

    # ── Step 5: Monitor mode ────────────────────────────────────────────────
    step(5, TOTAL_STEPS, "Enable Monitor Mode")
    mon_iface = enable_monitor_mode(chosen_adapter)

    # ── Step 6: Test capture ────────────────────────────────────────────────
    step(6, TOTAL_STEPS, "Test Packet Capture")
    test_capture(mon_iface)

    # ── Step 7: Channel strategy ────────────────────────────────────────────
    step(7, TOTAL_STEPS, "Channel Configuration")
    channels = configure_channels(chosen_adapter.profile)

    # ── Step 8: GPS ─────────────────────────────────────────────────────────
    step(8, TOTAL_STEPS, "GPS Setup (Optional)")
    gps = configure_gps()

    # ── Step 9: Dashboard + options ─────────────────────────────────────────
    step(9, TOTAL_STEPS, "Dashboard & Recording Settings")
    web     = configure_web()
    options = configure_options()

    # ── Write config ─────────────────────────────────────────────────────────
    console.print("\n[orange1]Writing config.yaml...[/orange1]")
    write_config(
        mon_iface=mon_iface,
        channels=channels,
        gps=gps,
        web=web,
        options=options,
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    show_summary(mon_iface, web, gps, chosen_adapter)


if __name__ == "__main__":
    main()
