import json
import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import settings
from src.http_limits import RequestBodyLimitMiddleware
from src.persistence.database import get_db
from src.persistence.models import Base, DeadLetterJob, IngestJob, SensorBatchCursor
from src.runtime import wav_writer
from src.runtime.ingest_api import BoundedSensorIPCache, router
from src.validation import SensorBatch


def valid_payload(**updates):
    payload = {
        "mac_address": "aa-bb-cc-dd-ee-ff",
        "sample_rate": 380,
        "readings": [{"kind": "bio_signal", "value": 123.4, "unit": "mV"}],
    }
    payload.update(updates)
    return payload


def test_batch_canonicalizes_mac():
    batch = SensorBatch.model_validate(valid_payload())
    assert batch.mac_address == "AA:BB:CC:DD:EE:FF"
    assert batch.protocol_version == 1


def test_batch_accepts_bounded_source_continuity_metadata():
    batch = SensorBatch.model_validate(
        valid_payload(sequence=42, uptime_ms=123_456, dropped_samples_total=7)
    )
    assert batch.sequence == 42
    assert batch.uptime_ms == 123_456
    assert batch.dropped_samples_total == 7


def test_batch_accepts_versioned_quality_metadata():
    batch = SensorBatch.model_validate(
        valid_payload(
            protocol_version=2,
            firmware_version="1.4.0",
            calibration_version="nominal-adc-3v3-v1",
            boot_id=42,
            quality_counts={
                "valid": 1,
                "lead_off": 0,
                "rail_high": 0,
                "rail_low": 0,
                "jump": 0,
                "recovery": 0,
            },
        )
    )
    assert batch.protocol_version == 2
    assert batch.quality_counts.valid == 1


def test_batch_accepts_compact_protocol_three_samples():
    batch = SensorBatch.model_validate(
        {
            "mac_address": "aa-bb-cc-dd-ee-ff",
            "sample_rate": 380,
            "protocol_version": 3,
            "kind": "bio_signal",
            "unit": "mV",
            "value_scale_mv": 0.1,
            "values_deci_mv": [1000, 1001, 1002],
        }
    )

    assert batch.decoded_values_mv() == pytest.approx([100.0, 100.1, 100.2])


def test_batch_rejects_quality_count_above_sample_count():
    with pytest.raises(ValidationError, match="quality counter exceeds"):
        SensorBatch.model_validate(
            valid_payload(
                quality_counts={
                    "valid": 2,
                    "lead_off": 0,
                    "rail_high": 0,
                    "rail_low": 0,
                    "jump": 0,
                    "recovery": 0,
                }
            )
        )


@pytest.mark.parametrize(
    "change",
    [
        {"mac_address": "../../escape"},
        {"sample_rate": 20},
        {"sample_rate": "380"},
        {"readings": []},
        {"readings": [{"kind": "temperature", "value": 1.0, "unit": "mV"}]},
        {"readings": [{"kind": "bio_signal", "value": 1.0, "unit": "V"}]},
        {"readings": [{"kind": "bio_signal", "value": math.inf, "unit": "mV"}]},
        {"gateway_serial": "attacker-controlled"},
    ],
)
def test_batch_rejects_invalid_protocol_values(change):
    with pytest.raises(ValidationError):
        SensorBatch.model_validate(valid_payload(**change))


def test_batch_rejects_excess_samples(monkeypatch):
    monkeypatch.setattr(settings, "max_samples_per_batch", 2)
    readings = [{"kind": "bio_signal", "value": float(index), "unit": "mV"} for index in range(3)]
    with pytest.raises(ValidationError, match="readings exceeds"):
        SensorBatch.model_validate(valid_payload(readings=readings))


def test_sensor_ip_cache_is_bounded_and_canonical(monkeypatch):
    monkeypatch.setattr(settings, "max_sensor_ip_entries", 2)
    cache = BoundedSensorIPCache()
    cache.remember("00-00-00-00-00-01", "192.0.2.1")
    cache.remember("00:00:00:00:00:02", "192.0.2.2")
    cache.remember("00:00:00:00:00:03", "192.0.2.3")
    assert len(cache) == 2
    assert cache.get("00:00:00:00:00:01") is None
    assert cache.get("00-00-00-00-00-03") == "192.0.2.3"


