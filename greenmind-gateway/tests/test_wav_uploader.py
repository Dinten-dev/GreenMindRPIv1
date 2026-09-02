import array
import os
import wave
from datetime import datetime, timezone

from src.config import settings
from src.runtime import wav_uploader
from src.runtime.wav_uploader import (
    _find_completed_wavs,
    _parse_wav_filename,
    _read_wav_metadata,
    _record_failure,
    _should_attempt,
)


def test_finder_exposes_only_finalized_valid_wavs(tmp_path):
    sensor_dir = tmp_path / "AABBCCDDEEFF"
    sensor_dir.mkdir()
    final = sensor_dir / "AABBCCDDEEFF_20260101T000000.wav"
    final.write_bytes(b"final")
    (sensor_dir / "AABBCCDDEEFF_20260101T000001.wav.part").write_bytes(b"active")

    assert _find_completed_wavs(str(tmp_path)) == [final]


def test_finder_orders_backlog_by_capture_timestamp_not_mtime(tmp_path):
    sensor_dir = tmp_path / "AABBCCDDEEFF"
    sensor_dir.mkdir()
    older = sensor_dir / "AABBCCDDEEFF_20260101T000000.wav"
    newer = sensor_dir / "AABBCCDDEEFF_20260101T001000.wav"
    newer.write_bytes(b"newer")
    older.write_bytes(b"older")

    assert _find_completed_wavs(str(tmp_path)) == [older, newer]


def test_filename_parser_rejects_directory_identity_mismatch(tmp_path):
    directory = tmp_path / "001122334455"
    directory.mkdir()
    path = directory / "AABBCCDDEEFF_20260101T000000.wav"
    assert _parse_wav_filename(path) is None


def test_wav_end_time_preserves_capture_window_from_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_sample_rates", (380,))
    path = tmp_path / "test.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(380)
        output.writeframes(array.array("h", [1] * 760).tobytes())

    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    capture_end = started.replace(minute=10)
    timestamp = capture_end.timestamp()
    os.utime(path, (timestamp, timestamp))
    sample_rate, ended = _read_wav_metadata(path, started)
    assert sample_rate == 380
    assert ended == capture_end


def test_permanent_failures_use_per_file_backoff(tmp_path):
    filepath = tmp_path / "poison.wav"
    wav_uploader._retry_state.clear()

    delay = _record_failure(filepath, "http_422", permanent=True, now=100.0)

    assert delay == 15 * 60
    assert not _should_attempt(filepath, now=100.0 + delay - 1)
    assert _should_attempt(filepath, now=100.0 + delay)
