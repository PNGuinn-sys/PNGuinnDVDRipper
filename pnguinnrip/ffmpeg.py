"""Everything that talks to ffmpeg/ffprobe.

We read the disc with ffmpeg's dvdvideo demuxer (FFmpeg 7.0+, built with
libdvdnav/libdvdread). It plays the title through libdvdnav the way a player
does, so timestamps stay continuous across cell boundaries and the layer
break -- the usual cause of audio drifting out of sync in VOB rips.
"""
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

LANG_639_2 = {
    "en": "eng", "fr": "fre", "es": "spa", "de": "ger", "it": "ita", "ja": "jpn",
    "pt": "por", "nl": "dut", "sv": "swe", "da": "dan", "no": "nor", "fi": "fin",
    "pl": "pol", "ru": "rus", "zh": "chi", "ko": "kor", "cs": "cze", "hu": "hun",
    "el": "gre", "tr": "tur", "he": "heb", "ar": "ara", "th": "tha", "hi": "hin",
}
LANG_639_2_ALT = {"fre": "fra", "ger": "deu", "dut": "nld", "chi": "zho", "cze": "ces", "gre": "ell"}


class FfmpegError(Exception):
    pass


def language_tags(code):
    """Every spelling ffmpeg might use for a 2-letter language code."""
    tags = {code}
    if code in LANG_639_2:
        tags.add(LANG_639_2[code])
        tags.add(LANG_639_2_ALT.get(LANG_639_2[code], LANG_639_2[code]))
    return tags


def check_tools():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise FfmpegError(f"{tool} not found on PATH")
    result = subprocess.run(["ffmpeg", "-hide_banner", "-demuxers"], capture_output=True, text=True)
    if not re.search(r"^\s*D\S*\s+dvdvideo\b", result.stdout, re.MULTILINE):
        raise FfmpegError("this ffmpeg has no dvdvideo demuxer; it needs FFmpeg 7.0+ "
                          "built with --enable-libdvdnav --enable-libdvdread")


def dvd_input(source, title, chapter_start=None):
    args = ["-f", "dvdvideo", "-title", str(title)]
    if chapter_start:
        args += ["-chapter_start", str(chapter_start)]
    return args + ["-i", source]


@dataclass
class ProbedStream:
    index: int
    kind: str  # "video", "audio", "subtitle"
    stream_id: int | None
    language: str


def parse_probe(raw_json):
    streams = []
    for s in json.loads(raw_json or "{}").get("streams", []):
        try:
            stream_id = int(s.get("id", ""), 0)
        except (TypeError, ValueError):
            stream_id = None
        streams.append(ProbedStream(
            index=s["index"],
            kind=s.get("codec_type", ""),
            stream_id=stream_id,
            language=(s.get("tags") or {}).get("language", "").lower(),
        ))
    return streams


