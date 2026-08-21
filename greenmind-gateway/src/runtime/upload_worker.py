"""Async upload worker that drains the SQLite queue to the cloud backend.

Coalesces many queued jobs into a single /ingest request (the cloud endpoint
accepts a list of readings, each tagged with its own sensor_mac), which lets a
single gateway serve 10+ sensors instead of being capped by one HTTP round-trip
per reading. Uses httpx.AsyncClient with exponential backoff. Transient network,
server, and gateway-auth failures remain queued; only malformed local records or
individually confirmed validation failures enter the Dead Letter Queue.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from collections import defaultdict
from datetime import timezone

import httpx

from src.config import settings
from src.persistence import database
from src.persistence.models import DeadLetterJob, IngestJob

logger = logging.getLogger(__name__)

MAX_RETRIES = 20
UNKNOWN_SENSOR_DETAIL = "Every sensor must already be registered to the authenticated gateway"
# How many queued jobs to coalesce into one cloud request. Each job is usually a
# single aggregate reading, so this is roughly "readings per HTTP round-trip".
BATCH_SIZE = 200

# Fixed namespace so the measurement_id derived from a set of job ids is stable
# across retries → the cloud's idempotency check dedupes a re-sent batch instead
# of double-inserting it.
_MEASUREMENT_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


async def upload_loop(credentials: dict) -> None:
    """Continuously drain the ingest queue and POST batches to the cloud."""
    api_key = credentials["api_key"]
    server_url = credentials.get("server_url") or settings.cloud_api_url
    headers = {"X-Api-Key": api_key}

    logger.info(
        "Upload worker started → %s/ingest (bulk mode, up to %d/req)", server_url, BATCH_SIZE
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
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

                # Group by source gateway_serial (normally one) and drain each
                # group in a single request.
                groups: dict[str, list[tuple[IngestJob, dict]]] = defaultdict(list)
                for job in jobs:
                    try:
                        payload = json.loads(job.payload_json)
                    except (ValueError, TypeError) as exc:
                        _move_to_dlq(db, job, f"Malformed payload_json: {exc}")
                        logger.error("Job %d has malformed JSON. Moved to DLQ.", job.id)
                        continue
                    serial = payload.get("gateway_serial") or settings.hardware_id
                    groups[serial].append((job, payload))
                db.commit()  # persist any DLQ moves from malformed jobs

                # If every job in the fetch was malformed, loop again immediately.
                if not groups:
                    continue

                stop = False
                for serial, items in groups.items():
                    if not await _flush_group(client, db, server_url, headers, serial, items):
                        stop = True
                        break  # transient failure → back off before re-fetching
                if stop:
                    continue

            except Exception as exc:
                logger.error("Upload worker loop error: %s", exc)
                await asyncio.sleep(settings.upload_interval)
            finally:
                db.close()


async def _flush_group(
    client: httpx.AsyncClient,
    db,
    server_url: str,
    headers: dict,
    serial: str,
    items: list[tuple[IngestJob, dict]],
) -> bool:
    """Upload one gateway's batch in a single request.

    Returns True if the group was handled (drained or isolated) and the loop may
    continue immediately, False on a transient failure that already applied a
    backoff and wants the loop to re-fetch.
    """
    cloud_payload = _build_cloud_request(serial, items)

    try:
        resp = await client.post(f"{server_url}/ingest", json=cloud_payload, headers=headers)
    except httpx.HTTPError as exc:
        backoff = _record_transient_failure(db, items, str(exc))
        logger.warning("Network error on batch of %d (%s) – backoff %ds", len(items), exc, backoff)
        await asyncio.sleep(backoff)
        return False

    if resp.status_code in (200, 201, 202):
        for job, _ in items:
            db.delete(job)
        db.commit()
        logger.info("Uploaded batch: %d readings from %s.", len(cloud_payload["readings"]), serial)
        return True

    if resp.status_code == 401:
        # A gateway credential can be repaired after an outage or rotation. Keep
        # every measurement queued instead of converting an auth outage into
        # permanent data loss.
        backoff = _record_transient_failure(db, items, "Gateway authentication failed (401)")
        logger.error(
            "[E-202] Cloud rejected batch (auth %d). Backoff %ds.", resp.status_code, backoff
        )
        await asyncio.sleep(min(60, backoff))
        return False

    if resp.status_code == 403:
        if _response_detail(resp) == UNKNOWN_SENSOR_DETAIL:
            logger.warning(
                "Cloud rejected a mixed batch for sensor assignment; isolating %d jobs.",
                len(items),
            )
            return await _isolate_rejected(client, db, server_url, headers, serial, items)

        backoff = _record_transient_failure(db, items, "Gateway authorization failed (403)")
        logger.error("Cloud rejected gateway authorization (403). Backoff %ds.", backoff)
        await asyncio.sleep(min(60, backoff))
        return False

    if resp.status_code == 410:
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("Cloud returned malformed JSON with HTTP 410: %s", exc)
        else:
            detail = data.get("detail") if isinstance(data, dict) else None
            action = detail.get("action") if isinstance(detail, dict) else None
            if action == "RESET_TO_SETUP_MODE":
                logger.critical("Gateway deleted remotely. Initiating reset sequence.")
                from src.runtime.reset import trigger_remote_reset

                await trigger_remote_reset()
        logger.error("Batch rejected (410 Gone). Backing off.")
        await asyncio.sleep(5)
        return False

    if resp.status_code == 422:
        # A poison job somewhere in the batch. Isolate by retrying per job so one
        # bad reading can't block the whole queue.
        logger.warning("Batch validation error (422). Isolating %d jobs individually.", len(items))
        return await _isolate_rejected(client, db, server_url, headers, serial, items)

    # Other 5xx – transient. Back off and retry the batch.
    backoff = _record_transient_failure(db, items, f"HTTP {resp.status_code}")
    logger.warning("Batch HTTP %d – backoff %ds.", resp.status_code, backoff)
    await asyncio.sleep(backoff)
    return False


async def _isolate_rejected(client, db, server_url, headers, serial, items) -> bool:
    """Retry jobs alone, draining valid jobs without losing recoverable ones."""
    retained = False
    max_backoff = 0
    for job, payload in items:
        single = _build_cloud_request(serial, [(job, payload)])
        try:
            resp = await client.post(f"{server_url}/ingest", json=single, headers=headers)
        except httpx.HTTPError as exc:
            max_backoff = max(
                max_backoff,
                _record_transient_failure(db, [(job, payload)], f"Network failure: {exc}"),
            )
            retained = True
            continue

        if resp.status_code in (200, 201, 202):
            db.delete(job)
            db.commit()
        elif resp.status_code == 422:
            _move_to_dlq(db, job, "Cloud validation error (HTTP 422)")
            logger.error("Job %d is a poison pill. Moved to DLQ.", job.id)
        else:
            reason = f"HTTP {resp.status_code}"
            if resp.status_code == 403 and _response_detail(resp) == UNKNOWN_SENSOR_DETAIL:
                reason = "Sensor is not assigned to this gateway (403)"
            max_backoff = max(
                max_backoff,
                _record_transient_failure(db, [(job, payload)], reason),
            )
            retained = True

    if retained:
        await asyncio.sleep(min(60, max_backoff))
        return False
    return True


def _response_detail(response: httpx.Response) -> str | None:
    """Return a bounded FastAPI error detail without trusting response shape."""
    try:
        payload = response.json()
    except ValueError:
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, str) and len(detail) <= 500 else None


def _record_transient_failure(db, items: list[tuple[IngestJob, dict]], reason: str) -> int:
    """Keep transiently failed jobs queued and return capped exponential backoff."""
    max_retry = 0
    for job, _ in items:
        job.retry_count = min((job.retry_count or 0) + 1, MAX_RETRIES)
        job.error_reason = reason[:500]
        max_retry = max(max_retry, job.retry_count)
    db.commit()
    return min(300, 5 * (2 ** min(max_retry, 6)))


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


def _build_cloud_request(serial: str, items: list[tuple[IngestJob, dict]]) -> dict:
    """Coalesce queued jobs into one cloud IngestRequest.

    Each queued job carries an ESP32 payload:
        {"mac_address": "...", "readings": [{"kind","value","unit"}, ...]}

    The cloud expects one request with a flat readings list, each reading tagged
    with sensor_mac and an absolute timestamp. We use the job's created_at (when
    the gateway received the batch) as the timestamp so a drained backlog keeps
    real capture times instead of being stamped at upload time.
    """
    readings: list[dict] = []
    for job, payload in items:
        mac = payload.get("mac_address", "")
        ts = job.created_at
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_iso = ts.isoformat() if ts is not None else None

        job_readings = payload.get("readings", [])
        sample_rate = payload.get("sample_rate", 0) or 0
        n = len(job_readings)
        # For multi-sample batches, spread timestamps back from created_at using
        # the sample rate so intra-batch ordering is preserved.
        spacing_ms = (1000.0 / sample_rate) if (n > 1 and sample_rate > 0) else 0.0

        for i, r in enumerate(job_readings):
            if spacing_ms and ts is not None:
                from datetime import timedelta

                rt = (ts - timedelta(milliseconds=spacing_ms * (n - 1 - i))).isoformat()
            else:
                rt = ts_iso
            readings.append(
                {
                    "sensor_mac": mac,
                    "sensor_kind": r.get("kind", r.get("sensor_kind", "bio_signal")),
                    "value": r.get("value", 0.0),
                    "unit": r.get("unit", "mV"),
                    "timestamp": rt,
                }
            )

    # Generate a unique deterministic string representing this batch of jobs
    parts = []
    for job, _ in items:
        ts_str = job.created_at.isoformat() if job.created_at else ""
        payload_hash = hashlib.sha256(job.payload_json.encode("utf-8")).hexdigest()
        parts.append(f"{job.id}:{ts_str}:{payload_hash}")

    parts.sort()
    measurement_id = str(uuid.uuid5(_MEASUREMENT_NS, ",".join(parts)))

    return {
        "measurement_id": measurement_id,
        "gateway_serial": serial,
        "readings": readings,
    }


def _transform_payload(payload: dict) -> dict:
    """Transform a single ESP32 payload into a cloud IngestRequest.

    Retained for single-payload callers/tests. Prefer _build_cloud_request for
    the drain loop, which batches many jobs into one request.
    """
    from datetime import datetime, timedelta

    mac = payload.get("mac_address", "")
    gateway_serial = payload.get("gateway_serial", "")
    sample_rate = payload.get("sample_rate", 20)

    now = datetime.now(timezone.utc)
    readings = payload.get("readings", [])
    n_readings = len(readings)
    spacing_ms = 0 if n_readings <= 1 else (1000.0 / sample_rate if sample_rate > 0 else 50)

    cloud_readings = []
    for i, reading in enumerate(readings):
        ts = (
            now
            if n_readings <= 1
            else now - timedelta(milliseconds=spacing_ms * (n_readings - 1 - i))
        )
        cloud_readings.append(
            {
                "sensor_mac": mac,
                "sensor_kind": reading.get("kind", reading.get("sensor_kind", "bio_signal")),
                "value": reading.get("value", 0.0),
                "unit": reading.get("unit", "mV"),
                "timestamp": ts.isoformat(),
            }
        )

    return {
        "measurement_id": str(uuid.uuid4()),
        "gateway_serial": gateway_serial,
        "readings": cloud_readings,
    }
