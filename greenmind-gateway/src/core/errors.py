"""Structured error codes for the GreenMind Gateway."""

import logging

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """Base exception for all gateway-specific errors."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class WiFiConnectionError(GatewayError):
    """E-101: WiFi connection failed."""

    def __init__(self, detail: str = ""):
        msg = "WiFi connection failed"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__("E-101", msg)


class CloudAuthError(GatewayError):
    """E-202: Cloud authentication rejected (API key invalid or pairing failed)."""

    def __init__(self, detail: str = ""):
        msg = "Cloud authentication rejected"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__("E-202", msg)


class SensorDiscoveryTimeout(GatewayError):
    """E-303: Sensor discovery timed out."""

    def __init__(self, detail: str = ""):
        msg = "Sensor discovery timeout"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__("E-303", msg)


# Map error codes to human-readable descriptions for diagnostics
ERROR_CATALOG = {
    "E-101": "WiFi connection failed – check SSID and password",
    "E-202": "Cloud authentication rejected – check API key or pairing code",
    "E-303": "No sensors discovered within the timeout window",
}