def test_request_body_limit_rejects_declared_oversize():
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=16)

    @app.post("/")
    async def endpoint():
        return {"ok": True}

    response = TestClient(app).post("/", content=b"x" * 17)
    assert response.status_code == 413


def test_ingest_queues_only_validated_aggregate(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(wav_writer, "write_samples", lambda *args, **kwargs: None)
    monkeypatch.setattr(settings, "hardware_id", "test-gateway")

    response = TestClient(app).post("/api/v1/ingest", json=valid_payload())
    assert response.status_code == 200
    assert response.json()["samples_archived"] == 1

    with session_factory() as session:
        [job] = session.query(IngestJob).all()
        queued = json.loads(job.payload_json)
        assert queued["mac_address"] == "AA:BB:CC:DD:EE:FF"
        assert queued["gateway_serial"] == "test-gateway"


def test_ingest_preserves_source_continuity_metadata(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(wav_writer, "write_samples", lambda *args, **kwargs: None)

    response = TestClient(app).post(
        "/api/v1/ingest",
        json=valid_payload(sequence=42, uptime_ms=123_456, dropped_samples_total=7),
    )

    assert response.status_code == 200
    with session_factory() as session:
        [job] = session.query(IngestJob).all()
        payload = json.loads(job.payload_json)
        assert payload["sequence"] == 42
        assert payload["uptime_ms"] == 123_456
        assert payload["dropped_samples_total"] == 7


def test_ingest_never_drops_aggregate_at_queue_warning_threshold(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_db
    archived = []
    monkeypatch.setattr(wav_writer, "write_samples", lambda *args, **kwargs: archived.append(args))
    monkeypatch.setattr(settings, "max_queue_size", 1)

    with session_factory() as session:
        session.add(DeadLetterJob(payload_json="{}", error_reason="poison"))
        session.commit()

    response = TestClient(app).post("/api/v1/ingest", json=valid_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert len(archived) == 1
    with session_factory() as session:
        assert session.query(IngestJob).count() == 1
        assert session.query(DeadLetterJob).count() == 1


def test_ingest_deduplicates_replayed_boot_sequence(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    writes = []
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(wav_writer, "write_samples", lambda *args, **kwargs: writes.append(args))

    payload = valid_payload(boot_id=123, sequence=7)
    first = TestClient(app).post("/api/v1/ingest", json=payload)
    replay = TestClient(app).post("/api/v1/ingest", json=payload)

    assert first.status_code == 200
    assert first.json()["boot_id"] == 123
    assert first.json()["sequence"] == 7
    assert replay.status_code == 200
    assert replay.json()["status"] == "duplicate"
    assert len(writes) == 1
    with session_factory() as session:
        assert session.query(IngestJob).count() == 1
        assert session.query(SensorBatchCursor).count() == 1


def test_compact_ingest_preserves_capture_time_and_statistics(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    captured = []
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(
        wav_writer,
        "write_samples",
        lambda *args, **kwargs: captured.append(kwargs["captured_at_epoch_ms"]),
    )

    epoch_ms = 1_767_225_601_000
    response = TestClient(app).post(
        "/api/v1/ingest",
        json={
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "sample_rate": 380,
            "protocol_version": 3,
            "boot_id": 10,
            "sequence": 20,
            "captured_at_epoch_ms": epoch_ms,
            "kind": "bio_signal",
            "unit": "mV",
            "value_scale_mv": 0.1,
            "values_deci_mv": [1000] * 380,
        },
    )

    assert response.status_code == 200
    assert captured == [epoch_ms]
    with session_factory() as session:
        [job] = session.query(IngestJob).all()
        queued = json.loads(job.payload_json)
        assert queued["readings"][0]["sample_count"] == 380
        assert queued["readings"][0]["coverage_ratio"] == 1.0
        assert job.created_at.isoformat().startswith("2026-01-01T00:00:00")


def test_ingest_reports_storage_exhaustion(monkeypatch):
    class Query:
        def filter(self, *_args):
            return self

        def count(self):
            return 0

    class Session:
        def query(self, *_args):
            return Query()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: Session()

    def refuse(*_args, **_kwargs):
        raise wav_writer.WavStorageError("full")

    monkeypatch.setattr(wav_writer, "write_samples", refuse)
    response = TestClient(app).post("/api/v1/ingest", json=valid_payload())
    assert response.status_code == 507
