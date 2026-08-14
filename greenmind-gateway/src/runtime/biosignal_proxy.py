import httpx
import logging
from fastapi import APIRouter, Request, HTTPException
from src.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/ingest")
async def proxy_biosignal(request: Request):
    """Transparently proxy the high-density AD8232 batches to the remote server."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    from src.runtime.gateway_app import _credentials
    if not _credentials or "api_key" not in _credentials:
        raise HTTPException(status_code=503, detail="Gateway credentials not loaded")

    # Inject gateway identity
    payload["gateway_serial"] = settings.hardware_id

    api_key = _credentials["api_key"]
    server_url = _credentials.get("server_url") or settings.cloud_api_url

    # Proxy to the central server
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"{server_url}/biosignal/ingest",
                json=payload,
                headers={"X-Api-Key": api_key}
            )
            if resp.status_code in (200, 201):
                return resp.json()
            else:
                logger.error("Cloud rejected biosignal batch: %s", resp.text)
                raise HTTPException(status_code=resp.status_code, detail="Cloud rejection")
        except httpx.RequestError as exc:
            logger.error("Biosignal proxy network error: %s", exc)
            # Alternatively, we could queue it like IngestJob if needed
            raise HTTPException(status_code=502, detail="Cloud unavailable")
