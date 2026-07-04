import os
import sys

import requests

TOKEN = os.environ.get("GREENMIND_TOKEN", "")
if not TOKEN:
    sys.exit("Error: Set GREENMIND_TOKEN environment variable")
BASE_URL = "https://green-mind.ch/api/v1"

headers = {"Authorization": f"Bearer {TOKEN}"}

res = requests.get(f"{BASE_URL}/sensors", headers=headers)
print(res.json())
