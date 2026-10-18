"""Decide which title is the movie and which audio/subtitle tracks to keep.

Decoy titles on protected discs (Paramount, Disney, Sony...) usually reuse
the real movie's cells in a scrambled order, repeat cells, or claim a long
runtime while pointing at very little data. A real feature plays its cells
forward across the disc, once each, at a normal DVD bitrate.
"""
from collections import Counter
from dataclasses import dataclass, field

from .ifo import AUDIO_STREAM_ID_BASE

MIN_PLAUSIBLE_MBPS = 1.0
MAX_PLAUSIBLE_MBPS = 15.0  # DVD tops out near 10; angle blocks inflate the estimate


@dataclass
class TitleReport:
    entry: object  # ifo.TitleEntry
    duration: float = 0.0
    cell_count: int = 0
    backward_jumps: int = 0
    repeated_cells: int = 0
    tiny_cells: int = 0
    size_bytes: int = 0
    problems: list = field(default_factory=list)

    @property
    def number(self):
        return self.entry.number

    @property
    def clean(self):
        return not self.problems

    @property
    def mbps(self):
        return self.size_bytes * 8 / self.duration / 1e6 if self.duration else 0.0


def analyze_title(disc, entry):
    report = TitleReport(entry=entry)
    vts = disc.title_sets.get(entry.vts)
    pgcs = vts.title_pgcs(entry.vts_ttn) if vts else []
    if not pgcs:
        report.problems.append("unreadable (no valid program chain)")
        return report

    # Inside an angle block only the first angle's cells count toward runtime.
    cells = [c for pgc in pgcs for c in pgc.cells if not (c.block_type == 1 and c.block_mode > 1)]
    report.cell_count = len(cells)
    report.duration = sum(c.duration for c in cells)
    if not cells or report.duration <= 0:
        report.problems.append("no playable cells")
        return report

    for previous, current in zip(cells, cells[1:]):
        if current.first_sector < previous.last_sector:
            report.backward_jumps += 1
    spans = Counter((c.first_sector, c.last_sector) for c in cells)
    report.repeated_cells = sum(n - 1 for n in spans.values() if n > 1)
    report.tiny_cells = sum(1 for c in cells if c.duration < 1.0)
    report.size_bytes = sum(c.sectors for c in {(c.first_sector, c.last_sector): c for c in cells}.values()) * 2048

    if report.backward_jumps:
        report.problems.append(f"plays out of order ({report.backward_jumps} backward jumps)")
    if report.repeated_cells:
        report.problems.append(f"repeats cells ({report.repeated_cells})")
    if report.mbps < MIN_PLAUSIBLE_MBPS:
        report.problems.append(f"too little data for its runtime ({report.mbps:.2f} Mbps)")
    elif report.mbps > MAX_PLAUSIBLE_MBPS:
        report.problems.append(f"too much data for its runtime ({report.mbps:.1f} Mbps)")
    pgc_claim = sum(p.duration for p in pgcs)
    if pgc_claim and abs(pgc_claim - report.duration) > max(5.0, 0.05 * report.duration):
        report.problems.append("claimed length doesn't match its cells")
    return report


def analyze_disc(disc):
    reports = [analyze_title(disc, entry) for entry in disc.titles]
    flag_low_data_copies(reports)
    return reports


def flag_low_data_copies(reports):
    """Flag titles that run as long as an in-order title but hold far less video.

    Seen on Paramount discs: several titles claim the movie's exact runtime and
    play in order, but point at a fraction of its data.
    """
    ordered = [r for r in reports if r.duration > 0 and not (r.backward_jumps or r.repeated_cells)]
    for r in reports:
        if r.duration <= 0:
            continue
        same_length = [o for o in ordered if o is not r
                       and abs(o.duration - r.duration) <= max(30.0, 0.01 * r.duration)]
        bigger = max(same_length, key=lambda o: o.size_bytes, default=None)
        if bigger and bigger.size_bytes >= 1.5 * r.size_bytes:
            share = r.size_bytes / bigger.size_bytes * 100
            r.problems.append(f"only {share:.0f}% of the video data of same-length title {bigger.number}")


