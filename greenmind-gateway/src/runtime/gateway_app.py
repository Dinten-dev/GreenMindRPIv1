"""Gateway runtime application.

Starts the FastAPI ingest server alongside async background tasks
for uploading, heartbeat, and remote management.
"""

import asyncio
import atexit
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from src.config import settings
from src.http_limits import RequestBodyLimitMiddleware
from src.ota.cloud_sync import cloud_sync_worker
from src.ota.local_server import router as ota_router
from src.persistence.database import init_db
from src.runtime.heartbeat import heartbeat_loop
from src.runtime.ingest_api import router as ingest_router
from src.runtime.remote_manager import remote_manager_loop
from src.runtime.udp_discovery import udp_discovery_server
from src.runtime.upload_worker import upload_loop
from src.runtime.wav_uploader import upload_loop as wav_upload_loop

logger = logging.getLogger(__name__)


def _force_flush_wavs():
    from src.runtime.wav_writer import close_all

    closed = close_all()
    if closed:
        logger.info("Crash-flush: Closed %d WAV writers.", len(closed))


atexit.register(_force_flush_wavs)

# Module-level reference set by run_gateway before uvicorn starts
_credentials: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background workers on app startup, cancel on shutdown."""
    init_db()
    from src.runtime.wav_writer import recover_part_files

    recovered = recover_part_files()
    if recovered:
        logger.info("Recovered %d finalized WAV chunks from previous shutdown", recovered)

    tasks = [
        asyncio.create_task(upload_loop(_credentials), name="upload_worker"),
        asyncio.create_task(heartbeat_loop(_credentials), name="heartbeat_worker"),
        asyncio.create_task(remote_manager_loop(_credentials), name="remote_manager"),
        asyncio.create_task(udp_discovery_server(), name="udp_discovery"),
        asyncio.create_task(wav_upload_loop(_credentials), name="wav_uploader"),
        asyncio.create_task(cloud_sync_worker(), name="cloud_sync_worker"),
    ]
    if settings.enable_ble_provisioning:
        from src.provisioning.ble_worker import provisioning_loop

        tasks.append(
            asyncio.create_task(provisioning_loop(_credentials), name="provisioning_worker")
        )
    else:
        logger.info("BLE provisioning is disabled (ENABLE_BLE_PROVISIONING=false)")
    logger.info("Background workers started: %s", [t.get_name() for t in tasks])
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    from src.runtime.wav_writer import close_all

    closed_wavs = close_all()
    logger.info("Closed %d active WAV writers.", len(closed_wavs))
    logger.info("Background workers stopped.")


app = FastAPI(title="GreenMind Edge Runtime", lifespan=lifespan)
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes=settings.max_request_body_bytes,
)
app.include_router(ingest_router, prefix="/api/v1")
app.include_router(ota_router, prefix="/api/v1")
if settings.enable_experimental_biosignal_proxy:
    from src.runtime.biosignal_proxy import router as biosignal_router

    app.include_router(biosignal_router, prefix="/api/v1/biosignal")
else:
    logger.info(
        "Experimental biosignal proxy is disabled (ENABLE_EXPERIMENTAL_BIOSIGNAL_PROXY=false)"
    )


async def run_gateway(credentials: dict, port: int = 80) -> None:
    """Start the operational runtime: ingest API + background workers."""
    global _credentials
    _credentials = credentials

    logger.info("Initializing runtime components...")
    init_db()

    logger.info("Starting ESP32 Ingestion server on 0.0.0.0:%d", port)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        limit_concurrency=settings.max_http_concurrency,
        timeout_keep_alive=settings.http_keepalive_seconds,
    )
    server = uvicorn.Server(config)
    await server.serve()
