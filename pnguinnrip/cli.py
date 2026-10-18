import argparse
import os
import re
import subprocess
import sys

from . import analyze, ffmpeg, navigator
from .discio import DiscReadError, capture_native_stderr, open_disc
from .ifo import IfoError


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="pnguinnrip",
        description="Find the main feature on a DVD and rip it with the right audio.")
    p.add_argument("source", nargs="?", default="/dev/sr0",
                   help="DVD drive, ISO image, or folder containing VIDEO_TS (default: /dev/sr0)")
    p.add_argument("-o", "--output-dir", default="./output")
    p.add_argument("--name", help="output file name without extension (default: from the disc label)")
    p.add_argument("--scan", action="store_true", help="analyze and report only; don't rip")
    p.add_argument("--title", type=int, help="rip this title number instead of auto-detecting")
    p.add_argument("--lang", default="en", help="preferred audio/subtitle language, 2 letters (default: en)")
    p.add_argument("--min-minutes", type=float, default=40, help="shortest runtime that counts as a feature")
    p.add_argument("--mode", choices=["transcode", "remux"], default="transcode",
                   help="transcode to H.264 (smaller, plays anywhere) or remux the original "
                        "MPEG-2 losslessly like MakeMKV (default: transcode)")
    p.add_argument("--container", choices=["mkv", "mp4"], default="mkv",
                   help="mkv keeps subtitles and surround audio; mp4 drops subtitles (default: mkv)")
    p.add_argument("--crf", type=int, default=20, help="x264 quality, lower is better (default: 20)")
    p.add_argument("--preset", default="slow", help="x264 preset (default: slow)")
    p.add_argument("--no-menu", action="store_true",
                   help="skip the menu playthrough and pick by structure only")
    p.add_argument("--menu-timeout", type=float, default=180, help="seconds to spend on the menu playthrough")
    p.add_argument("--eject", action="store_true", help="eject the disc when done")
    p.add_argument("--ffmpeg-dir", help="folder containing the ffmpeg and ffprobe to use")
    p.add_argument("--stall-minutes", type=float, default=5,
                   help="stop the rip if ffmpeg makes no progress for this long (default: 5)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show libdvdread/libdvdnav messages (CSS keys etc.)")
    args = p.parse_args(argv)
    args.lang = args.lang.lower()
    if args.mode == "remux" and args.container == "mp4":
        p.error("remux keeps DVD subtitles and audio formats that MP4 can't hold; use --container mkv")
    return args


def log(message=""):
    print(message, flush=True)


def show_messages(messages):
    for message in messages:
        log(f"      {message}")


def fmt_minutes(seconds):
    return f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d}"


def pretty_name(label):
    name = re.sub(r"[_\s]+", " ", label).strip()
    if name.isupper():
        name = name.title()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")
    return name or "DVD"


def print_titles(reports, min_minutes, chosen=None):
    long_titles = [r for r in reports if r.duration >= min_minutes * 60]
    log(f"[+] {len(reports)} titles on disc, {len(long_titles)} at least {min_minutes:g} min long:")
    for r in analyze.rank_structural(long_titles):
        mark = "->" if chosen and r.number == chosen.number else "  "
        status = "ok" if r.clean else "; ".join(r.problems)
        log(f"  {mark} title {r.number:3d}  {fmt_minutes(r.duration)}  {r.entry.chapters:3d} ch  "
            f"VTS {r.entry.vts:2d}  {r.size_bytes / 2**30:5.2f} GB  {status}")


def describe_audio(track):
    a = track.attr
    kind = " commentary" if a.is_commentary else " descriptive" if a.is_descriptive else ""
    return f"{a.language or '??'} {a.coding} {a.channels}ch{kind} (track {a.index + 1})"


def unique_path(directory, name, extension):
    path = os.path.join(directory, f"{name}.{extension}")
    n = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{name} ({n}).{extension}")
        n += 1
    return path


def pick_audio_with_data(streams, tracks, chosen, with_data, language):
    """Swap out a chosen audio track that the disc lists but never stores.

    Returns (track, ffmpeg_index); (None, None) means rip without audio.
    """
    if chosen is None:
        return None, None
    indexes = {}
    for track in tracks:
        try:
            indexes[track.stream_id] = ffmpeg.match_audio(streams, track, tracks)
        except ffmpeg.FfmpegError:
            pass
    if with_data is None or indexes.get(chosen.stream_id) in with_data:
        return chosen, ffmpeg.match_audio(streams, chosen, tracks)

    playable = [t for t in tracks if indexes.get(t.stream_id) in with_data]
    fallback = analyze.choose_audio(playable, language)
    if fallback is None:
        log(f"[!] The disc lists audio ({describe_audio(chosen)}) but stores none; ripping video only.")
        return None, None
    log(f"[!] Audio {describe_audio(chosen)} is listed but holds no sound; "
        f"using {describe_audio(fallback)} instead.")
    return fallback, indexes[fallback.stream_id]


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except (DiscReadError, IfoError, ffmpeg.FfmpegError) as exc:
        log(f"[!] {exc}")
        return 1
    except KeyboardInterrupt:
        log("\n[!] Interrupted.")
        return 130


