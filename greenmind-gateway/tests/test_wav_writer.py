import array
import struct
import threading
import wave
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.runtime import wav_writer
from src.config import settings

def test_conversion_equivalence():
    _MV_MAX = 3300.0
    _INT16_MAX = 32767
    _SCALE = _INT16_MAX / _MV_MAX
    
    test_values = [-5.0, 0.0, 1650.0, 3300.0, 4000.0]
    
    old_method = b"".join(
        struct.pack("<h", int(max(0.0, min(mv, _MV_MAX)) / _MV_MAX * _INT16_MAX))
        for mv in test_values
    )
    
    new_method = array.array("h", (int(max(0.0, min(mv, _MV_MAX)) * _SCALE) for mv in test_values)).tobytes()
    
    assert old_method == new_method

def test_wav_writer_concurrency(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wav_dir", str(tmp_path))
    monkeypatch.setattr(settings, "wav_chunk_minutes", 10)
    
    # reset global state
    wav_writer._writers.clear()
    
    macs = ["00:11:22:33:44:01", "00:11:22:33:44:02", "00:11:22:33:44:03", "00:11:22:33:44:04"]
    samples_per_thread = 380 * 10 # 10 seconds worth of data
    
    def write_task(mac):
        # We send in small batches to test concurrency
        batch_size = 38
        batches = samples_per_thread // batch_size
        for _ in range(batches):
            wav_writer.write_samples(mac, [1500.0] * batch_size, 380)
            
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write_task, macs))
        
    completed_paths = wav_writer.close_all()
    
    # verify
    for p in completed_paths:
        with wave.open(p, "rb") as w:
            assert w.getnframes() == samples_per_thread

def test_writer_registry_race(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wav_dir", str(tmp_path))
    wav_writer._writers.clear()
    
    mac = "AA:BB:CC:DD:EE:FF"
    
    def write_task():
        wav_writer.write_samples(mac, [1000.0] * 50, 380)
        
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: write_task(), range(4)))
        
    assert len(wav_writer._writers) == 1
    
    completed_paths = wav_writer.close_all()
    assert len(completed_paths) == 1
    
    with wave.open(completed_paths[0], "rb") as w:
        assert w.getnframes() == 200

def test_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wav_dir", str(tmp_path))
    monkeypatch.setattr(settings, "wav_chunk_minutes", 1) # 1 min chunk -> 22800 samples at 380Hz
    wav_writer._writers.clear()
    
    # Mock datetime.now to increment by 1 second on each call to prevent filename collision
    from datetime import datetime, timezone, timedelta
    class MockDatetime:
        _current = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        @classmethod
        def now(cls, tz=None):
            cls._current += timedelta(seconds=1)
            return cls._current
            
    monkeypatch.setattr(wav_writer, "datetime", MockDatetime)
    
    mac = "FF:EE:DD:CC:BB:AA"
    sample_rate = 380
    chunk_samples = sample_rate * 60
    
    # write chunk_samples + 10 to force rotation
    total_samples = chunk_samples + 10
    
    # It should rotate and return the completed chunk path
    rotated_path = wav_writer.write_samples(mac, [500.0] * total_samples, sample_rate)
    
    assert rotated_path is not None
    assert os.path.exists(rotated_path)
    
    # verify ICRD chunk exists
    with open(rotated_path, "rb") as f:
        content = f.read()
        assert b"ICRD" in content
        
    wav_writer.close_all()
