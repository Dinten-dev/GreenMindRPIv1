import os
import json
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class SecretStore:
    """Manages secure persistence of device credentials locally."""
    def __init__(self, filepath: str = "data/secrets.json"):
        self.filepath = filepath
        self._ensure_file()
        
    def _ensure_file(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
        if not os.path.exists(self.filepath):
            with open(self.filepath, "w") as f:
                json.dump({}, f)
            # Restrict permissions so only the owner can read/write the secrets
            try:
                os.chmod(self.filepath, 0o600)
            except Exception as e:
                logger.warning(f"Failed to set chmod 600 on secrets file: {e}")
            
    def load(self) -> Dict[str, Any]:
        try:
            with open(self.filepath, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error("JSON decode error in secrets file. Starting fresh.")
            return {}
        except FileNotFoundError:
            return {}

    def save(self, data: Dict[str, Any]):
        with open(self.filepath, "w") as f:
            json.dump(data, f, indent=4)
            
    def is_provisioned(self) -> bool:
        """Determines if the device is fully set up and ready for operational mode."""
        data = self.load()
        # Requires API Key, Device ID, and a target backend URL
        return bool(data.get("api_key") and data.get("device_id"))

    def get_credentials(self) -> Optional[Dict[str, str]]:
        """Returns provisioning credentials if they exist."""
        data = self.load()
        if self.is_provisioned():
            return {
                "api_key": data["api_key"],
                "device_id": data["device_id"],
                "greenhouse_id": data.get("greenhouse_id"),
                "server_url": data.get("server_url", "https://api.greenmind.xyz/api/v1")
            }
        return None

    def store_credentials(self, api_key: str, device_id: str, greenhouse_id: str, server_url: str):
        data = self.load()
        data["api_key"] = api_key
        data["device_id"] = device_id
        data["greenhouse_id"] = greenhouse_id
        data["server_url"] = server_url
        self.save(data)
        logger.info("Credentials firmly persisted to local storage.")
