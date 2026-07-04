import requests

TOKEN = "<REDACTED_JWT>"
BASE_URL = "https://green-mind.ch/api/v1"

headers = {"Authorization": f"Bearer {TOKEN}"}

res = requests.get(f"{BASE_URL}/sensors", headers=headers)
print(res.json())
