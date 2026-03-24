from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from src.persistence.database import get_db
from src.persistence.models import IngestJob
import json
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/ingest")
async def ingest_data(request: Request, db: Session = Depends(get_db)):
    """Receives data from ESP32 clients on the local network."""
    try:
        # Expect raw JSON representing bioelectric metadata and points
        payload = await request.json()
        payload_str = json.dumps(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Local Queue Persist (zero block on network outage)
    job = IngestJob(payload_json=payload_str, status="QUEUED")
    db.add(job)
    db.commit()
    logger.debug(f"Queued local ingest job {job.id}")
    return {"status": "queued", "local_queue_id": job.id}
