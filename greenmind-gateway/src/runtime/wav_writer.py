"""Crash-safe, bounded WAV archival for high-frequency sensor measurements.

Active chunks use ``.wav.part`` and become uploader-visible ``.wav`` files only
after the WAV header, metadata, and file contents have been flushed to disk.
Completed files are never removed here; only an acknowledged cloud upload may
delete them.
"""

from __future__ import annotations

import array
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import wave
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from src.config import settings
from src.validation import canonical_mac

logger = logging.getLogger(__name__)

if sys.byteorder != "little":
    raise RuntimeError("WAV generation requires a little-endian system")

_MV_MAX = 3300.0
_INT16_MAX = 32767
_SCALE = _INT16_MAX / _MV_MAX
_SAFE_WAV_NAME = re.compile(
    r"^(?P<mac>[0-9A-F]{12})_(?P<timestamp>\d{8}T\d{6})(?:_(?P<sequence>\d{3}))?\.wav$"
)

_writers: OrderedDict[str, "_SensorWriter"] = OrderedDict()
_lock = RLock()
_ntp_cached_status = False
_ntp_last_checked = 0.0
_storage_cache: tuple[float, dict[str, int | float | None]] | None = None


class WavStorageError(RuntimeError):
    """Raised when measurement storage cannot safely accept another batch."""


def _check_ntp_synced() -> bool:
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


def _get_cached_ntp() -> bool:
    global _ntp_cached_status, _ntp_last_checked
    now = time.monotonic()
    if now - _ntp_last_checked > 60.0:
        _ntp_cached_status = _check_ntp_synced()
        _ntp_last_checked = now
    return _ntp_cached_status


