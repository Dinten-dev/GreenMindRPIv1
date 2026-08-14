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
                payload = {
                    "hardware_id": hardware_id,
                    "local_ip": _get_local_ip(),
                    "cpu_temp_c": _read_cpu_temp(),
                    "ram_usage_pct": _read_ram_usage(),
                    "wifi_rssi_dbm": await NetworkManager.get_wifi_rssi(),
                    "queue_depth": _get_queue_depth(),
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
                        if data.get("detail", {}).get("action") == "RESET_TO_SETUP_MODE":
                            logger.critical("Gateway deleted remotely. Initiating reset sequence.")
                            from src.runtime.reset import trigger_remote_reset
                            await trigger_remote_reset()
                    except Exception:
                        pass
                    logger.warning("Heartbeat returned 410 Gone, but payload was unrecognized: %s", resp.text)
                else:
                    logger.warning(
                        "Heartbeat returned %d: %s", resp.status_code, resp.text
                    )

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
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def _get_queue_depth() -> int:
    """Count pending jobs in the local SQLite queue."""
    try:
        from src.persistence.database import SessionLocal

        if SessionLocal is None:
            return -1
        db = SessionLocal()
        count = db.query(IngestJob).filter(IngestJob.status == "QUEUED").count()
        db.close()
        return count
    except Exception:
        return -1
