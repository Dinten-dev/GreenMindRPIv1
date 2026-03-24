from sqlalchemy.orm import Session
from src.schemas.measurement import MeasurementCreate
from src.repository.models import MeasurementDB
from src.config import settings

def save_local_measurement(db: Session, data: MeasurementCreate) -> MeasurementDB:
    db_item = MeasurementDB(
        gateway_id=settings.gateway_id,
        device_id=data.device_id,
        sensor_type=data.sensor_type,
        timestamp=data.timestamp,
        sampling_rate_hz=data.sampling_rate_hz,
        duration_s=data.duration_s,
        payload=data.payload,
        checksum=data.checksum
    )
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item
