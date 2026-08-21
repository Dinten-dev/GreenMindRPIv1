import array
import os
import struct
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from src.config import settings
from src.runtime import wav_writer


@pytest.fixture(autouse=True)
def isolated_wav_storage(tmp_path, monkeypatch):
    wav_writer.close_all()
    monkeypatch.setattr(settings, "wav_dir", str(tmp_path))
    monkeypatch.setattr(settings, "wav_chunk_minutes", 10)
    monkeypatch.setattr(settings, "wav_max_open_writers", 64)
    monkeypatch.setattr(settings, "wav_min_free_bytes", 0)
    monkeypatch.setattr(settings, "wav_max_pending_files", 10_000)
    monkeypatch.setattr(settings, "wav_max_pending_bytes", 20 * 1024**3)
    wav_writer._invalidate_storage_cache()
    yield tmp_path
    wav_writer.close_all()
    wav_writer._invalidate_storage_cache()


def test_conversion_equivalence():
    values = [-5.0, 0.0, 1650.0, 3300.0, 4000.0]
    old_method = b"".join(
        struct.pack("<h", int(max(0.0, min(value, 3300.0)) / 3300.0 * 32767)) for value in values
    )
    new_method = array.array(
        "h", (int(max(0.0, min(value, 3300.0)) * (32767 / 3300.0)) for value in values)
    ).tobytes()
    assert old_method == new_method


def test_active_chunk_is_hidden_until_atomic_finalize(isolated_wav_storage):
    wav_writer.write_samples("aa-bb-cc-dd-ee-ff", [1000.0] * 10, 380)
    assert not list(isolated_wav_storage.rglob("*.wav"))
    assert len(list(isolated_wav_storage.rglob("*.wav.part"))) == 1

    [completed] = wav_writer.close_all()
    assert completed.endswith(".wav")
    assert os.path.exists(completed)
    assert not list(isolated_wav_storage.rglob("*.part"))
    with wave.open(completed, "rb") as reader:
        assert reader.getnframes() == 10


def test_wav_writer_concurrency():
    macs = [f"00:11:22:33:44:{index:02X}" for index in range(1, 5)]

    def write_task(mac):
        for _ in range(20):
            wav_writer.write_samples(mac, [1500.0] * 38, 380)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write_task, macs))

    completed_paths = wav_writer.close_all()
    assert len(completed_paths) == 4
    for path in completed_paths:
        with wave.open(path, "rb") as reader:
            assert reader.getnframes() == 760


def test_writer_registry_race():
    mac = "AA:BB:CC:DD:EE:FF"
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: wav_writer.write_samples(mac, [1000.0] * 50, 380), range(4)))

    assert wav_writer.active_writer_count() == 1
    [completed] = wav_writer.close_all()
    with wave.open(completed, "rb") as reader:
        assert reader.getnframes() == 200


def test_lru_bound_finalizes_oldest_writer(monkeypatch, isolated_wav_storage):
    monkeypatch.setattr(settings, "wav_max_open_writers", 2)
    for mac in ("00:00:00:00:00:01", "00:00:00:00:00:02", "00:00:00:00:00:03"):
        wav_writer.write_samples(mac, [1.0], 380)

    assert wav_writer.active_writer_count() == 2
    assert len(list(isolated_wav_storage.rglob("*.wav"))) == 1
    assert len(list(isolated_wav_storage.rglob("*.wav.part"))) == 2


def test_rotation(monkeypatch):
    monkeypatch.setattr(settings, "wav_chunk_minutes", 1)

    class MockDatetime:
        _current = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            cls._current += timedelta(seconds=1)
            return cls._current

        strptime = staticmethod(datetime.strptime)

    monkeypatch.setattr(wav_writer, "datetime", MockDatetime)
    mac = "FF:EE:DD:CC:BB:AA"
    assert wav_writer.write_samples(mac, [500.0] * 10, 380) is None
    MockDatetime._current += timedelta(seconds=60)
    rotated_path = wav_writer.write_samples(mac, [500.0] * 10, 380)

    assert rotated_path is not None
    with open(rotated_path, "rb") as completed:
        assert b"ICRD" in completed.read()


def test_rejects_path_unsafe_identity(isolated_wav_storage):
    with pytest.raises(ValueError, match="MAC"):
        wav_writer.write_samples("../../etc/passwd", [1.0], 380)
    assert not list(isolated_wav_storage.iterdir())


def test_pending_limit_retains_unacknowledged_file(monkeypatch, isolated_wav_storage):
    pending_dir = isolated_wav_storage / "AABBCCDDEEFF"
    pending_dir.mkdir()
    pending = pending_dir / "AABBCCDDEEFF_20260101T000000.wav"
    pending.write_bytes(b"unacknowledged")
    monkeypatch.setattr(settings, "wav_max_pending_files", 1)
    wav_writer._invalidate_storage_cache()

    with pytest.raises(wav_writer.WavStorageError, match="maximum number"):
        wav_writer.write_samples("00:11:22:33:44:55", [1.0], 380)
    assert pending.read_bytes() == b"unacknowledged"


def test_unknown_disk_capacity_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "wav_min_free_bytes", 1)
    monkeypatch.setattr(
        wav_writer,
        "storage_status",
        lambda: {
            "pending_files": 0,
            "pending_bytes": 0,
            "free_bytes": -1,
            "oldest_pending_age_hours": None,
        },
    )

    with pytest.raises(wav_writer.WavStorageError, match="determine free"):
        wav_writer._ensure_storage_capacity()


def test_recovers_valid_part_file(isolated_wav_storage):
    sensor_dir = isolated_wav_storage / "AABBCCDDEEFF"
    sensor_dir.mkdir()
    part = sensor_dir / "AABBCCDDEEFF_20260101T000000.wav.part"
    with wave.open(str(part), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(380)
        output.writeframes(array.array("h", [1, 2, 3]).tobytes())

    assert wav_writer.recover_part_files() == 1
    final = sensor_dir / "AABBCCDDEEFF_20260101T000000.wav"
    assert final.exists()
    assert not part.exists()
    with wave.open(str(final), "rb") as reader:
        assert reader.getnframes() == 3
