import time
import json
import logging
import requests
from src.persistence.database import SessionLocal
from src.persistence.models import IngestJob

logger = logging.getLogger(__name__)

class UploadWorker:
    """Reads the SQLite buffer and pushes payloads asynchronously to Hetzner."""
    def __init__(self, credentials: dict):
        self.device_id = credentials["device_id"]
        self.api_key = credentials["api_key"]
        self.server_url = credentials.get("server_url", "https://api.greenmind.xyz/api/v1")
        self.base_interval = 10

    def run(self):
        logger.info("Upload worker started.")
        while True:
            db = SessionLocal()
            try:
                # Fetch pending jobs FIFO
                jobs = db.query(IngestJob).filter(IngestJob.status == "QUEUED").order_by(IngestJob.created_at.asc()).limit(50).all()
                
                if not jobs:
                    time.sleep(self.base_interval)
                    continue
                
                logger.debug(f"Found {len(jobs)} pending jobs in local buffer.")
                
                # We have jobs, process sequentially
                for job in jobs:
                    payload = json.loads(job.payload_json)
                    headers = {"X-Api-Key": self.api_key}
                    
                    try:
                        resp = requests.post(f"{self.server_url}/ingest", json=payload, headers=headers, timeout=15)
                        resp.raise_for_status()
                        
                        # Upload success -> Clear from SQLite cache
                        db.delete(job)
                        db.commit()
                        logger.info(f"Successfully uploaded job {job.id}")
                        
                    except requests.exceptions.RequestException as e:
                        logger.warning(f"Failed to upload job {job.id} (Network/Server error): {e}")
                        job.retry_count += 1
                        if job.retry_count > 20: # Giving up after 20 tries to prevent poison pill head-of-line blocking
                            logger.error(f"Permanent failure mapping on job {job.id}. Dropping data.")
                            job.status = "FAILED"
                        db.commit()
                        
                        # Dynamic Backoff mapped on retry attempt
                        backoff = min(300, 5 * job.retry_count)
                        logger.info(f"Applying backoff wait state: {backoff} seconds")
                        time.sleep(backoff)
                        break 
                        
            except Exception as e:
                logger.error(f"UploadWorker fatal db loop error: {e}")
                time.sleep(self.base_interval)
            finally:
                db.close()
