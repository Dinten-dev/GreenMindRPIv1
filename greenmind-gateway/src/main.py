import sys
import logging
from src.core.config_store import SecretStore
from src.network.wifi_manager import NetworkManager
from src.setup_portal.server import run_setup_server
from src.runtime.gateway_app import run_gateway

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("GreenMindBootloader")

def main():
    logger.info("Booting GreenMind Systems Edge Gateway")
    
    store = SecretStore(filepath="/opt/greenmind/data/secrets.json")
    
    if store.is_provisioned():
        logger.info("Mode Decision: PROVISIONED -> Transition to Runtime Mode")
        
        # Security Measure: Ensure the open/setup AP is explicitly turned off
        NetworkManager.ensure_ap_off()
        
        credentials = store.get_credentials()
        
        # Starts the multi-threaded or async runtime block (Ingest API + Upload Workers)
        run_gateway(credentials)
        
    else:
        logger.info("Mode Decision: UNPROVISIONED -> Transition to Setup Mode")
        
        # Activate Raspberry Pi Access Point
        NetworkManager.start_ap()
        
        # This will block until the web interface is used successfully to acquire keys
        success = run_setup_server(store, port=80)
        
        if success:
            logger.info("Setup successfully completed. Bootloader exiting to allow clean service restart.")
            # Normal exit. systemd Restart=always will reboot the script
            # and next time it will drop into Runtime Mode gracefully.
            sys.exit(0)
        else:
            logger.error("Setup server exited early without success. Terminating.")
            sys.exit(1)

if __name__ == "__main__":
    main()
