import os
import sys

import httpx

TOKEN = os.environ.get("GREENMIND_TOKEN", "")
if not TOKEN:
    sys.exit("Error: Set GREENMIND_TOKEN environment variable")
if "--yes" not in sys.argv[1:]:
    sys.exit("Refusing fleet-wide restart without --yes confirmation")
BASE_URL = "https://green-mind.ch/api/v1/admin"

headers = {"Authorization": f"Bearer {TOKEN}"}

res = httpx.get(f"{BASE_URL}/gateway-fleet", headers=headers, timeout=30.0)
res.raise_for_status()
fleet = res.json()

for gw in fleet["items"]:
    gw_id = gw["id"]
    print(f"Sending command to {gw_id}...")
    cmd_data = {"command_type": "reload_gateway_config", "payload": {}}
    res = httpx.post(
        f"{BASE_URL}/gateway/{gw_id}/command",
        json=cmd_data,
        headers=headers,
        timeout=30.0,
    )
    res.raise_for_status()
    print(f"Command accepted for {gw_id} (HTTP {res.status_code})")
