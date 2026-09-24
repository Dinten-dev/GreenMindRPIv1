"""Async heartbeat worker that reports gateway health to the cloud.

Sends CPU temperature, RAM usage, WiFi RSSI, queue depth, and local IP
to the cloud backend every 60 seconds.
"""

import asyncio
import logging
import os
import socket

import httpx

from src.config import settings
from src.network.wifi_manager import NetworkManager
from src.persistence.models import IngestJob
from src.runtime.wav_uploader import upload_status
from src.runtime.wav_writer import storage_status

logger = logging.getLogger(__name__)


async def heartbeat_loop(credentials: dict) -> None:
    """Send periodic heartbeats to the cloud backend."""
    api_key = credentials["api_key"]
    hardware_id = credentials.get("hardware_id") or settings.hardware_id
    server_url = credentials.get("server_url") or settings.cloud_api_url

    logger.info("Heartbeat worker started (every %ds).", settings.heartbeat_interval)

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                wav_storage = await asyncio.to_thread(storage_status)
                wav_upload = upload_status()
                queue_depth = _get_queue_depth()
                if queue_depth is not None and queue_depth >= settings.max_queue_size:
                    logger.error(
                        "Local queue warning threshold exceeded: %d records retained",
                        queue_depth,
                    )
                payload = {
                    "hardware_id": hardware_id,
                    "local_ip": _get_local_ip(),
                    "cpu_temp_c": _read_cpu_temp(),
                    "ram_usage_pct": _read_ram_usage(),
                    "wifi_rssi_dbm": await NetworkManager.get_wifi_rssi(),
                    "queue_depth": queue_depth,
                    "wav_pending_files": wav_storage["pending_files"],
                    "wav_pending_bytes": wav_storage["pending_bytes"],
                    "wav_oldest_pending_age_hours": wav_storage["oldest_pending_age_hours"],
                    "wav_last_upload_at": wav_upload["last_upload_at"],
                    "wav_last_error_code": wav_upload["last_error_code"],
                }
                headers = {"X-Api-Key": api_key}

                resp = await client.post(
                    f"{server_url}/gateways/heartbeat",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 200:
                    logger.debug("Heartbeat OK.")
                elif resp.status_code == 410:
                    try:
                        data = resp.json()
                        if (
                            data.get("detail", {}).get("action") == "RESET_TO_SETUP_MODE"
                            and settings.allow_remote_reset
                        ):
                            logger.critical("Gateway deleted remotely. Initiating reset sequence.")
                            from src.runtime.reset import trigger_remote_reset

                            await trigger_remote_reset()
                    except (ValueError, TypeError, AttributeError) as exc:
                        logger.warning("Heartbeat returned malformed 410 response: %s", exc)
                    logger.warning("Heartbeat returned 410 Gone without a recognized reset action")
                else:
                    logger.warning("Heartbeat returned HTTP %d", resp.status_code)

            except httpx.HTTPError as exc:
                logger.warning("Heartbeat failed (offline?): %s", exc)
            except Exception as exc:
                logger.error("Heartbeat unexpected error: %s", exc)

            await asyncio.sleep(settings.heartbeat_interval)


def _read_cpu_temp() -> float | None:
    """Read CPU temperature from sysfs (Raspberry Pi)."""
    try:
        path = "/sys/class/thermal/thermal_zone0/temp"
        if os.path.exists(path):
            with open(path, "r") as fh:
                return round(int(fh.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    return None


def _read_ram_usage() -> float | None:
    """Read RAM usage percentage via psutil (if installed) or /proc/meminfo."""
    try:
        import psutil

        return round(psutil.virtual_memory().percent, 1)
    except ImportError:
        pass

    try:
        with open("/proc/meminfo", "r") as fh:
            lines = fh.readlines()
        mem = {}
        for line in lines:
            parts = line.split()
            if parts[0] in ("MemTotal:", "MemAvailable:"):
                mem[parts[0].rstrip(":")] = int(parts[1])
        if "MemTotal" in mem and "MemAvailable" in mem:
            used_pct = (1 - mem["MemAvailable"] / mem["MemTotal"]) * 100
            return round(used_pct, 1)
    except (OSError, ValueError, KeyError):
        pass
    return None


def _get_local_ip() -> str | None:
    """Determine the local IP address by opening a dummy UDP socket."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _get_queue_depth() -> int | None:
    """Count pending jobs in the local SQLite queue."""
    db = None
    try:
        from src.persistence.database import SessionLocal

        if SessionLocal is None:
            return None
        db = SessionLocal()
        return db.query(IngestJob).filter(IngestJob.status == "QUEUED").count()
    except Exception as exc:
        logger.warning("Could not read local queue depth: %s", type(exc).__name__)
        return None
    finally:
        if db is not None:
            db.close()
