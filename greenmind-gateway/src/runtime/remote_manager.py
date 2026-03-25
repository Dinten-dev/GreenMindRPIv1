"""Remote management module.

Polls the cloud for pending commands (e.g. reboot) and executes them securely.
Future: OTA updates via git pull + service restart.
"""

import asyncio
import logging
import subprocess

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds
ALLOWED_COMMANDS = {"reboot", "restart_service"}


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
                        await _execute_command(cmd)

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


async def _execute_command(cmd: dict) -> None:
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
