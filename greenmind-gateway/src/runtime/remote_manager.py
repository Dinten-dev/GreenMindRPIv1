"""Remote management module.

Polls the cloud for pending commands (e.g. reboot) and executes them securely.
Future: OTA updates via git pull + service restart.
"""

import asyncio
import logging
import subprocess

import httpx

from src.config import settings
from src.network.wifi_manager import NetworkManager
from src.runtime.ingest_api import sensor_ips

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds
ALLOWED_COMMANDS = {"reboot", "restart_service", "provision_sensor", "delete_sensor"}


async def remote_manager_loop(credentials: dict) -> None:
    """Poll the cloud for pending remote commands."""
    api_key = credentials["api_key"]
    gateway_id = credentials["gateway_id"]
    server_url = credentials.get("server_url") or settings.cloud_api_url

    logger.info("Remote manager started (poll every %ds).", POLL_INTERVAL)

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                headers = {"X-Api-Key": api_key}
                resp = await client.get(
                    f"{server_url}/gateways/{gateway_id}/commands",
                    headers=headers,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    commands = data if isinstance(data, list) else data.get("commands", [])
                    for cmd in commands:
                        await _execute_command(cmd, credentials)

                elif resp.status_code == 404:
                    # Endpoint not yet implemented on the cloud – silent skip
                    pass
                else:
                    logger.debug(
                        "Remote manager poll returned %d.", resp.status_code
                    )

            except httpx.HTTPError as exc:
                logger.debug("Remote manager poll failed (offline?): %s", exc)
            except Exception as exc:
                logger.error("Remote manager error: %s", exc)

            await asyncio.sleep(POLL_INTERVAL)


async def _execute_command(cmd: dict, credentials: dict) -> None:
    """Execute a verified remote command."""
    action = cmd.get("action", "")

    if action not in ALLOWED_COMMANDS:
        logger.warning("Ignored unknown remote command: %s", action)
        return

    logger.info("Executing remote command: %s", action)

    if action == "reboot":
        logger.warning("Remote reboot requested – rebooting in 3 seconds.")
        await asyncio.sleep(3)
        subprocess.run(["sudo", "reboot"], check=False)

    elif action == "restart_service":
        logger.info("Restarting greenmind-gateway service.")
        subprocess.run(
            ["sudo", "systemctl", "restart", "greenmind-gateway"],
            check=False,
        )
    
    elif action == "provision_sensor":
        target_mac = cmd.get("mac_address", "")
        if not target_mac:
            return
            
        logger.info("Provisioning sensor %s", target_mac)
        wifi_ssid = credentials.get("wifi_ssid")
        wifi_password = credentials.get("wifi_password")
        
        # SoftAP Ninja Hop
        target_ap = f"GreenMind-Sensor-{target_mac.replace(':', '')[-4:]}"
        original_ssid = await NetworkManager.get_current_wifi_ssid()
        
        if original_ssid and await NetworkManager.ninja_hop(target_ap):
            await asyncio.sleep(3)
            # Send WiFi data
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post("http://192.168.4.1/provision", json={
                        "wifi_ssid": wifi_ssid,
                        "wifi_password": wifi_password
                    })
                    logger.info("Provisioning sent to sensor!")
            except Exception as e:
                logger.error("Failed to provision sensor via HTTP: %s", e)
            
            # Hop back
            await NetworkManager.connect_to_wifi(original_ssid, wifi_password)

    elif action == "delete_sensor":
        target_mac = cmd.get("mac_address", "")
        if not target_mac:
            return
            
        ip = sensor_ips.get(target_mac)
        if ip:
            logger.info("Sending DELETE command to sensor %s at %s", target_mac, ip)
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(f"http://{ip}/provision", content="DELETE /provision HTTP/1.1\r\n\r\n")
            except Exception as e:
                logger.error("Failed to delete sensor %s via HTTP: %s", target_mac, e)
        else:
            logger.warning("Could not delete sensor %s: IP not in cache", target_mac)
