"""Async worker that uploads completed WAV files to the cloud backend.

Scans the local WAV directory for completed (non-active) files and
uploads them via multipart POST to /api/v1/wav/upload, then deletes
the local copy on success.
"""

import asyncio
import logging
import os
import re
import wave
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_WAV_NAME = re.compile(r"^(?P<mac>[0-9A-F]{12})_(?P<timestamp>\d{8}T\d{6})(?:_\d{3})?\.wav$")


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

    return sorted(completed, key=lambda path: path.stat().st_mtime)


def _read_wav_metadata(filepath: Path, started_at: datetime) -> tuple[int, datetime]:
    """Return rate and data-derived end timestamp from a finalized WAV."""
    with wave.open(str(filepath), "rb") as reader:
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
            raise wave.Error("unexpected WAV format")
        sample_rate = reader.getframerate()
        if sample_rate not in settings.allowed_sample_rates:
            raise wave.Error("unsupported WAV sample rate")
        frame_count = reader.getnframes()
    duration_seconds = frame_count / sample_rate
    return sample_rate, datetime.fromtimestamp(
        started_at.timestamp() + duration_seconds, tz=timezone.utc
    )


async def upload_loop(credentials: dict) -> None:
    """Continuously scan for completed WAV files and upload them."""
    api_key = credentials["api_key"]
    server_url = credentials.get("server_url") or settings.cloud_api_url
    gateway_serial = settings.hardware_id

    logger.info("WAV upload worker started → %s/wav/upload", server_url)

    while True:
        try:
            completed = await asyncio.to_thread(_find_completed_wavs, settings.wav_dir)

            if not completed:
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
                            logger.info(
                                "Uploaded WAV: %s → %s",
                                filepath.name,
                                object_key,
                            )
                            # Only an explicit successful response acknowledges deletion.
                            filepath.unlink(missing_ok=True)
                            from src.runtime.wav_writer import notify_completed_file_removed

                            notify_completed_file_removed()
                        elif resp.status_code in (401, 403):
                            logger.error(
                                "WAV upload auth error for %s (HTTP %d)",
                                filepath.name,
                                resp.status_code,
                            )
                            await asyncio.sleep(60)
                            break
                        else:
                            logger.warning(
                                "WAV upload failed for %s: HTTP %d",
                                filepath.name,
                                resp.status_code,
                            )

                    except (httpx.HTTPError, OSError, wave.Error) as exc:
                        logger.warning(
                            "WAV upload network error for %s: %s",
                            filepath.name,
                            exc,
                        )
                        await asyncio.sleep(30)
                        break

        except Exception as exc:
            logger.error("WAV upload loop error: %s", exc)

        await asyncio.sleep(30)
