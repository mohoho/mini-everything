#!/usr/bin/env python3
"""
mini_everything.py - a minimal "Everything"-style filename indexer for NTFS.

It reproduces the core mechanism confirmed by reverse engineering Everything.exe:
  1. open the raw volume handle (\\.\X:) and ask NTFS for the whole file table
     through the USN/MFT control code FSCTL_ENUM_USN_DATA (0x000900B3) -- one
     pass, no directory walking, typically a few seconds for a whole volume;
  2. rebuild full paths from the (FileReferenceNumber, ParentFileReferenceNumber)
     pairs by resolving the parent chain up to the volume root (FRN 5);
  3. serve instant substring / regex searches from the resulting in-memory index;
  4. optionally poll the USN change journal (FSCTL_READ_USN_JOURNAL) for live
     create/rename/delete deltas.

Requires: Windows, Python 3, and (for the fast USN path) an elevated session.
Falls back to a recursive directory walk when the volume handle is unavailable.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import re
import struct
import sys
import time

# ---------------------------------------------------------------- Win32 glue
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

FSCTL_ENUM_USN_DATA = 0x000900B3        # walk the MFT through USN
FSCTL_QUERY_USN_JOURNAL = 0x000900F4    # USN_JOURNAL_DATA_V0 (56 bytes)
FSCTL_READ_USN_JOURNAL = 0x000900BB     # incremental deltas

ROOT_FRN = 5  # the volume's root directory record number

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                            wt.DWORD, wt.DWORD, wt.HANDLE]
k32.DeviceIoControl.restype = wt.BOOL
k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                ctypes.c_void_p]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.GetVolumeInformationW.restype = wt.BOOL


class MFT_ENUM_DATA_V0(ctypes.Structure):
    _fields_ = [("StartFileReferenceNumber", ctypes.c_ulonglong),
                ("LowUsn", ctypes.c_ulonglong),
                ("HighUsn", ctypes.c_ulonglong)]


class READ_USN_JOURNAL_DATA_V0(ctypes.Structure):
    _fields_ = [("StartUsn", ctypes.c_ulonglong),
                ("ReasonMask", wt.DWORD),
                ("ReturnOnlyOnClose", wt.DWORD),
                ("Timeout", ctypes.c_ulonglong),
                ("BytesToWaitFor", ctypes.c_ulonglong),
                ("UsnJournalID", ctypes.c_ulonglong)]


def open_volume(letter):
    path = "\\\\.\\%s:" % letter
    h = k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def device_io(h, code, in_buf, out_size, allow_eof=False):
    out = ctypes.create_string_buffer(out_size)
    returned = wt.DWORD(0)
    ok = k32.DeviceIoControl(h, code, ctypes.byref(in_buf) if in_buf else None,
                             ctypes.sizeof(in_buf) if in_buf else 0,
                             out, out_size, ctypes.byref(returned), None)
    if not ok:
        err = ctypes.get_last_error()
        if allow_eof and err == 38:      # ERROR_HANDLE_EOF: nothing new to read
            return b""
        raise ctypes.WinError(err)
    return out.raw[:returned.value]


# ------------------------------------------------------------ USN parsing
def _iter_usn_records(data, start=8):
    off = start
    n = len(data)
    while off + 60 <= n:
        reclen, major, _minor = struct.unpack_from("<IHH", data, off)
        if reclen < 60 or off + reclen > n:
            break
        if major == 2:
            frn, pfrn = struct.unpack_from("<QQ", data, off + 8)
            attrs = struct.unpack_from("<I", data, off + 52)[0]
            name_len = struct.unpack_from("<H", data, off + 56)[0]
            name_off = struct.unpack_from("<H", data, off + 58)[0]
        elif major == 3:                       # 128-bit file references
            frn = struct.unpack_from("<Q", data, off + 8)[0]
            pfrn = struct.unpack_from("<Q", data, off + 24)[0]
            attrs = struct.unpack_from("<I", data, off + 60)[0]
            name_len = struct.unpack_from("<H", data, off + 64)[0]
            name_off = struct.unpack_from("<H", data, off + 66)[0]
        else:
            off += reclen
            continue
        raw = data[off + name_off: off + name_off + name_len]
        name = raw.decode("utf-16-le", "replace")
        yield frn & 0xFFFFFFFFFFFF, pfrn & 0xFFFFFFFFFFFF, attrs, name
        off += reclen


_DOS_ALIAS = re.compile(r"^[^.]{1,8}(\.[^.]{0,3})?$")


def _looks_dos(name):
    return "~" in name and bool(_DOS_ALIAS.match(name))


def enum_volume(letter, progress=True):
    """Return {frn: (name, parent_frn, attrs)} for every live record."""
    h = open_volume(letter)
    try:
        _jid, _first, next_usn, _lowest, _maxusn, _maxsz, _delta = struct.unpack_from("<7Q", device_io(h, FSCTL_QUERY_USN_JOURNAL, None, 0x38), 0)
        table = {}
        med = MFT_ENUM_DATA_V0(0, 0, next_usn)
        total = 0
        while True:
            data = device_io(h, FSCTL_ENUM_USN_DATA, med, 1 << 20, allow_eof=True)
            if len(data) < 8:
                break
            next_frn = struct.unpack_from("<Q", data, 0)[0]
            got = 0
            for frn, pfrn, attrs, name in _iter_usn_records(data):
                got += 1
                total += 1
                prev = table.get(frn)
                if prev is None or (_looks_dos(prev[0]) and not _looks_dos(name)):
                    table[frn] = (name, pfrn, attrs)
            if progress and total % 200000 < got:
                sys.stderr.write("\r  enumerated %d records..." % total)
                sys.stderr.flush()
            if got == 0 or next_frn == 0 or next_frn == med.StartFileReferenceNumber:
                break
            med.StartFileReferenceNumber = next_frn
        if progress:
            sys.stderr.write("\r  enumerated %d records.        \n" % total)
        return table
    finally:
        k32.CloseHandle(h)


def resolve_paths(table, letter, include_dirs=True, hide_dollar=True):
    """Rebuild full paths by walking the parent chain. Memoized per FRN."""
    cache = {}

    def resolve(frn):
        parts = []
        seen = set()
        cur = frn
        while cur and cur not in seen and cur != ROOT_FRN:
            seen.add(cur)
            entry = table.get(cur)
            if entry is None:
                return None
            parts.append(entry[0])
            cur = entry[1]
        parts.reverse()
        return "%s:\\%s" % (letter, "\\".join(parts))

    out = []
    for frn, (name, _parent, attrs) in table.items():
        if frn == ROOT_FRN:
            continue
        if hide_dollar and name.startswith("$"):
            continue
        if not include_dirs and (attrs & FILE_ATTRIBUTE_DIRECTORY):
            continue
        p = cache.get(frn) or resolve(frn)
        if p:
            cache[frn] = p
            out.append(p)
    out.sort(key=str.lower)
    return out


# --------------------------------------------------- recursive fallback
def walk_volume_entries(letter):
    """Yield paths with directory flags without guessing from file extensions."""
    root = "%s:\\" % letter
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None,
                                               followlinks=False):
        if dirpath == root:
            yield dirpath, True
        for name in dirnames:
            yield os.path.join(dirpath, name), True
        for f in filenames:
            yield os.path.join(dirpath, f), False


def walk_volume(letter):
    out = [path for path, _is_dir in walk_volume_entries(letter)]
    out.sort(key=str.lower)
    return out


# ------------------------------------------------------------- commands
INDEX_MAGIC = "#mini-everything index v1"


def cmd_index(args):
    t0 = time.time()
    letter = args.drive.rstrip(":").rstrip("\\")
    table = None
    try:
        table = enum_volume(letter)
        paths = resolve_paths(table, letter)
        how = "USN/MFT (FSCTL_ENUM_USN_DATA)"
    except OSError as exc:
        sys.stderr.write("USN path unavailable (%s); falling back to directory walk\n" % exc)
        paths = walk_volume(letter)
        how = "recursive directory walk (slow path)"
    with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("%s\n" % INDEX_MAGIC)
        for p in paths:
            fh.write(p + "\n")
    sys.stderr.write("indexed %d entries from %s:\\ in %.2fs -> %s\n"
                     % (len(paths), letter, time.time() - t0, args.output))
    sys.stderr.write("method: %s\n" % how)


def load_index(path):
    paths = []
    with open(path, "r", encoding="utf-8") as fh:
        first = fh.readline()
        if not first.startswith("#mini-everything"):
            paths.append(first.rstrip("\n"))
        for line in fh:
            paths.append(line.rstrip("\n"))
    return paths


def cmd_find(args):
    paths = load_index(args.index)
    if args.regex:
        rx = re.compile(args.pattern, 0 if args.case else re.IGNORECASE)
        hits = [p for p in paths if rx.search(p)]
    elif args.case:
        hits = [p for p in paths if args.pattern in p]
    else:
        needle = args.pattern.lower()
        hits = [p for p in paths if needle in p.lower()]
    if args.dirs_only:
        hits = [p for p in hits if not os.path.splitext(p)[1]]
    if args.limit and len(hits) > args.limit:
        shown = hits[:args.limit]
    else:
        shown = hits
    for p in shown:
        print(p)
    sys.stderr.write("%d match(es) of %d indexed\n" % (len(hits), len(paths)))


REASON_BITS = [
    (0x00000001, "data-overwrite"), (0x00000002, "data-extend"),
    (0x00000004, "data-truncate"), (0x00000010, "named-data-overwrite"),
    (0x00000020, "named-data-extend"), (0x00000040, "named-data-truncate"),
    (0x00000100, "file-create"), (0x00000200, "file-delete"),
    (0x00000400, "ea-change"), (0x00000800, "security-change"),
    (0x00001000, "rename-old"), (0x00002000, "rename-new"),
    (0x00004000, "indexable-change"), (0x00008000, "basic-info-change"),
    (0x00010000, "hardlink-change"), (0x00020000, "compression-change"),
    (0x00040000, "encryption-change"), (0x00080000, "objectid-change"),
    (0x00100000, "reparse-change"), (0x00200000, "stream-change"),
    (0x00400000, "transacted-change"), (0x00800000, "integrity-change"),
    (0x80000000, "close"),
]
NAME_FILE_MASK = 0x00000100 | 0x00000200 | 0x00001000 | 0x00002000


def _reason_text(reason):
    tags = [t for b, t in REASON_BITS if reason & b]
    return ",".join(tags) if tags else "0x%x" % reason


def cmd_watch(args):
    letter = args.drive.rstrip(":").rstrip("\\")
    h = open_volume(letter)
    try:
        _jid, _first, next_usn, _low, _max, _sz, _delta = struct.unpack_from(
            "<7Q", device_io(h, FSCTL_QUERY_USN_JOURNAL, None, 0x38), 0)
        print("watching %s:\\ from usn=%d for %ss  (filenames only; --all for everything)"
              % (letter, next_usn, args.seconds))
        mask = 0xFFFFFFFF if args.all else NAME_FILE_MASK
        rd = READ_USN_JOURNAL_DATA_V0(next_usn, mask, 0, 0, 0, _jid)
        end = time.time() + args.seconds
        while time.time() < end:
            data = device_io(h, FSCTL_READ_USN_JOURNAL, rd, 1 << 16, allow_eof=True)
            if len(data) < 8:
                time.sleep(0.3)
                continue
            nxt = struct.unpack_from("<Q", data, 0)[0]
            off = 8
            while off + 60 <= len(data):
                reclen, major, _ = struct.unpack_from("<IHH", data, off)
                if reclen < 60 or off + reclen > len(data):
                    break
                reason = struct.unpack_from("<I", data, off + 40)[0]
                name_len = struct.unpack_from("<H", data, off + 56)[0]
                name_off = struct.unpack_from("<H", data, off + 58)[0]
                nm = data[off + name_off: off + name_off + name_len].decode("utf-16-le", "replace")
                if args.grep and args.grep.lower() not in nm.lower():
                    off += reclen
                    continue
                print("  %-28s %s" % (_reason_text(reason), nm))
                off += reclen
            rd.StartUsn = nxt
    finally:
        k32.CloseHandle(h)


def build_parser():
    p = argparse.ArgumentParser(prog="mini_everything",
                                description="Minimal Everything-style NTFS filename indexer")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="build the filename index (USN/MFT fast path)")
    pi.add_argument("drive", nargs="?", default="C", help="drive letter (default C)")
    pi.add_argument("-o", "--output", default="mini_everything.index", help="index file")
    pi.set_defaults(func=cmd_index)

    pf = sub.add_parser("find", help="search the index")
    pf.add_argument("pattern")
    pf.add_argument("-i", "--index", default="mini_everything.index")
    pf.add_argument("-r", "--regex", action="store_true")
    pf.add_argument("-c", "--case", action="store_true")
    pf.add_argument("-d", "--dirs-only", action="store_true")
    pf.add_argument("-n", "--limit", type=int, default=0)
    pf.set_defaults(func=cmd_find)

    pw = sub.add_parser("watch", help="poll the USN journal for live changes")
    pw.add_argument("drive", nargs="?", default="C")
    pw.add_argument("-s", "--seconds", type=int, default=30)
    pw.add_argument("--all", action="store_true", help="show every reason, including data writes")
    pw.add_argument("-g", "--grep", default="", help="only show names containing this text")
    pw.set_defaults(func=cmd_watch)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    args.func(args)


# ------------------------------------------------- MFT record attributes
FSCTL_GET_NTFS_FILE_RECORD = 0x00090068
FILETIME_EPOCH_DELTA = 11644473600
_NS_PRIORITY = {3: 0, 1: 1, 0: 2, 2: 9}   # Win32&DOS > Win32 > POSIX > DOS(8.3)


def _filetime_to_unix(ft):
    return None if not ft else ft / 10000000.0 - FILETIME_EPOCH_DELTA


def _apply_usa_fixup(rec):
    """Undo the multi-sector transfer protection so attribute bytes are valid."""
    if len(rec) < 8:
        return rec
    usa_off, usa_cnt = struct.unpack_from("<HH", rec, 4)
    if usa_cnt < 2 or usa_off + usa_cnt * 2 > len(rec):
        return rec
    usn = rec[usa_off:usa_off + 2]
    buf = bytearray(rec)
    for i in range(1, usa_cnt):
        pos = i * 512 - 2
        if pos + 2 <= len(buf):
            buf[pos:pos + 2] = usn
    return bytes(buf)


def parse_mft_record(rec, expected_name=None):
    """Pull (size, mtime, is_dir) out of one raw MFT record.

    mtime comes from $STANDARD_INFORMATION (0x10) last-write time: NTFS only
    refreshes $FILE_NAME timestamps on rename/create, so they are often stale.
    Size comes from the $FILE_NAME (0x30) entry with a real namespace -- the DOS
    8.3 alias carries a zero size.
    """
    if len(rec) < 48 or rec[:4] != b"FILE":
        return None
    rec = _apply_usa_fixup(rec)
    off = struct.unpack_from("<H", rec, 0x14)[0]
    n = len(rec)
    si_write = None
    names = []
    data_size = None
    while off + 16 <= n:
        atype, alen = struct.unpack_from("<II", rec, off)
        if atype == 0xFFFFFFFF or alen < 16:
            break
        nonres = rec[off + 8]
        name_len = rec[off + 9]
        if atype == 0x80 and name_len == 0 and data_size is None:
            # unnamed $DATA carries the real file size
            if nonres:
                if alen >= 56:
                    # compressed/sparse attributes stash the compression unit in
                    # the high bits of RealSize; the size is the low 48 bits
                    data_size = struct.unpack_from("<Q", rec, off + 48)[0] & 0xFFFFFFFFFFFF
            else:
                data_size = struct.unpack_from("<I", rec, off + 16)[0]
        elif nonres == 0:
            vlen = struct.unpack_from("<I", rec, off + 16)[0]
            voff = struct.unpack_from("<H", rec, off + 20)[0]
            if voff + vlen <= alen:
                val = rec[off + voff: off + voff + vlen]
                if atype == 0x10 and len(val) >= 36:        # $STANDARD_INFORMATION
                    if si_write is None:
                        si_write = struct.unpack_from("<Q", val, 8)[0]
                elif atype == 0x30 and len(val) >= 66:      # $FILE_NAME
                    nlen = val[64]
                    ns = val[65]
                    if 66 + nlen <= len(val):
                        nm = val[66:66 + nlen].decode("utf-16-le", "replace")
                        names.append((ns, nm,
                                      struct.unpack_from("<Q", val, 48)[0],
                                      struct.unpack_from("<I", val, 56)[0],
                                      struct.unpack_from("<Q", val, 16)[0]))
        off += alen
    if not names:
        return None
    chosen = None
    if expected_name:
        for c in names:
            if c[1].lower() == expected_name.lower():
                chosen = c
                break
    if chosen is None:
        chosen = sorted(names, key=lambda c: _NS_PRIORITY.get(c[0], 5))[0]
    ns, nm, fn_size, flags, fn_write = chosen
    mtime = _filetime_to_unix(si_write or fn_write)
    is_dir = bool(flags & FILE_ATTRIBUTE_DIRECTORY)
    if is_dir:
        return 0, mtime, True
    if data_size is None:
        #  carries a placeholder size and the real \ lives in an
        # extension record (reachable only through \). Report
        # unknown so the caller can fall back to the filesystem.
        return None
    return data_size, mtime, False


def mft_stat(h, frn, expected_name=None):
    """Resolve one file reference to (size, mtime, is_dir) through the MFT.

    Uses FSCTL_GET_NTFS_FILE_RECORD -- the same control code Everything.exe uses
    in FUN_004a50f0 -- so it works even for files this process cannot open.
    """
    inp = struct.pack("<Q", frn & 0xFFFFFFFFFFFF)
    out = ctypes.create_string_buffer(1 << 16)
    returned = wt.DWORD(0)
    ok = k32.DeviceIoControl(h, FSCTL_GET_NTFS_FILE_RECORD,
                             ctypes.c_char_p(inp), len(inp),
                             out, ctypes.sizeof(out), ctypes.byref(returned), None)
    if not ok or returned.value < 12:
        return None
    data = out.raw[:returned.value]
    rec_len = struct.unpack_from("<I", data, 8)[0]
    return parse_mft_record(data[12:12 + rec_len], expected_name)


def iter_usn_records_full(data, start=8):
    """Like _iter_usn_records but also yields the raw reason mask."""
    off = start
    n = len(data)
    while off + 60 <= n:
        reclen, major, _minor = struct.unpack_from("<IHH", data, off)
        if reclen < 60 or off + reclen > n:
            break
        if major == 2:
            frn, pfrn = struct.unpack_from("<QQ", data, off + 8)
            reason = struct.unpack_from("<I", data, off + 40)[0]
            attrs = struct.unpack_from("<I", data, off + 52)[0]
            name_len = struct.unpack_from("<H", data, off + 56)[0]
            name_off = struct.unpack_from("<H", data, off + 58)[0]
        elif major == 3:
            frn = struct.unpack_from("<Q", data, off + 8)[0]
            pfrn = struct.unpack_from("<Q", data, off + 24)[0]
            reason = struct.unpack_from("<I", data, off + 48)[0]
            attrs = struct.unpack_from("<I", data, off + 60)[0]
            name_len = struct.unpack_from("<H", data, off + 64)[0]
            name_off = struct.unpack_from("<H", data, off + 66)[0]
        else:
            off += reclen
            continue
        nm = data[off + name_off: off + name_off + name_len].decode("utf-16-le", "replace")
        yield frn & 0xFFFFFFFFFFFF, pfrn & 0xFFFFFFFFFFFF, attrs, reason, nm
        off += reclen
