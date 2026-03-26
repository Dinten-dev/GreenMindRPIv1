"""Setup portal server for first-run provisioning.

Serves a local web UI on the AP network. The user enters WiFi credentials
and the pairing code from the cloud dashboard. On success, the gateway
registers with the cloud and stores its API key locally.
"""

import asyncio
import logging
import os
import signal

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.config import settings
from src.core.config_store import SecretStore
from src.core.errors import CloudAuthError, WiFiConnectionError
from src.network.wifi_manager import NetworkManager

logger = logging.getLogger(__name__)

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

app = FastAPI(title="GreenMind Gateway Setup", docs_url=None, redoc_url=None)


def _load_template() -> str:
    """Read the setup HTML template from disk."""
    path = os.path.join(TEMPLATE_DIR, "setup.html")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


@app.get("/", response_class=HTMLResponse)
async def get_form():
    """Serve the setup form."""
    html = _load_template()
    html = html.replace("{{ server_url }}", settings.cloud_api_url)
    return html


@app.get("/tailwind.js", response_class=HTMLResponse)
async def get_tailwind():
    """Serve the local tailwind CSS script for offline rendering."""
    path = os.path.join(TEMPLATE_DIR, "tailwind.js")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return HTMLResponse(content=fh.read(), media_type="application/javascript")
    except FileNotFoundError:
        return HTMLResponse(content="", status_code=404)


@app.post("/setup")
async def do_setup(
    background_tasks: BackgroundTasks,
    ssid: str = Form(...),
    password: str = Form(""),
    pairing_code: str = Form(...),
    gateway_name: str = Form(""),
    server_url: str = Form(""),
):
    """Process the setup form submission."""
    if not server_url:
        server_url = settings.cloud_api_url

    store: SecretStore = app.state.store

    # Schedule the actual provisioning in the background so we can return a 200 OK
    # to the client before we drop the AP connection.
    background_tasks.add_task(
        _run_provisioning, store, ssid, password, pairing_code, gateway_name, server_url
    )

    return JSONResponse(content={"status": "success"})


async def _run_provisioning(store, ssid, password, pairing_code, gateway_name, server_url):
    """Executes the Wi-Fi connection and Cloud registration flow."""
    # Sleep to allow the HTTP response to reach the browser before dropping AP
    await asyncio.sleep(2)

    # 1. Connect to WiFi
    try:
        await NetworkManager.connect_to_wifi(ssid, password)
    except WiFiConnectionError as exc:
        logger.error("[E-101] %s", exc)
        return

    # 2. Check internet
    if not await NetworkManager.check_internet():
        logger.error("Connected to WiFi but no internet.")
        await NetworkManager.start_ap(hw_suffix=settings.hardware_id[-4:])
        return

    # 3. Register with cloud backend
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{server_url}/gateways/register",
                json={
                    "code": pairing_code,
                    "hardware_id": settings.hardware_id,
                    "name": gateway_name or None,
                    "local_ip": None,
                },
            )
            if resp.status_code != 201:
                detail = resp.text
                logger.error("[E-202] Pairing rejected: %s", detail)
                await NetworkManager.start_ap(hw_suffix=settings.hardware_id[-4:])
                return

            data = resp.json()
            gateway_id = data["gateway_id"]
            api_key = data["api_key"]
            greenhouse_id = data.get("greenhouse_id", "")

    except httpx.HTTPError as exc:
        logger.error("[E-202] Cloud connection failed: %s", exc)
        await NetworkManager.start_ap(hw_suffix=settings.hardware_id[-4:])
        return

    # 4. Persist credentials
    store.store_credentials(
        api_key=api_key,
        gateway_id=gateway_id,
        greenhouse_id=greenhouse_id,
        hardware_id=settings.hardware_id,
        server_url=server_url,
    )

    logger.info("Provisioning complete. Scheduling service restart.")
    await asyncio.sleep(1)
    _kill_server()


def _kill_server():
    """Send SIGINT to terminate the uvicorn loop cleanly."""
    logger.info("Terminating setup server via SIGINT.")
    os.kill(os.getpid(), signal.SIGINT)


async def run_setup_server(store: SecretStore, port: int = 80) -> bool:
    """Block and run the setup portal until provisioning succeeds."""
    app.state.store = store

    hw_suffix = settings.hardware_id[-4:] if len(settings.hardware_id) >= 4 else "0000"
    logger.info(
        "Setup Portal starting on 0.0.0.0:%d (AP suffix: %s)", port, hw_suffix
    )

    try:
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Setup server terminated.")

    return store.is_provisioned()
