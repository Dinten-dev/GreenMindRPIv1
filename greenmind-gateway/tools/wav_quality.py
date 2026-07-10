#!/usr/bin/env python3
import argparse
import glob
import json
import os
import struct
import wave
from datetime import datetime, timezone
import array

def extract_icrd(filepath):
    with open(filepath, "rb") as f:
        data = f.read()
        idx = data.find(b"ICRD")
        if idx != -1:
            size = struct.unpack("<I", data[idx+4:idx+8])[0]
            icrd_str = data[idx+8:idx+8+size].decode("ascii").strip('\x00')
            try:
                return datetime.strptime(icrd_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None

def analyze_wav(filepath):
    icrd_time = extract_icrd(filepath)
    if not icrd_time:
        return None
    
    with wave.open(filepath, "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        duration = frames / rate if rate > 0 else 0
        
        raw_data = w.readframes(frames)
        samples = array.array("h", raw_data)
        
        window_size = rate
        railing_windows = 0
        total_windows = frames // window_size
        
        for i in range(total_windows):
            window = samples[i*window_size : (i+1)*window_size]
            railing_count = sum(1 for x in window if x == 0 or x >= 32767 or x <= -32768)
            if railing_count > window_size * 0.9:
                railing_windows += 1
                
        railing_pct = (railing_windows / total_windows * 100) if total_windows > 0 else 0.0
        
        return {
            "start": icrd_time.timestamp(),
            "duration": duration,
            "end": icrd_time.timestamp() + duration,
            "railing_pct": railing_pct,
            "samples": frames
        }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dir", help="Directory containing WAV files")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()
    
    wavs = glob.glob(os.path.join(args.dir, "**", "*.wav"), recursive=True)
    sensors = {}
    
    for w in wavs:
        mac = os.path.basename(os.path.dirname(w))
        if not mac:
            mac = "unknown"
        if mac not in sensors:
            sensors[mac] = []
        res = analyze_wav(w)
        if res:
            sensors[mac].append(res)
            
    results = {}
    for mac, chunks in sensors.items():
        if not chunks:
            continue
        chunks.sort(key=lambda x: x["start"])
        
        first_start = chunks[0]["start"]
        last_end = chunks[-1]["end"]
        total_wall_time = last_end - first_start
        total_duration = sum(c["duration"] for c in chunks)
        
        coverage = (total_duration / total_wall_time * 100) if total_wall_time > 0 else 100.0
        
        avg_railing = sum(c["railing_pct"] for c in chunks) / len(chunks) if chunks else 0
        
        gaps = []
        for i in range(len(chunks) - 1):
            gap_duration = chunks[i+1]["start"] - chunks[i]["end"]
            if gap_duration > 1.0:
                gaps.append({
                    "start": datetime.fromtimestamp(chunks[i]["end"], timezone.utc).isoformat(),
                    "duration": gap_duration
                })
                
        results[mac] = {
            "time_coverage_pct": coverage,
            "railing_pct": avg_railing,
            "gaps": gaps,
            "total_files": len(chunks)
        }
        
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"{'MAC':<18} | {'Coverage (%)':<12} | {'Railing (%)':<12} | {'Files':<6} | {'Gaps'}")
        print("-" * 70)
        for mac, stats in results.items():
            print(f"{mac:<18} | {stats['time_coverage_pct']:<12.1f} | {stats['railing_pct']:<12.1f} | {stats['total_files']:<6} | {len(stats['gaps'])}")
            for g in stats["gaps"]:
                print(f"  Gap: {g['start']} for {g['duration']:.1f}s")

if __name__ == "__main__":
    main()
