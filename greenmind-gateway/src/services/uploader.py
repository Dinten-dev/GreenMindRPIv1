import httpx
import logging
import asyncio
from datetime import datetime, timedelta
from src.repository.database import SessionLocal
from src.repository.models import MeasurementDB
from src.config import settings

logger = logging.getLogger(__name__)

async def process_upload_queue():
    logger.info("Starting Background Upload Worker...")
    while True:
        try:
            with SessionLocal() as db:
                now = datetime.utcnow()
                pending_items = db.query(MeasurementDB).filter(
                    MeasurementDB.upload_status == "pending"
                ).all()

                for item in pending_items:
                    if item.last_retry_at and item.retry_count > 0:
                        backoff = timedelta(seconds=(5 * (2 ** (item.retry_count - 1))))
                        if now < item.last_retry_at + backoff:
                            continue
                            
                    item.last_retry_at = now
                    db.commit()

                    payload_data = {
                        "measurement_id": item.measurement_id,
                        "gateway_id": item.gateway_id,
                        "device_id": item.device_id,
                        "sensor_type": item.sensor_type,
                        "timestamp": item.timestamp.isoformat(),
                        "payload": item.payload
                    }
                    
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            settings.hetzner_api_url,
                            json=payload_data,
                            headers={"Authorization": f"Bearer {settings.hetzner_api_token}"},
                            timeout=10.0
                        )
                        
                        if resp.status_code in (200, 201, 202):
                            item.upload_status = "uploaded"
                            logger.info(f"Uploaded {item.measurement_id} successfully.")
                        else:
                            item.retry_count += 1
                            logger.warning(f"Failed to upload {item.measurement_id}, retry {item.retry_count}")
                            
                    db.commit()
                    
        except Exception as e:
            logger.error(f"Upload task error: {e}")
        
        await asyncio.sleep(5)
