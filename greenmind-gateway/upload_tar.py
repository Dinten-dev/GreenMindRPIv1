import requests
import os
import sys

TOKEN = os.environ.get("GREENMIND_TOKEN", "")
if not TOKEN:
    sys.exit("Error: Set GREENMIND_TOKEN environment variable")
BASE_URL = "https://green-mind.ch/api/v1/admin"

headers = {"Authorization": f"Bearer {TOKEN}"}

# 1. Upload
print("Uploading...")
with open("/tmp/greenmind-gateway-1.0.5.tar.gz", "rb") as f:
    res = requests.post(
        f"{BASE_URL}/gateway-app-releases",
        headers=headers,
        data={"version": "1.0.5", "channel": "stable", "mandatory": "true"},
        files={"file": f}
    )

print(res.status_code, res.text)
if res.status_code != 201:
    sys.exit(1)

release_id = res.json()["id"]

# 2. Activate
print(f"Activating release {release_id}...")
res = requests.patch(f"{BASE_URL}/gateway-app-releases/{release_id}/status", params={"is_active": "true"}, headers=headers)
print(res.status_code, res.text)
if res.status_code != 200:
    sys.exit(1)

# 3. Rollout
print("Starting rollout...")
data = {
    "release_version": "1.0.5",
    "target_ring": "all"
}
res = requests.post(f"{BASE_URL}/gateway-rollout", json=data, headers=headers)
print(res.status_code, res.text)
