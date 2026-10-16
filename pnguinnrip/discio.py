"""Read IFO files from a DVD drive, an ISO image, or a VIDEO_TS folder.

A folder is read directly. Drives and ISOs go through libdvdread (the same
library ffmpeg and VLC use), which understands the disc's UDF filesystem and
copes with protections that plant misleading file sizes.
"""
import ctypes
import ctypes.util
import os

from .ifo import load_disc

DVD_READ_INFO_FILE = 0
DVD_READ_INFO_BACKUP_FILE = 1


class DiscReadError(Exception):
    pass


def open_disc(source):
    """Return (Disc, volume_label) for a device path, ISO, or folder."""
    if os.path.isdir(source):
        reader = FolderReader(source)
    else:
        reader = DvdReadReader(source)
    try:
        return load_disc(reader.read_ifo), reader.volume_label()
    finally:
        reader.close()


def find_video_ts(path):
    if os.path.basename(os.path.normpath(path)).upper() == "VIDEO_TS":
        return path
    for name in os.listdir(path):
        if name.upper() == "VIDEO_TS" and os.path.isdir(os.path.join(path, name)):
            return os.path.join(path, name)
    raise DiscReadError(f"no VIDEO_TS folder in {path}")


class FolderReader:
    def __init__(self, path):
        self.video_ts = find_video_ts(path)
        self.files = {name.upper(): os.path.join(self.video_ts, name)
                      for name in os.listdir(self.video_ts)}

    def read_ifo(self, vts):
        stem = "VIDEO_TS" if vts == 0 else f"VTS_{vts:02d}_0"
        found = []
        for ext in (".IFO", ".BUP"):
            path = self.files.get(stem + ext)
            if path:
                with open(path, "rb") as handle:
                    found.append(handle.read())
        return found

    def volume_label(self):
        parent = os.path.dirname(os.path.abspath(self.video_ts))
        return os.path.basename(parent) or "DVD"

    def close(self):
        pass


def _load_library(names):
    for name in names:
        if not name:
            continue
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


def load_libdvdread():
    lib = _load_library(["libdvdread.so.8", "libdvdread.so.7", "libdvdread.so.4",
                         ctypes.util.find_library("dvdread"), "libdvdread.dylib",
                         "libdvdread-8.dll"])
    if lib is None:
        return None
    lib.DVDOpen.restype = ctypes.c_void_p
    lib.DVDOpen.argtypes = [ctypes.c_char_p]
    lib.DVDClose.argtypes = [ctypes.c_void_p]
    lib.DVDOpenFile.restype = ctypes.c_void_p
    lib.DVDOpenFile.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.DVDCloseFile.argtypes = [ctypes.c_void_p]
    lib.DVDFileSize.restype = ctypes.c_ssize_t
    lib.DVDFileSize.argtypes = [ctypes.c_void_p]
    lib.DVDReadBytes.restype = ctypes.c_ssize_t
    lib.DVDReadBytes.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.DVDUDFVolumeInfo.restype = ctypes.c_int
    lib.DVDUDFVolumeInfo.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint,
                                     ctypes.c_void_p, ctypes.c_uint]
    return lib


class DvdReadReader:
    def __init__(self, source):
        self.lib = load_libdvdread()
        if self.lib is None:
            raise DiscReadError("libdvdread not found (Debian/Ubuntu: apt install libdvdread8)")
        self.dvd = self.lib.DVDOpen(os.fsencode(source))
        if not self.dvd:
            raise DiscReadError(f"libdvdread could not open {source} (is a disc inserted?)")

    def _read(self, vts, domain):
        handle = self.lib.DVDOpenFile(self.dvd, vts, domain)
        if not handle:
            return None
        try:
            size = self.lib.DVDFileSize(handle) * 2048
            if size <= 0:
                return None
            buffer = ctypes.create_string_buffer(size)
            got = self.lib.DVDReadBytes(handle, buffer, size)
            return buffer.raw[:got] if got > 0 else None
        finally:
            self.lib.DVDCloseFile(handle)

    def read_ifo(self, vts):
        found = [self._read(vts, DVD_READ_INFO_FILE), self._read(vts, DVD_READ_INFO_BACKUP_FILE)]
        return [data for data in found if data]

    def volume_label(self):
        label = ctypes.create_string_buffer(33)
        if self.lib.DVDUDFVolumeInfo(self.dvd, label, 33, None, 0) == 0:
            text = label.value.decode("latin-1").strip()
            if text:
                return text
        return "DVD"

    def close(self):
        if self.dvd:
            self.lib.DVDClose(self.dvd)
            self.dvd = None
