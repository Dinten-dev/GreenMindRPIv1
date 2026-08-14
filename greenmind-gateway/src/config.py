"""Gateway configuration via pydantic-settings.

All values come from environment variables or the .env file.
Hardware ID is auto-detected from the Raspberry Pi serial number.
"""

import logging
import os

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _read_hardware_id() -> str:
    """Read the Raspberry Pi serial number from the device tree.

    Falls back to a placeholder on non-Pi systems (e.g. during development).
    """
    serial_path = "/sys/firmware/devicetree/base/serial-number"
    try:
        if os.path.exists(serial_path):
            with open(serial_path, "r") as f:
                serial = f.read().strip().rstrip("\x00")
                if serial:
                    return serial
    except OSError as exc:
        logger.warning("Could not read hardware serial: %s", exc)

    # Fallback for dev machines
    import uuid

    fallback = f"dev-{uuid.getnode():012x}"
    logger.info("Using fallback hardware ID: %s", fallback)
    return fallback


class Settings(BaseSettings):
    """Central configuration for the gateway service."""

    # Cloud backend
    cloud_api_url: str = "https://green-mind.ch/api/v1"
    firmware_api_url: str = "https://green-mind.ch/api/v1"
    backend_host: str = "green-mind.ch"
    backend_port: int = 443

    # Intervals (seconds)
    upload_interval: int = 1
    heartbeat_interval: int = 60

    # Persistence
    db_path: str = "/opt/greenmind/data/queue.db"
    secrets_path: str = "/opt/greenmind/data/secrets.json"
    ota_db_path: str = "/opt/greenmind/data/ota.db"
    firmware_dir: str = "/opt/greenmind/data/firmware"

    # Logging
    log_dir: str = "/opt/greenmind/data/logs"
    log_level: str = "INFO"

    # Queue limits
    max_queue_size: int = 100_000

    # WAV archival
    wav_dir: str = "/opt/greenmind/data/wav"
    wav_chunk_minutes: int = 10

    # Local OTA Server
    ota_port: int = 8080

    # Hardware (auto-detected, overridable)
    hardware_id: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def model_post_init(self, __context) -> None:
        if not self.hardware_id:
            self.hardware_id = _read_hardware_id()


settings = Settings()
