"""Async wrapper around nmcli for Raspberry Pi OS (Bookworm+).

Provides AP management, WiFi client connection, internet check, and RSSI reading.
"""

import asyncio
import logging
import secrets
import sys

from src.core.errors import WiFiConnectionError

logger = logging.getLogger(__name__)

AP_CONNECTION_NAME = "GreenMind-Setup-AP"
WIFI_CONNECT_TIMEOUT = 30  # seconds
_SETUP_AP_PASSWORD = secrets.token_urlsafe(16)
_SENSITIVE_ARGUMENTS = {"password", "wifi-sec.psk", "psk", "--password"}
_credentials_announced = False


def get_setup_ap_password() -> str:
    """Return the process-local, cryptographically random setup credential."""
    return _SETUP_AP_PASSWORD


def _redact_command(args: list[str]) -> str:
    sanitized: list[str] = []
    redact_next = False
    for argument in args:
        if redact_next:
            sanitized.append("***REDACTED***")
            redact_next = False
            continue
        lowered = argument.lower()
        if lowered in _SENSITIVE_ARGUMENTS:
            sanitized.append(argument)
            redact_next = True
        elif any(lowered.startswith(f"{name}=") for name in _SENSITIVE_ARGUMENTS):
            sanitized.append(f"{argument.split('=', 1)[0]}=***REDACTED***")
        else:
            sanitized.append(argument)
    return " ".join(sanitized)


def _sensitive_values(args: list[str]) -> list[str]:
    values: list[str] = []
    for index, argument in enumerate(args[:-1]):
        if argument.lower() in _SENSITIVE_ARGUMENTS:
            values.append(args[index + 1])
    return [value for value in values if value]


def _redact_output(output: str, args: list[str]) -> str:
    for value in _sensitive_values(args):
        output = output.replace(value, "***REDACTED***")
    return output


def _announce_setup_credentials(ssid: str) -> None:
    """Show credentials on the local service console, never in file logging."""
    global _credentials_announced
    if _credentials_announced:
        return
    sys.stderr.write(
        f"GreenMind local setup AP: {ssid}\nGreenMind local setup password: {_SETUP_AP_PASSWORD}\n"
    )
    sys.stderr.flush()
    _credentials_announced = True


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
            decoded_stdout = _redact_output(stdout.decode(errors="replace").strip(), args)
            decoded_stderr = _redact_output(stderr.decode(errors="replace").strip(), args)
            if proc.returncode == 0:
                return True, decoded_stdout
            logger.error("Command failed: %s → %s", _redact_command(args), decoded_stderr)
            return False, decoded_stderr
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.error("Command timed out: %s", _redact_command(args))
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

        _announce_setup_credentials(ssid)

        logger.info("Starting Setup Access Point: %s", ssid)
        await NetworkManager._run(["nmcli", "radio", "wifi", "on"])

        # Check whether the profile already exists
        ok, out = await NetworkManager._run(["nmcli", "-t", "-f", "NAME", "connection", "show"])
        existing = out.splitlines() if ok else []

        if AP_CONNECTION_NAME in existing:
            logger.info("Refreshing existing AP profile with this boot's credentials.")
            ok, _ = await NetworkManager._run(
                [
                    "nmcli",
                    "connection",
                    "modify",
                    AP_CONNECTION_NAME,
                    "802-11-wireless.ssid",
                    ssid,
                    "wifi-sec.key-mgmt",
                    "wpa-psk",
                    "wifi-sec.psk",
                    _SETUP_AP_PASSWORD,
                ]
            )
            if not ok:
                return False
            ok, _ = await NetworkManager._run(["nmcli", "connection", "up", AP_CONNECTION_NAME])
        else:
            logger.info("Creating new AP profile with a per-boot password.")
            ok, _ = await NetworkManager._run(
                [
                    "nmcli",
                    "device",
                    "wifi",
                    "hotspot",
                    "ifname",
                    "wlan0",
                    "ssid",
                    ssid,
                    "password",
                    _SETUP_AP_PASSWORD,
                    "con-name",
                    AP_CONNECTION_NAME,
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

        # Force a Wi-Fi rescan since the interface just left AP mode and its cache is empty
        await NetworkManager._run(["nmcli", "device", "wifi", "rescan"])
        await asyncio.sleep(4)

        for attempt in range(3):
            # Delete any existing broken profile with the same SSID name
            await NetworkManager._run(["nmcli", "connection", "delete", ssid])

            # Build the connection manually to bypass 'key-mgmt is missing' bugs
            # prevalent in 'nmcli device wifi connect' auto-detection
            ok, out = await NetworkManager._run(
                [
                    "nmcli",
                    "connection",
                    "add",
                    "type",
                    "wifi",
                    "con-name",
                    ssid,
                    "ifname",
                    "wlan0",
                    "ssid",
                    ssid,
                ]
            )

            if password:
                await NetworkManager._run(
                    [
                        "nmcli",
                        "connection",
                        "modify",
                        ssid,
                        "wifi-sec.key-mgmt",
                        "wpa-psk",
                        "wifi-sec.psk",
                        password,
                    ]
                )

            logger.info("Connection attempt %d/3 for %s", attempt + 1, ssid)
            ok, out = await NetworkManager._run(
                ["nmcli", "connection", "up", ssid], timeout=WIFI_CONNECT_TIMEOUT
            )

            if ok:
                logger.info("Connected to %s", ssid)
                return True

            if "No network with SSID" in out:
                logger.warning("SSID not found on attempt %d, rescanning...", attempt + 1)
                await NetworkManager._run(["nmcli", "device", "wifi", "rescan"])
                await asyncio.sleep(3)
            else:
                logger.warning("Connection failed on attempt %d: %s", attempt + 1, out)
                await asyncio.sleep(2)

        # If we exhausted all retries, raise error
        logger.error("WiFi connection failed – reverting to AP mode.")
        await NetworkManager.start_ap()
        raise WiFiConnectionError(f"Could not connect to '{ssid}': {out}")

    @staticmethod
    async def check_internet() -> bool:
        """Ping 8.8.8.8 to verify outbound connectivity."""
        ok, _ = await NetworkManager._run(["ping", "-c", "1", "-W", "3", "8.8.8.8"], timeout=10)
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

    @staticmethod
    async def get_current_wifi_ssid() -> str | None:
        """Get the SSID of the currently active WiFi connection."""
        ok, out = await NetworkManager._run(
            ["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show"]
        )
        if not ok:
            return None
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[1] == "802-11-wireless" and parts[2] == "activated":
                return parts[0]
        return None


def quality_to_dbm(quality: int) -> int:
    """Convert nmcli signal quality (0-100) to approximate dBm."""
    return -90 + int(quality * 0.6)
