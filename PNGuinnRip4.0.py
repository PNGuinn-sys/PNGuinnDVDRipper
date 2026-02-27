#!/usr/bin/env python3
import os
import subprocess
import sys
import re
import time
import signal

# --- CONFIGURATION ---
DVD_DEVICE = "/dev/sr0" 
OUTPUT_DIR = "./1917"
MIN_MINUTES = 40        # Ignore trailers/extras

os.makedirs(OUTPUT_DIR, exist_ok=True)

def hhmmss_to_seconds(hhmmss):
    """Converts DVD timestamp (01:23:45.678) to total seconds."""
    try:
        parts = hhmmss.split(':')
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except: return 0

def analyze_titles():
    """Scans the DVD and sorts titles by 'Realness' (Chaos Score) and Runtime."""
    print(f"[+] Scanning {DVD_DEVICE}...")
    if not os.path.exists(DVD_DEVICE):
        print(f"[!] Error: {DVD_DEVICE} not found. Check drive connection.")
        sys.exit(1)

    # Scan with lsdvd
    result = subprocess.run(["lsdvd", "-c", "-x", DVD_DEVICE], capture_output=True, text=True)
    raw_data = result.stdout
    
    if not raw_data or "libdvdread: Can't open" in raw_data:
        print("[!] Error: lsdvd cannot read the disc. Try: eject /dev/sr0 && eject -t /dev/sr0")
        return []

    title_blocks = re.split(r'Title: ', raw_data)[1:]
    candidates = []

    for block in title_blocks:
        lines = block.splitlines()
        header = lines[0]
        t_id_match = re.search(r'(\d+)', header)
        len_match = re.search(r'Length: ([\d:.]+)', header)
        
        if t_id_match and len_match:
            t_id = int(t_id_match.group(1))
            t_runtime = hhmmss_to_seconds(len_match.group(1))
            
            if t_runtime > (MIN_MINUTES * 60):
                # Calculate chaos (structural protection detection)
                sectors = [int(s) for s in re.findall(r'Start: (\d+)', block)]
                chaos_score = 0
                for i in range(len(sectors) - 1):
                    if sectors[i+1] <= sectors[i]: chaos_score += 10000 
                
                candidates.append({
                    'id': t_id, 
                    'runtime': t_runtime, 
                    'chaos': chaos_score, 
                    'length': len_match.group(1)
                })

    # Sort: Low Chaos Score (Real) first, then Longest Duration
    candidates.sort(key=lambda x: (x['chaos'], -x['runtime']))
    return candidates

def attempt_rip(title_id):
    """Stages extraction and transcoding with high-compatibility TV settings."""
    raw_file = os.path.join(OUTPUT_DIR, f"title_{title_id}_RAW.vob")
    final_file = os.path.join(OUTPUT_DIR, f"movie_final_{title_id}.mp4")
    
    print(f"\n[?] Attempting Direct Dump of Title {title_id}...")
    
    # STAGE 1: Direct Dump (No pipes, hardware-limit speed)
    dump_cmd = [
        "mplayer", f"dvd://{title_id}",
        "-dvd-device", DVD_DEVICE,
        "-dumpstream", "-dumpfile", raw_file,
        "-really-quiet"
    ]
    
    proc = subprocess.Popen(dump_cmd, preexec_fn=os.setsid)
    
    # Verify data is actually being written
    time.sleep(20)
    
    if os.path.exists(raw_file) and os.path.getsize(raw_file) > 10000000:
        print(f"[!] Data confirmed ({os.path.getsize(raw_file)/1024/1024:.1f} MB). Reading disc...")
        proc.wait() 
        
        # Post-extraction size check (Decoy detection)
        if os.path.getsize(raw_file) < 500000000:
            print(f"[-] Title {title_id} was a fake/decoy (Under 500MB). Cleaning up...")
            if os.path.exists(raw_file): os.remove(raw_file)
            return False

        # STAGE 2: Hardware-Compatible Transcode for Plex/TVs
        print(f"[+] Extraction complete. Starting TV-Compatible Transcode (Stage 2)...")
        transcode_cmd = [
            "ffmpeg", "-i", raw_file,
            "-vf", "yadif",             # Deinterlace (Essential for DVD clarity)
            "-c:v", "libx264", 
            "-crf", "22",               # High quality balance
            "-preset", "slow",          # Best compression efficiency
            "-profile:v", "high",       # Required for hardware TV decoders
            "-level", "4.1",            # Required for older/budget streamboxes
            "-pix_fmt", "yuv420p",      # Standard color space
            "-c:a", "aac", 
            "-ac", "2",                 # Stereo Downmix (fixes 'No Audio' errors on TVs)
            "-b:a", "128k",
            "-movflags", "+faststart",   # Web/Server streaming optimization
            "-map_metadata", "-1",       # Clean metadata
            "-y", final_file
        ]
        
        subprocess.run(transcode_cmd)
        
        # Final cleanup of massive VOB file
        if os.path.exists(raw_file): os.remove(raw_file)
        return True
        
    print(f"[-] Title {title_id} failed to dump data.")
    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except: pass
    if os.path.exists(raw_file): os.remove(raw_file)
    return False

def main():
    candidates = analyze_titles()
    if not candidates:
        print("[!] No feature-length titles found. Clean the disc and retry.")
        return

    print(f"[+] Identified {len(candidates)} candidates.")
    for cand in candidates:
        if attempt_rip(cand['id']):
            print(f"\n[***] SUCCESS! Movie saved to: {final_file}")
            break
        print(f"[!] Title {cand['id']} was a decoy or error. Checking next candidate...")

if __name__ == "__main__":
    main()