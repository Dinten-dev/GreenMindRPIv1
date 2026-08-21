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
from src.persistence.models import Base, DeadLetterJob, IngestJob
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
        assert '"mac_address": "AA:BB:CC:DD:EE:FF"' in job.payload_json
        assert '"gateway_serial": "test-gateway"' in job.payload_json


def test_ingest_caps_total_queue_records_including_dead_letters(monkeypatch):
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
    assert response.json() == {
        "status": "archived",
        "queue_full": True,
        "samples_archived": 1,
    }
    assert len(archived) == 1
    with session_factory() as session:
        assert session.query(IngestJob).count() == 0
        assert session.query(DeadLetterJob).count() == 1


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
