from pydantic import BaseModel, Field
from typing import Optional, Any, Dict
from datetime import datetime

class MeasurementCreate(BaseModel):
    device_id: Optional[str] = "esp32-default"
    sensor_type: Optional[str] = "unknown"
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    sampling_rate_hz: Optional[float] = None
    duration_s: Optional[float] = None
    payload: Dict[str, Any]
    checksum: Optional[str] = None

class MeasurementResponse(BaseModel):
    measurement_id: str
    status: str
