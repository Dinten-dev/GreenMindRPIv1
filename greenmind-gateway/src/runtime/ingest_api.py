"""Local ESP32 ingestion endpoint.

Receives JSON from ESP32 sensors on the local network and buffers them
in the SQLite queue for later upload to the cloud.
"""

import ipaddress
import json
import logging
from collections import OrderedDict
from threading import Lock

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from src.config import settings
from src.persistence.database import get_db
from src.persistence.models import DeadLetterJob, IngestJob
from src.validation import SensorBatch, SensorRegistration, canonical_mac

logger = logging.getLogger(__name__)

router = APIRouter()


class BoundedSensorIPCache:
    """Small thread-safe LRU used by the legacy remote-manager lookup."""

    def __init__(self) -> None:
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._lock = Lock()

    def remember(self, mac: str, host: str) -> None:
        try:
            canonical = canonical_mac(mac)
            address = str(ipaddress.ip_address(host))
        except ValueError:
            logger.warning("Not caching invalid sensor address metadata")
            return
        with self._lock:
            self._entries[canonical] = address
            self._entries.move_to_end(canonical)
            while len(self._entries) > settings.max_sensor_ip_entries:
                self._entries.popitem(last=False)

    def get(self, mac: str, default=None):
        try:
            canonical = canonical_mac(mac)
        except ValueError:
            return default
        with self._lock:
            value = self._entries.get(canonical, default)
            if canonical in self._entries:
                self._entries.move_to_end(canonical)
            return value

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


sensor_ips = BoundedSensorIPCache()


@router.post("/ingest")
def ingest_data(request: Request, payload: SensorBatch, db: Session = Depends(get_db)):
    """Receive sensor data from ESP32 and queue for cloud upload.

    High-frequency data (380 Hz) is written to WAV files locally and is ALWAYS
    archived, regardless of cloud-queue backpressure. Only the low-resolution
    aggregate (one mean value per batch) is subject to the queue-size guard, so
    a saturated cloud queue can never cost us the full-resolution biosignal.
    """

    mac = payload.mac_address
    if request.client:
        sensor_ips.remember(mac, request.client.host)

    readings = payload.readings
    sample_rate = payload.sample_rate

    # 1) Archive high-frequency raw data FIRST and unconditionally.
    samples_archived = 0
    raw_values = [reading.value for reading in readings]
    if raw_values:
        from src.runtime import wav_writer

        try:
            wav_writer.write_samples(mac, raw_values, sample_rate)
        except wav_writer.WavStorageError as exc:
            logger.error("WAV storage refused sensor batch: %s", exc)
            raise HTTPException(status_code=507, detail="Local measurement storage unavailable")
        samples_archived = len(raw_values)

    # 2) Aggregate for the cloud is best-effort: skip it (never the WAV) if all
    #    retained queue records, including diagnostics in the DLQ, have reached
    #    the configured bound. This prevents poison records from growing SQLite
    #    indefinitely while preserving the full-resolution WAV archive.
    retained_jobs = db.query(IngestJob).count() + db.query(DeadLetterJob).count()
    if retained_jobs >= settings.max_queue_size:
        logger.warning(
            "Local queue storage full (%d records) – aggregate dropped, WAV still archived.",
            retained_jobs,
        )
        return {
            "status": "archived",
            "queue_full": True,
            "samples_archived": samples_archived,
        }

    if samples_archived > 0:
        mean_value = sum(raw_values) / len(raw_values)
        unit = readings[0].unit
        kind = readings[0].kind

        aggregate_payload = {
            "mac_address": mac,
            "gateway_serial": settings.hardware_id,
            "sample_rate": sample_rate,
            "readings": [{"kind": kind, "value": round(mean_value, 2), "unit": unit}],
        }

        payload_str = json.dumps(aggregate_payload)
        job = IngestJob(payload_json=payload_str, status="QUEUED")
        try:
            db.add(job)
            db.commit()
            db.refresh(job)
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Failed to queue sensor aggregate")
            raise HTTPException(status_code=503, detail="Local queue unavailable")

        logger.debug(
            "Queued aggregate (%.1f %s from %d samples) job %d",
            mean_value,
            unit,
            samples_archived,
            job.id,
        )
        return {"status": "queued", "local_queue_id": job.id, "samples_archived": samples_archived}

    raise HTTPException(status_code=422, detail="Sensor batch contains no readings")


@router.post("/sensors/register")
async def register_sensor(payload: SensorRegistration):
    """Bridge ESP32 captive portal registration to Cloud backend."""
    mac = payload.mac_address
    code = payload.code

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
                headers={"X-Api-Key": api_key},
            )
            if resp.status_code in (200, 201):
                return {"status": "ok"}
            else:
                logger.error("Cloud rejected sensor %s with HTTP %d", mac, resp.status_code)
                status = resp.status_code if 400 <= resp.status_code < 500 else 502
                raise HTTPException(status_code=status, detail="Cloud rejected registration")
        except httpx.RequestError as exc:
            logger.error("Sensors register network error: %s", exc)
            raise HTTPException(status_code=502, detail="Cloud unavailable")


@router.get("/health")
async def health(db: Session = Depends(get_db)):
    """Local health endpoint for diagnostics."""
    queued = db.query(IngestJob).filter(IngestJob.status == "QUEUED").count()
    failed = db.query(IngestJob).filter(IngestJob.status == "FAILED").count()
    ingest_records = db.query(IngestJob).count()
    dead_letter = db.query(DeadLetterJob).count()

    from src.runtime.wav_writer import active_writer_count, storage_status

    wav_status = storage_status()
    return {
        "status": "ok",
        "hardware_id": settings.hardware_id,
        "queue_depth": queued,
        "failed_count": failed,
        "dead_letter_count": dead_letter,
        "retained_queue_records": ingest_records + dead_letter,
        "active_wav_writers": active_writer_count(),
        "completed_wavs_pending": wav_status["pending_files"],
        "wav_storage": wav_status,
    }
