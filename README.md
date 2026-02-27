PNGuinnRip 4.0: Autonomous DVD-to-Plex Transcoder

PNGuinnRip 4.0 is a streamlined, two-stage Python utility designed to bypass structural DVD protections (common in Paramount/Disney releases) and generate high-compatibility, Plex-ready MP4s.

This script is optimized for headless Linux servers and manual execution via SSH. It prioritizes stability and playback compatibility over complex automation.
🚀 The Workflow

    DNA Analysis: Scans the disc for feature-length titles. It uses a "Chaos Score" to detect structural protection and prioritize the real movie over decoy titles.

    Stage 1 (Direct Dump): Uses mplayer to perform a bit-for-bit stream dump from the disc directly to an SSD. This removes the "sync lag" caused by real-time encoding and maximizes hardware read speeds.

    Stage 2 (Plex-TV Transcode): Once the raw data is safe on the drive, ffmpeg utilizes the CPU to convert the VOB into a hardware-compatible H.264 MP4.

🛠 Prerequisites

Ensure your Linux environment has the following packages installed:

    sudo apt update
    sudo apt install lsdvd mplayer ffmpeg libdvd-pkg
    sudo dpkg-reconfigure libdvd-pkg

📂 Configuration

The script contains a configuration block at the top for easy adjustment:

    DVD_DEVICE: Default is /dev/sr0.

    OUTPUT_DIR: Directory where files are processed and saved.

    MIN_MINUTES: Minimum length to consider a title "the movie" (default: 40).

🖥 Usage

Run the script manually once a disc is inserted:

    python3 PNGuinnRip4.0.py

For SSH sessions where you want to disconnect while the CPU works:

    nohup python3 PNGuinnRip4.0.py &

📺 Plex & TV Optimization

The transcode stage is specifically tuned for TV and Streambox hardware (Roku, FireStick, LG/Samsung TVs) to prevent "File Not Supported" errors:

    Deinterlacing (yadif): Fixes the "jagged lines" (combing) typical of DVD sources.

    H.264 Level 4.1: Locks the complexity to a standard supported by hardware decoding chips.

    Stereo Downmix (-ac 2): Ensures audio playback on TVs without external receivers.

    FastStart: Moves metadata to the front of the file so Plex can start the stream instantly.

🛡 Decoy Handling

If the script encounters a "fake" title (one that claims to be long but contains no real data), it performs a size-check after extraction. If the raw file is under 500MB, it is flagged as a decoy, deleted, and the script moves to the next candidate in the prioritized list.
