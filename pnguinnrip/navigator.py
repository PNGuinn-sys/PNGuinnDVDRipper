"""Find the main feature the way a DVD player does: by pressing Play.

A DVD contains a small program (the "VM" commands in its menus) that decides
what plays. Copy-protected discs hide the real movie among decoys and rely
on that program to steer players to it. libdvdnav runs the program for us;
we skip warnings and trailers, press the highlighted button on menus that
wait for input, and note which long title playback lands in.

Like a person with a remote, we only press on a menu that is waiting: one
frozen on a still frame, or one that has come around again. Some discs
(Paranormal Activity 2) open with brief black screens full of hidden
buttons that time out on their own; pressing them leads into a maze that
ends at a decoy title.
"""
import ctypes
import ctypes.util
import os
import time
from collections import Counter

from .discio import LibraryLog, LoggerCallbacks

DVDNAV_STATUS_OK = 1

DVDNAV_BLOCK_OK = 0
DVDNAV_STILL_FRAME = 2
DVDNAV_VTS_CHANGE = 5
DVDNAV_CELL_CHANGE = 6
DVDNAV_NAV_PACKET = 7
DVDNAV_STOP = 8
DVDNAV_WAIT = 13

# Offsets into libdvdread's packed pci_t: hli.hl_gi.btn_ns (number of
# buttons), hli.hl_gi.fosl_btnn (button the disc says to highlight first),
# and hli.btnit (18-byte button entries, each ending in its 8-byte command).
PCI_BUTTON_COUNT_OFFSET = 113
PCI_FORCED_SELECT_OFFSET = 116
PCI_BUTTONS_OFFSET = 142
BUTTON_SIZE = 18
BUTTON_COMMAND_OFFSET = 10
MAX_BUTTONS = 36
STILL_FOREVER = 0xFF


def load_libdvdnav():
    lib = None
    for name in ["libdvdnav.so.4", ctypes.util.find_library("dvdnav"), "libdvdnav.dylib",
                 "libdvdnav-4.dll"]:
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        return None

    nav = ctypes.c_void_p
    i32p = ctypes.POINTER(ctypes.c_int32)
    signatures = {
        "dvdnav_open": ([ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p], ctypes.c_int),
        "dvdnav_close": ([nav], ctypes.c_int),
        "dvdnav_err_to_string": ([nav], ctypes.c_char_p),
        "dvdnav_set_readahead_flag": ([nav, ctypes.c_int32], ctypes.c_int),
        "dvdnav_set_PGC_positioning_flag": ([nav, ctypes.c_int32], ctypes.c_int),
        "dvdnav_menu_language_select": ([nav, ctypes.c_char_p], ctypes.c_int),
        "dvdnav_audio_language_select": ([nav, ctypes.c_char_p], ctypes.c_int),
        "dvdnav_spu_language_select": ([nav, ctypes.c_char_p], ctypes.c_int),
        "dvdnav_set_region_mask": ([nav, ctypes.c_int32], ctypes.c_int),
        "dvdnav_get_next_block": ([nav, ctypes.POINTER(ctypes.c_uint8), i32p, i32p], ctypes.c_int),
        "dvdnav_still_skip": ([nav], ctypes.c_int),
        "dvdnav_wait_skip": ([nav], ctypes.c_int),
        "dvdnav_is_domain_vts": ([nav], ctypes.c_int8),
        "dvdnav_is_domain_vmgm": ([nav], ctypes.c_int8),
        "dvdnav_current_title_info": ([nav, i32p, i32p], ctypes.c_int),
        "dvdnav_next_pg_search": ([nav], ctypes.c_int),
        "dvdnav_get_current_highlight": ([nav, i32p], ctypes.c_int),
        "dvdnav_get_current_nav_pci": ([nav], ctypes.c_void_p),
        "dvdnav_button_select": ([nav, ctypes.c_void_p, ctypes.c_int32], ctypes.c_int),
        "dvdnav_button_activate": ([nav, ctypes.c_void_p], ctypes.c_int),
    }
    optional = {
        "dvdnav_set_region_mask",  # missing from very old builds
        "dvdnav_open2",            # libdvdnav 6.1+; takes a log callback
    }
    signatures["dvdnav_open2"] = ([ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                   ctypes.POINTER(LoggerCallbacks), ctypes.c_char_p], ctypes.c_int)
    for name, (argtypes, restype) in signatures.items():
        func = getattr(lib, name, None)
        if func is None:
            if name in optional:
                continue
            return None
        func.argtypes = argtypes
        func.restype = restype
    return lib


def player_region(disc_region_mask):
    """A region bit the disc allows (the disc's mask marks *forbidden* regions)."""
    allowed = ~disc_region_mask & 0xFF
    return allowed & -allowed or 0x01


def button_count(pci):
    count = ctypes.c_uint8.from_address(pci + PCI_BUTTON_COUNT_OFFSET).value
    return count if 0 < count <= MAX_BUTTONS else None


def _menu_screen(lib, nav):
    """Identify the menu on screen by its buttons' commands, or None if it has none."""
    pci = lib.dvdnav_get_current_nav_pci(nav)
    count = button_count(pci) if pci else None
    if not count:
        return None
    raw = ctypes.string_at(pci + PCI_BUTTONS_OFFSET, BUTTON_SIZE * count)
    commands = tuple(raw[i * BUTTON_SIZE + BUTTON_COMMAND_OFFSET:(i + 1) * BUTTON_SIZE]
                     for i in range(count))
    return bool(lib.dvdnav_is_domain_vmgm(nav)), commands


