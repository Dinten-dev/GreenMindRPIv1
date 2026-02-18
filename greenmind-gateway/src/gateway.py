import os
import json
import logging
import sqlite3
import time
import threading
import requests
import uvicorn
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Depends
from pydantic import BaseModel

# --- Configuration ---
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://macmini.local:8000")
DEVICE_API_KEY = os.getenv("DEVICE_API_KEY", "changeme")
LISTEN_ADDR = os.getenv("LISTEN_ADDR", "0.0.0.0:8081")
QUEUE_DB_PATH = os.getenv("QUEUE_DB_PATH", "/var/lib/greenmind-gateway/queue.db")
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "100000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# New Configs
ALLOW_UNAUTHENTICATED_ESP32 = os.getenv("ALLOW_UNAUTHENTICATED_ESP32", "true").lower() == "true"
ESP32_KEYS_FILE = os.getenv("ESP32_KEYS_FILE", "/etc/greenmind-gateway/esp32_keys.json")
STATION_MAP_FILE = os.getenv("STATION_MAP_FILE", "/etc/greenmind-gateway/station_map.json")

# --- Logging Setup ---
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("greenmind-gateway")

# --- Globals ---
esp32_keys: Dict[str, str] = {} # station_key -> station_id
station_map: Dict[str, Dict[str, Any]] = {} # station_id -> {plant_id: ..., sensor_id: ...}

def load_maps():
    global esp32_keys, station_map
    # Load Keys
    if os.path.exists(ESP32_KEYS_FILE):
        try:
            with open(ESP32_KEYS_FILE, 'r') as f:
                data = json.load(f)
                # Invert for lookup: key -> station_id
                esp32_keys = {v: k for k, v in data.items()}
                logger.info(f"Loaded {len(esp32_keys)} ESP32 keys.")
        except Exception as e:
            logger.error(f"Failed to load ESP32 keys: {e}")
    
    # Load Station Map
    if os.path.exists(STATION_MAP_FILE):
        try:
            with open(STATION_MAP_FILE, 'r') as f:
                station_map = json.load(f)
                logger.info(f"Loaded {len(station_map)} station mappings.")
        except Exception as e:
             logger.error(f"Failed to load station map: {e}")