def run(args):
    if not args.scan:
        # Fail now, not after the menu playthrough, if ffmpeg can't do the job.
        log(f"[+] Using {ffmpeg.check_tools(args.ffmpeg_dir, need_x264=args.mode == 'transcode')}")

    log(f"[+] Reading disc structure from {args.source}...")
    messages = []
    with capture_native_stderr(not args.verbose):
        disc, label = open_disc(args.source, messages)
    if args.verbose:
        show_messages(messages)
    reports = analyze.analyze_disc(disc)
    by_number = {r.number: r for r in reports}

    # 1. Pick the title.
    if args.title:
        if args.title not in by_number:
            log(f"[!] Title {args.title} doesn't exist (disc has {len(reports)}).")
            return 1
        choice = analyze.TitleChoice(by_number[args.title], "chosen with --title")
    else:
        nav_title = None
        if not args.no_menu:
            log("[+] Playing the disc's menus to see where 'Play' leads...")
            durations = {r.number: r.duration for r in reports}
            messages = []
            with capture_native_stderr(not args.verbose) as noise:
                nav_title = navigator.find_main_title(
                    args.source, durations, args.min_minutes * 60, language=args.lang,
                    region_mask=disc.region_mask, time_budget=args.menu_timeout, log=log,
                    messages=messages)
            messages += noise[0].strip().splitlines()
            if args.verbose:
                show_messages(messages)
            if nav_title:
                log(f"    'Play' leads to title {nav_title}.")
            elif messages and not args.verbose:
                log("    libdvdnav's last messages:")
                show_messages(messages[-12:])
        choice = analyze.choose_title(reports, args.min_minutes, nav_title)
        if choice is None:
            print_titles(reports, args.min_minutes)
            log(f"[!] No title is at least {args.min_minutes:g} minutes long.")
            return 1

    report = choice.report
    print_titles(reports, args.min_minutes, report)
    log(f"[+] Main feature: title {report.number} ({fmt_minutes(report.duration)}), {choice.reason}.")
    for warning in choice.warnings:
        log(f"    note: {warning}")

    # 2. Pick audio and subtitles from the IFO, where the languages live.
    entry = report.entry
    audio_tracks = analyze.title_audio_tracks(disc, entry)
    audio = analyze.choose_audio(audio_tracks, args.lang)
    if audio is None:
        log("    note: the disc lists no audio for this title")
    for track in audio_tracks:
        log(f"    {'->' if track is audio else '  '} audio {describe_audio(track)}")
    if audio and audio.attr.language != args.lang:
        log(f"    note: no main '{args.lang}' audio track; using the disc's default")
    sub_tracks = analyze.choose_subtitles(analyze.title_subtitle_tracks(disc, entry), args.lang)
    log(f"    subtitles: {len(sub_tracks)} '{args.lang}' track(s)"
        + ("" if args.container == "mkv" or not sub_tracks else " (dropped: mp4)"))

    if args.scan:
        return 0

    # 3. Rip.
    streams = ffmpeg.probe(args.source, report.number)
    video = next((s.index for s in streams if s.kind == "video"), None)
    if video is None:
        raise ffmpeg.FfmpegError("ffmpeg sees no video in this title")
    subtitle_indexes = ffmpeg.match_subtitles(streams, sub_tracks, args.lang)
    forced_ids = {i for t in sub_tracks if t.attr.is_forced for i in t.stream_ids}

    log("[+] Checking that the tracks hold data...")
    with_data = ffmpeg.streams_with_data(args.source, report.number)
    if with_data is not None and video not in with_data:
        raise ffmpeg.FfmpegError("the title's video stream holds no data; the disc may be damaged")
    audio, audio_index = pick_audio_with_data(streams, audio_tracks, audio, with_data, args.lang)

    scan_type = None
    if args.mode == "transcode":
        standard = disc.title_sets[entry.vts].video.standard
        log("[+] Checking whether the video is film, telecined film, or interlaced...")
        scan_type = ffmpeg.detect_scan_type(args.source, report.number, entry.chapters, standard)
        log(f"    {scan_type or 'unknown (deinterlacing to be safe)'}")

    name = args.name or pretty_name(label)
    os.makedirs(args.output_dir, exist_ok=True)
    # Work in a hidden folder so Plex never scans a half-written file.
    work_dir = os.path.join(args.output_dir, ".pnguinnrip-incomplete")
    os.makedirs(work_dir, exist_ok=True)
    partial = os.path.join(work_dir, f"{name}.{args.container}")

    plan = ffmpeg.RipPlan(
        source=args.source, title=report.number, output=partial,
        video_index=video, audio_index=audio_index,
        audio_channels=audio.attr.channels if audio else 0,
        audio_coding=audio.attr.coding if audio else "",
        audio_language=audio.attr.language if audio else "",
        subtitle_indexes=subtitle_indexes,
        forced_subtitle_indexes=[s.index for s in streams if s.stream_id in forced_ids],
        mode=args.mode, container=args.container, scan_type=scan_type,
        crf=args.crf, preset=args.preset, name=name)

    log(f"[+] Ripping title {report.number} to {name}.{args.container} ({args.mode})...")
    try:
        ffmpeg.run_with_progress(ffmpeg.build_command(plan), report.duration, log,
                                 stall_seconds=args.stall_minutes * 60)
    except BaseException:
        if os.path.exists(partial):
            os.remove(partial)
        raise

    # 4. Check the result before calling it done.
    status = 0
    actual = ffmpeg.media_duration(partial)
    if actual is None:
        log("[!] Couldn't read the finished file's duration; keeping it anyway.")
        status = 2
    elif abs(actual - report.duration) > max(10.0, 0.02 * report.duration):
        log(f"[!] Output is {fmt_minutes(actual)} but the title is {fmt_minutes(report.duration)}: "
            "the disc may have read errors. Keeping the file; check it before trusting it.")
        status = 2

    final = unique_path(args.output_dir, name, args.container)
    os.replace(partial, final)
    log(f"[***] Done: {final}")

    if args.eject:
        subprocess.run(["eject", args.source], capture_output=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
