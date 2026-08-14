import subprocess
import time
import os
import signal
import sys

os.environ["DB_PATH"] = "./test_queue.db"
os.environ["WAV_DIR"] = "./test_wav"
os.environ["SECRETS_PATH"] = "./test_secrets.json"
os.environ["OTA_DB_PATH"] = "./test_ota.db"
os.environ["FIRMWARE_DIR"] = "./test_firmware"
os.environ["LOG_DIR"] = "./test_logs"

os.makedirs("./test_wav", exist_ok=True)
os.makedirs("./test_firmware", exist_ok=True)
os.makedirs("./test_logs", exist_ok=True)

# Start uvicorn
print("Starting uvicorn...")
server = subprocess.Popen(["python", "-m", "uvicorn", "src.runtime.gateway_app:app", "--port", "8000"], env=os.environ.copy())
time.sleep(3)

# Run simulation
print("Running simulation...")
sim = subprocess.run(["python", "simulate_esp32.py", "--url", "http://localhost:8000", "--duration", "10", "--rate", "2.0"])
print(f"Simulation exited with {sim.returncode}")

# Terminate gracefully
print("Stopping server gracefully...")
server.send_signal(signal.SIGINT)
server.wait(timeout=10)
print("Server stopped.")

# Check wav dir
print("Checking test_wav directory:")
subprocess.run(["find", "./test_wav"])

