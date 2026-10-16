"""Build synthetic VIDEO_TS folders for tests.

Writes only the IFO structures pnguinnrip reads, at the offsets the
DVD-Video spec defines, so tests can model protected discs without needing
real ones.
"""
import os
import struct
from dataclasses import dataclass, field

SECTOR = 2048


def bcd(n):
    return ((n // 10) << 4) | (n % 10)


def bcd_time(seconds, ntsc=True):
    whole = int(seconds)
    frames = int(round((seconds - whole) * (30 if ntsc else 25)))
    rate = 0xC0 if ntsc else 0x40
    return bytes([bcd(whole // 3600), bcd(whole % 3600 // 60), bcd(whole % 60), rate | bcd(frames)])


@dataclass
class FakeCell:
    first: int
    last: int
    seconds: float
    block_mode: int = 0
    block_type: int = 0


@dataclass
class FakeAudio:
    language: str = "en"
    coding: int = 0  # 0 ac3, 4 lpcm, 6 dts
    channels: int = 6
    code_extension: int = 1


@dataclass
class FakeSub:
    language: str = "en"
    code_extension: int = 1


@dataclass
class FakeTitle:
    cells: list
    vts: int = 1
    pgc_seconds: float | None = None  # override the PGC's claimed length


@dataclass
class FakeVts:
    audio: list = field(default_factory=lambda: [FakeAudio()])
    subs: list = field(default_factory=list)
    pal: bool = False
    widescreen: bool = True


def contiguous_cells(start_sector, minutes, count=20, mbps=6.0):
    """Cells for a normal title: forward order, realistic bitrate."""
    seconds = minutes * 60 / count
    sectors = int(seconds * mbps * 1e6 / 8 / SECTOR)
    cells, sector = [], start_sector
    for _ in range(count):
        cells.append(FakeCell(sector, sector + sectors - 1, seconds))
        sector += sectors
    return cells


def _pad(data):
    return data + b"\0" * (-len(data) % SECTOR)


def build_pgc(cells, audio_count, sub_count, claimed_seconds):
    head = bytearray(0xEC)
    head[2] = 1  # programs
    head[3] = len(cells)
    head[4:8] = bcd_time(claimed_seconds)
    for i in range(audio_count):
        struct.pack_into(">H", head, 0x0C + i * 2, 0x8000 | (i << 8))
    for i in range(sub_count):
        struct.pack_into(">I", head, 0x1C + i * 4, 0x80000000 | (i << 24) | (i << 16) | (i << 8) | i)
    program_map = bytes([1])
    playback = bytearray()
    positions = bytearray()
    for n, c in enumerate(cells, 1):
        entry = bytearray(24)
        entry[0] = (c.block_mode << 6) | (c.block_type << 4)
        entry[4:8] = bcd_time(c.seconds)
        struct.pack_into(">I", entry, 8, c.first)
        struct.pack_into(">I", entry, 20, c.last)
        playback += entry
        positions += struct.pack(">HBB", 1, 0, n)
    program_offset = 0xEC
    playback_offset = program_offset + len(program_map) + (-(program_offset + len(program_map)) % 4)
    position_offset = playback_offset + len(playback)
    struct.pack_into(">HHHH", head, 0xE4, 0, program_offset, playback_offset, position_offset)
    body = bytes(head) + program_map
    body += b"\0" * (playback_offset - len(body))
    return body + bytes(playback) + bytes(positions)


def build_vts(vts, titles):
    ifo = bytearray(SECTOR)
    ifo[:12] = b"DVDVIDEO-VTS"
    ifo[0x200] = (1 << 6) | ((1 if vts.pal else 0) << 4) | ((3 if vts.widescreen else 0) << 2)
    struct.pack_into(">H", ifo, 0x202, len(vts.audio))
    for i, a in enumerate(vts.audio):
        o = 0x204 + i * 8
        ifo[o] = (a.coding << 5) | (1 << 2)
        ifo[o + 1] = a.channels - 1
        ifo[o + 2:o + 4] = a.language.encode()
        ifo[o + 5] = a.code_extension
    struct.pack_into(">H", ifo, 0x254, len(vts.subs))
    for i, s in enumerate(vts.subs):
        o = 0x256 + i * 6
        ifo[o] = 1
        ifo[o + 2:o + 4] = s.language.encode()
        ifo[o + 5] = s.code_extension

    # PTT_SRPT: one chapter per title, each pointing at its own PGC.
    ptt = bytearray(8 + 4 * len(titles))
    struct.pack_into(">H", ptt, 0, len(titles))
    entries = b""
    for i in range(len(titles)):
        struct.pack_into(">I", ptt, 8 + i * 4, len(ptt) + len(entries))
        entries += struct.pack(">HH", i + 1, 1)
    ptt += entries
    struct.pack_into(">I", ptt, 4, len(ptt) - 1)

    pgcit = bytearray(8 + 8 * len(titles))
    struct.pack_into(">H", pgcit, 0, len(titles))
    bodies = b""
    for i, t in enumerate(titles):
        claimed = t.pgc_seconds if t.pgc_seconds is not None else sum(c.seconds for c in t.cells)
        struct.pack_into(">BxxxI", pgcit, 8 + i * 8, 0x80 | (i + 1), len(pgcit) + len(bodies))
        bodies += build_pgc(t.cells, len(vts.audio), len(vts.subs), claimed)
    pgcit = bytes(pgcit) + bodies

    ptt_sector = 1
    pgcit_sector = ptt_sector + len(_pad(bytes(ptt))) // SECTOR
    struct.pack_into(">I", ifo, 0xC8, ptt_sector)
    struct.pack_into(">I", ifo, 0xCC, pgcit_sector)
    return bytes(ifo) + _pad(bytes(ptt)) + _pad(pgcit)


def build_vmg(titles, region_mask=0):
    ifo = bytearray(SECTOR)
    ifo[:12] = b"DVDVIDEO-VMG"
    ifo[0x23] = region_mask
    ifo[0x40:0x44] = b"TEST"
    struct.pack_into(">I", ifo, 0xC4, 1)
    srpt = bytearray(8 + 12 * len(titles))
    struct.pack_into(">H", srpt, 0, len(titles))
    ttn = {}
    for i, t in enumerate(titles):
        ttn[t.vts] = ttn.get(t.vts, 0) + 1
        struct.pack_into(">BBHHBBI", srpt, 8 + i * 12, 0x3C, 1, 1, 0, t.vts, ttn[t.vts], 0)
    struct.pack_into(">I", srpt, 4, len(srpt) - 1)
    return bytes(ifo) + _pad(bytes(srpt))


def write_disc(root, titles, title_sets=None, region_mask=0, write_ifo=True, write_bup=True):
    """Write VIDEO_TS under root. titles share VTS numbers via FakeTitle.vts."""
    title_sets = title_sets or {}
    video_ts = os.path.join(root, "VIDEO_TS")
    os.makedirs(video_ts, exist_ok=True)
    files = {"VIDEO_TS": build_vmg(titles, region_mask)}
    for vts in sorted({t.vts for t in titles}):
        files[f"VTS_{vts:02d}_0"] = build_vts(title_sets.get(vts, FakeVts()),
                                              [t for t in titles if t.vts == vts])
    for stem, data in files.items():
        for ext, wanted in ((".IFO", write_ifo), (".BUP", write_bup)):
            if wanted:
                with open(os.path.join(video_ts, stem + ext), "wb") as handle:
                    handle.write(data)
    return root
