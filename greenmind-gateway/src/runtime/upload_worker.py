"""Async upload worker that drains the SQLite queue to the cloud backend.

Uses httpx.AsyncClient with exponential backoff. Permanent failures (after 20
retries or 4xx errors) are moved to the Dead Letter Queue.
"""

import asyncio
import json
import logging

import httpx

from src.config import settings
from src.persistence.database import SessionLocal
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
            db = SessionLocal()
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

                    try:
                        resp = await client.post(
                            f"{server_url}/ingest",
                            json=payload,
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
