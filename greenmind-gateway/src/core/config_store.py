"""Manages secure persistence of gateway credentials in secrets.json."""

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class SecretStore:
    """Thread-safe read/write of device credentials on disk (chmod 600)."""

    def __init__(self, filepath: str = "/opt/greenmind/data/secrets.json"):
        self.filepath = filepath
        self._ensure_file()

    def _ensure_file(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
        if not os.path.exists(self.filepath):
            with open(self.filepath, "w") as fh:
                json.dump({}, fh)
            try:
                os.chmod(self.filepath, 0o600)
            except OSError as exc:
                logger.warning("Failed to set chmod 600 on secrets file: %s", exc)

    def load(self) -> dict[str, Any]:
        try:
            with open(self.filepath, "r") as fh:
                return json.load(fh)
        except json.JSONDecodeError:
            logger.error("Corrupt secrets file – starting fresh.")
            return {}
        except FileNotFoundError:
            return {}

    def save(self, data: dict[str, Any]) -> None:
        with open(self.filepath, "w") as fh:
            json.dump(data, fh, indent=4)
        try:
            os.chmod(self.filepath, 0o600)
        except OSError:
            pass

    def is_provisioned(self) -> bool:
        """True if the gateway has a valid API key and gateway ID."""
        data = self.load()
        return bool(data.get("api_key") and data.get("gateway_id"))

    def get_credentials(self) -> dict[str, str] | None:
        """Return credentials dict or None if not provisioned."""
        data = self.load()
        if not self.is_provisioned():
            return None
        return {
            "api_key": data["api_key"],
            "gateway_id": data["gateway_id"],
            "greenhouse_id": data.get("greenhouse_id", ""),
            "hardware_id": data.get("hardware_id", ""),
            "server_url": data.get("server_url", ""),
        }

    def store_credentials(
        self,
        api_key: str,
        gateway_id: str,
        greenhouse_id: str,
        hardware_id: str,
        server_url: str,
    ) -> None:
        """Persist pairing result securely."""
        data = self.load()
        data["api_key"] = api_key
        data["gateway_id"] = gateway_id
        data["greenhouse_id"] = greenhouse_id
        data["hardware_id"] = hardware_id
        data["server_url"] = server_url
        self.save(data)
        logger.info("Credentials persisted to %s", self.filepath)

    def wipe(self) -> None:
        """Remove all stored credentials (hard-reset)."""
        self.save({})
        logger.warning("All credentials wiped from secrets store.")
