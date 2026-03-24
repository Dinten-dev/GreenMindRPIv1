from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime, timezone

Base = declarative_base()

class IngestJob(Base):
    """
    Local Queue Model holding sensor data blocks.
    Ensures that if the Hetzner target is disconnected, no data is lost.
    """
    __tablename__ = "ingest_jobs"
    
    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    payload_json = Column(Text, nullable=False)
    status = Column(String(20), default="QUEUED", index=True)
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
