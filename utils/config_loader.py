from __future__ import annotations

import yaml
from pathlib import Path
from typing import Any, Dict

# Sensible defaults — merged with any user config file
_DEFAULTS: Dict[str, Any] = {
    "interface": "wlan0mon",
    "channels": {
        "bands_2_4ghz": [1, 6, 11],
        "bands_5ghz": [36, 40, 44, 48, 149, 153, 157, 161],
        "hop_interval": 0.5,
        "fixed_channel": None,
    },
    "confidence_weights": {
        "oui_match": 40,
        "ssid_match": 30,
        "channel_match": 15,
        "traffic_behavior": 15,
    },
    "thresholds": {"low": 30, "medium": 60, "high": 80},
    "gps": {
        "enabled": False,
        "port": "/dev/ttyUSB0",
        "baud_rate": 9600,
        "timeout": 5.0,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "map_provider": "openstreetmap",
        "google_maps_api_key": "",
    },
    "pcap": {
        "enabled": False,
        "output_dir": "./pcap_recordings",
        "rotate_size_mb": 100,
    },
    "logging": {
        "level": "INFO",
        "file": "drone_detect.log",
        "max_bytes": 10_485_760,
        "backup_count": 3,
    },
    "theme": {
        "style": "fallout",
        "primary_color": "#FF8C00",
        "scanlines": True,
        "flicker": True,
        "flicker_intensity": 0.02,
    },
    "tracking": {
        "device_timeout": 300,
        "history_length": 100,
        "min_packets_for_traffic_analysis": 10,
    },
}


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str = "config/config.yaml") -> Dict[str, Any]:
    """Load config YAML, merging with built-in defaults."""
    config = _DEFAULTS.copy()

    path = Path(config_path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        config = _deep_merge(config, user_cfg)
    else:
        import warnings
        warnings.warn(f"Config file not found at {config_path!r}. Using defaults.")

    return config
