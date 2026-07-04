import requests
import sys

TOKEN = "<REDACTED_JWT>"
BASE_URL = "https://green-mind.ch/api/v1/admin"

headers = {"Authorization": f"Bearer {TOKEN}"}

res = requests.get(f"{BASE_URL}/fleet", headers=headers)
print(res.text)
