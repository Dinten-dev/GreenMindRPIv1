import logging

import httpx
from fastapi import APIRouter, HTTPException

from src.config import settings
from src.validation import SensorBatch

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/ingest")
async def proxy_biosignal(payload: SensorBatch):
    """Transparently proxy the high-density AD8232 batches to the remote server."""
    from src.runtime.gateway_app import _credentials

    if not _credentials or "api_key" not in _credentials:
        raise HTTPException(status_code=503, detail="Gateway credentials not loaded")

    # Inject gateway identity
    cloud_payload = payload.model_dump()
    cloud_payload["gateway_serial"] = settings.hardware_id

    api_key = _credentials["api_key"]
    server_url = _credentials.get("server_url") or settings.cloud_api_url

    # Proxy to the central server
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"{server_url}/biosignal/ingest", json=cloud_payload, headers={"X-Api-Key": api_key}
            )
            if resp.status_code in (200, 201):
                return resp.json()
            else:
                logger.error("Cloud rejected biosignal batch with HTTP %d", resp.status_code)
                status = resp.status_code if 400 <= resp.status_code < 500 else 502
                raise HTTPException(status_code=status, detail="Cloud rejection")
        except httpx.RequestError as exc:
            logger.error("Biosignal proxy network error: %s", exc)
            # Alternatively, we could queue it like IngestJob if needed
            raise HTTPException(status_code=502, detail="Cloud unavailable")