def _fsync_directory(directory: Path) -> None:
    """Persist a rename on POSIX filesystems that support directory fsync."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _embed_icrd(filepath: Path, timestamp_iso: str) -> None:
    icrd_data = timestamp_iso.encode("ascii")
    if len(icrd_data) % 2:
        icrd_data += b"\x00"
    icrd_chunk = b"ICRD" + struct.pack("<I", len(icrd_data)) + icrd_data
    list_payload = b"INFO" + icrd_chunk
    list_chunk = b"LIST" + struct.pack("<I", len(list_payload)) + list_payload

    with filepath.open("r+b") as output:
        output.seek(4)
        size_bytes = output.read(4)
        if len(size_bytes) != 4:
            raise WavStorageError(f"invalid WAV header in {filepath.name}")
        riff_size = struct.unpack("<I", size_bytes)[0]
        output.seek(0, os.SEEK_END)
        output.write(list_chunk)
        output.seek(4)
        output.write(struct.pack("<I", riff_size + len(list_chunk)))
        output.flush()
        os.fsync(output.fileno())


def _scan_storage() -> dict[str, int | float | None]:
    root = Path(settings.wav_dir)
    pending_files = 0
    pending_bytes = 0
    oldest_mtime: float | None = None
    if root.exists():
        for path in root.rglob("*.wav"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            pending_files += 1
            pending_bytes += stat.st_size
            oldest_mtime = (
                stat.st_mtime if oldest_mtime is None else min(oldest_mtime, stat.st_mtime)
            )

    try:
        free_bytes = shutil.disk_usage(root if root.exists() else root.parent).free
    except OSError:
        free_bytes = -1

    oldest_age_hours = (
        max(0.0, (time.time() - oldest_mtime) / 3600.0) if oldest_mtime is not None else None
    )
    return {
        "pending_files": pending_files,
        "pending_bytes": pending_bytes,
        "free_bytes": free_bytes,
        "oldest_pending_age_hours": oldest_age_hours,
    }


def storage_status(*, refresh: bool = False) -> dict[str, int | float | None]:
    """Return bounded-storage telemetry without deleting queued measurements."""
    global _storage_cache
    now = time.monotonic()
    with _lock:
        if refresh or _storage_cache is None or now - _storage_cache[0] > 30:
            _storage_cache = (now, _scan_storage())
        return dict(_storage_cache[1])


def _invalidate_storage_cache() -> None:
    global _storage_cache
    _storage_cache = None


def notify_completed_file_removed() -> None:
    """Invalidate metrics after the uploader deletes an acknowledged chunk."""
    with _lock:
        _invalidate_storage_cache()


def _ensure_storage_capacity() -> None:
    status = storage_status()
    raw_free_bytes = status["free_bytes"]
    free_bytes = int(raw_free_bytes) if raw_free_bytes is not None else -1
    if free_bytes < 0 and settings.wav_min_free_bytes > 0:
        raise WavStorageError("could not determine free measurement-storage capacity")
    if free_bytes < settings.wav_min_free_bytes:
        raise WavStorageError(
            f"only {free_bytes} bytes free; minimum is {settings.wav_min_free_bytes}"
        )
    if int(status["pending_files"] or 0) >= settings.wav_max_pending_files:
        raise WavStorageError("maximum number of unacknowledged WAV files reached")
    if int(status["pending_bytes"] or 0) >= settings.wav_max_pending_bytes:
        raise WavStorageError("maximum unacknowledged WAV storage reached")

    age = status["oldest_pending_age_hours"]
    if age is not None and float(age) >= settings.wav_warn_pending_age_hours:
        logger.error(
            "Oldest unacknowledged WAV is %.1f hours old; files are retained until upload acknowledgement",
            age,
        )


def _new_chunk_paths(directory: Path, mac_clean: str, now: datetime) -> tuple[Path, Path]:
    timestamp = now.strftime("%Y%m%dT%H%M%S")
    for sequence in range(1000):
        suffix = "" if sequence == 0 else f"_{sequence:03d}"
        final_path = directory / f"{mac_clean}_{timestamp}{suffix}.wav"
        part_path = final_path.with_suffix(".wav.part")
        if not final_path.exists() and not part_path.exists():
            return part_path, final_path
    raise WavStorageError("could not allocate a unique WAV filename")


class _SensorWriter:
    def __init__(self, mac: str, sample_rate: int):
        self.mac = canonical_mac(mac)
        if sample_rate not in settings.allowed_sample_rates:
            raise ValueError("unsupported WAV sample rate")
        self.sample_rate = sample_rate
        self.wav_dir = Path(settings.wav_dir) / self.mac.replace(":", "")
        self.wav_dir.mkdir(parents=True, exist_ok=True)
        self._file = None
        self._writer: wave.Wave_write | None = None
        self._started_at: datetime | None = None
        self._ntp_synced = False
        self._sample_count = 0
        self._part_path: Path | None = None
        self._final_path: Path | None = None
        self._last_flush = time.monotonic()
        self._last_write = time.monotonic()
        self._captured_end: datetime | None = None

    def write(self, samples: list[float], captured_at: datetime | None = None) -> str | None:
        if not samples or len(samples) > settings.max_samples_per_batch:
            raise ValueError("invalid WAV sample count")
        if any(not math.isfinite(value) for value in samples):
            raise ValueError("WAV samples must be finite")

        _ensure_storage_capacity()
        completed_path = None
        sample_time = captured_at or datetime.now(timezone.utc)
        if self._writer is None:
            self._open_new_chunk(sample_time, captured_at is not None)
        else:
            if self._started_at:
                interval = settings.wav_chunk_minutes
                current_bucket = (sample_time.hour * 60 + sample_time.minute) // interval
                started_bucket = (self._started_at.hour * 60 + self._started_at.minute) // interval
                if (
                    current_bucket != started_bucket
                    or sample_time.date() != self._started_at.date()
                ):
                    completed_path = self._rotate()
                    self._open_new_chunk(sample_time, captured_at is not None)

        frames = array.array(
            "h", (int(max(0.0, min(value, _MV_MAX)) * _SCALE) for value in samples)
        ).tobytes()
        assert self._writer is not None
        self._writer.writeframes(frames)
        self._sample_count += len(samples)
        if captured_at is not None:
            batch_end = datetime.fromtimestamp(
                captured_at.timestamp() + len(samples) / self.sample_rate,
                tz=timezone.utc,
            )
            if self._captured_end is None or batch_end > self._captured_end:
                self._captured_end = batch_end
        self._last_write = time.monotonic()
        self._flush_if_due()
        return completed_path

    def close(self) -> str | None:
        if self._writer is None:
            return None
        return self._close_current()

    def _open_new_chunk(
        self,
        started_at: datetime | None = None,
        timestamp_synced: bool = False,
    ) -> None:
        _ensure_storage_capacity()
        now = started_at or datetime.now(timezone.utc)
        self._started_at = now
        self._ntp_synced = timestamp_synced or _get_cached_ntp()
        self._part_path, self._final_path = _new_chunk_paths(
            self.wav_dir, self.mac.replace(":", ""), now
        )
        try:
            self._file = self._part_path.open("xb")
            self._writer = wave.open(self._file, "wb")
            self._writer.setnchannels(1)
            self._writer.setsampwidth(2)
            self._writer.setframerate(self.sample_rate)
            self._sample_count = 0
            self._captured_end = None
            self._last_flush = time.monotonic()
        except Exception:
            if self._file is not None:
                self._file.close()
            self._file = None
            self._writer = None
            raise
        logger.info("Opened active WAV chunk: %s (NTP: %s)", self._part_path, self._ntp_synced)

    def _flush_if_due(self, *, force: bool = False) -> None:
        if self._file is None:
            return
        now = time.monotonic()
        if force or now - self._last_flush >= settings.wav_flush_interval_seconds:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._last_flush = now

    def _close_current(self) -> str:
        assert self._part_path is not None and self._final_path is not None
        part_path = self._part_path
        final_path = self._final_path
        started_at = self._started_at

        try:
            assert self._writer is not None
            self._writer.close()
            self._writer = None
            self._flush_if_due(force=True)
            assert self._file is not None
            self._file.close()
            self._file = None
            if started_at:
                _embed_icrd(part_path, started_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if self._captured_end is not None:
                captured_timestamp = self._captured_end.timestamp()
                os.utime(part_path, (captured_timestamp, captured_timestamp))
            os.replace(part_path, final_path)
            _fsync_directory(final_path.parent)
            _invalidate_storage_cache()
        except Exception as exc:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None
            self._writer = None
            logger.exception("Could not finalize WAV chunk %s", part_path)
            raise WavStorageError(str(exc)) from exc

        duration = self._sample_count / self.sample_rate
        logger.info(
            "Finalized WAV chunk: %s (%.1fs, %d samples)",
            final_path,
            duration,
            self._sample_count,
        )
        return str(final_path)

    def _rotate(self) -> str:
        return self._close_current()

    @property
    def ntp_synced(self) -> bool:
        return self._ntp_synced

    @property
    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._last_write)


def write_samples(
    mac: str,
    samples: list[float],
    sample_rate: int = 380,
    *,
    captured_at_epoch_ms: int | None = None,
) -> str | None:
    """Write one validated batch and evict/finalize the least-recent writer."""
    canonical = canonical_mac(mac)
    with _lock:
        writer = _writers.get(canonical)
        if writer is None:
            if len(_writers) >= settings.wav_max_open_writers:
                _, oldest = _writers.popitem(last=False)
                oldest.close()
            writer = _SensorWriter(canonical, sample_rate)
            _writers[canonical] = writer
        elif writer.sample_rate != sample_rate:
            raise ValueError("sample rate changed for active sensor writer")
        _writers.move_to_end(canonical)
        captured_at = None
        if captured_at_epoch_ms is not None:
            captured_end = datetime.fromtimestamp(captured_at_epoch_ms / 1000, tz=timezone.utc)
            captured_at = datetime.fromtimestamp(
                captured_end.timestamp() - len(samples) / sample_rate,
                tz=timezone.utc,
            )
        return writer.write(samples, captured_at)


def finalize_idle_writers(idle_seconds: int) -> list[str]:
    """Finalize inactive chunks so disconnected sensors remain uploadable."""
    with _lock:
        paths: list[str] = []
        for mac, writer in tuple(_writers.items()):
            if writer.idle_seconds < idle_seconds:
                continue
            path = writer.close()
            if path:
                paths.append(path)
            del _writers[mac]
        return paths


def get_ntp_status(mac: str) -> bool:
    try:
        canonical = canonical_mac(mac)
    except ValueError:
        return False
    with _lock:
        writer = _writers.get(canonical)
        return writer.ntp_synced if writer else False


def active_writer_count() -> int:
    with _lock:
        return len(_writers)


def close_all() -> list[str]:
    """Finalize all active chunks, retaining any failed ``.part`` for recovery."""
    with _lock:
        paths: list[str] = []
        for writer in _writers.values():
            try:
                path = writer.close()
            except WavStorageError:
                continue
            if path:
                paths.append(path)
        _writers.clear()
        return paths


def recover_part_files() -> int:
    """Finalize valid inactive chunks left by a prior unclean shutdown.

    Corrupt or ambiguous files are retained for operator inspection; this
    function never deletes measurement data.
    """
    root = Path(settings.wav_dir)
    if not root.exists():
        return 0
    recovered = 0
    for part_path in root.rglob("*.wav.part"):
        if part_path.is_symlink() or not part_path.is_file():
            logger.error("Refusing unsafe WAV recovery candidate: %s", part_path)
            continue
        final_path = Path(str(part_path)[: -len(".part")])
        match = _SAFE_WAV_NAME.fullmatch(final_path.name)
        if not match or final_path.exists() or part_path.parent.name != match.group("mac"):
            logger.error("Retaining unrecognized active WAV candidate: %s", part_path)
            continue
        try:
            with wave.open(str(part_path), "rb") as reader:
                frame_count = reader.getnframes()
                if (
                    reader.getnchannels() != 1
                    or reader.getsampwidth() != 2
                    or reader.getframerate() not in settings.allowed_sample_rates
                    or frame_count < 1
                ):
                    raise wave.Error("unexpected WAV parameters")
                if len(reader.readframes(frame_count)) != frame_count * 2:
                    raise wave.Error("truncated WAV frames")
            timestamp = datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc
            )
            with part_path.open("rb") as candidate:
                candidate.seek(max(0, part_path.stat().st_size - 256))
                has_icrd = b"ICRD" in candidate.read(256)
            if not has_icrd:
                _embed_icrd(part_path, timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"))
            os.replace(part_path, final_path)
            _fsync_directory(final_path.parent)
            recovered += 1
        except (OSError, ValueError, wave.Error, EOFError) as exc:
            logger.error("Retaining unrecoverable WAV part %s: %s", part_path, exc)
    if recovered:
        _invalidate_storage_cache()
    return recovered
