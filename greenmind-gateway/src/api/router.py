from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.orm import Session
from src.schemas.measurement import MeasurementCreate, MeasurementResponse
from src.services.ingestion import save_local_measurement
from src.repository.database import get_db
from src.config import settings

router = APIRouter()

def verify_esp32_token(authorization: str = Header(None)):
    if settings.allow_unauthenticated_esp32:
        return True
    if not authorization or authorization != f"Bearer {settings.esp32_auth_token}":
        raise HTTPException(status_code=401, detail="Invalid token")
    return True

@router.post("/measurements", response_model=MeasurementResponse, dependencies=[Depends(verify_esp32_token)])
def ingest_measurement(data: MeasurementCreate, db: Session = Depends(get_db)):
    try:
        saved_item = save_local_measurement(db, data)
        return MeasurementResponse(
            measurement_id=saved_item.measurement_id,
            status="queued_for_upload"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail="Local persistence failed")

@router.get("/health")
def health_check(db: Session = Depends(get_db)):
    from src.repository.models import MeasurementDB
    pending_count = db.query(MeasurementDB).filter(MeasurementDB.upload_status == "pending").count()
    return {"status": "ok", "gateway_id": settings.gateway_id, "queue_length": pending_count}
