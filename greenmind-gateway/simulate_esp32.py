import requests
import time
import json
import random
import uuid
import threading
import argparse

# Default Configuration
GATEWAY_URL = "http://localhost:8000"

def generate_payload(mac):
    # 0.5s chunks at 380Hz => 190 samples
    return {
        "mac_address": mac,
        "sample_rate": 380,
        "readings": [{"kind": "bio_signal", "value": random.uniform(0, 3300), "unit": "mV"} for _ in range(190)]
    }

def send_data(endpoint, payload):
    try:
        url = f"{GATEWAY_URL}{endpoint}"
        start = time.time()
        resp = requests.post(url, json=payload, timeout=2)
        elapsed = (time.time() - start) * 1000
        if resp.status_code != 200:
            print(f"[{resp.status_code}] Error: {resp.text}")
        return elapsed
    except requests.exceptions.Timeout:
        return -1
    except Exception as e:
        print(f"Failed: {e}")
        return -2

def run_sim(mac, rate_hz=2, duration_s=10):
    # rate_hz = 2 means 2 times per second (190 samples each) -> 380Hz
    latencies = []
    end_time = time.time() + duration_s
    while time.time() < end_time:
        payload = generate_payload(mac)
        lat = send_data("/api/v1/ingest", payload)
        if lat > 0:
            latencies.append(lat)
        time.sleep(1.0 / rate_hz)
    return latencies

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=GATEWAY_URL, help="Gateway URL")
    parser.add_argument("--rate", type=float, default=2.0, help="Rate in Hz")
    parser.add_argument("--duration", type=int, default=10, help="Test duration in seconds")
    args = parser.parse_args()
    
    GATEWAY_URL = args.url
    
    macs = [f"00:11:22:33:44:{i:02x}" for i in range(8)]
    print(f"Starting ESP32 Simulation -> {GATEWAY_URL} with 8 sensors for {args.duration}s")
    
    results = []
    def worker(mac):
        lats = run_sim(mac, args.rate, args.duration)
        results.extend(lats)
        
    threads = []
    for m in macs:
        t = threading.Thread(target=worker, args=(m,))
        t.start()
        threads.append(t)
        
    for t in threads:
        t.join()
        
    if not results:
        print("No successful requests.")
    else:
        results.sort()
        p95 = results[int(len(results) * 0.95)]
        avg = sum(results) / len(results)
        print(f"Requests: {len(results)}")
        print(f"Average Latency: {avg:.1f}ms")
        print(f"P95 Latency: {p95:.1f}ms")
