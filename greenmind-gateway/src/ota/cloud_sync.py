"""Cloud Synchronization for OTA Firmware.

Periodically contacts the firmware.green-mind.ch cloud to fetch applicable updates.
Validates SHA256 hashes and saves them to local storage for ESP32 clients.
"""

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import urllib.parse
from typing import Any

import aiohttp
import aiofiles

from src.config import settings

logger = logging.getLogger(__name__)


def init_ota_db():
    """Initialize the local SQLite database for caching firmware metadata."""
    os.makedirs(os.path.dirname(settings.ota_db_path), exist_ok=True)
    conn = sqlite3.connect(settings.ota_db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS firmware (
            id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            board_type TEXT NOT NULL,
            hardware_revision TEXT NOT NULL,
            local_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            mandatory BOOLEAN NOT NULL,
            min_version TEXT,
            changelog TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_firmware_metadata(fw: dict[str, Any], local_path: str):
    """Save metadata to local SQLite db."""
    conn = sqlite3.connect(settings.ota_db_path)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO firmware 
        (id, version, board_type, hardware_revision, local_path, sha256, mandatory, min_version, changelog)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        fw["id"], fw["version"], fw["board_type"], fw["hardware_revision"], 
        local_path, fw["sha256"], fw.get("mandatory", False), 
        fw.get("min_version"), fw.get("changelog")
    ))
    conn.commit()
    conn.close()


async def download_firmware(session: aiohttp.ClientSession, release: dict[str, Any]) -> bool:
    """Download the firmware binary, verify hash, and save locally."""
    # Assume the URL returned is either relative or absolute.
    url = release["firmware_url"]
    if url.startswith("/"):
        base_url = urllib.parse.urlparse(settings.firmware_api_url)
        url = f"{base_url.scheme}://{base_url.netloc}{url}"

    local_path = os.path.join(settings.firmware_dir, f"{release['id']}.bin")
    os.makedirs(settings.firmware_dir, exist_ok=True)

    # Check if already downloaded and valid
    if os.path.exists(local_path):
        hasher = hashlib.sha256()
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        if hasher.hexdigest() == release["sha256"]:
            logger.debug(f"Firmware {release['version']} already cached and validated.")
            save_firmware_metadata(release, local_path)
            return True
        else:
            logger.warning(f"Cached firmware {release['version']} hash mismatch. Re-downloading.")

    # Download
    logger.info(f"Downloading firmware {release['version']} for {release['board_type']}...")
    try:
        async with session.get(url) as response:
            if response.status != 200:
                logger.error(f"Failed to download firmware: {response.status}")
                return False

            hasher = hashlib.sha256()
            async with aiofiles.open(local_path, "wb") as f:
                async for chunk in response.content.iter_chunked(8192):
                    hasher.update(chunk)
                    await f.write(chunk)

            if hasher.hexdigest() != release["sha256"]:
                logger.error(f"Downloaded firmware hash mismatch: Expected {release['sha256']}, got {hasher.hexdigest()}")
                os.remove(local_path)
                return False

            save_firmware_metadata(release, local_path)
            logger.info(f"Firmware {release['version']} downloaded and validated successfully.")
            return True
            
    except Exception as e:
        logger.error(f"Exception downloading firmware: {e}")
        return False


async def cloud_sync_worker():
    """Background task syncing firmware metadata and binaries."""
    init_ota_db()
    
    api_key = ""
    try:
        with open(settings.secrets_path, "r") as f:
            secret_data = json.load(f)
            api_key = secret_data.get("api_key", "")
    except Exception as e:
        logger.error(f"Failed to read API key for OTA sync: {e}")
        return

    headers = {"X-Api-Key": api_key}
    sync_url = f"{settings.firmware_api_url}/firmware/sync"

    while True:
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(sync_url) as response:
                    if response.status == 200:
                        releases = await response.json()
                        for release in releases:
                            await download_firmware(session, release)
                    elif response.status == 401:
                        logger.error("OTA Sync Unauthorized (401). Check API Key.")
                    else:
                        logger.warning(f"OTA Sync failed with status: {response.status}")
        except Exception as e:
            logger.error(f"OTA Sync error: {e}")

        # Check every 60 minutes
        await asyncio.sleep(3600)
