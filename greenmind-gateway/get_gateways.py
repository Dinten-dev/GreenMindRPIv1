import os
import sys

import requests

TOKEN = os.environ.get("GREENMIND_TOKEN", "")
if not TOKEN:
    sys.exit("Error: Set GREENMIND_TOKEN environment variable")
BASE_URL = "https://green-mind.ch/api/v1/admin"

headers = {"Authorization": f"Bearer {TOKEN}"}

res = requests.get(f"{BASE_URL}/fleet", headers=headers)
print(res.text)
