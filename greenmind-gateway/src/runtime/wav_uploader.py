"""Async worker that uploads completed WAV files to the cloud backend.

Scans the local WAV directory for completed (non-active) files and
uploads them via multipart POST to /api/v1/wav/upload, then deletes
the local copy on success.
"""

import asyncio
import hashlib
import logging
import os
import re
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_WAV_NAME = re.compile(r"^(?P<mac>[0-9A-F]{12})_(?P<timestamp>\d{8}T\d{6})(?:_\d{3})?\.wav$")
_PERMANENT_RETRY_SECONDS = 15 * 60
_TRANSIENT_RETRY_SECONDS = 30
_MAX_PERMANENT_RETRY_SECONDS = 6 * 60 * 60
_MAX_TRANSIENT_RETRY_SECONDS = 5 * 60
_PCM_ENCODING_VERSION = "unsigned-mv-linear-int16-v1"
_PCM_SCALE_MV = 3300.0 / 32767.0
_PCM_OFFSET_MV = 0.0
_CALIBRATION_VERSION = "nominal-adc-3v3-v1"


@dataclass
class _RetryState:
    attempts: int
    next_attempt: float


_retry_state: dict[Path, _RetryState] = {}
_last_upload_at: datetime | None = None
_last_error_code: str | None = None


def _should_attempt(filepath: Path, *, now: float | None = None) -> bool:
    state = _retry_state.get(filepath)
    return state is None or state.next_attempt <= (time.monotonic() if now is None else now)


def _record_failure(
    filepath: Path,
    error_code: str,
    *,
    permanent: bool,
    now: float | None = None,
) -> int:
    """Record bounded per-file backoff and return its delay."""
    global _last_error_code
    previous = _retry_state.get(filepath)
    attempts = min((previous.attempts if previous else 0) + 1, 16)
    base = _PERMANENT_RETRY_SECONDS if permanent else _TRANSIENT_RETRY_SECONDS
    maximum = _MAX_PERMANENT_RETRY_SECONDS if permanent else _MAX_TRANSIENT_RETRY_SECONDS
    delay = min(maximum, base * (2 ** min(attempts - 1, 8)))
    current = time.monotonic() if now is None else now
    _retry_state[filepath] = _RetryState(attempts=attempts, next_attempt=current + delay)
    _last_error_code = error_code[:100]
    return delay


def _record_success(filepath: Path) -> None:
    global _last_error_code, _last_upload_at
    _retry_state.pop(filepath, None)
    _last_upload_at = datetime.now(timezone.utc)
    if not _retry_state:
        _last_error_code = None


def _drop_stale_retry_state(existing: list[Path]) -> None:
    global _last_error_code
    existing_set = set(existing)
    for filepath in tuple(_retry_state):
        if filepath not in existing_set:
            _retry_state.pop(filepath, None)
    if not _retry_state:
        _last_error_code = None


def upload_status() -> dict[str, str | None]:
    """Return heartbeat-safe uploader state without response bodies."""
    return {
        "last_upload_at": _last_upload_at.isoformat() if _last_upload_at else None,
        "last_error_code": _last_error_code,
    }


def _parse_wav_filename(filepath: Path) -> dict | None:
    """Extract sensor MAC and start time from WAV filename.

    Expected format: {MAC}_{YYYYMMDDTHHmmss}.wav
    Example: AABBCCDDEEFF_20260403T120000.wav
    """
    match = _WAV_NAME.fullmatch(filepath.name)
    if not match or filepath.parent.name != match.group("mac"):
        return None

    mac_clean = match.group("mac")
    time_str = match.group("timestamp")
    mac = ":".join(mac_clean[i : i + 2] for i in range(0, 12, 2))

    try:
        started_at = datetime.strptime(time_str, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    return {"sensor_mac": mac, "started_at": started_at}


def _find_completed_wavs(wav_dir: str) -> list[Path]:
    """Return finalized chunks; active writers only ever expose ``.part``."""
    completed: list[Path] = []
    wav_path = Path(wav_dir)

    if not wav_path.exists():
        return completed

    for sensor_dir in wav_path.iterdir():
        if sensor_dir.is_symlink() or not sensor_dir.is_dir():
            continue
        for filepath in sensor_dir.glob("*.wav"):
            if filepath.is_symlink() or not filepath.is_file():
                continue
            if _parse_wav_filename(filepath):
                completed.append(filepath)

    return sorted(completed, key=lambda path: _parse_wav_filename(path)["started_at"])


def _sha256_file(filepath: Path) -> str:
    digest = hashlib.sha256()
    with filepath.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_wav_metadata(filepath: Path, started_at: datetime) -> tuple[int, datetime]:
    """Return rate and the best available capture-window end timestamp."""
    with wave.open(str(filepath), "rb") as reader:
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
            raise wave.Error("unexpected WAV format")
        sample_rate = reader.getframerate()
        if sample_rate not in settings.allowed_sample_rates:
            raise wave.Error("unsupported WAV sample rate")
        frame_count = reader.getnframes()
    duration_seconds = frame_count / sample_rate
    inferred_end = datetime.fromtimestamp(
        started_at.timestamp() + duration_seconds,
        tz=timezone.utc,
    )
    modified_end = datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc)
    return sample_rate, max(inferred_end, modified_end)


