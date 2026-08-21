import os
import sys
from datetime import datetime, timedelta

import httpx

TOKEN = os.environ.get("GREENMIND_TOKEN", "")
if not TOKEN:
    sys.exit("Error: Set GREENMIND_TOKEN environment variable")
headers = {"Authorization": f"Bearer {TOKEN}"}
BASE_URL = "https://green-mind.ch/api/v1/wav/files"

res = httpx.get(f"{BASE_URL}?limit=1000", headers=headers, timeout=30.0)
if res.status_code != 200:
    print(f"Error: API returned HTTP {res.status_code}")
    sys.exit(1)

data = res.json()
wavs = data if isinstance(data, list) else data.get("items", [])
print(f"Total WAVs fetched: {len(wavs)}")

stats = {}
now = datetime.utcnow()
cutoff = now - timedelta(hours=3)

for w in wavs:
    try:
        cat = datetime.strptime(w["created_at"].split(".")[0].replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        cat = datetime.strptime(w["created_at"], "%Y-%m-%dT%H:%M:%S")

    if cat < cutoff:
        continue

    sid = w["sensor_id"]
    if sid not in stats:
        stats[sid] = {"count": 0, "total_duration": 0}

    stats[sid]["count"] += 1
    stats[sid]["total_duration"] += w.get("duration_seconds", 600)

print("\nData since Gateway Update (last 3 hours):")
print(f"{'Sensor ID':<36} | {'Files':<5} | {'Coverage (Est %)'}")
print("-" * 65)

for sid, s in stats.items():
    coverage = min(100.0, (s["count"] / 18.0) * 100)
    print(f"{sid:<36} | {s['count']:<5} | {coverage:.1f}%")