def rank_structural(reports):
    """Best guess from structure alone: clean, then longest, then most data."""
    def key(r):
        return (len(r.problems), r.backward_jumps + r.repeated_cells,
                -round(r.duration / 60), -r.size_bytes, r.number)
    return sorted(reports, key=key)


@dataclass
class TitleChoice:
    report: TitleReport
    reason: str
    warnings: list = field(default_factory=list)


def choose_title(reports, min_minutes, navigator_title=None):
    """Pick the main feature, trusting the disc's own Play button first."""
    long_enough = [r for r in reports if r.duration >= min_minutes * 60]
    if not long_enough:
        return None
    best = rank_structural(long_enough)[0]
    warnings = []

    if navigator_title is not None:
        picked = next((r for r in reports if r.number == navigator_title), None)
        if picked is None:
            warnings.append(f"menu led to title {navigator_title}, which the IFO doesn't list")
        elif picked.duration < min_minutes * 60:
            warnings.append(f"menu led to title {navigator_title}, but it is only "
                            f"{picked.duration / 60:.0f} min")
        elif picked.duration < 0.5 * best.duration:
            warnings.append(f"menu led to title {navigator_title} ({picked.duration / 60:.0f} min), "
                            f"much shorter than title {best.number} ({best.duration / 60:.0f} min); "
                            "probably a bonus feature")
        else:
            if picked.problems and picked.number != best.number:
                warnings.append(f"title {picked.number} looks unusual ({'; '.join(picked.problems)}), "
                                "but it is what the disc's menu plays")
            return TitleChoice(picked, "the disc's own menu plays it", warnings)

    if best.problems:
        warnings.append("every long title looks suspicious; picked the least suspicious one")
        return TitleChoice(best, "least suspicious structure", warnings)
    clean_count = sum(1 for r in long_enough if r.clean)
    reason = "only title with a clean structure" if clean_count == 1 else \
        "longest title with a clean structure"
    return TitleChoice(best, reason, warnings)


@dataclass
class AudioTrack:
    attr: object  # ifo.AudioAttr
    stream_id: int


@dataclass
class SubtitleTrack:
    attr: object  # ifo.SubpAttr
    stream_ids: tuple  # every physical id this logical stream may use


def _first_pgc(disc, entry):
    vts = disc.title_sets.get(entry.vts)
    pgcs = vts.title_pgcs(entry.vts_ttn) if vts else []
    return vts, (pgcs[0] if pgcs else None)


def title_audio_tracks(disc, entry):
    vts, pgc = _first_pgc(disc, entry)
    if pgc is None:
        return []
    tracks = []
    for attr in vts.audio:
        physical = pgc.audio_control[attr.index]
        if physical is not None and attr.coding in AUDIO_STREAM_ID_BASE:
            tracks.append(AudioTrack(attr, AUDIO_STREAM_ID_BASE[attr.coding] + physical))
    return tracks


def choose_audio(tracks, language):
    """Main-feature track in the wanted language; otherwise the disc's default."""
    if not tracks:
        return None
    main = [t for t in tracks if not (t.attr.is_commentary or t.attr.is_descriptive)] or tracks
    wanted = [t for t in main if t.attr.language == language]
    if not wanted:
        return main[0]  # first listed track is the disc's default (usually original language)
    # Commentary is often left unflagged, but it is nearly always plain
    # stereo listed after the movie's own track, so more channels then
    # earlier position picks the movie.
    return max(wanted, key=lambda t: (t.attr.channels, -t.attr.index))


def title_subtitle_tracks(disc, entry):
    vts, pgc = _first_pgc(disc, entry)
    if pgc is None:
        return []
    tracks = []
    for attr in vts.subpictures:
        physical = pgc.subp_control[attr.index] if attr.index < len(pgc.subp_control) else None
        if physical is not None:
            tracks.append(SubtitleTrack(attr, tuple(sorted({0x20 + p for p in physical}))))
    return tracks


def choose_subtitles(tracks, language):
    return [t for t in tracks if t.attr.language == language and not t.attr.is_commentary]
