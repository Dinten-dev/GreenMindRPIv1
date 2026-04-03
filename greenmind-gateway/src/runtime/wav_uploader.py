"""Async worker that uploads completed WAV files to the cloud backend.

Scans the local WAV directory for completed (non-active) files and
uploads them via multipart POST to /api/v1/wav/upload, then deletes
the local copy on success.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

# Files currently being written by wav_writer (don't upload these)
_ACTIVE_SUFFIX = ".wav"


def _parse_wav_filename(filepath: Path) -> dict | None:
    """Extract sensor MAC and start time from WAV filename.

    Expected format: {MAC}_{YYYYMMDDTHHmmss}.wav
    Example: AABBCCDDEEFF_20260403T120000.wav
    """
    stem = filepath.stem  # e.g. AABBCCDDEEFF_20260403T120000
    parts = stem.split("_", 1)
    if len(parts) != 2:
        return None

    mac_clean = parts[0]
    time_str = parts[1]

    # Reconstruct MAC with colons
    if len(mac_clean) == 12:
        mac = ":".join(mac_clean[i : i + 2] for i in range(0, 12, 2))
    else:
        mac = mac_clean

    try:
        started_at = datetime.strptime(time_str, "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None

    return {"sensor_mac": mac, "started_at": started_at}


def _find_completed_wavs(wav_dir: str) -> list[Path]:
    """Find WAV files that are completed (not currently being written).

    A file is considered complete if it's not the most recent file
    for its sensor MAC directory, OR if it's older than 15 minutes.
    """
    completed = []
    wav_path = Path(wav_dir)

    if not wav_path.exists():
        return completed

    for sensor_dir in wav_path.iterdir():
        if not sensor_dir.is_dir():
            continue

        wav_files = sorted(sensor_dir.glob("*.wav"))
        if len(wav_files) <= 1:
            # Only one file — might be the active one
            # Check by age: if older than chunk_minutes + buffer, it's done
            for f in wav_files:
                age_seconds = (
                    datetime.now(timezone.utc).timestamp() - f.stat().st_mtime
                )
                if age_seconds > (settings.wav_chunk_minutes * 60 + 60):
                    completed.append(f)
        else:
            # All but the last (most recent) are completed
            completed.extend(wav_files[:-1])

    return completed


async def upload_loop(credentials: dict) -> None:
    """Continuously scan for completed WAV files and upload them."""
    api_key = credentials["api_key"]
    server_url = credentials.get("server_url") or settings.cloud_api_url
    gateway_serial = settings.hardware_id

    logger.info("WAV upload worker started → %s/wav/upload", server_url)

    while True:
        try:
            completed = _find_completed_wavs(settings.wav_dir)

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

                    # Calculate end time from file duration
                    file_size = filepath.stat().st_size
                    # WAV header is 44 bytes, each sample is 2 bytes
                    data_bytes = max(0, file_size - 44)
                    n_samples = data_bytes // 2
                    duration = n_samples / 380  # sample_rate
                    from datetime import timedelta

                    ended_at = meta["started_at"] + timedelta(seconds=duration)

                    try:
                        with open(filepath, "rb") as f:
                            files = {"file": (filepath.name, f, "audio/wav")}
                            data = {
                                "sensor_mac": meta["sensor_mac"],
                                "gateway_serial": gateway_serial,
                                "sample_rate": "380",
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
                            logger.info(
                                "Uploaded WAV: %s → %s",
                                filepath.name,
                                resp.json().get("s3_key", "?"),
                            )
                            # Delete local file after successful upload
                            filepath.unlink(missing_ok=True)
                        elif resp.status_code in (401, 403):
                            logger.error(
                                "WAV upload auth error for %s: %s",
                                filepath.name,
                                resp.text,
                            )
                            await asyncio.sleep(60)
                            break
                        else:
                            logger.warning(
                                "WAV upload failed for %s: HTTP %d %s",
                                filepath.name,
                                resp.status_code,
                                resp.text[:200],
                            )

                    except httpx.HTTPError as exc:
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
