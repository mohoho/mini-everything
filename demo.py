#!/usr/bin/env python3
"""Synthetic sample index for demoing the UI without touching a real volume.

Deliberately uses chr(92) so no backslash literal appears anywhere in this file.
"""
import array
import os
import queue
import random
import threading
import time

BS = chr(92)


def _entries():
    rnd = random.Random(20261014)
    roots = ["C:" + BS + "dev", "C:" + BS + "projects",
             "D:" + BS + "media", "D:" + BS + "archive"]
    folders = ["src", "lib", "tests", "docs", "build", "assets", "node_modules",
               "__pycache__", "logs", "config", "dist", "vendor"]
    subs = ["core", "modules", "utils"]
    names = ["main", "app", "index", "server", "client", "handler", "model", "parser",
             "router", "schema", "worker", "cache", "report", "notes", "logo", "banner",
             "dataset", "config", "release", "changelog"]
    exts = [".py", ".js", ".ts", ".html", ".css", ".json", ".yml", ".md", ".png",
            ".jpg", ".pdf", ".csv", ".sql", ".txt", ".log", ".dll", ".exe"]
    sizes = [512, 1024, 4096, 18432, 65536, 262144, 1048576, 5242880, 15728640, 104857600]
    now = time.time()
    rows = []
    for root in roots:
        for folder in folders:
            base = BS.join([root, folder])
            rows.append((base, 0, now - rnd.randint(0, 60 * 60 * 24 * 400), True))
            for sub in subs:
                if rnd.random() < 0.5:
                    continue
                deeper = BS.join([base, sub])
                rows.append((deeper, 0, now - rnd.randint(0, 60 * 60 * 24 * 400), True))
                for name in names:
                    if rnd.random() < 0.6:
                        continue
                    rows.append((deeper + BS + name + rnd.choice(exts), rnd.choice(sizes),
                                 now - rnd.randint(60, 60 * 60 * 24 * 400), False))
    return rows


def build_volume_rows(letter):
    """Synthetic rows for one volume letter, sorted case-insensitively."""
    out = []
    for path, size, mtime, is_dir in _entries():
        if path[0].upper() != letter.upper():
            continue
        out.append((path, size, mtime, is_dir))
    out.sort(key=lambda r: r[0].lower())
    return out


def install(svc, emit_events=True):
    """Replace every volume's index with the synthetic sample."""
    for letter, vol in svc.volumes.items():
        rows = build_volume_rows(letter)
        with vol.lock:
            vol.paths = [r[0] for r in rows]
            vol.lower = [r[0].lower() for r in rows]
            vol.frns = array.array("Q", (0 for _ in rows))
            vol.isdirs = bytearray(1 if r[3] else 0 for r in rows)
            vol.dirs = {}
            vol.added = {}
            vol.removed = set()
            vol.removed_prefixes = []
            vol.stat_cache = {r[0].lower(): (0 if r[3] else r[1], r[2], r[3]) for r in rows}
            vol.method = "demo sample (synthetic, no volume access)"
            vol.seconds = 0.0
            vol.built_at = time.time()
            vol.journal_id = None
    if emit_events:
        threading.Thread(target=_spinner, args=(svc,), daemon=True).start()


def _spinner(svc):
    tags = [("file-create", 0x100),
            ("data-extend,close", 0x80000002),
            ("rename-new,close", 0x80002000),
            ("file-delete", 0x200)]
    pool = []
    for vol in svc.volumes.values():
        pool.extend(vol.paths[:400])
    if not pool:
        return
    i = 0
    while True:
        time.sleep(2.2)
        if not svc.listening:
            continue
        path = pool[i % len(pool)]
        i += 1
        label, reason = tags[i % len(tags)]
        ev = {"t": time.time(), "tags": label, "name": os.path.basename(path),
              "path": path, "reason": reason,
              "vol": path[0].upper()}
        for vol in svc.volumes.values():
            for q in list(vol.subscribers):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass
