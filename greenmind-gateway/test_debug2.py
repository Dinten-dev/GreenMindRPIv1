from src.runtime import wav_writer
from src.config import settings
import os
import wave

settings.wav_dir = "tmp_wav2"
settings.wav_chunk_minutes = 1
wav_writer._writers.clear()
os.makedirs("tmp_wav2", exist_ok=True)
mac = "FF:EE:DD:CC:BB:AA"
path = wav_writer.write_samples(mac, [500.0] * 22810, 380)
print("Returned path:", path)
print("File size:", os.path.getsize(path))
with open(path, "rb") as f:
    print("Content length:", len(f.read()))
