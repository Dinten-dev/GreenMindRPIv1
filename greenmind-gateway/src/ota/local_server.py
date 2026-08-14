"""Local OTA API Server for ESP32.

Provides endpoints for ESP32s to poll for cached updates and download firmware over HTTP.
Implemented as a FastAPI router to be included in the main Gateway app.
"""

import json
import logging
import sqlite3
import os
import aiohttp

from typing import Any
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from packaging import version as semver

from src.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ota", tags=["ota"])


def get_applicable_firmware(board_type: str, hardware_revision: str, current_version: str) -> dict[str, Any] | None:
    """Find the best firmware match for the requesting device."""
    conn = sqlite3.connect(settings.ota_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = """
        SELECT * FROM firmware
        WHERE board_type = ? AND hardware_revision = ?
    """
    cursor.execute(query, (board_type, hardware_revision))
    rows = cursor.fetchall()
    conn.close()

    best_match = None
    best_ver = None

    try:
        curr_v = semver.parse(current_version)
    except Exception:
        # If current version is unparseable, default to a very low version
        curr_v = semver.parse("0.0.0")

    for row in rows:
        fw_v = semver.parse(row["version"])
        
        if fw_v > curr_v:
            if row["min_version"]:
                min_v = semver.parse(row["min_version"])
                if curr_v < min_v:
                    continue
            
            if best_ver is None or fw_v > best_ver:
                best_ver = fw_v
                best_match = dict(row)

    return best_match


@router.get("/check")
async def check_firmware(board_type: str, hardware_revision: str, current_version: str):
    """ESP32 calls this to check if an update is available."""
    if not all([board_type, hardware_revision, current_version]):
        raise HTTPException(status_code=400, detail="Missing parameters")

    try:
        best_fw = get_applicable_firmware(board_type, hardware_revision, current_version)
        if not best_fw:
            # 204 No Content means no update available
            return JSONResponse(status_code=204, content={}) 
            
        return {
            "update_available": True,
            "version": best_fw["version"],
            "download_url": f"/api/v1/ota/download/{best_fw['id']}",
            "sha256": best_fw["sha256"],
            "mandatory": bool(best_fw["mandatory"])
        }
    except Exception as e:
        logger.error(f"Error checking firmware: {e}")
        raise HTTPException(status_code=500, detail="Internal error")


@router.get("/download/{fw_id}")
async def download_firmware(fw_id: str):
    """Serve the firmware binary file."""
    conn = sqlite3.connect(settings.ota_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT local_path FROM firmware WHERE id = ?", (fw_id,))
    row = cursor.fetchone()
    conn.close()

    if not row or not os.path.exists(row["local_path"]):
        raise HTTPException(status_code=404, detail="Firmware not found locally")

    return FileResponse(path=row["local_path"], media_type='application/octet-stream')


@router.post("/report")
async def report_device(request: Request):
    """ESP reports OTA status (success/failure) via gateway."""
    try:
        data = await request.json()
        
        sensor_mac = data.get("mac_address")
        status = data.get("status")
        release_id = data.get("release_id")
        
        # Forward report to the main cloud API
        api_key = ""
        try:
            with open(settings.secrets_path, "r") as f:
                secret_data = json.load(f)
                api_key = secret_data.get("api_key", "")
        except Exception:
            pass

        headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        report_url = f"{settings.firmware_api_url}/firmware/report"
        
        payload = {
            "sensor_mac": sensor_mac,
            "release_id": release_id,
            "status": status,
            "error_message": data.get("error_message")
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(report_url, json=payload) as resp:
                logger.debug(f"Cloud report status: {resp.status}")

        return {"status": "reported"}
    except Exception as e:
        logger.error(f"Error forwarding device report: {e}")
        raise HTTPException(status_code=500, detail="Internal error")
