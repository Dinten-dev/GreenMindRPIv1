"""GreenMind Gateway boot loader.

Determines whether the gateway is provisioned and starts the appropriate mode:
- Unprovisioned: starts the AP + setup web portal
- Provisioned: starts the runtime (ingest API + background workers)

Supports hard-reset via /boot/reset_greenmind.txt.
"""

import asyncio
import logging
import os
import signal
import sys

logger = logging.getLogger("GreenMind")

RESET_FLAG = "/boot/reset_greenmind.txt"


async def async_main() -> None:
    """Async entry point for the gateway service."""
    # Import here to avoid circular imports and ensure logging is configured first
    from src.config import settings
    from src.core.config_store import SecretStore
    from src.core.logging_config import setup_logging
    from src.network.wifi_manager import NetworkManager

    # 1. Initialise logging
    setup_logging(log_dir=settings.log_dir, level=settings.log_level)
    logger.info("GreenMind Gateway Bootloader starting (hw: %s)", settings.hardware_id)

    store = SecretStore(filepath=settings.secrets_path)

    # 2. Check for hard-reset flag
    if os.path.exists(RESET_FLAG):
        logger.warning("Hard-reset flag detected at %s – wiping credentials.", RESET_FLAG)
        store.wipe()
        await NetworkManager.delete_all_wifi_profiles()
        try:
            os.remove(RESET_FLAG)
        except OSError:
            pass
        logger.info("Hard-reset complete. Continuing into setup mode.")

    # 3. Decide mode
    if store.is_provisioned():
        logger.info("State: PROVISIONED → starting runtime mode.")
        await NetworkManager.ensure_ap_off()

        credentials = store.get_credentials()
        if not credentials:
            logger.error("Credentials file corrupt despite is_provisioned() == True. Entering setup.")
            await _enter_setup_mode(store, settings)
            return

        from src.runtime.gateway_app import run_gateway
        run_gateway(credentials)
    else:
        logger.info("State: UNPROVISIONED → entering setup mode.")
        await _enter_setup_mode(store, settings)


async def _enter_setup_mode(store, settings) -> None:
    """Activate AP and start the setup portal."""
    from src.network.wifi_manager import NetworkManager
    from src.setup_portal.server import run_setup_server

    hw_suffix = settings.hardware_id[-4:] if len(settings.hardware_id) >= 4 else "0000"
    await NetworkManager.start_ap(hw_suffix=hw_suffix)

    success = run_setup_server(store, port=80)

    if success:
        logger.info("Provisioning complete. Exiting for systemd restart.")
        sys.exit(0)
    else:
        logger.error("Setup server exited without successful provisioning.")
        sys.exit(1)


def _handle_signal(sig, _frame):
    """Graceful shutdown on SIGTERM/SIGINT."""
    logger.info("Received signal %s – shutting down.", signal.Signals(sig).name)
    sys.exit(0)


def main() -> None:
    """Synchronous entry point."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Interrupted. Exiting.")
    except SystemExit:
        raise
    except Exception as exc:
        logger.critical("Fatal error in bootloader: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
