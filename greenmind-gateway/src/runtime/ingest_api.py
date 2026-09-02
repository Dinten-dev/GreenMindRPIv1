"""Local ESP32 ingestion endpoint.

Receives JSON from ESP32 sensors on the local network and buffers them
in the SQLite queue for later upload to the cloud.
"""

import hashlib
import ipaddress
import json
import logging
import math
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from threading import Lock

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from src.config import settings
from src.persistence.database import get_db
from src.persistence.models import DeadLetterJob, IngestJob, SensorBatchCursor
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


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _summarize_batch(payload: SensorBatch, values: list[float]) -> dict:
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    quality = payload.quality_counts
    return {
        "kind": payload.readings[0].kind if payload.readings else payload.kind,
        "value": round(mean, 4),
        "unit": payload.readings[0].unit if payload.readings else payload.unit,
        "sample_count": len(values),
        "sample_rate_hz": float(payload.sample_rate),
        "median": _percentile(values, 0.5),
        "rms": math.sqrt(sum(value * value for value in values) / len(values)),
        "standard_deviation": math.sqrt(variance),
        "minimum": min(values),
        "maximum": max(values),
        "p05": _percentile(values, 0.05),
        "p95": _percentile(values, 0.95),
        "coverage_ratio": min(1.0, len(values) / payload.sample_rate),
        "quality_valid_count": quality.valid if quality else None,
        "quality_lead_off_count": quality.lead_off if quality else None,
        "quality_rail_high_count": quality.rail_high if quality else None,
        "quality_rail_low_count": quality.rail_low if quality else None,
        "quality_jump_count": quality.jump if quality else None,
        "quality_recovery_count": quality.recovery if quality else None,
    }


@router.post("/ingest")
def ingest_data(request: Request, payload: SensorBatch, db: Session = Depends(get_db)):
    """Receive sensor data from ESP32 and queue for cloud upload.

    High-frequency data is archived before acknowledgement. Compact protocol-v3
    batches and legacy reading objects share the same lossless local path.
    """

    mac = payload.mac_address
    if request.client:
        sensor_ips.remember(mac, request.client.host)

    sample_rate = payload.sample_rate
    raw_values = payload.decoded_values_mv()
    payload_hash = hashlib.sha256(
        payload.model_dump_json(exclude_none=True).encode("utf-8")
    ).hexdigest()

    cursor = None
    if payload.boot_id is not None and payload.sequence is not None:
        cursor = (
            db.query(SensorBatchCursor)
            .filter(
                SensorBatchCursor.mac_address == mac,
                SensorBatchCursor.boot_id == payload.boot_id,
            )
            .first()
        )
        if cursor and payload.sequence <= cursor.last_sequence:
            if (
                payload.sequence == cursor.last_sequence
                and payload_hash != cursor.last_payload_hash
            ):
                raise HTTPException(status_code=409, detail="Batch identity payload mismatch")
            return {
                "status": "duplicate",
                "boot_id": payload.boot_id,
                "sequence": payload.sequence,
                "samples_archived": len(raw_values),
            }

    samples_archived = 0
    if raw_values:
        from src.runtime import wav_writer

        try:
            wav_writer.write_samples(
                mac,
                raw_values,
                sample_rate,
                captured_at_epoch_ms=payload.captured_at_epoch_ms,
            )
        except wav_writer.WavStorageError as exc:
            logger.error("WAV storage refused sensor batch: %s", exc)
            raise HTTPException(status_code=507, detail="Local measurement storage unavailable")
        samples_archived = len(raw_values)

    if samples_archived > 0:
        summary = _summarize_batch(payload, raw_values)

        aggregate_payload = {
            "mac_address": mac,
            "gateway_serial": settings.hardware_id,
            "sample_rate": sample_rate,
            "protocol_version": payload.protocol_version,
            "firmware_version": payload.firmware_version,
            "calibration_version": payload.calibration_version,
            "boot_id": payload.boot_id,
            "sequence": payload.sequence,
            "uptime_ms": payload.uptime_ms,
            "dropped_samples_total": payload.dropped_samples_total,
            "readings": [summary],
        }

        captured_at = None
        if payload.captured_at_epoch_ms is not None:
            captured_end = datetime.fromtimestamp(
                payload.captured_at_epoch_ms / 1000,
                tz=timezone.utc,
            )
            captured_at = captured_end - timedelta(seconds=len(raw_values) / sample_rate)

        payload_str = json.dumps(aggregate_payload, separators=(",", ":"))
        job = IngestJob(payload_json=payload_str, status="QUEUED")
        if captured_at is not None:
            job.created_at = captured_at
        try:
            db.add(job)
            if payload.boot_id is not None and payload.sequence is not None:
                if cursor is None:
                    db.add(
                        SensorBatchCursor(
                            mac_address=mac,
                            boot_id=payload.boot_id,
                            last_sequence=payload.sequence,
                            last_payload_hash=payload_hash,
                        )
                    )
                else:
                    cursor.last_sequence = payload.sequence
                    cursor.last_payload_hash = payload_hash
            db.commit()
            db.refresh(job)
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Failed to queue sensor aggregate")
            raise HTTPException(status_code=503, detail="Local queue unavailable")

        logger.debug(
            "Queued aggregate (%.1f %s from %d samples) job %d",
            summary["value"],
            summary["unit"],
            samples_archived,
            job.id,
        )
        return {
            "status": "queued",
            "boot_id": payload.boot_id,
            "sequence": payload.sequence,
            "local_queue_id": job.id,
            "samples_archived": samples_archived,
        }

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
        "utc_epoch_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
        "hardware_id": settings.hardware_id,
        "queue_depth": queued,
        "failed_count": failed,
        "dead_letter_count": dead_letter,
        "retained_queue_records": ingest_records + dead_letter,
        "queue_warning": ingest_records + dead_letter >= settings.max_queue_size,
        "active_wav_writers": active_writer_count(),
        "completed_wavs_pending": wav_status["pending_files"],
        "wav_storage": wav_status,
    }
