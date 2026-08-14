import os
import sys

import requests

TOKEN = os.environ.get("GREENMIND_TOKEN", "")
if not TOKEN:
    sys.exit("Error: Set GREENMIND_TOKEN environment variable")
BASE_URL = "https://green-mind.ch/api/v1/admin"

headers = {"Authorization": f"Bearer {TOKEN}"}

res = requests.get(f"{BASE_URL}/gateway-fleet", headers=headers)
fleet = res.json()

for gw in fleet["items"]:
    gw_id = gw["id"]
    print(f"Sending command to {gw_id}...")
    cmd_data = {
        "command_type": "reload_gateway_config",
        "payload": {}
    }
    res = requests.post(f"{BASE_URL}/gateway/{gw_id}/command", json=cmd_data, headers=headers)
    print(res.status_code, res.text)
