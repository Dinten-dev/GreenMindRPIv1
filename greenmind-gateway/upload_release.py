import os
import sys

import requests

TOKEN = os.environ.get("GREENMIND_TOKEN", "")
if not TOKEN:
    sys.exit("Error: Set GREENMIND_TOKEN environment variable")
BASE_URL = "https://green-mind.ch/api/v1/admin"
RELEASE_ID = "a983a198-0698-40bf-94b1-91abc20eee38"

headers = {"Authorization": f"Bearer {TOKEN}"}

print("Activating release...")
res = requests.patch(f"{BASE_URL}/gateway-app-releases/{RELEASE_ID}/status", params={"is_active": "true"}, headers=headers)
print(res.status_code, res.text)
if res.status_code != 200:
    sys.exit(1)

print("Starting rollout...")
data = {
    "release_version": "1.0.1",
    "target_ring": "all"
}
res = requests.post(f"{BASE_URL}/gateway-rollout", json=data, headers=headers)
print(res.status_code, res.text)
