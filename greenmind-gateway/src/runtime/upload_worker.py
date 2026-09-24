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
import math
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
# How many raw ESP32 batches to coalesce into one cloud request.
BATCH_SIZE = 200

# Fixed namespace so the measurement_id derived from a set of job ids is stable
# across retries → the cloud's idempotency check dedupes a re-sent batch instead
# of double-inserting it.
_MEASUREMENT_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _aggregate_readings(payload: dict) -> list[dict]:
    """Reduce each sensor batch to dashboard-compatible per-kind summaries."""
    preaggregated = payload.get("readings", [])
    if preaggregated and all("sample_count" in reading for reading in preaggregated):
        return [
            {
                "sensor_kind": reading["kind"],
                "value": reading["value"],
                "unit": reading["unit"],
                "sample_count": reading["sample_count"],
                "sample_rate_hz": reading["sample_rate_hz"],
                "median": reading["median"],
                "rms": reading["rms"],
                "standard_deviation": reading["standard_deviation"],
                "minimum": reading["minimum"],
                "maximum": reading["maximum"],
                "p05": reading["p05"],
                "p95": reading["p95"],
                "coverage_ratio": reading["coverage_ratio"],
                "protocol_version": payload.get("protocol_version", 1),
                "firmware_version": payload.get("firmware_version"),
                "calibration_version": payload.get("calibration_version"),
                "quality_valid_count": reading.get("quality_valid_count"),
                "quality_lead_off_count": reading.get("quality_lead_off_count"),
                "quality_rail_high_count": reading.get("quality_rail_high_count"),
                "quality_rail_low_count": reading.get("quality_rail_low_count"),
                "quality_jump_count": reading.get("quality_jump_count"),
                "quality_recovery_count": reading.get("quality_recovery_count"),
            }
            for reading in preaggregated
        ]

    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for reading in payload.get("readings", []):
        kind = reading.get("kind", reading.get("sensor_kind", "bio_signal"))
        unit = reading.get("unit", "mV")
        groups[(kind, unit)].append(float(reading.get("value", 0.0)))

    sample_rate = int(payload.get("sample_rate", 0) or 0)
    quality = payload.get("quality_counts") or {}
    aggregates = []
    for (kind, unit), values in groups.items():
        sample_count = len(values)
        mean = sum(values) / sample_count
        variance = sum((value - mean) ** 2 for value in values) / sample_count
        rms = math.sqrt(sum(value * value for value in values) / sample_count)
        aggregates.append(
            {
                "sensor_kind": kind,
                "value": mean,
                "unit": unit,
                "sample_count": sample_count,
                "sample_rate_hz": float(sample_rate) if sample_rate else None,
                "median": _percentile(values, 0.5),
                "rms": rms,
                "standard_deviation": math.sqrt(variance),
                "minimum": min(values),
                "maximum": max(values),
                "p05": _percentile(values, 0.05),
                "p95": _percentile(values, 0.95),
                "coverage_ratio": min(1.0, sample_count / sample_rate) if sample_rate else 1.0,
                "protocol_version": payload.get("protocol_version", 1),
                "firmware_version": payload.get("firmware_version"),
                "calibration_version": payload.get("calibration_version"),
                "quality_valid_count": quality.get("valid"),
                "quality_lead_off_count": quality.get("lead_off"),
                "quality_rail_high_count": quality.get("rail_high"),
                "quality_rail_low_count": quality.get("rail_low"),
                "quality_jump_count": quality.get("jump"),
                "quality_recovery_count": quality.get("recovery"),
            }
        )
    return aggregates


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
            if action == "RESET_TO_SETUP_MODE" and settings.allow_remote_reset:
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

    The gateway keeps every raw sample in WAV storage. PostgreSQL receives one
    statistical row per sensor batch and kind, retaining the existing dashboard
    ``bio_signal`` series without duplicating hundreds of raw samples per second.
    """
    readings: list[dict] = []
    for job, payload in items:
        mac = payload.get("mac_address", "")
        ts = job.created_at
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_iso = ts.isoformat() if ts is not None else None

        source_metadata = {
            "source_boot_id": payload.get("boot_id"),
            "source_sequence": payload.get("sequence"),
            "source_uptime_ms": payload.get("uptime_ms"),
            "source_dropped_samples_total": payload.get("dropped_samples_total"),
        }
        for aggregate in _aggregate_readings(payload):
            readings.append(
                {
                    "sensor_mac": mac,
                    **aggregate,
                    "timestamp": ts_iso,
                    **source_metadata,
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
        "aggregation_window": "sensor_batch",
        "readings": readings,
    }


def _transform_payload(payload: dict) -> dict:
    """Transform a single ESP32 payload into a cloud IngestRequest.

    Retained for single-payload callers/tests. Prefer _build_cloud_request for
    the drain loop, which batches many jobs into one request.
    """
    from datetime import datetime

    mac = payload.get("mac_address", "")
    gateway_serial = payload.get("gateway_serial", "")
    source_metadata = {
        "source_boot_id": payload.get("boot_id"),
        "source_sequence": payload.get("sequence"),
        "source_uptime_ms": payload.get("uptime_ms"),
        "source_dropped_samples_total": payload.get("dropped_samples_total"),
    }

    now = datetime.now(timezone.utc)
    cloud_readings = [
        {
            "sensor_mac": mac,
            **aggregate,
            "timestamp": now.isoformat(),
            **source_metadata,
        }
        for aggregate in _aggregate_readings(payload)
    ]

    return {
        "measurement_id": str(uuid.uuid4()),
        "gateway_serial": gateway_serial,
        "aggregation_window": "sensor_batch",
        "readings": cloud_readings,
    }
