"""UDP Discovery Listener for ESP32 captive portal.

Runs on port 50000. When it receives a discovery probe from an ESP32,
it replies with its own IP address so the ESP32 can POST data.
"""

import asyncio
import logging
import socket

logger = logging.getLogger(__name__)

UDP_PORT = 50000

class UdpDiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, ip: str):
        self.ip = ip
        super().__init__()

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]):
        try:
            msg = data.decode('utf-8')
            if "DISCOVER_GREENMIND_GATEWAY" in msg:
                reply = f"GATEWAY_IP:{self.ip}".encode('utf-8')
                self.transport.sendto(reply, addr)
                logger.debug("Answered discovery from %s", addr)
        except Exception as e:
            logger.debug("Failed discovery rx: %s", e)

def get_local_ip() -> str:
    """Tries to determine the primary local IP address to broadcast."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.0.1"

async def udp_discovery_server() -> None:
    """Run a UDP server that replies to ESP32 broadcast discoveries."""
    loop = asyncio.get_running_loop()
    ip = get_local_ip()
    
    logger.info("Starting UDP Discovery Server on %s:%d", "0.0.0.0", UDP_PORT)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UdpDiscoveryProtocol(ip),
        local_addr=("0.0.0.0", UDP_PORT),
        allow_broadcast=True
    )
    
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        transport.close()
