"""Explicit local registration after BLE WLAN provisioning; no credentials logged."""

import argparse
import getpass
import ipaddress

import httpx

from src.validation import canonical_mac


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-ip", required=True, help="Local gateway IPv4 address")
    args = parser.parse_args()
    address = ipaddress.IPv4Address(args.gateway_ip)
    if not (address.is_private or address.is_loopback) or address.is_unspecified:
        parser.error("Use the local gateway's private IPv4 address")
    mac = canonical_mac(input("Sensor MAC from display/label: ").strip())
    code = (
        getpass.getpass("Fresh Gateway sensor code from the target dashboard zone: ")
        .strip()
        .upper()
    )
    if len(code) != 6 or not code.isascii() or not code.isalnum():
        parser.error("Expected six letters/digits, not the BLE or Direct code")
    response = httpx.post(
        f"http://{address}/api/v1/sensors/register",
        json={"mac_address": mac, "code": code},
        timeout=15,
        follow_redirects=False,
        trust_env=False,
    )
    if response.status_code not in (200, 201):
        raise SystemExit(
            f"Registration rejected (HTTP {response.status_code}); check zone/code/gateway"
        )
    if response.json() != {"status": "ok"}:
        raise SystemExit("Unexpected gateway response; registration not confirmed")
    print("Registration accepted. Verify this MAC, its zone and incoming data in the dashboard.")


if __name__ == "__main__":
    main()
