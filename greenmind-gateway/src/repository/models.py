from sqlalchemy import Column, String, Float, Integer, JSON, DateTime
from sqlalchemy.orm import declarative_base
import uuid
from datetime import datetime

Base = declarative_base()

class MeasurementDB(Base):
    __tablename__ = "measurements"
    
    measurement_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    gateway_id = Column(String, index=True)
    device_id = Column(String, index=True)
    sensor_type = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    sampling_rate_hz = Column(Float, nullable=True)
    duration_s = Column(Float, nullable=True)
    payload = Column(JSON)
    checksum = Column(String, nullable=True)
    
    upload_status = Column(String, default="pending", index=True)
    retry_count = Column(Integer, default=0)
    last_retry_at = Column(DateTime, nullable=True)
