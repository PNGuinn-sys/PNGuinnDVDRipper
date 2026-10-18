# PNGuinnRip

Put a DVD in, get the movie out: the real main feature, with the right audio track, in sync, ready for Plex. Like HandBrake or MakeMKV, but with no clicking through titles.

## How it finds the real movie

Copy-protected discs (Paramount, Disney, Sony, special editions) often carry dozens or hundreds of decoy titles that all claim to be the movie. PNGuinnRip uses two independent signals:

1. **Press Play like a DVD player.** A DVD contains a small program that runs its menus and decides what plays, and protected discs use it to steer real players past the decoys. PNGuinnRip runs that program through libdvdnav (the library VLC uses): it skips warnings and trailers, presses the highlighted menu button, and notes which long title playback lands in.
2. **Check every title's structure.** It reads the disc's IFO files (its table of contents) and looks at the list of cells (stretches of video) each title plays. A real feature plays its cells forward across the disc, once each, at a normal bitrate. Decoys play the same cells scrambled, repeat cells, or claim two hours of runtime while pointing at a few megabytes.

The menu result wins unless it is clearly a bonus feature, meaning less than half the length of the best-looking title. If the menu playthrough fails, the structure check decides alone.

## How it picks the audio

Track languages and types come from the IFO files, where the disc stores them. A raw VOB dump carries no language tags, which is why older versions sometimes picked Italian. Rules:

- Flagged commentary and audio description tracks are skipped.
- Main tracks in your language (`--lang`, default `en`) come first; among those it takes the most channels, then the earliest-listed track.
- If there is no main track in your language, it takes the disc's first track (usually the original language).

## How it rips

ffmpeg reads the title directly through its `dvdvideo` input, which plays the title the way a player does, so timestamps stay continuous across the disc's internal seams and the audio stays in sync. Before encoding, it samples the middle of the movie to tell film from telecined film (NTSC 3:2 pulldown) from true interlaced video:

| Source | Filter |
|---|---|
| Progressive / soft-telecined film | none |
| Hard-telecined film (NTSC) | `fieldmatch,yadif=deint=interlaced,decimate` → back to 23.976 fps |
| Interlaced video | `bwdif` |

Output (default MKV):
- H.264 video, x264 `--crf 20 --preset slow`, High@4.1 for TV and streambox decoders.
- Stereo AAC as the default track, plus the original 5.1 AC3/DTS track copied untouched.
- Subtitles in your language (MKV only), with forced tracks flagged.
- The disc's chapters.

`--mode remux` skips encoding and copies the original MPEG-2 video and audio losslessly, like MakeMKV.

The file is written to `output/.pnguinnrip-incomplete/` and moved into place only after ffmpeg succeeds and the result's length matches the title, so Plex never picks up a half-written or truncated rip.

Before ripping, it also checks that the chosen audio track actually holds data. Some discs list tracks they never store; if so, it falls back to another track, or rips video only. While ripping, a watchdog stops ffmpeg if it makes no progress for 5 minutes (`--stall-minutes`), so a damaged disc fails with an error instead of hanging forever.

## Requirements (Linux)

```bash
sudo apt install libdvdnav4 libdvdread8 libdvd-pkg
sudo dpkg-reconfigure libdvd-pkg   # builds libdvdcss for encrypted discs
python3 -m pnguinnrip.get_ffmpeg   # downloads a known-good ffmpeg (see below)
```

Python 3.10+ (no pip packages).

**ffmpeg:** pnguinnrip needs FFmpeg 7.0+ with the `dvdvideo` DVD reader **and** the `libx264` encoder, and most distro builds lack one or the other. `python3 -m pnguinnrip.get_ffmpeg` downloads [BtbN's GPL build](https://github.com/BtbN/FFmpeg-Builds), which has both:
- It verifies the build's checksum and that it includes both.
- It installs to `~/.local/share/pnguinnrip/ffmpeg`, which pnguinnrip uses automatically. Nothing is installed system-wide.

To use a different ffmpeg, pass `--ffmpeg-dir /path/to/bin`. pnguinnrip checks ffmpeg before it starts and tells you exactly what is missing.

## Usage

```bash
# Analyze only: shows every long title, which one it picked and why, and the audio choice
python3 -m pnguinnrip --scan

# Rip the main feature from /dev/sr0 into ./output
python3 -m pnguinnrip

# Common options
python3 -m pnguinnrip /dev/sr1 -o /srv/media/Movies --name "The Hobbit (2012)" --eject
python3 -m pnguinnrip --title 12            # override the automatic choice
python3 -m pnguinnrip --mode remux          # lossless, MakeMKV-style
python3 -m pnguinnrip --container mp4       # MP4 instead of MKV (drops subtitles)
python3 -m pnguinnrip movie.iso             # ISO images and VIDEO_TS folders work too
python3 -m pnguinnrip --verbose             # show libdvdread/libdvdnav messages (CSS keys etc.)
```

Run `python3 -m pnguinnrip --help` for everything. Exit status is 0 on success, 2 if the rip finished but its length doesn't match the title (possible read errors; the file is kept), and 1 on failure.

For long rips over SSH: `nohup python3 -m pnguinnrip --eject > rip.log 2>&1 &`

## When it picks wrong

Run with `--scan` and look at the title table. Each long title is listed with its runtime, chapter count, size, and anything suspicious about its structure. Rip the right one with `--title N`, and please note the disc so the heuristics can be improved.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The tests build fake DVD structures, including a 99-decoy protected disc, so they need no drive. To also check the IFO parser against real discs, point `PNGUINNRIP_REAL_DISCS` at a folder of `VIDEO_TS` copies.
