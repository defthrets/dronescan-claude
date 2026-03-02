import logging
import logging.handlers
from pathlib import Path


def setup_logging(
    level: str = "INFO",
    log_file: str = "drone_detect.log",
    max_bytes: int = 10_485_760,
    backup_count: int = 3,
) -> logging.Logger:
    """Configure application-wide logging with rotating file + console handlers."""

    log_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("drone_detect")
    root.setLevel(log_level)

    # Avoid duplicate handlers if called more than once
    if root.handlers:
        return root

    # Console handler (stderr)
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    # Rotating file handler
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    fh.setLevel(log_level)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    return root
