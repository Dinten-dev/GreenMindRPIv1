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
from fastapi import FastAPI, Form, Request
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


@app.post("/setup")
async def do_setup(
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

    # 1. Connect to WiFi
    try:
        await NetworkManager.connect_to_wifi(ssid, password)
    except WiFiConnectionError as exc:
        logger.error("[E-101] %s", exc)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": str(exc)},
        )

    # 2. Check internet
    if not await NetworkManager.check_internet():
        logger.error("Connected to WiFi but no internet.")
        await NetworkManager.start_ap(hw_suffix=settings.hardware_id[-4:])
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "WLAN verbunden, aber kein Internet."},
        )

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
                return JSONResponse(
                    status_code=400,
                    content={"status": "error", "detail": f"Pairing fehlgeschlagen: {detail}"},
                )

            data = resp.json()
            gateway_id = data["gateway_id"]
            api_key = data["api_key"]
            greenhouse_id = data.get("greenhouse_id", "")

    except httpx.HTTPError as exc:
        logger.error("[E-202] Cloud connection failed: %s", exc)
        await NetworkManager.start_ap(hw_suffix=settings.hardware_id[-4:])
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": f"Cloud nicht erreichbar: {exc}"},
        )

    # 4. Persist credentials
    store.store_credentials(
        api_key=api_key,
        gateway_id=gateway_id,
        greenhouse_id=greenhouse_id,
        hardware_id=settings.hardware_id,
        server_url=server_url,
    )

    logger.info("Provisioning complete. Scheduling service restart.")

    # Give the HTTP response time to reach the client before killing the server
    asyncio.get_event_loop().call_later(2.0, _kill_server)

    return JSONResponse(content={"status": "success"})


def _kill_server():
    """Send SIGINT to terminate the uvicorn loop cleanly."""
    logger.info("Terminating setup server via SIGINT.")
    os.kill(os.getpid(), signal.SIGINT)


def run_setup_server(store: SecretStore, port: int = 80) -> bool:
    """Block and run the setup portal until provisioning succeeds."""
    app.state.store = store

    hw_suffix = settings.hardware_id[-4:] if len(settings.hardware_id) >= 4 else "0000"
    logger.info(
        "Setup Portal starting on 0.0.0.0:%d (AP suffix: %s)", port, hw_suffix
    )

    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    except KeyboardInterrupt:
        logger.info("Setup server terminated.")

    return store.is_provisioned()
