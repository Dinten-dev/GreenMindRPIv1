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

logger = logging.getLogger(__name__)

router = APIRouter()


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

    payload_str = json.dumps(payload)
    job = IngestJob(payload_json=payload_str, status="QUEUED")
    db.add(job)
    db.commit()

    logger.debug("Queued ingest job %d (%d bytes)", job.id, len(payload_str))
    return {"status": "queued", "local_queue_id": job.id}


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
