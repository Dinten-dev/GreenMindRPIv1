"""Local ESP32 ingestion endpoint.

Receives JSON from ESP32 sensors on the local network and buffers them
in the SQLite queue for later upload to the cloud.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Body
from sqlalchemy.orm import Session

from src.config import settings
from src.persistence.database import get_db
from src.persistence.models import IngestJob
import httpx

logger = logging.getLogger(__name__)

router = APIRouter()

sensor_ips = {}  # Cache MAC to IP mapping for reverse control

@router.post("/ingest")
def ingest_data(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Receive sensor data from ESP32 and queue for cloud upload.

    High-frequency data (380 Hz) is written to WAV files locally and is ALWAYS
    archived, regardless of cloud-queue backpressure. Only the low-resolution
    aggregate (one mean value per batch) is subject to the queue-size guard, so
    a saturated cloud queue can never cost us the full-resolution biosignal.
    """

    # Inject gateway_serial so the cloud knows the source
    if "gateway_serial" not in payload:
        payload["gateway_serial"] = settings.hardware_id

    # Track sensor IP
    mac = payload.get("mac_address")
    if mac and request.client:
        sensor_ips[mac] = request.client.host

    readings = payload.get("readings", [])
    sample_rate = payload.get("sample_rate", 20)

    # 1) Archive high-frequency raw data FIRST and unconditionally.
    samples_archived = 0
    if mac and readings:
        raw_values = [r.get("value", 0.0) for r in readings]
        if raw_values:
            from src.runtime import wav_writer
            wav_writer.write_samples(mac, raw_values, sample_rate)
            samples_archived = len(raw_values)

    # 2) Aggregate for the cloud is best-effort: skip it (never the WAV) if the
    #    local queue is saturated.
    pending = db.query(IngestJob).filter(IngestJob.status == "QUEUED").count()
    if pending >= settings.max_queue_size:
        logger.warning("Local queue full (%d) – aggregate dropped, WAV still archived.", pending)
        return {
            "status": "archived",
            "queue_full": True,
            "samples_archived": samples_archived,
        }

    if mac and readings and samples_archived > 0:
        raw_values = [r.get("value", 0.0) for r in readings]
        mean_value = sum(raw_values) / len(raw_values)
        unit = readings[0].get("unit", "mV")
        kind = readings[0].get("kind", "bio_signal")

        aggregate_payload = {
            "mac_address": mac,
            "gateway_serial": payload.get("gateway_serial", ""),
            "sample_rate": sample_rate,
            "readings": [
                {"kind": kind, "value": round(mean_value, 2), "unit": unit}
            ],
        }

        payload_str = json.dumps(aggregate_payload)
        job = IngestJob(payload_json=payload_str, status="QUEUED")
        db.add(job)
        db.commit()

        logger.debug(
            "Queued aggregate (%.1f %s from %d samples) job %d",
            mean_value, unit, samples_archived, job.id,
        )
        return {"status": "queued", "local_queue_id": job.id, "samples_archived": samples_archived}

    # Fallback: queue raw payload as-is (for non-batch or legacy sensors)
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
