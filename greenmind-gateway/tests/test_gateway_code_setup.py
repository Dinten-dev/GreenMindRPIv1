import asyncio

from src.config import settings
from src.runtime import gateway_app, wav_writer


def test_old_ble_flag_never_restarts_the_deprecated_provisioning_worker(monkeypatch):
    monkeypatch.setattr(settings, "enable_ble_provisioning", True)
    monkeypatch.setattr(gateway_app, "init_db", lambda: None)
    monkeypatch.setattr(wav_writer, "recover_part_files", lambda: [])
    monkeypatch.setattr(wav_writer, "close_all", lambda: [])

    async def wait(*args):
        await asyncio.Future()

    for name in (
        "upload_loop",
        "heartbeat_loop",
        "remote_manager_loop",
        "udp_discovery_server",
        "wav_upload_loop",
        "cloud_sync_worker",
    ):
        monkeypatch.setattr(gateway_app, name, wait)

    async def run():
        async with gateway_app.lifespan(gateway_app.app):
            names = {task.get_name() for task in asyncio.all_tasks()}
            assert "upload_worker" in names and "wav_uploader" in names
            assert "provisioning_worker" not in names

    asyncio.run(run())
