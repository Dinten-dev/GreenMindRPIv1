"""Async wrapper around nmcli for Raspberry Pi OS (Bookworm+).

Provides AP management, WiFi client connection, internet check, and RSSI reading.
"""

import asyncio
import logging

from src.core.errors import WiFiConnectionError

logger = logging.getLogger(__name__)

AP_CONNECTION_NAME = "GreenMind-Setup-AP"
WIFI_CONNECT_TIMEOUT = 30  # seconds


class NetworkManager:
    """Static async methods wrapping nmcli commands."""

    @staticmethod
    async def _run(args: list[str], timeout: float = 15) -> tuple[bool, str]:
        """Execute a subprocess and return (success, stdout)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode == 0:
                return True, stdout.decode().strip()
            logger.error("Command failed: %s → %s", " ".join(args), stderr.decode().strip())
            return False, stderr.decode().strip()
        except asyncio.TimeoutError:
            logger.error("Command timed out: %s", " ".join(args))
            return False, "timeout"
        except OSError as exc:
            logger.error("OS error running %s: %s", args[0], exc)
            return False, str(exc)

    @staticmethod
    async def start_ap(ssid: str | None = None, hw_suffix: str = "0000") -> bool:
        """Spin up a local WiFi Access Point.

        Args:
            ssid: Custom SSID. Defaults to ``GreenMind-Gateway-<hw_suffix>``.
            hw_suffix: Last 4 hex characters of the hardware serial.
        """
        if ssid is None:
            ssid = f"GreenMind-Gateway-{hw_suffix}"

        logger.info("Starting Setup Access Point: %s", ssid)
        await NetworkManager._run(["nmcli", "radio", "wifi", "on"])

        # Check whether the profile already exists
        ok, out = await NetworkManager._run(
            ["nmcli", "-t", "-f", "NAME", "connection", "show"]
        )
        existing = out.splitlines() if ok else []

        if AP_CONNECTION_NAME in existing:
            logger.info("AP profile exists – bringing it up.")
            ok, _ = await NetworkManager._run(["nmcli", "connection", "up", AP_CONNECTION_NAME])
        else:
            logger.info("Creating new AP profile.")
            ok, _ = await NetworkManager._run(
                [
                    "nmcli", "device", "wifi", "hotspot",
                    "ifname", "wlan0",
                    "ssid", ssid,
                    "con-name", AP_CONNECTION_NAME,
                ]
            )
        return ok

    @staticmethod
    async def ensure_ap_off() -> bool:
        """Explicitly shut down the setup AP."""
        logger.info("Shutting down Setup AP: %s", AP_CONNECTION_NAME)
        ok, _ = await NetworkManager._run(["nmcli", "connection", "down", AP_CONNECTION_NAME])
        return ok

    @staticmethod
    async def connect_to_wifi(ssid: str, password: str) -> bool:
        """Connect to a client WiFi network.

        Raises WiFiConnectionError (E-101) on failure so the caller can handle it.
        """
        logger.info("Connecting to WiFi: %s", ssid)

        # Free the radio first
        await NetworkManager.ensure_ap_off()
        await asyncio.sleep(2)

        args = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            args += ["password", password]

        ok, out = await NetworkManager._run(args, timeout=WIFI_CONNECT_TIMEOUT)
        if ok:
            logger.info("Connected to %s", ssid)
            return True

        logger.error("WiFi connection failed – reverting to AP mode.")
        await NetworkManager.start_ap()
        raise WiFiConnectionError(f"Could not connect to '{ssid}': {out}")

    @staticmethod
    async def check_internet() -> bool:
        """Ping 8.8.8.8 to verify outbound connectivity."""
        ok, _ = await NetworkManager._run(
            ["ping", "-c", "1", "-W", "3", "8.8.8.8"], timeout=10
        )
        return ok

    @staticmethod
    async def get_wifi_rssi() -> int | None:
        """Return the current WiFi signal strength in dBm, or None."""
        ok, out = await NetworkManager._run(
            ["nmcli", "-t", "-f", "IN-USE,SIGNAL", "device", "wifi", "list"]
        )
        if not ok:
            return None
        for line in out.splitlines():
            if line.startswith("*:"):
                try:
                    # Signal is 0-100 quality; convert to approximate dBm
                    quality = int(line.split(":")[1])
                    return quality_to_dbm(quality)
                except (IndexError, ValueError):
                    pass
        return None

    @staticmethod
    async def delete_all_wifi_profiles() -> None:
        """Remove all stored WiFi connection profiles (for hard-reset)."""
        ok, out = await NetworkManager._run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"]
        )
        if not ok:
            return
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and "wireless" in parts[1]:
                name = parts[0]
                logger.info("Deleting WiFi profile: %s", name)
                await NetworkManager._run(["nmcli", "connection", "delete", name])


def quality_to_dbm(quality: int) -> int:
    """Convert nmcli signal quality (0-100) to approximate dBm."""
    # Linear approximation: 100% ≈ -30 dBm, 0% ≈ -90 dBm
    return -90 + int(quality * 0.6)
