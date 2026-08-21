import os
import sys

import httpx

TOKEN = os.environ.get("GREENMIND_TOKEN", "")
if not TOKEN:
    sys.exit("Error: Set GREENMIND_TOKEN environment variable")
BASE_URL = "https://green-mind.ch/api/v1"

headers = {"Authorization": f"Bearer {TOKEN}"}

res = httpx.get(f"{BASE_URL}/sensors", headers=headers, timeout=30.0)
res.raise_for_status()
print(res.json())
