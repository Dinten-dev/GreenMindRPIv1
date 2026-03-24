import uvicorn
from fastapi import FastAPI
import threading
import logging
from src.persistence.database import init_db
from src.runtime.ingest_api import router as ingest_router
from src.runtime.upload_worker import UploadWorker
from src.runtime.heartbeat import HeartbeatWorker

logger = logging.getLogger(__name__)

app = FastAPI(title="GreenMind Edge Runtime")
app.include_router(ingest_router, prefix="/api/v1")

def run_gateway(credentials: dict, port: int = 80):
    """
    Main Execution Block for the Operational Gateway Mode.
    Spins up the SQLite DB, Background threads, and the FastAPI ingress.
    """
    logger.info("Initializing runtime mode components...")
    
    # 1. Ensure SQLite queue is ready
    init_db()
    
    # 2. Fire up background threads
    uploader = UploadWorker(credentials)
    heartbeat = HeartbeatWorker(credentials)
    
    t1 = threading.Thread(target=uploader.run, daemon=True, name="UploadWorker")
    t2 = threading.Thread(target=heartbeat.run, daemon=True, name="HeartbeatWorker")
    t1.start()
    t2.start()
    
    # 3. Start ingress HTTP server on Port 80
    logger.info(f"Starting local ESP32 Ingestion server on 0.0.0.0:{port}")
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except KeyboardInterrupt:
        logger.info("Gateway Runtime terminated.")
