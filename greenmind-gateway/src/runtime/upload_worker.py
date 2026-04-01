"""Async upload worker that drains the SQLite queue to the cloud backend.

Uses httpx.AsyncClient with exponential backoff. Permanent failures (after 20
retries or 4xx errors) are moved to the Dead Letter Queue.
"""

import asyncio
import json
import logging
import uuid

import httpx

from src.config import settings
from src.persistence import database
from src.persistence.models import DeadLetterJob, IngestJob

logger = logging.getLogger(__name__)

MAX_RETRIES = 20
BATCH_SIZE = 50


async def upload_loop(credentials: dict) -> None:
    """Continuously drain the ingest queue and POST to the cloud."""
    api_key = credentials["api_key"]
    server_url = credentials.get("server_url") or settings.cloud_api_url

    logger.info("Upload worker started → %s/ingest", server_url)

    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            db = database.SessionLocal()
            try:
                jobs = (
                    db.query(IngestJob)
                    .filter(IngestJob.status == "QUEUED")
                    .order_by(IngestJob.created_at.asc())
                    .limit(BATCH_SIZE)
                    .all()
                )

                if not jobs:
                    await asyncio.sleep(settings.upload_interval)
                    continue

                logger.debug("Processing %d queued jobs.", len(jobs))

                for job in jobs:
                    payload = json.loads(job.payload_json)
                    headers = {"X-Api-Key": api_key}

                    # Transform sensor payload into cloud schema
                    cloud_payload = _transform_payload(payload)

                    try:
                        resp = await client.post(
                            f"{server_url}/ingest",
                            json=cloud_payload,
                            headers=headers,
                        )

                        if resp.status_code in (200, 201, 202):
                            db.delete(job)
                            db.commit()
                            logger.info("Uploaded job %d.", job.id)

                        elif resp.status_code in (401, 403):
                            _move_to_dlq(
                                db, job, f"Auth error {resp.status_code}: {resp.text}"
                            )
                            logger.error(
                                "[E-202] Cloud rejected job %d (auth). Moved to DLQ.",
                                job.id,
                            )
                        elif resp.status_code == 410:
                            try:
                                data = resp.json()
                                if data.get("detail", {}).get("action") == "RESET_TO_SETUP_MODE":
                                    logger.critical("Gateway deleted remotely. Initiating reset sequence.")
                                    from src.runtime.reset import trigger_remote_reset
                                    await trigger_remote_reset()
                            except Exception:
                                pass
                            _move_to_dlq(db, job, f"Unassigned 410: {resp.text}")
                            logger.error("Job %d rejected (410 Gone). DLQ.", job.id)
                            await asyncio.sleep(5)

                        elif resp.status_code == 422:
                            _move_to_dlq(
                                db, job, f"Validation error: {resp.text}"
                            )
                            logger.error(
                                "Validation error on job %d. Moved to DLQ.", job.id
                            )

                        else:
                            # Temporary server error – retry with backoff
                            job.retry_count += 1
                            job.error_reason = f"HTTP {resp.status_code}"
                            if job.retry_count > MAX_RETRIES:
                                _move_to_dlq(db, job, f"Max retries exceeded ({resp.status_code})")
                                logger.error("Job %d exceeded max retries. DLQ.", job.id)
                            else:
                                db.commit()
                            backoff = min(300, 5 * (2 ** min(job.retry_count, 6)))
                            logger.warning(
                                "Job %d retry %d – backoff %ds.",
                                job.id, job.retry_count, backoff,
                            )
                            await asyncio.sleep(backoff)
                            break  # Re-fetch from DB after backoff

                    except httpx.HTTPError as exc:
                        job.retry_count += 1
                        job.error_reason = str(exc)
                        if job.retry_count > MAX_RETRIES:
                            _move_to_dlq(db, job, f"Network failure: {exc}")
                        else:
                            db.commit()
                        backoff = min(300, 5 * (2 ** min(job.retry_count, 6)))
                        logger.warning(
                            "Network error on job %d (retry %d): %s – backoff %ds",
                            job.id, job.retry_count, exc, backoff,
                        )
                        await asyncio.sleep(backoff)
                        break

            except Exception as exc:
                logger.error("Upload worker loop error: %s", exc)
                await asyncio.sleep(settings.upload_interval)
            finally:
                db.close()


def _move_to_dlq(db, job: IngestJob, reason: str) -> None:
    """Move a permanently failed job to the Dead Letter Queue."""
    dlq = DeadLetterJob(
        original_id=job.id,
        payload_json=job.payload_json,
        error_reason=reason,
    )
    db.add(dlq)
    db.delete(job)
    db.commit()


def _transform_payload(payload: dict) -> dict:
    """Transform ESP32 sensor payload into cloud IngestRequest schema.

    ESP32 sends:
        {"mac_address": "...", "gateway_serial": "...",
         "readings": [{"kind": "bio_signal", "value": 1.23, "unit": "V"}]}

    Cloud expects:
        {"measurement_id": "...", "gateway_serial": "...",
         "readings": [{"sensor_mac": "...", "sensor_kind": "bio_signal",
                        "value": 1.23, "unit": "V", "timestamp": "..."}]}

    Each reading gets a unique timestamp spaced 20ms apart (50Hz sample rate)
    to avoid primary key collisions in TimescaleDB (PK = timestamp+sensor_id+kind).
    """
    from datetime import datetime, timedelta, timezone

    mac = payload.get("mac_address", "")
    gateway_serial = payload.get("gateway_serial", "")

    now = datetime.now(timezone.utc)
    n_readings = len(payload.get("readings", []))

    cloud_readings = []
    for i, reading in enumerate(payload.get("readings", [])):
        # Space readings 20ms apart, oldest first
        ts = now - timedelta(milliseconds=20 * (n_readings - 1 - i))
        cloud_readings.append({
            "sensor_mac": mac,
            "sensor_kind": reading.get("kind", reading.get("sensor_kind", "bio_signal")),
            "value": reading.get("value", 0.0),
            "unit": reading.get("unit", "V"),
            "timestamp": ts.isoformat(),
        })

    return {
        "measurement_id": str(uuid.uuid4()),
        "gateway_serial": gateway_serial,
        "readings": cloud_readings,
    }
