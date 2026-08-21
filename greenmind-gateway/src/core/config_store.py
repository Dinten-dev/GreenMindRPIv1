"""Manages secure persistence of gateway credentials in secrets.json."""

import errno
import json
import logging
import os
import stat
import tempfile
from typing import Any

from src.cloud_url import validate_cloud_url

logger = logging.getLogger(__name__)


class SecretStoreError(RuntimeError):
    """Credential persistence is unavailable or contains invalid data."""


class SecretStore:
    """Read/write device credentials on disk with mode 0640."""

    def __init__(
        self,
        filepath: str = "/opt/greenmind/data/secrets.json",
        *,
        allow_insecure_cloud_http: bool = False,
    ):
        self.filepath = filepath
        self.allow_insecure_cloud_http = allow_insecure_cloud_http
        self._ensure_file()

    def _ensure_file(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.filepath))
        try:
            os.makedirs(directory, mode=0o750, exist_ok=True)
            if os.path.lexists(self.filepath):
                file_stat = os.stat(self.filepath, follow_symlinks=False)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise SecretStoreError("Credential store must be a regular file")
                os.chmod(self.filepath, 0o640, follow_symlinks=False)
            else:
                self.save({})
        except SecretStoreError:
            raise
        except OSError as exc:
            raise SecretStoreError("Cannot initialize credential store") from exc

    def load(self) -> dict[str, Any]:
        try:
            with open(self.filepath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SecretStoreError("Credential store contains invalid JSON") from exc
        except OSError as exc:
            raise SecretStoreError("Cannot read credential store") from exc
        if not isinstance(data, dict):
            raise SecretStoreError("Credential store root must be a JSON object")
        return data

    def save(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise SecretStoreError("Credential store value must be a mapping")

        directory = os.path.dirname(os.path.abspath(self.filepath))
        temp_path: str | None = None
        raw_fd = -1
        try:
            existing_stat = None
            if os.path.lexists(self.filepath):
                existing_stat = os.stat(self.filepath, follow_symlinks=False)
                if not stat.S_ISREG(existing_stat.st_mode):
                    raise SecretStoreError("Credential store must be a regular file")

            raw_fd, temp_path = tempfile.mkstemp(prefix=".secrets-", suffix=".tmp", dir=directory)
            os.fchmod(raw_fd, 0o640)
            if existing_stat is not None and hasattr(os, "fchown"):
                os.fchown(raw_fd, existing_stat.st_uid, existing_stat.st_gid)

            output = os.fdopen(raw_fd, "w", encoding="utf-8")
            raw_fd = -1
            with output:
                json.dump(data, output, indent=4, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())

            os.replace(temp_path, self.filepath)
            temp_path = None
            self._fsync_parent(directory)
        except SecretStoreError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise SecretStoreError("Cannot persist credential store") from exc
        finally:
            if raw_fd >= 0:
                os.close(raw_fd)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _fsync_parent(directory: str) -> None:
        """Persist the rename when directory fsync is supported by the OS."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            directory_fd = os.open(directory, flags)
        except OSError as exc:
            if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                return
            raise
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
        finally:
            os.close(directory_fd)

    def is_provisioned(self) -> bool:
        """True if the gateway has a valid API key and gateway ID."""
        data = self.load()
        return bool(data.get("api_key") and data.get("gateway_id"))

    def get_credentials(self) -> dict[str, str] | None:
        """Return credentials dict or None if not provisioned."""
        data = self.load()
        if not (data.get("api_key") and data.get("gateway_id")):
            return None
        server_url = data.get("server_url", "")
        if server_url:
            try:
                server_url = validate_cloud_url(
                    server_url,
                    allow_insecure_loopback=self.allow_insecure_cloud_http,
                )
            except ValueError as exc:
                raise SecretStoreError("Stored cloud URL is invalid") from exc
        return {
            "api_key": data["api_key"],
            "gateway_id": data["gateway_id"],
            "zone_id": data.get("zone_id", data.get("greenhouse_id", "")),
            "hardware_id": data.get("hardware_id", ""),
            "server_url": server_url,
        }

    def store_credentials(
        self,
        api_key: str,
        gateway_id: str,
        zone_id: str,
        hardware_id: str,
        server_url: str,
    ) -> None:
        """Persist pairing result securely."""
        try:
            server_url = validate_cloud_url(
                server_url,
                allow_insecure_loopback=self.allow_insecure_cloud_http,
            )
        except ValueError as exc:
            raise SecretStoreError("Cloud URL is invalid") from exc
        data = self.load()
        data["api_key"] = api_key
        data["gateway_id"] = gateway_id
        data["zone_id"] = zone_id
        data["hardware_id"] = hardware_id
        data["server_url"] = server_url
        self.save(data)
        logger.info("Credentials persisted to %s", self.filepath)

    def wipe(self) -> None:
        """Remove all stored credentials (hard-reset)."""
        self.save({})
        logger.warning("All credentials wiped from secrets store.")
