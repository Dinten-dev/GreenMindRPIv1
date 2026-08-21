import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="greenmind-load-test-") as temp_dir:
    root = Path(temp_dir)
    environment = os.environ.copy()
    environment.update(
        {
            "DB_PATH": str(root / "queue.db"),
            "WAV_DIR": str(root / "wav"),
            "SECRETS_PATH": str(root / "secrets.json"),
            "OTA_DB_PATH": str(root / "ota.db"),
            "FIRMWARE_DIR": str(root / "firmware"),
            "LOG_DIR": str(root / "logs"),
        }
    )
    for directory in ("wav", "firmware", "logs"):
        (root / directory).mkdir()

    print("Starting uvicorn...")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.runtime.gateway_app:app", "--port", "8000"],
        env=environment,
    )
    try:
        time.sleep(3)
        simulation = subprocess.run(
            [
                sys.executable,
                "simulate_esp32.py",
                "--url",
                "http://localhost:8000",
                "--duration",
                "10",
                "--rate",
                "2.0",
            ],
            check=False,
        )
        print(f"Simulation exited with {simulation.returncode}")
    finally:
        print("Stopping server gracefully...")
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)

    wav_files = sorted((root / "wav").rglob("*.wav"))
    print(f"Load test produced {len(wav_files)} finalized WAV files")
