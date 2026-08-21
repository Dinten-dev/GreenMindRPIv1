"""Gateway configuration via pydantic-settings.

All values come from environment variables or the .env file.
Hardware ID is auto-detected from the Raspberry Pi serial number.
"""

import logging
import os
import re

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.cloud_url import validate_cloud_url

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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # Cloud backend
    allow_insecure_cloud_http: bool = False
    cloud_api_url: str = "https://green-mind.ch/api/v1"
    firmware_api_url: str = "https://green-mind.ch/api/v1"
    backend_host: str = "green-mind.ch"
    backend_port: int = Field(default=443, ge=1, le=65535)

    # Intervals (seconds)
    upload_interval: int = Field(default=1, ge=1, le=3600)
    heartbeat_interval: int = Field(default=60, ge=5, le=86400)

    # Persistence
    db_path: str = "/opt/greenmind/data/queue.db"
    secrets_path: str = "/opt/greenmind/data/secrets.json"
    ota_db_path: str = "/opt/greenmind/data/ota.db"
    firmware_dir: str = "/opt/greenmind/data/firmware"

    # Logging
    log_dir: str = "/opt/greenmind/data/logs"
    log_level: str = "INFO"

    # Queue limits
    max_queue_size: int = Field(default=100_000, ge=100, le=10_000_000)

    # Local HTTP ingress limits. Sensor packets are deliberately bounded before
    # FastAPI parses JSON so malformed clients cannot allocate memory without
    # limit. The production firmware sends 190/380 readings at 380 Hz.
    max_request_body_bytes: int = Field(default=256 * 1024, ge=1024, le=4 * 1024 * 1024)
    max_http_concurrency: int = Field(default=128, ge=8, le=4096)
    http_keepalive_seconds: int = Field(default=5, ge=1, le=60)
    max_samples_per_batch: int = Field(default=760, ge=1, le=10_000)
    allowed_sample_rates: tuple[int, ...] = (380,)
    max_sensor_ip_entries: int = Field(default=1024, ge=16, le=100_000)
    sensor_value_min_mv: float = Field(default=-1000.0, ge=-100_000.0, le=0.0)
    sensor_value_max_mv: float = Field(default=5000.0, ge=1.0, le=100_000.0)

    # WAV archival
    wav_dir: str = "/opt/greenmind/data/wav"
    wav_chunk_minutes: int = Field(default=10, ge=1, le=1440)
    wav_max_open_writers: int = Field(default=64, ge=1, le=4096)
    wav_flush_interval_seconds: int = Field(default=5, ge=1, le=300)
    wav_min_free_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    wav_max_pending_files: int = Field(default=10_000, ge=1)
    wav_max_pending_bytes: int = Field(default=20 * 1024 * 1024 * 1024, ge=1024)
    wav_warn_pending_age_hours: int = Field(default=72, ge=1)

    # The vendored BLE implementation is retained for compatibility, but is not
    # production-ready and therefore requires an explicit operator opt-in.
    enable_ble_provisioning: bool = False

    # Retained compatibility proxy. It bypasses durable local WAV/SQLite
    # buffering, so production keeps it off unless an operator explicitly
    # accepts that weaker delivery path.
    enable_experimental_biosignal_proxy: bool = False

    # Local OTA Server
    ota_port: int = Field(default=8080, ge=1, le=65535)

    # Hardware (auto-detected, overridable)
    hardware_id: str = ""

    @field_validator("allowed_sample_rates")
    @classmethod
    def _validate_sample_rates(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        unique = tuple(dict.fromkeys(values))
        if not unique or any(value < 1 or value > 20_000 for value in unique):
            raise ValueError("allowed_sample_rates must contain 1..20000 Hz values")
        return unique

    @field_validator("hardware_id")
    @classmethod
    def _validate_hardware_id(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value):
            raise ValueError("hardware_id contains unsafe characters")
        return value

    @model_validator(mode="after")
    def _validate_cloud_urls(self) -> "Settings":
        self.cloud_api_url = validate_cloud_url(
            self.cloud_api_url,
            allow_insecure_loopback=self.allow_insecure_cloud_http,
        )
        self.firmware_api_url = validate_cloud_url(
            self.firmware_api_url,
            allow_insecure_loopback=self.allow_insecure_cloud_http,
        )
        return self

    def model_post_init(self, __context) -> None:
        if not self.hardware_id:
            self.hardware_id = _read_hardware_id()


settings = Settings()
