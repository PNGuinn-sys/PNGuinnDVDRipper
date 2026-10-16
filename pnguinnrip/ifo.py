"""Parse DVD-Video IFO files (VIDEO_TS.IFO and VTS_xx_0.IFO).

The IFO files are the disc's table of contents: which titles exist, which
title set (VTS) holds each one, the ordered list of cells (stretches of video
on the disc) each title plays, and the language/type of every audio and
subtitle track. They are never CSS-encrypted, so we can read them freely.

Byte offsets follow the DVD-Video layout documented at dvd.sourceforge.net
and in libdvdread's ifo_types.h. All multi-byte values are big-endian.
"""
import struct
from dataclasses import dataclass, field

SECTOR = 2048


class IfoError(Exception):
    pass


def _u16(data, offset):
    return struct.unpack_from(">H", data, offset)[0]


def _u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def _bcd(value):
    return (value >> 4) * 10 + (value & 0x0F)


def parse_time(data, offset):
    """Decode a 4-byte BCD playback time (hh mm ss ff) into seconds."""
    hours, minutes, seconds, frame_byte = data[offset:offset + 4]
    total = _bcd(hours) * 3600 + _bcd(minutes) * 60 + _bcd(seconds)
    fps = {1: 25.0, 3: 30000 / 1001}.get(frame_byte >> 6)
    if fps:
        total += _bcd(frame_byte & 0x3F) / fps
    return total


def _lang(data, offset, present):
    if not present:
        return ""
    code = data[offset:offset + 2].decode("ascii", "replace").strip("\0 ").lower()
    return code if len(code) == 2 and code.isalpha() else ""


AUDIO_CODING = {0: "ac3", 2: "mpeg1", 3: "mpeg2", 4: "lpcm", 6: "dts"}

# First MPEG stream id for each audio coding; add the physical stream number.
AUDIO_STREAM_ID_BASE = {"ac3": 0x80, "dts": 0x88, "lpcm": 0xA0, "mpeg1": 0xC0, "mpeg2": 0xC0}


@dataclass
class AudioAttr:
    index: int  # logical stream number within the title set (0-7)
    coding: str
    channels: int
    language: str  # ISO 639-1, e.g. "en"; "" when the disc doesn't say
    code_extension: int  # 1 normal, 2 visually impaired, 3/4 director's comments

    @property
    def is_commentary(self):
        return self.code_extension in (3, 4)

    @property
    def is_descriptive(self):
        return self.code_extension == 2


@dataclass
class SubpAttr:
    index: int
    language: str
    code_extension: int  # 1 normal, 2 large, 3 children, 5-7 captions, 9 forced, 13-15 commentary

    @property
    def is_forced(self):
        return self.code_extension == 9

    @property
    def is_commentary(self):
        return self.code_extension in (13, 14, 15)


@dataclass
class VideoAttr:
    standard: str  # "ntsc" or "pal"
    aspect: str  # "4:3" or "16:9"


@dataclass
class Cell:
    first_sector: int
    last_sector: int
    duration: float
    block_mode: int  # 0 not in a block, 1 first, 2 middle, 3 last cell of block
    block_type: int  # 1 = angle block
    vob_id: int = 0
    cell_id: int = 0

    @property
    def sectors(self):
        return max(0, self.last_sector - self.first_sector + 1)


@dataclass
class Pgc:
    """A program chain: the ordered list of cells a title plays."""
    duration: float
    cells: list
    # Per logical audio stream: the physical stream number, or None if the
    # stream is switched off for this PGC.
    audio_control: list
    # Per logical subtitle stream: (4:3, wide, letterbox, pan-scan) physical
    # stream numbers, or None if unavailable.
    subp_control: list


@dataclass
class TitleEntry:
    number: int  # global title number (what lsdvd, libdvdnav and ffmpeg call "title")
    angles: int
    chapters: int
    vts: int
    vts_ttn: int  # title number inside its title set


@dataclass
class TitleSet:
    number: int
    video: VideoAttr
    audio: list
    subpictures: list
    pgcs: list  # index 0 is PGC number 1; None for PGCs that failed to parse
    ptt: dict  # vts_ttn -> [(pgcn, pgn), ...] (one entry per chapter)

    def title_pgcs(self, vts_ttn):
        """The PGCs a title plays, in order, without repeats."""
        numbers = []
        for pgcn, _pgn in self.ptt.get(vts_ttn, []):
            if pgcn not in numbers:
                numbers.append(pgcn)
        return [self.pgcs[n - 1] for n in numbers if 0 < n <= len(self.pgcs) and self.pgcs[n - 1]]


@dataclass
class Disc:
    titles: list
    title_sets: dict = field(default_factory=dict)
    region_mask: int = 0  # bit n set = region n+1 NOT allowed
    provider: str = ""


def parse_vmg(data):
    if data[:12] != b"DVDVIDEO-VMG":
        raise IfoError("not a VIDEO_TS.IFO (bad signature)")
    try:
        region_mask = data[0x23]
        provider = data[0x40:0x60].split(b"\0")[0].decode("latin-1").strip()
        tt_srpt = _u32(data, 0xC4) * SECTOR
        count = _u16(data, tt_srpt)
        titles = []
        for i in range(count):
            o = tt_srpt + 8 + i * 12
            titles.append(TitleEntry(
                number=i + 1,
                angles=data[o + 1],
                chapters=_u16(data, o + 2),
                vts=data[o + 6],
                vts_ttn=data[o + 7],
            ))
    except (struct.error, IndexError) as exc:
        raise IfoError(f"truncated VIDEO_TS.IFO: {exc}") from exc
    return Disc(titles=titles, region_mask=region_mask, provider=provider)