async def upload_loop(credentials: dict) -> None:
    """Continuously scan for completed WAV files and upload them."""
    api_key = credentials["api_key"]
    server_url = credentials.get("server_url") or settings.cloud_api_url
    gateway_serial = settings.hardware_id

    logger.info("WAV upload worker started → %s/wav/upload", server_url)

    while True:
        try:
            from src.runtime.wav_writer import finalize_idle_writers

            await asyncio.to_thread(
                finalize_idle_writers,
                settings.wav_idle_finalize_seconds,
            )
            completed = await asyncio.to_thread(_find_completed_wavs, settings.wav_dir)
            _drop_stale_retry_state(completed)

            if not completed:
                await asyncio.sleep(30)
                continue

            if not _should_attempt(completed[0]):
                await asyncio.sleep(30)
                continue

            logger.info("Found %d completed WAV files to upload", len(completed))

            async with httpx.AsyncClient(timeout=60.0) as client:
                for filepath in completed:
                    meta = _parse_wav_filename(filepath)
                    if not meta:
                        logger.warning("Skipping unparseable WAV: %s", filepath)
                        continue

                    try:
                        expected_sha256 = await asyncio.to_thread(_sha256_file, filepath)
                        sample_rate, ended_at = await asyncio.to_thread(
                            _read_wav_metadata, filepath, meta["started_at"]
                        )
                        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                        descriptor = os.open(filepath, flags)
                        with os.fdopen(descriptor, "rb") as f:
                            files = {"file": (filepath.name, f, "audio/wav")}
                            data = {
                                "sensor_mac": meta["sensor_mac"],
                                "gateway_serial": gateway_serial,
                                "sample_rate": str(sample_rate),
                                "started_at": meta["started_at"].isoformat(),
                                "ended_at": ended_at.isoformat(),
                                "pcm_encoding_version": _PCM_ENCODING_VERSION,
                                "pcm_scale_mv": str(_PCM_SCALE_MV),
                                "pcm_offset_mv": str(_PCM_OFFSET_MV),
                                "calibration_version": _CALIBRATION_VERSION,
                            }

                            resp = await client.post(
                                f"{server_url}/wav/upload",
                                files=files,
                                data=data,
                                headers={"X-Api-Key": api_key},
                            )

                        if resp.status_code in (200, 201):
                            try:
                                response_data = resp.json()
                                object_key = response_data.get("s3_key", "?")
                            except (ValueError, AttributeError):
                                object_key = "?"
                            if not object_key.endswith(f"_{expected_sha256}.wav"):
                                delay = _record_failure(
                                    filepath,
                                    "ack_checksum_mismatch",
                                    permanent=False,
                                )
                                logger.error(
                                    "WAV acknowledgement checksum mismatch for %s; retry %ds",
                                    filepath.name,
                                    delay,
                                )
                                break
                            logger.info(
                                "Uploaded WAV: %s → %s",
                                filepath.name,
                                object_key,
                            )
                            # Only an explicit successful response acknowledges deletion.
                            filepath.unlink(missing_ok=True)
                            _record_success(filepath)
                            from src.runtime.wav_writer import notify_completed_file_removed

                            notify_completed_file_removed()
                        elif resp.status_code in (401, 403):
                            delay = _record_failure(
                                filepath,
                                f"http_{resp.status_code}",
                                permanent=False,
                            )
                            logger.error(
                                "WAV upload auth error for %s (HTTP %d, retry %ds)",
                                filepath.name,
                                resp.status_code,
                                delay,
                            )
                            await asyncio.sleep(60)
                            break
                        else:
                            permanent = resp.status_code in (400, 409, 422)
                            delay = _record_failure(
                                filepath,
                                f"http_{resp.status_code}",
                                permanent=permanent,
                            )
                            logger.warning(
                                "WAV upload failed for %s: HTTP %d, retry %ds",
                                filepath.name,
                                resp.status_code,
                                delay,
                            )
                            if not permanent:
                                break
                            break

                    except (OSError, wave.Error) as exc:
                        delay = _record_failure(
                            filepath,
                            "local_wav_invalid",
                            permanent=True,
                        )
                        logger.error(
                            "Invalid local WAV %s (%s), retry %ds",
                            filepath.name,
                            type(exc).__name__,
                            delay,
                        )
                        break
                    except httpx.HTTPError as exc:
                        delay = _record_failure(
                            filepath,
                            "network_error",
                            permanent=False,
                        )
                        logger.warning(
                            "WAV upload network error for %s (%s), retry %ds",
                            filepath.name,
                            type(exc).__name__,
                            delay,
                        )
                        await asyncio.sleep(30)
                        break

        except Exception as exc:
            logger.error("WAV upload loop error: %s", exc)

        await asyncio.sleep(30)
