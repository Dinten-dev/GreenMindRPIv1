"""WAV file writer for high-frequency sensor data archival.

Receives raw mV float samples from ingest_api and writes them into
10-minute WAV file chunks per sensor MAC address. Files are stored
locally and picked up by wav_uploader for cloud transfer.

Format: 16-bit PCM, mono, 380 Hz sample rate.
Mapping: 0–3300 mV → 0–32767 int16 (linear scale).
"""

import logging
import os
import struct
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from src.config import settings

logger = logging.getLogger(__name__)

# mV to int16 conversion: 0-3300 mV maps to 0-32767
_MV_MAX = 3300.0
_INT16_MAX = 32767

# Track active writers per sensor MAC
_writers: dict[str, "_SensorWriter"] = {}
_lock = Lock()


class _SensorWriter:
    """Manages a WAV file for a single sensor, rotating every chunk interval."""

    def __init__(self, mac: str, sample_rate: int):
        self.mac = mac
        self.sample_rate = sample_rate
        self.wav_dir = Path(settings.wav_dir) / mac.replace(":", "").upper()
        self.wav_dir.mkdir(parents=True, exist_ok=True)

        self._file = None
        self._writer: wave.Wave_write | None = None
        self._started_at: datetime | None = None
        self._sample_count = 0
        self._chunk_samples = sample_rate * settings.wav_chunk_minutes * 60
        self._filepath: Path | None = None

    def write(self, samples: list[float]) -> str | None:
        """Write samples to the current WAV chunk.

        Returns the filepath of a completed chunk if rotation happened,
        otherwise None.
        """
        completed_path = None

        if self._writer is None:
            self._open_new_chunk()

        for mv in samples:
            # Clamp and convert mV float to int16
            clamped = max(0.0, min(mv, _MV_MAX))
            int_val = int((clamped / _MV_MAX) * _INT16_MAX)
            self._writer.writeframes(struct.pack("<h", int_val))
            self._sample_count += 1

            # Check rotation
            if self._sample_count >= self._chunk_samples:
                completed_path = self._rotate()

        return completed_path

    def close(self) -> str | None:
        """Close the current chunk. Returns filepath if there was data."""
        if self._writer is not None:
            return self._close_current()
        return None

    def _open_new_chunk(self):
        """Open a new WAV file for writing."""
        now = datetime.now(timezone.utc)
        self._started_at = now
        ts = now.strftime("%Y%m%dT%H%M%S")
        mac_clean = self.mac.replace(":", "").upper()
        filename = f"{mac_clean}_{ts}.wav"
        self._filepath = self.wav_dir / filename

        self._file = open(self._filepath, "wb")
        self._writer = wave.open(self._file, "wb")
        self._writer.setnchannels(1)  # Mono
        self._writer.setsampwidth(2)  # 16-bit
        self._writer.setframerate(self.sample_rate)
        self._sample_count = 0

        logger.info("Opened WAV chunk: %s", self._filepath)

    def _close_current(self) -> str:
        """Close the current WAV file. Returns its path."""
        path = str(self._filepath)
        try:
            self._writer.close()
        except Exception:
            pass
        try:
            self._file.close()
        except Exception:
            pass
        self._writer = None
        self._file = None

        duration = self._sample_count / self.sample_rate if self.sample_rate > 0 else 0
        logger.info(
            "Closed WAV chunk: %s (%.1fs, %d samples)",
            path,
            duration,
            self._sample_count,
        )
        return path

    def _rotate(self) -> str:
        """Close current chunk and open a new one. Returns completed filepath."""
        completed = self._close_current()
        self._open_new_chunk()
        return completed

    @property
    def started_at(self) -> datetime | None:
        return self._started_at


def write_samples(mac: str, samples: list[float], sample_rate: int = 380) -> str | None:
    """Write samples for a sensor MAC. Thread-safe.

    Returns filepath of a completed WAV chunk if rotation happened.
    """
    with _lock:
        if mac not in _writers:
            _writers[mac] = _SensorWriter(mac, sample_rate)
        return _writers[mac].write(samples)


def close_all() -> list[str]:
    """Close all active writers. Returns list of completed filepaths."""
    with _lock:
        paths = []
        for mac, writer in _writers.items():
            path = writer.close()
            if path:
                paths.append(path)
        _writers.clear()
        return paths
