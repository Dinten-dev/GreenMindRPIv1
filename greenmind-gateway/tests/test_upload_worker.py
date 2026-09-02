import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.persistence.models import Base, DeadLetterJob, IngestJob
from src.runtime import upload_worker


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(db, mac: str) -> tuple[IngestJob, dict]:
    payload = {
        "mac_address": mac,
        "sample_rate": 380,
        "readings": [{"kind": "bio_signal", "value": 123.0, "unit": "mV"}],
    }
    import json

    job = IngestJob(
        payload_json=json.dumps(payload),
        status="QUEUED",
        created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job, payload


class StaticClient:
    def __init__(self, status_code: int, detail: str | None = None):
        self.status_code = status_code
        self.detail = detail

    async def post(self, url, **_kwargs):
        body = {"detail": self.detail} if self.detail else {}
        return httpx.Response(
            self.status_code,
            json=body,
            request=httpx.Request("POST", url),
        )


@pytest.mark.parametrize("status_code", [401, 500, 503])
def test_outage_and_gateway_auth_failures_remain_queued(monkeypatch, status_code):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(upload_worker.asyncio, "sleep", no_sleep)
    db = _session()
    job, payload = _job(db, "AA:BB:CC:DD:EE:01")

    handled = asyncio.run(
        upload_worker._flush_group(
            StaticClient(status_code),
            db,
            "https://example.invalid/api/v1",
            {"X-Api-Key": "redacted"},
            "gateway-1",
            [(job, payload)],
        )
    )

    assert handled is False
    assert db.query(IngestJob).filter_by(id=job.id, status="QUEUED").one().retry_count == 1
    assert db.query(DeadLetterJob).count() == 0
    db.close()


def test_unknown_sensor_isolated_without_losing_recoverable_job(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(upload_worker.asyncio, "sleep", no_sleep)
    db = _session()
    valid = _job(db, "AA:BB:CC:DD:EE:01")
    unknown = _job(db, "AA:BB:CC:DD:EE:02")

    class MixedClient:
        async def post(self, url, json, **_kwargs):
            macs = {reading["sensor_mac"] for reading in json["readings"]}
            status = 201 if macs == {"AA:BB:CC:DD:EE:01"} else 403
            detail = None if status == 201 else upload_worker.UNKNOWN_SENSOR_DETAIL
            return httpx.Response(
                status,
                json={"detail": detail} if detail else {},
                request=httpx.Request("POST", url),
            )

    handled = asyncio.run(
        upload_worker._flush_group(
            MixedClient(),
            db,
            "https://example.invalid/api/v1",
            {"X-Api-Key": "redacted"},
            "gateway-1",
            [valid, unknown],
        )
    )

    assert handled is False
    assert db.query(IngestJob).filter_by(id=valid[0].id).first() is None
    retained = db.query(IngestJob).filter_by(id=unknown[0].id, status="QUEUED").one()
    assert retained.retry_count == 1
    assert db.query(DeadLetterJob).count() == 0
    db.close()


def test_cloud_payload_forwards_source_continuity_metadata():
    db = _session()
    job, payload = _job(db, "AA:BB:CC:DD:EE:01")
    payload.update({"sequence": 42, "uptime_ms": 123_456, "dropped_samples_total": 7})

    cloud = upload_worker._build_cloud_request("gateway-1", [(job, payload)])

    [reading] = cloud["readings"]
    assert reading["source_sequence"] == 42
    assert reading["source_uptime_ms"] == 123_456
    assert reading["source_dropped_samples_total"] == 7
    db.close()


def test_cloud_payload_reduces_one_second_batch_to_one_sql_row():
    db = _session()
    job, payload = _job(db, "AA:BB:CC:DD:EE:01")
    payload.update(
        {
            "readings": [
                {"kind": "bio_signal", "value": value, "unit": "mV"}
                for value in (100.0, 200.0, 300.0, 400.0)
            ],
            "sample_rate": 4,
            "protocol_version": 2,
            "firmware_version": "1.4.0",
            "calibration_version": "nominal-adc-3v3-v1",
            "boot_id": 123,
            "quality_counts": {
                "valid": 3,
                "lead_off": 1,
                "rail_high": 0,
                "rail_low": 0,
                "jump": 0,
                "recovery": 0,
            },
        }
    )

    cloud = upload_worker._build_cloud_request("gateway-1", [(job, payload)])

    [reading] = cloud["readings"]
    assert cloud["aggregation_window"] == "sensor_batch"
    assert reading["value"] == 250.0
    assert reading["sample_count"] == 4
    assert reading["median"] == 250.0
    assert reading["rms"] == pytest.approx(273.86127875)
    assert reading["coverage_ratio"] == 1.0
    assert reading["quality_valid_count"] == 3
    assert reading["source_boot_id"] == 123
    db.close()
