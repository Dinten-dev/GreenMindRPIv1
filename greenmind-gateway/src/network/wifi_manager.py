import subprocess
import logging
import time

logger = logging.getLogger(__name__)

class NetworkManager:
    """Wrapper around nmcli for Debian/Raspbian to control WiFi AP and Client connections."""

    @staticmethod
    def run_cmd(args):
        try:
            result = subprocess.run(args, capture_output=True, text=True, check=True)
            return True, result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logger.error(f"nmcli command failed: {' '.join(args)} -> {e.stderr}")
            return False, e.stderr.strip()

    @staticmethod
    def start_ap(ssid: str = "GreenMind-Setup-AP"):
        """Spins up a local WiFi Access Point."""
        logger.info(f"Starting Setup Access Point: {ssid}")
        # Turn on wifi if it's off
        NetworkManager.run_cmd(["nmcli", "radio", "wifi", "on"])
        
        # Check if the connection profile already exists
        success, out = NetworkManager.run_cmd(["nmcli", "-t", "-f", "NAME", "connection", "show"])
        if ssid in out.split('\\n'):
            logger.info("AP profile exists, bringing it up.")
            success, out = NetworkManager.run_cmd(["nmcli", "connection", "up", ssid])
        else:
            logger.info("Creating new AP profile.")
            success, out = NetworkManager.run_cmd([
                "nmcli", "device", "wifi", "hotspot", 
                "ifname", "wlan0", 
                "ssid", ssid, 
                "con-name", ssid
            ])
        return success

    @staticmethod
    def ensure_ap_off(ssid: str = "GreenMind-Setup-AP"):
        """Ensure the open setup AP is completely disabled."""
        logger.info(f"Shutting down Setup AP: {ssid}")
        success, out = NetworkManager.run_cmd(["nmcli", "connection", "down", ssid])
        return success

    @staticmethod
    def connect_to_wifi(ssid: str, password: str) -> bool:
        """Connects to the client's home/greenhouse WiFi."""
        logger.info(f"Attempting to connect to target WiFi: {ssid}")
        
        # Turn off AP temporarily if it's running (to free the radio)
        # NetworkManager handles this concurrently in some chips, but to be sure:
        NetworkManager.ensure_ap_off()
        time.sleep(2)

        if password:
            args = ["nmcli", "device", "wifi", "connect", ssid, "password", password]
        else:
            args = ["nmcli", "device", "wifi", "connect", ssid]

        success, out = NetworkManager.run_cmd(args)
        if success:
            logger.info(f"Successfully connected to {ssid}")
            return True
        else:
            logger.error(f"Failed to connect to {ssid}. Reverting to AP mode.")
            NetworkManager.start_ap()
            return False

    @staticmethod
    def check_internet() -> bool:
        """Pings 8.8.8.8 to verify outbound internet connectivity."""
        try:
            subprocess.run(["ping", "-c", "1", "-W", "3", "8.8.8.8"], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False
