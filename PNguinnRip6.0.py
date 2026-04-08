#!/usr/bin/env python3
import json
import os
import re
import signal
import subprocess
import sys
import time

# --- CONFIGURATION ---
DVD_DEVICE = "/dev/sr0"
OUTPUT_DIR = "./output"
MIN_MINUTES = 40  # Ignore trailers/extras

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
        parts = hhmmss.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except (ValueError, IndexError):
        return 0


def normalize_language(language):
    """Normalizes ffprobe/lsdvd language tags."""
    if not language:
        return ""
    return language.strip().lower()


def is_english(language):
    """True when a language tag is English."""
    normalized = normalize_language(language)
    return normalized in {"en", "eng", "english"} or normalized.startswith("en-")


def is_secondary_audio_track(title):
    """
    Identifies commentary/descriptive tracks that should be deprioritized.
    """
    text = (title or "").lower()
    markers = (
        "commentary",
        "director",
        "producer",
        "descriptive",
        "description",
        "narration",
        "visually impaired",
    )
    return any(marker in text for marker in markers)


def extract_header_int(header, field_name):
    match = re.search(rf"{re.escape(field_name)}:\s*(\d+)", header, re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_title_block(block):
    """Parses one lsdvd title block into a candidate dict."""
    lines = block.splitlines()
    header = lines[0] if lines else ""

    t_id_match = re.search(r"(\d+)", header)
    len_match = re.search(r"Length:\s*([\d:.]+)", header, re.IGNORECASE)
    if not (t_id_match and len_match):
        return None

    t_id = int(t_id_match.group(1))
    t_runtime = hhmmss_to_seconds(len_match.group(1))

    chapters = extract_header_int(header, "Chapters")
    if chapters is None:
        chapters = len(re.findall(r"\bChapter:\s*\d+", block, re.IGNORECASE))

    audio_streams = extract_header_int(header, "Audio streams")
    audio_lines = re.findall(r"\bAudio:[^\n]*", block, re.IGNORECASE)
    if audio_streams is None:
        audio_streams = len(audio_lines)

    english_audio = 0
    for line in audio_lines:
        lower = line.lower()
        if "english" in lower or re.search(r"\blanguage:\s*(?:en|eng)\b", lower):
            english_audio += 1

    sectors = [int(s) for s in re.findall(r"Start:\s*(\d+)", block, re.IGNORECASE)]
    chaos_score = 0
    for i in range(len(sectors) - 1):
        if sectors[i + 1] <= sectors[i]:
            chaos_score += 10000

    unique_sector_count = len(set(sectors))
    sector_span = (max(sectors) - min(sectors)) if len(sectors) > 1 else 0

    return {
        "id": t_id,
        "runtime": t_runtime,
        "chaos": chaos_score,
        "length": len_match.group(1),
        "chapter_count": chapters,
        "audio_stream_count": audio_streams,
        "english_audio_count": english_audio,
        "unique_sector_count": unique_sector_count,
        "sector_span": sector_span,
    }


def rank_candidates(candidates):
    """
    Ranks likely-real feature titles first.
    Lower chaos + richer structure beat fake-playlist decoys.
    """
    return sorted(
        candidates,
        key=lambda x: (
            x["chaos"],
            -x["chapter_count"],
            -x["unique_sector_count"],
            -x["sector_span"],
            -x["english_audio_count"],
            -x["runtime"],
            -x["audio_stream_count"],
            x["id"],
        ),
    )


def parse_lsdvd_output(raw_data, min_minutes=MIN_MINUTES):
    """Extracts volume name and ranked title candidates from lsdvd output."""
    vol_match = re.search(r"Disc Title:\s*(.+)", raw_data)
    volume_name = vol_match.group(1).strip() if vol_match else "Unknown_DVD"
    volume_name = re.sub(r"[^\w\-_]", "_", volume_name)

    title_blocks = re.split(r"Title:\s*", raw_data)[1:]
    candidates = []

    for block in title_blocks:
        candidate = parse_title_block(block)
        if not candidate:
            continue
        if candidate["runtime"] > (min_minutes * 60):
            candidates.append(candidate)

    return volume_name, rank_candidates(candidates)


def parse_ffprobe_audio_streams(raw_json):
    """Parses ffprobe JSON and returns ordered audio streams."""
    if not raw_json:
        return []

    try:
        probe_data = json.loads(raw_json)
    except json.JSONDecodeError:
        return []

    streams = []
    audio_index = 0
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue

        tags = stream.get("tags", {}) or {}
        bit_rate = stream.get("bit_rate", 0)
        try:
            bit_rate = int(bit_rate)
        except (ValueError, TypeError):
            bit_rate = 0

        streams.append(
            {
                "audio_index": audio_index,
                "language": normalize_language(tags.get("language", "")),
                "title": tags.get("title", ""),
                "channels": int(stream.get("channels", 0) or 0),
                "bit_rate": bit_rate,
            }
        )
        audio_index += 1

    return streams


def select_preferred_audio_stream(audio_streams):
    """
    Picks the best audio stream:
    1) Avoid commentary/descriptive tracks when possible
    2) Prefer English
    3) Prefer better technical quality (channels/bitrate)
    """
    if not audio_streams:
        return None

    primary_pool = [s for s in audio_streams if not is_secondary_audio_track(s.get("title", ""))]
    candidate_pool = primary_pool if primary_pool else audio_streams

    def score(stream):
        language = normalize_language(stream.get("language"))
        return (
            1 if is_english(language) else 0,
            1 if language in {"", "und"} else 0,
            stream.get("channels", 0),
            stream.get("bit_rate", 0),
            -stream.get("audio_index", 0),
        )

    return max(candidate_pool, key=score)


def detect_preferred_audio_stream(media_file):
    """Runs ffprobe and returns selected audio stream metadata (or None)."""
    probe_cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-of",
        "json",
        media_file,
    ]
    result = subprocess.run(probe_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None

    audio_streams = parse_ffprobe_audio_streams(result.stdout)
    return select_preferred_audio_stream(audio_streams)


def build_transcode_command(raw_file, final_file, preferred_audio=None):
    """Builds ffmpeg command with explicit audio mapping."""
    command = [
        "ffmpeg",
        "-i",
        raw_file,
        "-map",
        "0:v:0",
    ]

    if preferred_audio:
        command.extend(["-map", f"0:a:{preferred_audio['audio_index']}"])
    else:
        command.extend(["-map", "0:a:0?"])

    command.extend(
        [
            "-vf",
            "yadif",
            "-c:v",
            "libx264",
            "-crf",
            "22",
            "-preset",
            "slow",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
        ]
    )

    if preferred_audio and is_english(preferred_audio.get("language", "")):
        command.extend(["-metadata:s:a:0", "language=eng"])

    command.extend(["-y", final_file])
    return command


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

    return parse_lsdvd_output(raw_data)


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
            if os.path.exists(raw_file):
                os.remove(raw_file)
            return False, None

        preferred_audio = detect_preferred_audio_stream(raw_file)
        if preferred_audio:
            lang = preferred_audio.get("language", "und") or "und"
            print(
                f"[+] Using audio stream a:{preferred_audio['audio_index']} "
                f"(lang={lang}, channels={preferred_audio['channels']})."
            )
        else:
            print("[!] Could not validate audio language. Falling back to first audio stream.")

        print(f"[+] Starting TV-Compatible Transcode (Stage 2)...")
        transcode_cmd = build_transcode_command(raw_file, final_file, preferred_audio)

        subprocess.run(transcode_cmd)
        if os.path.exists(raw_file):
            os.remove(raw_file)
        return True, final_file

    print(f"[-] Title {title_id} failed to dump.")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    if os.path.exists(raw_file):
        os.remove(raw_file)
    return False, None


def main():
    volume_name, candidates = get_disc_info()
    if not candidates:
        print("[!] No feature-length titles found.")
        return

    print(f"[+] Identified {len(candidates)} candidates for {volume_name}.")
    for cand in candidates:
        success, final_path = attempt_rip(cand["id"], volume_name)
        if success:
            print(f"\n[***] SUCCESS! Movie saved to: {final_path}")
            break
        print(f"[!] Title {cand['id']} was a decoy or error. Checking next...")


if __name__ == "__main__":
    main()