def probe(source, title):
    cmd = ["ffprobe", "-v", "error", *dvd_input(source, title), "-show_streams", "-of", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FfmpegError(f"ffprobe could not read title {title}: {result.stderr.strip()[-500:]}")
    return parse_probe(result.stdout)


def match_audio(streams, track, ifo_tracks):
    """ffmpeg's index for the chosen IFO audio track."""
    audio = [s for s in streams if s.kind == "audio"]
    for s in audio:
        if s.stream_id == track.stream_id:
            return s.index
    # No usable ids: fall back to position, since the demuxer adds audio
    # streams in the IFO's order.
    position = [t.stream_id for t in ifo_tracks].index(track.stream_id)
    if len(audio) == len(ifo_tracks):
        return audio[position].index
    raise FfmpegError(f"can't find audio stream 0x{track.stream_id:x} in ffmpeg's view of the title")


def match_subtitles(streams, tracks, language):
    subs = [s for s in streams if s.kind == "subtitle"]
    wanted_ids = {i for t in tracks for i in t.stream_ids}
    picked = [s.index for s in subs if s.stream_id in wanted_ids]
    if not picked:
        tags = language_tags(language)
        picked = [s.index for s in subs if s.language in tags]
    return picked


# --- interlacing detection ---------------------------------------------------

IDET_MULTI = re.compile(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*"
                        r"Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)")


def classify_idet(stderr, standard):
    """Return "progressive", "telecine" or "interlaced" from idet's summary."""
    matches = IDET_MULTI.findall(stderr)
    if not matches:
        return None
    tff, bff, progressive, _ = (int(v) for v in matches[-1])
    total = tff + bff + progressive
    if total == 0:
        return None
    combed = (tff + bff) / total
    if combed < 0.10:
        return "progressive"
    if standard == "ntsc" and combed < 0.75:
        return "telecine"  # film at 24fps with 3:2 pulldown baked in
    return "interlaced"


def detect_scan_type(source, title, chapters, standard):
    middle = max(1, chapters // 2) if chapters > 1 else None
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", *dvd_input(source, title, middle),
           "-map", "0:v:0", "-frames:v", "1500", "-vf", "idet", "-an", "-sn", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return classify_idet(result.stderr, standard)


VIDEO_FILTERS = {
    "progressive": None,
    # Match fields back into the original film frames, then drop the
    # duplicate frame from each group of five: 29.97 -> 23.976 fps.
    "telecine": "fieldmatch,yadif=deint=interlaced,decimate",
    "interlaced": "bwdif=mode=send_frame",
    None: "bwdif=mode=send_frame",
}


# --- the rip itself ------------------------------------------------------------

@dataclass
class RipPlan:
    source: str
    title: int
    output: str
    video_index: int
    audio_index: int
    audio_channels: int
    audio_coding: str
    audio_language: str
    subtitle_indexes: list = field(default_factory=list)
    forced_subtitle_indexes: list = field(default_factory=list)
    mode: str = "transcode"  # or "remux"
    container: str = "mkv"
    scan_type: str | None = None
    crf: int = 20
    preset: str = "slow"
    name: str = ""


def build_command(plan):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *dvd_input(plan.source, plan.title),
           "-map", f"0:{plan.video_index}", "-map", f"0:{plan.audio_index}"]

    # Also keep the original surround track, untouched, where the container allows it.
    copyable = {"ac3", "dts"} if plan.container == "mkv" else {"ac3"}
    keep_surround = (plan.mode == "transcode" and plan.audio_channels > 2
                     and plan.audio_coding in copyable)
    if keep_surround:
        cmd += ["-map", f"0:{plan.audio_index}"]
    subtitles = plan.subtitle_indexes if plan.container == "mkv" else []
    for index in subtitles:
        cmd += ["-map", f"0:{index}"]

    if plan.mode == "remux":
        cmd += ["-c", "copy"]
        if plan.audio_coding == "lpcm":
            cmd += ["-c:a", "flac"]  # DVD LPCM can't be stored as-is; FLAC is lossless
    else:
        cmd += ["-c:v", "libx264", "-crf", str(plan.crf), "-preset", plan.preset,
                "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p"]
        video_filter = VIDEO_FILTERS.get(plan.scan_type, VIDEO_FILTERS[None])
        if video_filter:
            cmd += ["-vf", video_filter]
        # Stereo AAC plays everywhere. aresample=async keeps the audio locked
        # to its timestamps, padding or trimming across any small gaps.
        cmd += ["-c:a:0", "aac", "-ac:a:0", "2", "-b:a:0", "192k",
                "-filter:a:0", "aresample=async=1:first_pts=0"]
        if keep_surround:
            cmd += ["-c:a:1", "copy"]
        cmd += ["-c:s", "copy"]
        cmd += ["-fps_mode", "vfr"]  # never pad soft-telecined film out to 29.97 with duplicates

    language = LANG_639_2.get(plan.audio_language)
    audio_count = 2 if keep_surround else 1
    for i in range(audio_count):
        if language:
            cmd += [f"-metadata:s:a:{i}", f"language={language}"]
        cmd += [f"-disposition:a:{i}", "default" if i == 0 else "0"]
    if keep_surround:
        cmd += ["-metadata:s:a:0", "title=Stereo",
                "-metadata:s:a:1", f"title=Surround {plan.audio_channels - 1}.1"]
    for i, index in enumerate(subtitles):
        cmd += [f"-disposition:s:{i}", "forced" if index in plan.forced_subtitle_indexes else "0"]

    cmd += ["-map_metadata", "-1"]
    if plan.name:
        cmd += ["-metadata", f"title={plan.name}"]
    if plan.container == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-max_muxing_queue_size", "4096", plan.output]
    return cmd


def run_with_progress(cmd, duration, log=print):
    """Run ffmpeg, printing progress every 5%. Raises FfmpegError on failure."""
    # stderr goes to a file: a scratched disc can produce enough read errors
    # to fill a pipe and stall ffmpeg while we're busy reading progress.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errors:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors, text=True)
        next_report = 5
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit() and duration:
                percent = int(value) / 1e6 / duration * 100
                if percent >= next_report:
                    log(f"    {min(percent, 100):.0f}%")
                    next_report = int(percent // 5) * 5 + 5
        proc.wait()
        errors.seek(0)
        stderr = errors.read()
    if proc.returncode != 0:
        raise FfmpegError(f"ffmpeg failed (exit {proc.returncode}): {stderr.strip()[-1500:]}")
    return stderr


def media_duration(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None
