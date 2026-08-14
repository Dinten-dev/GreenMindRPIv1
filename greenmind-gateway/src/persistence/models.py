"""SQLite persistence models for the local ingest queue."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class IngestJob(Base):
    """Buffered sensor reading that has not yet been uploaded to the cloud."""

    __tablename__ = "ingest_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    payload_json = Column(Text, nullable=False)
    status = Column(String(20), default="QUEUED", index=True)
    retry_count = Column(Integer, default=0)
    error_reason = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class DeadLetterJob(Base):
    """Permanently failed ingest job, kept for diagnostics."""

    __tablename__ = "dead_letter_queue"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    original_id = Column(Integer, nullable=True)
    payload_json = Column(Text, nullable=False)
    error_reason = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