def find_main_title(source, title_durations, min_seconds, *, language="en",
                    region_mask=0, time_budget=180.0, log=print, messages=None):
    """Return the title number "Play" leads to, or None if we couldn't tell.

    libdvdnav's own messages are appended to `messages` if a list is given.
    """
    lib = load_libdvdnav()
    if lib is None:
        log("    libdvdnav not found; skipping menu playthrough "
            "(Debian/Ubuntu: apt install libdvdnav4)")
        return None

    nav = ctypes.c_void_p()
    library_log = LibraryLog("libdvdnav", messages)  # must outlive the dvdnav handle
    if hasattr(lib, "dvdnav_open2"):
        opened = lib.dvdnav_open2(ctypes.byref(nav), None, ctypes.byref(library_log.callbacks),
                                  os.fsencode(source))
    else:
        opened = lib.dvdnav_open(ctypes.byref(nav), os.fsencode(source))
    if opened != DVDNAV_STATUS_OK:
        log("    libdvdnav could not open the disc")
        return None

    try:
        lib.dvdnav_set_readahead_flag(nav, 1)
        lib.dvdnav_set_PGC_positioning_flag(nav, 1)
        code = language.encode("ascii")
        lib.dvdnav_menu_language_select(nav, code)
        lib.dvdnav_audio_language_select(nav, code)
        lib.dvdnav_spu_language_select(nav, code)
        if hasattr(lib, "dvdnav_set_region_mask"):
            lib.dvdnav_set_region_mask(nav, player_region(region_mask))
        return _play(lib, nav, title_durations, min_seconds, time_budget, log)
    finally:
        lib.dvdnav_close(nav)


def _play(lib, nav, title_durations, min_seconds, time_budget, log):
    buf = (ctypes.c_uint8 * 2048)()
    event, length = ctypes.c_int32(), ctypes.c_int32()
    title, part = ctypes.c_int32(), ctypes.c_int32()
    presses = Counter()  # (screen, button) -> times activated
    visits = Counter()   # screen -> times it has started playing
    new_cell = True
    seen_short = set()
    deadline = time.monotonic() + time_budget

    while time.monotonic() < deadline:
        if lib.dvdnav_get_next_block(nav, buf, ctypes.byref(event), ctypes.byref(length)) != DVDNAV_STATUS_OK:
            log(f"    playback error: {(lib.dvdnav_err_to_string(nav) or b'').decode(errors='replace')}")
            return None
        ev = event.value
        in_menu = not lib.dvdnav_is_domain_vts(nav)

        if ev == DVDNAV_STILL_FRAME:
            # A menu frozen for good is waiting for input; a timed still is not.
            screen = _menu_screen(lib, nav) if in_menu and buf[0] == STILL_FOREVER else None
            if not (screen and _press_menu_button(lib, nav, screen, presses, log, "waiting")):
                lib.dvdnav_still_skip(nav)
        elif ev == DVDNAV_WAIT:
            lib.dvdnav_wait_skip(nav)
        elif ev == DVDNAV_STOP:
            log("    disc stopped before reaching a long title")
            return None
        elif ev in (DVDNAV_VTS_CHANGE, DVDNAV_CELL_CHANGE):
            new_cell = True
            if in_menu:
                continue
            lib.dvdnav_current_title_info(nav, ctypes.byref(title), ctypes.byref(part))
            if title.value <= 0:
                continue
            if title_durations.get(title.value, 0) >= min_seconds:
                return title.value
            if title.value not in seen_short:
                seen_short.add(title.value)
                log(f"    skipping title {title.value} "
                    f"({title_durations.get(title.value, 0) / 60:.1f} min: warning/trailer)")
            # Press "Next" like you would on a remote to get past it.
            lib.dvdnav_next_pg_search(nav)
        elif ev == DVDNAV_NAV_PACKET and in_menu and new_cell:
            screen = _menu_screen(lib, nav)
            if screen is None:
                continue
            new_cell = False
            visits[screen] += 1
            # A looping (animated) menu never freezes; press once it comes around again.
            if visits[screen] >= 2:
                _press_menu_button(lib, nav, screen, presses, log, "looped")

    log("    gave up: menu playthrough took too long")
    return None


def _press_menu_button(lib, nav, screen, presses, log, why):
    """Press the button a viewer would land on; True if the press went through."""
    pci = lib.dvdnav_get_current_nav_pci(nav)
    if not pci:
        return False
    count = len(screen[1])
    button = ctypes.c_int32()
    if lib.dvdnav_get_current_highlight(nav, ctypes.byref(button)) != DVDNAV_STATUS_OK:
        button.value = 0
    # libdvdnav ignores the disc's forced-select button; a player highlights it.
    forced = ctypes.c_uint8.from_address(pci + PCI_FORCED_SELECT_OFFSET).value
    choice = forced if 0 < forced <= count else button.value
    if not 0 < choice <= count:
        choice = 1

    if presses[(screen, choice)] >= 2:
        # The highlighted button keeps bringing us back here (e.g. a
        # "Languages" page whose default is "Back"); try the next one.
        untried = [b for b in range(1, count + 1) if presses[(screen, b)] < 2]
        if not untried:
            return False
        choice = untried[0]
    if choice != button.value:
        lib.dvdnav_button_select(nav, pci, choice)

    if lib.dvdnav_button_activate(nav, pci) != DVDNAV_STATUS_OK:
        return False
    presses[(screen, choice)] += 1
    where = "disc menu" if screen[0] else "title menu"
    log(f"    pressed button {choice} of {count} on a {where} ({why})")
    return True
