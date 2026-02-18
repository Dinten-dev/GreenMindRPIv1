#!/usr/bin/env python3
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone



def iso_utc(seconds_offset: int) -> str:
    ts = datetime.now(timezone.utc) + timedelta(seconds=seconds_offset)
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")



def post_json(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        body = response.read().decode("utf-8")
        return response.status, json.loads(body)



def get_health(base_url: str) -> dict:
    with urllib.request.urlopen(f"{base_url}/gw/health", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))



def send_samples(base_url: str, sample_count: int, device_id: str) -> None:
    accepted = 0
    for i in range(sample_count):
        payload = {
            "device_id": device_id,
            "samples": [
                {
                    "timestamp": iso_utc(i),
                    "metric_key": "air_temperature_c",
                    "species_name": "Tomate",
                    "value": 20.0 + (i % 10),
                }
            ],
        }
        status, body = post_json(f"{base_url}/gw/ingest", payload)
        if status != 202:
            raise RuntimeError(f"Unexpected status {status} at sample {i}: {body}")
        accepted += 1

    if accepted != sample_count:
        raise RuntimeError(f"Expected {sample_count} accepted samples, got {accepted}")



def run_offline_scenario(base_url: str, sample_count: int, device_id: str, drain_timeout: int) -> None:
    print("[offline] Expectation: backend is DOWN while sending, then brought UP.")
    before_depth = get_health(base_url).get("queue_depth", 0)
    send_samples(base_url, sample_count, device_id)

    time.sleep(2)
    after_send_depth = get_health(base_url).get("queue_depth", 0)
    if after_send_depth <= before_depth:
        raise RuntimeError(
            "Queue did not grow. Ensure backend is down during offline scenario test."
        )

    print(f"[offline] Queue grew from {before_depth} to {after_send_depth}. Bring backend up now.")
    deadline = time.time() + drain_timeout
    while time.time() < deadline:
        depth = get_health(base_url).get("queue_depth", 0)
        if depth <= before_depth:
            print(f"[offline] Queue drained to {depth} (target <= {before_depth}).")
            return
        print(f"[offline] Waiting for drain... current queue_depth={depth}")
        time.sleep(3)

    raise RuntimeError("Queue did not drain before timeout. Check backend availability and logs.")



def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate ESP32 sample ingestion for Plant Wiki gateway")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8081", help="Gateway base URL")
    parser.add_argument("--samples", type=int, default=100, help="Number of samples to send")
    parser.add_argument("--device-id", default=str(uuid.uuid4()), help="Device UUID")
    parser.add_argument(
        "--offline-scenario",
        action="store_true",
        help="Test queue growth when backend is down and draining when backend returns",
    )
    parser.add_argument("--drain-timeout", type=int, default=180, help="Drain timeout in seconds")
    args = parser.parse_args()

    try:
        if args.offline_scenario:
            run_offline_scenario(args.gateway_url, args.samples, args.device_id, args.drain_timeout)
        else:
            send_samples(args.gateway_url, args.samples, args.device_id)
            print(f"Sent {args.samples} samples successfully (all accepted).")
    except urllib.error.URLError as err:
        print(f"Network error talking to gateway: {err}", file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001
        print(f"Simulation failed: {err}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
