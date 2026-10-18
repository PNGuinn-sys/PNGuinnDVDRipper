"""Download an ffmpeg that has everything pnguinnrip needs.

    python3 -m pnguinnrip.get_ffmpeg

Fetches BtbN's GPL build (github.com/BtbN/FFmpeg-Builds), which includes the
dvdvideo DVD reader (libdvdnav, libdvdread, libdvdcss) and the x264 encoder,
verifies its checksum, and installs it where pnguinnrip looks first. Nothing
is installed system-wide; delete the folder to remove it.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

from .ffmpeg import missing_features, tools_dir

RELEASE_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"


def platform_tag():
    machine = platform.machine().lower()
    arm = machine in ("aarch64", "arm64")
    if sys.platform.startswith("linux"):
        return "linuxarm64" if arm else "linux64"
    if sys.platform == "win32":
        return "winarm64" if arm else "win64"
    return None


def pick_asset(assets, tag, version=None):
    """Newest numbered GPL release for this platform (never the nightly 'master')."""
    pattern = re.compile(rf"^ffmpeg-n(\d+)\.(\d+)-latest-{tag}-gpl-\d+\.\d+\.(?:tar\.xz|zip)$")
    best = None
    for asset in assets:
        match = pattern.match(asset["name"])
        if not match:
            continue
        found = (int(match[1]), int(match[2]))
        if version and f"{found[0]}.{found[1]}" != version:
            continue
        if found >= (7, 0) and (best is None or found > best[0]):
            best = (found, asset)
    return best[1] if best else None


def fetch(url, destination=None):
    request = urllib.request.Request(url, headers={"User-Agent": "pnguinnrip"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if destination is None:
            return response.read()
        total = int(response.headers.get("Content-Length") or 0)
        done, next_report = 0, 10
        with open(destination, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if total and done * 100 / total >= next_report:
                    print(f"    {done * 100 // total}%", flush=True)
                    next_report += 10
    return None


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(archive, destination):
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(destination)
    else:
        with tarfile.open(archive) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(destination, filter="data")
            else:
                t.extractall(destination)
    # The archive holds one top-level folder, e.g. ffmpeg-n8.1-latest-linux64-gpl-8.1/
    entries = os.listdir(destination)
    return os.path.join(destination, entries[0]) if len(entries) == 1 else destination


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pnguinnrip.get_ffmpeg", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", help="FFmpeg release to fetch, e.g. 8.1 (default: newest)")
    parser.add_argument("--dest", default=tools_dir(), help=f"install folder (default: {tools_dir()})")
    args = parser.parse_args(argv)

    tag = platform_tag()
    if tag is None:
        print("[!] Prebuilt ffmpeg is only available for Linux and Windows here.")
        return 1

    print("[+] Looking up the latest BtbN FFmpeg build...")
    release = json.loads(fetch(RELEASE_API))
    asset = pick_asset(release.get("assets", []), tag, args.version)
    if asset is None:
        print(f"[!] No {tag} GPL build{' for ' + args.version if args.version else ''} in the latest release.")
        return 1
    checksums_asset = next((a for a in release["assets"] if a["name"] == "checksums.sha256"), None)

    with tempfile.TemporaryDirectory() as work:
        archive = os.path.join(work, asset["name"])
        print(f"[+] Downloading {asset['name']} ({asset['size'] / 2**20:.0f} MB)...")
        fetch(asset["browser_download_url"], archive)

        if checksums_asset:
            listing = fetch(checksums_asset["browser_download_url"]).decode()
            expected = next((line.split()[0] for line in listing.splitlines()
                             if line.strip().endswith(asset["name"])), None)
            if expected and sha256(archive) != expected.lower():
                print("[!] Checksum mismatch; the download is corrupt or was tampered with. Not installing.")
                return 1
            print("[+] Checksum verified." if expected else "[!] No checksum listed for this file; continuing.")

        root = extract(archive, os.path.join(work, "x"))
        exe = ".exe" if os.name == "nt" else ""
        ffmpeg = os.path.join(root, "bin", "ffmpeg" + exe)
        demuxers = subprocess.run([ffmpeg, "-hide_banner", "-demuxers"], capture_output=True, text=True).stdout
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        missing = missing_features(demuxers, encoders, need_x264=True)
        if missing:
            print(f"[!] This build is missing {' and '.join(missing)}. Not installing.")
            return 1

        if os.path.exists(args.dest):
            shutil.rmtree(args.dest)
        os.makedirs(os.path.dirname(args.dest), exist_ok=True)
        shutil.move(root, args.dest)

    version = subprocess.run([os.path.join(args.dest, "bin", "ffmpeg" + exe), "-hide_banner", "-version"],
                             capture_output=True, text=True).stdout.splitlines()[:1]
    version = version[0].split(" Copyright")[0] if version else "ffmpeg"
    print(f"[***] Installed {version} to {args.dest}")
    if os.path.abspath(args.dest) == os.path.abspath(tools_dir()):
        print("      pnguinnrip will use it automatically.")
    else:
        print(f"      Use it with: --ffmpeg-dir {os.path.join(args.dest, 'bin')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