# --- Database Setup ---
def get_db_connection():
    conn = sqlite3.connect(QUEUE_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(QUEUE_DB_PATH), exist_ok=True)
    conn = get_db_connection()
    try:
        # WAL mode for concurrency and reliability
        conn.execute("PRAGMA journal_mode=WAL;")
        # Queue Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS request_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                headers_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                retry_count INTEGER DEFAULT 0
            )
        """)
        # Dead Letter Queue
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dead_letter_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                headers_json TEXT NOT NULL,
                error_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        logger.info(f"Database initialized at {QUEUE_DB_PATH}")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise
    finally:
        conn.close()

# --- Forwarder Worker ---
class ForwarderWorker(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.running = True
        self.lock = threading.Lock()
        self.last_success = None
        self.backend_connected = False
        self.last_auth_error_log = 0

    def run(self):
        logger.info("Forwarder worker started")
        while self.running:
            self._process_queue()
            time.sleep(0.1) # Fast polling

    def _process_queue(self):
        conn = get_db_connection()
        try:
            # Fetch oldest record
            cursor = conn.execute("SELECT * FROM request_queue ORDER BY id ASC LIMIT 1")
            row = cursor.fetchone()
            
            if not row:
                time.sleep(1)
                return # Queue empty

            record_id = row["id"]
            endpoint = row["endpoint"]
            payload = json.loads(row["payload_json"])
            headers = json.loads(row["headers_json"]) # Original headers

            # Construct full URL
            url = f"{BACKEND_BASE_URL}{endpoint}"
            
            # --- Auth Strategy ---
            # 1. We ALWAYS use the Gateway's DEVICE_API_KEY for the backend.
            # 2. We preserve original source info if needed, but Auth header is replaced.
            forward_headers = headers.copy()
            forward_headers["Authorization"] = f"Bearer {DEVICE_API_KEY}"
            forward_headers["User-Agent"] = "GreenMind-Gateway/1.0"

            try:
                response = requests.post(url, json=payload, headers=forward_headers, timeout=5)
                
                if response.status_code in [200, 201, 202]:
                    # Success
                    conn.execute("DELETE FROM request_queue WHERE id = ?", (record_id,))
                    conn.commit()
                    self.last_success = time.time()
                    self.backend_connected = True
                    logger.info(f"Forwarded id={record_id} to {endpoint} - Status: {response.status_code}")
                
                elif response.status_code in [401, 403]:
                    # Auth Failure - Gateway Configuration Invalid!
                    # Move to Dead Letter Queue to unblock other requests (if they might work? Unlikely if key is wrong)
                    # OR just block and scream?
                    # Policy: Move to DLQ helps debug, but if Key is wrong, ALL will fail.
                    # We will move to DLQ to avoid loop.
                    
                    # Rate limit logs
                    if time.time() - self.last_auth_error_log > 10:
                        logger.error(f"FATAL: Backend Auth Failed (401/403). Check DEVICE_API_KEY! Moving id={record_id} to DLQ.")
                        self.last_auth_error_log = time.time()
                        
                    self._move_to_dlq(conn, row, f"Auth Failed: {response.status_code} - {response.text}")
                    self.backend_connected = False
                    time.sleep(2) # Slow down

                elif response.status_code == 422:
                    # Validation Error - Bad Payload. Never gonna work. DLQ it.
                    logger.error(f"Validation Error (422) for id={record_id}. Moving to DLQ.")
                    self._move_to_dlq(conn, row, f"Validation Error: {response.text}")
                    
                else:
                    # 5xx or temporary network issues
                    # logger.warning(f"Backend error {response.status_code} for id={record_id}. Retrying...")
                    self.backend_connected = False
                    self._increment_retry(conn, record_id)
                    time.sleep(2) # Backoff

            except requests.RequestException as e:
                # logger.warning(f"Connection failed for id={record_id}: {e}")
                self.backend_connected = False
                self._increment_retry(conn, record_id)
                time.sleep(5) # Backoff

        except Exception as e:
            logger.error(f"Worker exception: {e}")
            time.sleep(5) 
        finally:
            conn.close()

    def _increment_retry(self, conn, record_id):
        conn.execute("UPDATE request_queue SET retry_count = retry_count + 1 WHERE id = ?", (record_id,))
        conn.commit()

    def _move_to_dlq(self, conn, row, reason):
        conn.execute(
            "INSERT INTO dead_letter_queue (endpoint, payload_json, headers_json, error_reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (row["endpoint"], row["payload_json"], row["headers_json"], reason, row["created_at"])
        )
        conn.execute("DELETE FROM request_queue WHERE id = ?", (row["id"],))
        conn.commit()

# --- FastAPI App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    load_maps()
    worker = ForwarderWorker()
    worker.start()
    app.state.worker = worker
    yield
    # Shutdown
    worker.running = False
    worker.join(timeout=2)

app = FastAPI(title="GreenMind Gateway", lifespan=lifespan)

# --- Ingest Helpers ---
def buffer_request(endpoint: str, request: Request, payload: Dict[str, Any]):
    # 1. Mapping / Enrichment
    # We expect 'station_id' or 'sensor_id' in payload to identify source.
    # If using station map, we can inject plant_id.
    
    # Identify Source
    station_id = payload.get("station_id") # Explicit station id
    
    # If not present, maybe we can infer from sensor_id? 
    # Let's trust payload first.
    
    if station_id and station_id in station_map:
        mapping = station_map[station_id]
        if "plant_id" in mapping and "plant_id" not in payload:
            payload["plant_id"] = mapping["plant_id"]
        # Can map other fields too
    
    # 2. Check Queue
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT COUNT(*) as count FROM request_queue")
        count = cursor.fetchone()["count"]
        if count >= MAX_QUEUE_SIZE:
             raise HTTPException(status_code=503, detail="Gateway queue full")
        
        # 3. Store
        headers_dict = dict(request.headers)
        
        conn.execute(
            "INSERT INTO request_queue (endpoint, payload_json, headers_json) VALUES (?, ?, ?)",
            (endpoint, json.dumps(payload), json.dumps(headers_dict))
        )
        conn.commit()
        logger.info(f"Buffered to {endpoint}. Q: {count + 1}")
    finally:
        conn.close()

# --- Auth Middleware/Dependency ---
async def verify_esp32_auth(request: Request):
    if ALLOW_UNAUTHENTICATED_ESP32:
        return True # Open Gateway
    
    auth = request.headers.get("Authorization")
    if not auth:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    
    # Expect "Bearer <station_key>"
    try:
        scheme, token = auth.split()
        if scheme.lower() != 'bearer':
             raise HTTPException(status_code=401, detail="Invalid auth scheme")
        
        if token not in esp32_keys:
             raise HTTPException(status_code=403, detail="Invalid station key")
             
        # Valid. Maybe inject station_id into request state?
        return True
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid auth header format")


# --- Routes ---

@app.post("/gw/ingest/plant-signal-1hz")
async def ingest_plant_signal(request: Request, payload: Dict[str, Any], authorized: bool = Depends(verify_esp32_auth)):
    buffer_request("/v1/ingest/plant-signal-1hz", request, payload)
    return {"status": "buffered"}

@app.post("/gw/ingest/env")
async def ingest_env(request: Request, payload: Dict[str, Any], authorized: bool = Depends(verify_esp32_auth)):
    buffer_request("/v1/ingest/env", request, payload)
    return {"status": "buffered"}

@app.post("/gw/ingest/events")
async def ingest_events(request: Request, payload: Dict[str, Any], authorized: bool = Depends(verify_esp32_auth)):
    buffer_request("/v1/ingest/events", request, payload)
    return {"status": "buffered"}

@app.get("/gw/health")
async def health_check():
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT COUNT(*) as count FROM request_queue")
        queue_depth = cursor.fetchone()["count"]
        
        cursor = conn.execute("SELECT COUNT(*) as count FROM dead_letter_queue")
        dlq_depth = cursor.fetchone()["count"]
    except:
        queue_depth = -1
        dlq_depth = -1
    finally:
        conn.close()

    worker: ForwarderWorker = app.state.worker
    
    return {
        "status": "ok",
        "queue_depth": queue_depth,
        "dead_letter_depth": dlq_depth,
        "backend_connected": worker.backend_connected,
        "last_success": worker.last_success,
        "backend_url": BACKEND_BASE_URL,
        "max_queue_size": MAX_QUEUE_SIZE
    }

if __name__ == "__main__":
    host, port = LISTEN_ADDR.split(":")
    uvicorn.run(app, host=host, port=int(port))
