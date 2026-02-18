import requests
import time
import json
import random
import uuid
import threading
import argparse

# Default Configuration
GATEWAY_URL = "http://localhost:8081"
STATION_ID = "station-01"
PLANT_ID = "test-plant-01" 
SENSOR_ID = "esp32-sim-01"
AUTH_TOKEN = None

def generate_plant_signal():
    return {
        "station_id": STATION_ID,
        "plant_id": PLANT_ID,
        "sensor_id": SENSOR_ID,
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "dt_seconds": 1.0,
        "values_uV": [random.uniform(500, 800) for _ in range(10)],
        "quality": [1 for _ in range(10)],
        "request_id": str(uuid.uuid4())
    }

def generate_env_data():
    return {
        "station_id": STATION_ID,
        "plant_id": PLANT_ID,
        "sensor_id": SENSOR_ID,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "temperature_c": random.uniform(20, 25),
        "humidity_pct": random.uniform(40, 60),
        "soil_moisture_raw": random.randint(2000, 3000),
        "light_lux": random.uniform(100, 500)
    }

def send_data(endpoint, payload):
    try:
        url = f"{GATEWAY_URL}{endpoint}"
        headers = {}
        if AUTH_TOKEN:
            headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
            
        start = time.time()
        resp = requests.post(url, json=payload, headers=headers, timeout=2)
        elapsed = (time.time() - start) * 1000
        print(f"[{resp.status_code}] {endpoint} ({elapsed:.1f}ms)")
        if resp.status_code != 200:
            print(f"  Error: {resp.text}")
    except Exception as e:
        print(f"  Failed: {e}")

def run_sim(rate_hz=1):
    print(f"Starting ESP32 Simulation -> {GATEWAY_URL}")
    print(f"Station: {STATION_ID}")
    
    while True:
        signal = generate_plant_signal()
        send_data("/gw/ingest/plant-signal-1hz", signal)

        if random.random() < 0.2:
            env = generate_env_data()
            send_data("/gw/ingest/env", env)

        time.sleep(1.0 / rate_hz)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=GATEWAY_URL, help="Gateway URL")
    parser.add_argument("--rate", type=float, default=1.0, help="Rate in Hz")
    parser.add_argument("--station", default=STATION_ID, help="Station ID")
    parser.add_argument("--token", default=None, help="Auth Token")
    args = parser.parse_args()
    
    GATEWAY_URL = args.url
    STATION_ID = args.station
    AUTH_TOKEN = args.token
    run_sim(args.rate)
