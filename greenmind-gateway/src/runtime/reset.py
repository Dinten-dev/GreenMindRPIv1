"""Remote reset handler for GreenMind Gateway."""

import logging
import os
import sys

from src.config import settings
from src.core.config_store import SecretStore

logger = logging.getLogger(__name__)

async def trigger_remote_reset() -> None:
    """Wipe credentials, delete WiFi profiles, and restart in setup mode."""
    logger.critical("Triggering remote reset. Wiping all local data.")
    
    # 1. Wipe SecretStore
    store = SecretStore(filepath=settings.secrets_path)
    store.wipe()
    
    # 2. Write hard-reset flag for bootloader
    try:
        with open("/boot/reset_greenmind.txt", "w") as fh:
            fh.write("reset_triggered_by_cloud\n")
    except OSError as e:
        logger.warning(f"Could not write reset flag to /boot: {e}")
        
    # 3. Delete WiFi profiles
    try:
        from src.network.wifi_manager import NetworkManager
        await NetworkManager.delete_all_wifi_profiles()
    except Exception as e:
        logger.error(f"Failed to delete WiFi profiles: {e}")
        
    logger.info("Local wipe complete. Exiting service to respawn in Setup Mode.")
    sys.exit(0)
