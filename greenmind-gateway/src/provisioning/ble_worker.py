import asyncio
import json
import logging
import os
import sys

import aiohttp

from src.config import settings

logger = logging.getLogger(__name__)

# This optional worker uses the same validated cloud origin as the core gateway.
# HTTPS maps to WSS; explicitly enabled loopback HTTP maps to WS for local tests.
HTTP_BASE = f"{settings.cloud_api_url}/provisioning"
_WS_SCHEME = "wss" if settings.cloud_api_url.startswith("https://") else "ws"
_WS_ORIGIN = settings.cloud_api_url.split("://", maxsplit=1)[1]
WS_URL = f"{_WS_SCHEME}://{_WS_ORIGIN}/provisioning/ws"


class ProvisioningWorker:
    def __init__(self, credentials: dict):
        self.credentials = credentials
        self.current_job = None
        self.running = False

    async def start(self):
        self.running = True
        logger.info("Starting BLE Provisioning Worker...")

        while self.running:
            try:
                # Try WebSocket first
                await self._run_websocket()
            except Exception as e:
                logger.warning(f"WebSocket disconnected or failed: {e}. Falling back to polling.")
                # Fallback to polling
                await self._run_polling()

            await asyncio.sleep(5)

    async def _run_websocket(self):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                logger.info("Connected to Provisioning WebSocket")

                # Check for pending jobs immediately upon connection
                await self._check_pending_jobs(session)

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get("event") == "new_job_available":
                            logger.info("Received new job notification via WS")
                            await self._check_pending_jobs(session)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

    async def _run_polling(self):
        async with aiohttp.ClientSession() as session:
            for _ in range(6):  # Poll for ~30 seconds before retrying WS
                if not self.running:
                    break
                await self._check_pending_jobs(session)
                await asyncio.sleep(10)

    async def _check_pending_jobs(self, session: aiohttp.ClientSession):
        if self.current_job is not None:
            return  # Already processing a job

        try:
            async with session.get(f"{HTTP_BASE}/jobs/pending") as resp:
                if resp.status == 200:
                    jobs = await resp.json()
                    if jobs:
                        # Take the first pending job
                        job = jobs[0]
                        logger.info(f"Found pending job: {job['id']}")
                        await self._process_job(job, session)
        except Exception as e:
            logger.error(f"Failed to fetch pending jobs: {e}")

    async def _process_job(self, job: dict, session: aiohttp.ClientSession):
        self.current_job = job
        job_id = job["id"]
        pairing_code = job["pairing_code"]
        ssid = job["ssid"]
        password = job.get("password", "")

        logger.info("Starting a queued BLE provisioning job")

        # Mark as in_progress
        await self._update_job_status(session, job_id, "in_progress")

        success = await self._run_esp_prov(pairing_code, ssid, password)

        if success:
            logger.info(f"Provisioning job {job_id} completed successfully")
            await self._update_job_status(session, job_id, "completed")
        else:
            logger.error(f"Provisioning job {job_id} failed")
            await self._update_job_status(session, job_id, "failed")

        self.current_job = None

    async def _update_job_status(self, session: aiohttp.ClientSession, job_id: str, status: str):
        try:
            async with session.patch(f"{HTTP_BASE}/jobs/{job_id}", json={"status": status}) as resp:
                if resp.status not in (200, 204):
                    logger.error(f"Failed to update job {job_id} to {status}: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"Exception updating job status: {e}")

    async def _run_esp_prov(self, pairing_code: str, ssid: str, password: str) -> bool:
        """Run the ESP provisioning script via subprocess."""
        esp_prov_script = os.path.join(
            os.path.dirname(__file__), "..", "provisioning_tools", "esp_prov.py"
        )
        ble_name = f"GM-{pairing_code}"

        cmd = [
            sys.executable,
            esp_prov_script,
            "--transport",
            "ble",
            "--name",
            ble_name,
            "--sec_ver",
            "1",
            "--pop",
            pairing_code,
            "--ssid",
            ssid,
            "--passphrase",
            password,
        ]

        # The command contains the one-time proof-of-possession value, SSID,
        # and passphrase. Never include any portion of it in logs.
        logger.info("Starting the ESP BLE provisioning tool")

        try:
            # We use subprocess.Popen because esp_prov.py has some interactive parts if it fails,
            # but we pass all arguments so it shouldn't prompt.
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            await process.communicate()

            if process.returncode == 0:
                logger.info("ESP BLE provisioning completed")
                return True
            else:
                # The vendored provisioning tool may echo sensitive arguments
                # or device responses to either stream; report only the status.
                logger.error("ESP BLE provisioning failed with exit code %s", process.returncode)
                return False

        except Exception as exc:
            logger.error("ESP BLE provisioning process failed (%s)", type(exc).__name__)
            return False


async def provisioning_loop(credentials: dict):
    worker = ProvisioningWorker(credentials)
    await worker.start()
