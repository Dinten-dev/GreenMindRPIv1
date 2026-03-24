import time
import requests
import logging

logger = logging.getLogger(__name__)

class HeartbeatWorker:
    """Notifies the Hetzner backend regularly that this Raspberry Pi is online."""
    def __init__(self, credentials: dict):
        self.device_id = credentials["device_id"]
        self.api_key = credentials["api_key"]
        self.server_url = credentials.get("server_url", "https://api.greenmind.xyz/api/v1")
        self.interval = 60

    def run(self):
        logger.info("Heartbeat worker active. Polling every 60s.")
        while True:
            try:
                headers = {"X-Api-Key": self.api_key}
                payload = {"status": "online"}
                resp = requests.post(
                    f"{self.server_url}/devices/heartbeat", 
                    json=payload, 
                    headers=headers,
                    timeout=10
                )
                resp.raise_for_status()
                logger.debug(f"Heartbeat sent successfully: {resp.status_code}")
            except Exception as e:
                logger.warning(f"Heartbeat failed (network offline?): {e}")
                
            time.sleep(self.interval)
