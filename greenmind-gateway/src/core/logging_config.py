"""Rotating log configuration for the GreenMind Gateway.

Logs to both the systemd journal (stdout) and to rotating files under LOG_DIR.
A custom filter redacts API keys and passwords from log output.
"""

import logging
import os
import re
from logging.handlers import RotatingFileHandler

# Patterns that must never appear in logs
_REDACT_PATTERNS = [
    re.compile(r"(api[_-]?key\s*[:=]\s*)['\"]?[\w\-]+['\"]?", re.IGNORECASE),
    re.compile(r"(password\s*[:=]\s*)['\"]?[^\s,'\"}]+['\"]?", re.IGNORECASE),
    re.compile(r"(X-Api-Key\s*[:=]\s*)['\"]?[\w\-]+['\"]?", re.IGNORECASE),
]


class RedactFilter(logging.Filter):
    """Replaces sensitive values with ***REDACTED*** in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pattern in _REDACT_PATTERNS:
                record.msg = pattern.sub(r"\1***REDACTED***", record.msg)
        if record.args:
            args = list(record.args) if isinstance(record.args, tuple) else record.args
            if isinstance(args, list):
                for i, arg in enumerate(args):
                    if isinstance(arg, str):
                        for pattern in _REDACT_PATTERNS:
                            args[i] = pattern.sub(r"\1***REDACTED***", arg)
                record.args = tuple(args)
        return True


def setup_logging(log_dir: str = "/opt/greenmind/data/logs", level: str = "INFO") -> None:
    """Initialise root logger with console + rotating file handlers."""
    os.makedirs(log_dir, exist_ok=True)

    log_format = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Prevent duplicate handlers on re-init
    if root.handlers:
        return

    redact = RedactFilter()

    # Console handler (systemd journal captures stdout)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    console.addFilter(redact)
    root.addHandler(console)

    # Rotating file handler: 5 MB per file, 3 backups
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "gateway.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    file_handler.addFilter(redact)
    root.addHandler(file_handler)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
