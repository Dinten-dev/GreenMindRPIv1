"""Actual local ingest remains healthy during simulated cloud outages."""

import asyncio
import hashlib
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import Session

from src.config import settings
from src.persistence import database
from src.persistence.models import DeadLetterJob, IngestJob
from src.runtime import heartbeat, remote_manager, reset, upload_worker, wav_uploader, wav_writer
from src.runtime.ingest_api import router


def test_cloud_failures_keep_ingest_health_and_local_samples_running(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    monkeypatch.setattr(database, "SQLALCHEMY_DATABASE_URL", "sqlite:///" + str(path))
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionLocal", None)
    monkeypatch.setattr(settings, "wav_dir", str(tmp_path / "wav"))
    monkeypatch.setattr(settings, "wav_min_free_bytes", 0)
    monkeypatch.setattr(wav_writer, "_get_cached_ntp", lambda: False)
    monkeypatch.setattr(wav_writer, "_storage_cache", None)
    monkeypatch.setattr(wav_uploader, "_retry_state", {})
    monkeypatch.setattr(heartbeat.NetworkManager, "get_wifi_rssi", AsyncMock(return_value=None))
    reset_call, command_call = AsyncMock(), AsyncMock()
    monkeypatch.setattr(reset, "trigger_remote_reset", reset_call)
    monkeypatch.setattr(remote_manager, "_execute_command", command_call)
    database.init_db()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    original_client, original_sleep = httpx.AsyncClient, asyncio.sleep
    phase = {"mode": 503, "calls": 0}

    async def cloud(request):
        phase["calls"] += 1
        if phase["mode"] == "dns":
            raise httpx.ConnectError("simulated DNS failure", request=request)
        if phase["mode"] == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if phase["mode"] != "recovered":
            return httpx.Response(phase["mode"], json={"detail": "unavailable"})
        if request.url.path.endswith("/commands"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/wav/upload"):
            [file] = wav_uploader._find_completed_wavs(settings.wav_dir)
            sha = hashlib.sha256(file.read_bytes()).hexdigest()
            return httpx.Response(201, json={"s3_key": "local_" + sha + ".wav"})
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(cloud), **kw),
    )

    async def fast_sleep(seconds):
        await original_sleep(min(seconds, 0.005))

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    credentials = {
        "api_key": "local-only",
        "gateway_id": "local-gateway",
        "hardware_id": settings.hardware_id,
        "server_url": "https://cloud.invalid/api/v1",
    }

    async def scenario():
        tasks = [
            asyncio.create_task(loop(credentials))
            for loop in (
                upload_worker.upload_loop,
                heartbeat.heartbeat_loop,
                remote_manager.remote_manager_loop,
            )
        ]
        try:
            async with original_client(
                transport=httpx.ASGITransport(app=app), base_url="http://local"
            ) as local:
                for mode in (503, 401, 403, 502, "dns", "timeout"):
                    phase["mode"] = mode
                    before = phase["calls"]
                    for _ in range(3):
                        response = await local.post(
                            "/api/v1/ingest",
                            json={
                                "mac_address": "AA:BB:CC:DD:EE:FF",
                                "sample_rate": 380,
                                "readings": [{"kind": "bio_signal", "value": 123.0, "unit": "mV"}],
                            },
                        )
                        assert response.status_code == 200
                        assert response.json()["samples_archived"] == 1
                        await original_sleep(0.01)
                    response = await local.get("/api/v1/health")
                    assert response.status_code == 200 and response.json()["status"] == "ok"
                    assert phase["calls"] > before
                    assert all(not task.done() for task in tasks)
                with Session(database.engine) as db:
                    assert db.query(IngestJob).count() == 18
                    assert db.query(DeadLetterJob).count() == 0
                wav_writer.close_all()
                [wav] = wav_uploader._find_completed_wavs(settings.wav_dir)
                original_wav = wav.read_bytes()
                tasks.append(asyncio.create_task(wav_uploader.upload_loop(credentials)))
                await original_sleep(0.03)
                assert wav.read_bytes() == original_wav
                phase["mode"] = "recovered"
                wav_uploader._retry_state.clear()
                for _ in range(200):
                    with Session(database.engine) as db:
                        pending = db.query(IngestJob).count()
                    if pending == 0 and not wav.exists():
                        break
                    await original_sleep(0.01)
                assert pending == 0 and not wav.exists()
                assert (await local.get("/api/v1/health")).status_code == 200
                assert all(not task.done() for task in tasks)
                assert reset_call.await_count == command_call.await_count == 0
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        wav_writer.close_all()
        database.engine.dispose()