def parse_video_attr(data, offset):
    b0 = data[offset]
    standard = "pal" if (b0 >> 4) & 0x03 == 1 else "ntsc"
    aspect = "16:9" if (b0 >> 2) & 0x03 == 3 else "4:3"
    return VideoAttr(standard=standard, aspect=aspect)


def parse_audio_attr(data, offset, index):
    b0, b1 = data[offset], data[offset + 1]
    return AudioAttr(
        index=index,
        coding=AUDIO_CODING.get(b0 >> 5, "unknown"),
        channels=(b1 & 0x07) + 1,
        language=_lang(data, offset + 2, (b0 >> 2) & 0x03 == 1),
        code_extension=data[offset + 5],
    )


def parse_subp_attr(data, offset, index):
    return SubpAttr(
        index=index,
        language=_lang(data, offset + 2, data[offset] & 0x03 == 1),
        code_extension=data[offset + 5],
    )


def parse_pgc(data, o):
    cell_count = data[o + 3]
    audio_control = []
    for i in range(8):
        value = _u16(data, o + 0x0C + i * 2)
        audio_control.append((value >> 8) & 0x07 if value & 0x8000 else None)
    subp_control = []
    for i in range(32):
        value = _u32(data, o + 0x1C + i * 4)
        if value & 0x80000000:
            subp_control.append(((value >> 24) & 0x1F, (value >> 16) & 0x1F,
                                 (value >> 8) & 0x1F, value & 0x1F))
        else:
            subp_control.append(None)

    playback_offset = _u16(data, o + 0xE8)
    position_offset = _u16(data, o + 0xEA)
    cells = []
    if playback_offset:
        for i in range(cell_count):
            c = o + playback_offset + i * 24
            category = data[c]
            cell = Cell(
                first_sector=_u32(data, c + 8),
                last_sector=_u32(data, c + 20),
                duration=parse_time(data, c + 4),
                block_mode=(category >> 6) & 0x03,
                block_type=(category >> 4) & 0x03,
            )
            if position_offset:
                p = o + position_offset + i * 4
                cell.vob_id = _u16(data, p)
                cell.cell_id = data[p + 3]
            cells.append(cell)

    return Pgc(duration=parse_time(data, o + 4), cells=cells,
               audio_control=audio_control, subp_control=subp_control)


def parse_pgcit(data, base):
    count = _u16(data, base)
    pgcs = []
    for i in range(count):
        entry = base + 8 + i * 8
        try:
            pgcs.append(parse_pgc(data, base + _u32(data, entry + 4)))
        except (struct.error, IndexError, ValueError):
            # Copy protection sometimes plants deliberately broken PGCs.
            pgcs.append(None)
    return pgcs


def parse_ptt_srpt(data, base):
    count = _u16(data, base)
    end = _u32(data, base + 4) + 1
    offsets = [_u32(data, base + 8 + i * 4) for i in range(count)]
    table = {}
    for i, start in enumerate(offsets):
        stop = offsets[i + 1] if i + 1 < count else end
        entries = []
        for p in range(start, stop - 3, 4):
            entries.append((_u16(data, base + p), _u16(data, base + p + 2)))
        table[i + 1] = entries
    return table


def parse_vts(data, number):
    if data[:12] != b"DVDVIDEO-VTS":
        raise IfoError(f"VTS_{number:02d}_0.IFO has a bad signature")
    try:
        audio_count = min(_u16(data, 0x202), 8)
        subp_count = min(_u16(data, 0x254), 32)
        return TitleSet(
            number=number,
            video=parse_video_attr(data, 0x200),
            audio=[parse_audio_attr(data, 0x204 + i * 8, i) for i in range(audio_count)],
            subpictures=[parse_subp_attr(data, 0x256 + i * 6, i) for i in range(subp_count)],
            pgcs=parse_pgcit(data, _u32(data, 0xCC) * SECTOR),
            ptt=parse_ptt_srpt(data, _u32(data, 0xC8) * SECTOR),
        )
    except (struct.error, IndexError) as exc:
        raise IfoError(f"truncated VTS_{number:02d}_0.IFO: {exc}") from exc


def load_disc(read_ifo):
    """Build a Disc from a callable that returns IFO candidates.

    read_ifo(n) returns a list of byte strings to try for title set n
    (0 = VIDEO_TS.IFO): normally the .IFO and then its .BUP backup, since
    protected discs sometimes corrupt one of the pair.
    """
    disc = _first_valid(read_ifo(0), parse_vmg, "VIDEO_TS.IFO")
    for vts in sorted({t.vts for t in disc.titles}):
        try:
            disc.title_sets[vts] = _first_valid(read_ifo(vts), lambda d: parse_vts(d, vts),
                                                f"VTS_{vts:02d}_0.IFO")
        except IfoError:
            pass  # titles in this set will show up as unreadable
    return disc


def _first_valid(candidates, parse, name):
    errors = []
    for data in candidates:
        if not data:
            continue
        try:
            return parse(data)
        except IfoError as exc:
            errors.append(str(exc))
    raise IfoError(f"could not read {name}: {'; '.join(errors) or 'file missing'}")
