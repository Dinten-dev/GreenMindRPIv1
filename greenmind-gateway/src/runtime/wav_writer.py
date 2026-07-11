"""WAV file writer for high-frequency sensor data archival.

Receives raw mV float samples from ingest_api and writes them into
10-minute WAV file chunks per sensor MAC address. Files are stored
locally and picked up by wav_uploader for cloud transfer.

Format: 16-bit PCM, mono, 380 Hz sample rate.
Mapping: 0–3300 mV → 0–32767 int16 (linear scale).

Each WAV file embeds an absolute recording timestamp via a LIST/INFO
ICRD chunk (ISO 8601 UTC), making files self-describing for offline
analysis.
"""

import asyncio
import logging
import struct
import wave
import array
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from src.config import settings

logger = logging.getLogger(__name__)

assert sys.byteorder == "little", "WAV generation requires little-endian system"

# mV to int16 conversion: 0-3300 mV maps to 0-32767
_MV_MAX = 3300.0
_INT16_MAX = 32767
_SCALE = _INT16_MAX / _MV_MAX

# Track active writers per sensor MAC
_writers: dict[str, "_SensorWriter"] = {}
_lock = Lock()

_ntp_cached_status = False
_ntp_last_checked = 0.0


def _check_ntp_synced() -> bool:
    """Check whether the system clock is NTP-synchronized.

    Uses timedatectl on Raspberry Pi OS (systemd-timesyncd).
    Returns True if synchronized, False otherwise (or on error).
    """
    try:
        import subprocess

        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip().lower() == "yes"
    except Exception:
        return False

def _get_cached_ntp() -> bool:
    """Get NTP synced status, cached for 60 seconds."""
    global _ntp_cached_status, _ntp_last_checked
    now = time.time()
    if now - _ntp_last_checked > 60.0:
        _ntp_cached_status = _check_ntp_synced()
        _ntp_last_checked = now
    return _ntp_cached_status


def _embed_icrd(filepath: Path, timestamp_iso: str) -> None:
    """Append a LIST/INFO chunk with ICRD tag to a closed WAV file.

    The ICRD (Creation Date) tag stores the absolute recording start
    time as ISO 8601 UTC, e.g. '2026-06-12T08:46:38Z'.

    This modifies the RIFF container in-place by appending the chunk
    and updating the RIFF size field.
    """
    # Build the ICRD sub-chunk
    icrd_data = timestamp_iso.encode("ascii")
    # Pad to even length (RIFF requirement)
    if len(icrd_data) % 2 != 0:
        icrd_data += b"\x00"

    # ICRD sub-chunk: 'ICRD' + size(4 bytes LE) + data
    icrd_chunk = b"ICRD" + struct.pack("<I", len(icrd_data)) + icrd_data

    # LIST/INFO chunk: 'LIST' + size(4 bytes LE) + 'INFO' + sub-chunks
    list_payload = b"INFO" + icrd_chunk
    list_chunk = b"LIST" + struct.pack("<I", len(list_payload)) + list_payload

    with open(filepath, "r+b") as f:
        # Read current RIFF size (bytes 4-7)
        f.seek(4)
        riff_size = struct.unpack("<I", f.read(4))[0]

        # Append LIST chunk at end of file
        f.seek(0, 2)  # EOF
        f.write(list_chunk)

        # Update RIFF size
        new_riff_size = riff_size + len(list_chunk)
        f.seek(4)
        f.write(struct.pack("<I", new_riff_size))

    logger.debug("Embedded ICRD '%s' in %s", timestamp_iso, filepath.name)


class _SensorWriter:
    """Manages a WAV file for a single sensor, rotating every chunk interval."""

    def __init__(self, mac: str, sample_rate: int):
        self.mac = mac
        self.sample_rate = sample_rate
        self.wav_dir = Path(settings.wav_dir) / mac.replace(":", "").upper()
        self.wav_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

        self._file = None
        self._writer: wave.Wave_write | None = None
        self._started_at: datetime | None = None
        self._ntp_synced: bool = False
        self._sample_count = 0
        self._filepath: Path | None = None

    def write(self, samples: list[float]) -> str | None:
        """Write samples to the current WAV chunk.

        Returns the filepath of a completed chunk if rotation happened,
        otherwise None.
        """
        with self._lock:
            completed_path = None

            if self._writer is None:
                self._open_new_chunk()
            else:
                now = datetime.now(timezone.utc)
                if self._started_at and (now - self._started_at).total_seconds() >= settings.wav_chunk_minutes * 60:
                    completed_path = self._rotate()

            # Batch-convert all samples to int16 in one pass
            frame_data = array.array("h", (int(max(0.0, min(mv, _MV_MAX)) * _SCALE) for mv in samples)).tobytes()
            self._writer.writeframes(frame_data)
            self._sample_count += len(samples)

            return completed_path

    def close(self) -> str | None:
        """Close the current chunk. Returns filepath if there was data."""
        with self._lock:
            if self._writer is not None:
                return self._close_current()
            return None

    def _open_new_chunk(self):
        """Open a new WAV file for writing."""
        now = datetime.now(timezone.utc)
        self._started_at = now
        self._ntp_synced = _get_cached_ntp()
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

        logger.info("Opened WAV chunk: %s (NTP: %s)", self._filepath, self._ntp_synced)

    def _close_current(self) -> str:
        """Close the current WAV file and embed timestamp metadata. Returns its path."""
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

        # Embed absolute timestamp as ICRD chunk
        if self._started_at and self._filepath:
            try:
                _embed_icrd(self._filepath, self._started_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
            except Exception as exc:
                logger.warning("Failed to embed ICRD in %s: %s", path, exc)

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

    @property
    def ntp_synced(self) -> bool:
        return self._ntp_synced


def write_samples(mac: str, samples: list[float], sample_rate: int = 380) -> str | None:
    """Write samples for a sensor MAC. Thread-safe.

    Returns filepath of a completed WAV chunk if rotation happened.
    """
    with _lock:
        if mac not in _writers:
            _writers[mac] = _SensorWriter(mac, sample_rate)
        writer = _writers[mac]
        
    return writer.write(samples)


def get_ntp_status(mac: str) -> bool:
    """Get NTP sync status for a sensor's active writer."""
    with _lock:
        writer = _writers.get(mac)
        return writer.ntp_synced if writer else False


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

