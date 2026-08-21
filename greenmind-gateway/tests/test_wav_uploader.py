import array
import wave
from datetime import datetime, timezone

from src.config import settings
from src.runtime.wav_uploader import (
    _find_completed_wavs,
    _parse_wav_filename,
    _read_wav_metadata,
)


def test_finder_exposes_only_finalized_valid_wavs(tmp_path):
    sensor_dir = tmp_path / "AABBCCDDEEFF"
    sensor_dir.mkdir()
    final = sensor_dir / "AABBCCDDEEFF_20260101T000000.wav"
    final.write_bytes(b"final")
    (sensor_dir / "AABBCCDDEEFF_20260101T000001.wav.part").write_bytes(b"active")

    assert _find_completed_wavs(str(tmp_path)) == [final]


def test_filename_parser_rejects_directory_identity_mismatch(tmp_path):
    directory = tmp_path / "001122334455"
    directory.mkdir()
    path = directory / "AABBCCDDEEFF_20260101T000000.wav"
    assert _parse_wav_filename(path) is None


def test_wav_end_time_comes_from_frames_not_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_sample_rates", (380,))
    path = tmp_path / "test.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(380)
        output.writeframes(array.array("h", [1] * 760).tobytes())

    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sample_rate, ended = _read_wav_metadata(path, started)
    assert sample_rate == 380
    assert (ended - started).total_seconds() == 2
