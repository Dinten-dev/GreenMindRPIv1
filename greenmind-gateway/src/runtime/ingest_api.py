"""Local ESP32 ingestion endpoint.

Receives JSON from ESP32 sensors on the local network and buffers them
in the SQLite queue for later upload to the cloud.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.config import settings
from src.persistence.database import get_db
from src.persistence.models import IngestJob
import httpx

logger = logging.getLogger(__name__)

router = APIRouter()

sensor_ips = {}  # Cache MAC to IP mapping for reverse control

@router.post("/ingest")
async def ingest_data(request: Request, db: Session = Depends(get_db)):
    """Receive sensor data from ESP32 and queue for cloud upload."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Guard against queue overflow
    pending = db.query(IngestJob).filter(IngestJob.status == "QUEUED").count()
    if pending >= settings.max_queue_size:
        raise HTTPException(status_code=503, detail="Local queue full")

    # Inject gateway_serial so the cloud knows the source
    if "gateway_serial" not in payload:
        payload["gateway_serial"] = settings.hardware_id

    # Track sensor IP
    mac = payload.get("mac_address")
    if mac and request.client:
        sensor_ips[mac] = request.client.host

    payload_str = json.dumps(payload)
    job = IngestJob(payload_json=payload_str, status="QUEUED")
    db.add(job)
    db.commit()

    logger.debug("Queued ingest job %d (%d bytes)", job.id, len(payload_str))
    return {"status": "queued", "local_queue_id": job.id}

@router.post("/sensors/register")
async def register_sensor(request: Request):
    """Bridge ESP32 captive portal registration to Cloud backend."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
        
    mac = payload.get("mac_address")
    code = payload.get("code")
    if not mac or not code:
        raise HTTPException(status_code=400, detail="Missing mac_address or code")
        
    from src.runtime.gateway_app import _credentials
    if not _credentials or "api_key" not in _credentials:
        raise HTTPException(status_code=503, detail="Gateway credentials not loaded")
        
    api_key = _credentials["api_key"]
    server_url = _credentials.get("server_url") or settings.cloud_api_url
    gateway_id = _credentials.get("gateway_id", "")
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"{server_url}/gateways/{gateway_id}/sensors/register",
                json={"mac_address": mac, "code": code},
                headers={"X-Api-Key": api_key}
            )
            if resp.status_code in (200, 201):
                return {"status": "ok"}
            else:
                logger.error("Cloud rejected sensor %s: %s", mac, resp.text)
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
        except httpx.RequestError as exc:
            logger.error("Sensors register network error: %s", exc)
            raise HTTPException(status_code=502, detail="Cloud unavailable")


@router.get("/health")
async def health(db: Session = Depends(get_db)):
    """Local health endpoint for diagnostics."""
    queued = db.query(IngestJob).filter(IngestJob.status == "QUEUED").count()
    failed = db.query(IngestJob).filter(IngestJob.status == "FAILED").count()
    return {
        "status": "ok",
        "hardware_id": settings.hardware_id,
        "queue_depth": queued,
        "failed_count": failed,
    }
