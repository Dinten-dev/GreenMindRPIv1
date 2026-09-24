"""Concurrent sensor retries must archive one complete batch before ACK."""

import time
import wave
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.config import settings
from src.persistence import database
from src.persistence.models import IngestJob, SensorBatchCursor
from src.runtime import wav_writer
from src.runtime.ingest_api import router


def test_concurrent_retries_archive_once_and_ack_is_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setattr(database, "SQLALCHEMY_DATABASE_URL", "sqlite:///" + database.DB_PATH)
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionLocal", None)
    monkeypatch.setattr(settings, "wav_dir", str(tmp_path / "wav"))
    monkeypatch.setattr(settings, "wav_min_free_bytes", 0)
    monkeypatch.setattr(wav_writer, "_storage_cache", None)
    monkeypatch.setattr(wav_writer, "_get_cached_ntp", lambda: False)
    database.init_db()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    write = wav_writer.write_samples

    def slow_write(*args, **kwargs):
        # Expose the window between cursor lookup and durable acknowledgement.
        time.sleep(0.02)
        return write(*args, **kwargs)

    monkeypatch.setattr(wav_writer, "write_samples", slow_write)
    payload = {
        "mac_address": "AA:BB:CC:12:34:56",
        "protocol_version": 3,
        "sample_rate": 380,
        "boot_id": 123,
        "sequence": 0,
        "kind": "bio_signal",
        "unit": "mV",
        "value_scale_mv": 0.1,
        "values_deci_mv": [1200] * 380,
    }
    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(
                pool.map(lambda _: client.post("/api/v1/ingest", json=payload), range(24))
            )
        assert all(response.status_code == 200 for response in responses)
        assert sum(response.json()["status"] == "queued" for response in responses) == 1
        assert sum(response.json()["status"] == "duplicate" for response in responses) == 23
        with database.SessionLocal() as db:
            assert db.query(IngestJob).count() == 1
            assert db.query(SensorBatchCursor).one().last_sequence == 0
            assert db.execute(text("PRAGMA synchronous")).scalar() == 2  # FULL
        # Read from a different descriptor before close_all: ACK must not leave
        # the header or samples stranded in the writer's userspace buffer.
        parts = list((tmp_path / "wav").rglob("*.part"))
        assert len(parts) == 1
        with wave.open(str(parts[0]), "rb") as archived:
            assert archived.getnframes() == 380
            assert len(archived.readframes(380)) == 760
    finally:
        wav_writer.close_all()
        database.engine.dispose()
