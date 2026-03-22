#!/usr/bin/env python3
import os
import subprocess
import sys
import re
import time
import signal

# --- CONFIGURATION ---
DVD_DEVICE = "/dev/sr0" 
OUTPUT_DIR = "./output"
MIN_MINUTES = 40        # Ignore trailers/extras

os.makedirs(OUTPUT_DIR, exist_ok=True)

def decode_output(data):
    """Decode command output robustly for discs with non-UTF-8 metadata."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")

def hhmmss_to_seconds(hhmmss):
    """Converts DVD timestamp (01:23:45.678) to total seconds."""
    try:
        parts = hhmmss.split(':')
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except: return 0

def get_disc_info():
    """Scans the DVD for Volume Name and Title candidates."""
    # Safety: Kill any zombie processes before starting
    subprocess.run(["pkill", "-9", "mplayer"], capture_output=True)
    
    print(f"[+] Scanning {DVD_DEVICE}...")
    if not os.path.exists(DVD_DEVICE):
        print(f"[!] Error: {DVD_DEVICE} not found. Check drive connection.")
        sys.exit(1)

    result = subprocess.run(["lsdvd", "-c", "-x", DVD_DEVICE], capture_output=True, text=False)
    raw_data = decode_output(result.stdout)
    
    if not raw_data or "libdvdread: Can't open" in raw_data:
        print("[!] Error: lsdvd cannot read the disc. Try cleaning the disc.")
        return None, []

    # Extract Volume Name
    vol_match = re.search(r'Disc Title: (.+)', raw_data)
    volume_name = vol_match.group(1).strip() if vol_match else "Unknown_DVD"
    # Clean up filename (remove spaces/special chars)
    volume_name = re.sub(r'[^\w\-_]', '_', volume_name)

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

    candidates.sort(key=lambda x: (x['chaos'], -x['runtime']))
    return volume_name, candidates

def attempt_rip(title_id, volume_name):
    """Stages extraction and transcoding."""
    raw_file = os.path.join(OUTPUT_DIR, f"temp_raw_{title_id}.vob")
    final_file = os.path.join(OUTPUT_DIR, f"{volume_name}.mp4")
    
    # If file exists, add ID to avoid overwriting
    if os.path.exists(final_file):
        final_file = os.path.join(OUTPUT_DIR, f"{volume_name}_title_{title_id}.mp4")
    
    print(f"\n[?] Attempting Direct Dump of Title {title_id}...")
    
    dump_cmd = [
        "mplayer", f"dvd://{title_id}",
        "-dvd-device", DVD_DEVICE,
        "-dumpstream", "-dumpfile", raw_file,
        "-really-quiet"
    ]
    
    proc = subprocess.Popen(dump_cmd, preexec_fn=os.setsid)
    time.sleep(20)
    
    if os.path.exists(raw_file) and os.path.getsize(raw_file) > 10000000:
        print(f"[!] Data confirmed ({os.path.getsize(raw_file)/1024/1024:.1f} MB). Extracting full movie...")
        proc.wait() 
        
        if os.path.getsize(raw_file) < 500000000:
            print(f"[-] Title {title_id} too small (decoy).")
            if os.path.exists(raw_file): os.remove(raw_file)
            return False, None

        print(f"[+] Starting TV-Compatible Transcode (Stage 2)...")
        transcode_cmd = [
            "ffmpeg", "-i", raw_file,
            "-vf", "yadif", "-c:v", "libx264", "-crf", "22", "-preset", "slow",
            "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ac", "2", "-b:a", "128k",
            "-movflags", "+faststart", "-map_metadata", "-1", "-y", final_file
        ]
        
        subprocess.run(transcode_cmd)
        if os.path.exists(raw_file): os.remove(raw_file)
        return True, final_file
        
    print(f"[-] Title {title_id} failed to dump.")
    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except: pass
    if os.path.exists(raw_file): os.remove(raw_file)
    return False, None

def main():
    volume_name, candidates = get_disc_info()
    if not candidates:
        print("[!] No feature-length titles found.")
        return

    print(f"[+] Identified {len(candidates)} candidates for {volume_name}.")
    for cand in candidates:
        success, final_path = attempt_rip(cand['id'], volume_name)
        if success:
            print(f"\n[***] SUCCESS! Movie saved to: {final_path}")
            break
        print(f"[!] Title {cand['id']} was a decoy or error. Checking next...")

if __name__ == "__main__":
    main()
