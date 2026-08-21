"""Validation helpers for gateway-to-cloud base URLs."""

import ipaddress
from urllib.parse import urlsplit


def validate_cloud_url(value: str, *, allow_insecure_loopback: bool = False) -> str:
    """Return a normalized cloud base URL or raise ``ValueError``.

    Production cloud traffic must use HTTPS. Plain HTTP is available only as
    an explicit local-development escape hatch and only for literal loopback
    addresses or ``localhost``. Query strings, fragments, and embedded
    credentials are not valid in a base URL.
    """
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError("Cloud URL must be a non-empty absolute URL")

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port  # Validate the optional port while errors can be sanitized.
    except ValueError as exc:
        raise ValueError("Cloud URL is invalid") from exc

    if not parsed.netloc or not hostname:
        raise ValueError("Cloud URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Cloud URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Cloud URL must not contain a query string or fragment")

    if parsed.scheme == "https":
        return value.rstrip("/")

    if parsed.scheme == "http" and allow_insecure_loopback and _is_loopback(hostname):
        return value.rstrip("/")

    raise ValueError("Cloud URL must use HTTPS")


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
