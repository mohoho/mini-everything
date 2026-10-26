#!/usr/bin/env python3
"""Local web UI for mini-everything.

  * instant search across one or more NTFS volumes (USN/MFT index)
  * size + last-write columns read straight from the MFT
    ($DATA for size, $STANDARD_INFORMATION for the write time)
  * live USN journal feed whose deltas are merged into the searchable index
  * dark / light theme and a folder-outline result view
"""
import argparse
import array
import heapq
import json
import os
import queue
import re
import stat
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mini_everything as me

HERE = os.path.dirname(os.path.abspath(__file__))

R_FILE_CREATE = 0x100
R_FILE_DELETE = 0x200
R_RENAME_OLD = 0x1000
R_RENAME_NEW = 0x2000
POOL_CAP = 3000            # matches we will stat in order to sort by size/date


class Volume:
    """One NTFS volume: base index, live deltas, MFT attribute lookups."""

    def __init__(self, letter):
        self.letter = letter
        self.lock = threading.RLock()
        self.paths = []
        self.lower = []
        self.frns = array.array("Q")
        self.isdirs = bytearray()
        self.dirs = {}                  # frn -> resolved directory path
        self.added = {}                  # lower-path -> (path, frn, is_dir); dedupes USN bursts
        self.removed = set()
        self.removed_prefixes = []
        self.handle = None
        self.journal_id = None
        self.next_usn = None
        self.method = None
        self.seconds = None
        self.built_at = None
        self.building = False
        self.error = None
        self.deltas = 0
        self.stat_cache = {}
        self._stat_generation = 0
        self.subscribers = []
        self.stop = threading.Event()
        self._watch_gen = 0

    # ------------------------------------------------------------------ build
    def build(self):
        with self.lock:
            if self.building:
                return
            self.building = True
            self.error = None
        try:
            self._build()
        except Exception as exc:
            with self.lock:
                self.error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            with self.lock:
                self.building = False

    def _build(self):
        t0 = time.time()
        letter = self.letter
        was_paused = self.stop.is_set()
        # Retire the old reader before replacing its index or journal cursor.
        with self.lock:
            self._watch_gen += 1
        journal_id = next_usn = None
        fallback_reason = None
        try:
            if self.handle is None:
                self.handle = me.open_volume(letter)
            # Replay from BEFORE enumeration, including changes made during build.
            raw = me.device_io(self.handle, me.FSCTL_QUERY_USN_JOURNAL, None, 0x38)
            journal_id = struct.unpack_from("<Q", raw, 0)[0]
            next_usn = struct.unpack_from("<Q", raw, 16)[0]
            table = me.enum_volume(letter, progress=False)
        except OSError as exc:
            fallback_reason = str(exc)
            table = None
            journal_id = next_usn = None
        rows, dirs = [], {}
        for frn, (name, parent, attrs) in (table or {}).items():
            if frn == me.ROOT_FRN or name.startswith("$"):
                continue
            parts, seen, cur = [], set(), frn
            while cur and cur not in seen and cur != me.ROOT_FRN:
                seen.add(cur)
                entry = table.get(cur)
                if entry is None:
                    parts = None
                    break
                parts.append(entry[0])
                cur = entry[1]
            if not parts:
                continue
            parts.reverse()
            full = "%s:\\%s" % (letter, "\\".join(parts))
            is_dir = bool(attrs & me.FILE_ATTRIBUTE_DIRECTORY)
            if is_dir:
                dirs[frn] = full
            rows.append((full, frn, is_dir))
        if table is None:
            root = "%s:\\" % letter
            if not os.path.isdir(root):
                raise OSError("Volume %s is unavailable: %s" % (root, fallback_reason))
            rows = [(path, 0, is_dir)
                    for path, is_dir in me.walk_volume_entries(letter)]
        rows.sort(key=lambda r: r[0].lower())
        with self.lock:
            self.paths = [r[0] for r in rows]
            self.lower = [r[0].lower() for r in rows]
            self.frns = array.array("Q", (r[1] for r in rows))
            self.isdirs = bytearray(1 if r[2] else 0 for r in rows)
            self.dirs = dirs
            self.added = {}
            self.removed = set()
            self.removed_prefixes = []
            self.deltas = 0
            self.stat_cache = {}
            self._stat_generation += 1
            self.method = ("recursive directory walk (no live updates)" if table is None
                           else "USN/MFT (FSCTL_ENUM_USN_DATA)")
            self.seconds = time.time() - t0
            self.built_at = time.time()
            self.journal_id = journal_id
            self.next_usn = next_usn
        if self.journal_id is not None and not was_paused:
            self._start_watch()

    # -------------------------------------------------------------- live feed
    def _start_watch(self):
        """Start exactly one journal reader; a later generation retires the old one."""
        self.stop.clear()
        self._watch_gen += 1
        threading.Thread(target=self._watch_loop, args=(self._watch_gen,),
                         daemon=True).start()

    def _watch_loop(self, gen):
        rd = me.READ_USN_JOURNAL_DATA_V0(self.next_usn, 0xFFFFFFFF, 0, 0, 0, self.journal_id)
        while not self.stop.is_set() and gen == self._watch_gen:
            try:
                data = me.device_io(self.handle, me.FSCTL_READ_USN_JOURNAL, rd,
                                    1 << 16, allow_eof=True)
            except OSError:
                time.sleep(1.0)
                continue
            if len(data) < 8:
                time.sleep(0.35)
                continue
            nxt = struct.unpack_from("<Q", data, 0)[0]
            with self.lock:
                if self.stop.is_set() or gen != self._watch_gen:
                    return
                for frn, pfrn, attrs, reason, name in me.iter_usn_records_full(data):
                    self.apply_event(frn, pfrn, attrs, reason, name)
                rd.StartUsn = nxt
                self.next_usn = nxt
            if len(data) == 8:
                time.sleep(0.35)

    def apply_event(self, frn, pfrn, attrs, reason, name):
        parent = "%s:\\" % self.letter if pfrn == me.ROOT_FRN else self.dirs.get(pfrn)
        if parent is None:
            return
        full = parent.rstrip("\\") + "\\" + name
        is_dir = bool(attrs & me.FILE_ATTRIBUTE_DIRECTORY)
        with self.lock:
            self.stat_cache.pop(full.lower(), None)
            self._stat_generation += 1
            if not (reason & (R_FILE_DELETE | R_RENAME_OLD | R_RENAME_NEW | R_FILE_CREATE)):
                return                                # metadata invalidated; paths unchanged
            self.deltas += 1
            key = full.lower()
            if reason & (R_FILE_DELETE | R_RENAME_OLD):
                self.removed.add(full)
                self.added.pop(key, None)
                if is_dir:
                    self.removed_prefixes.append(full.rstrip("\\") + "\\")
                    self.dirs.pop(frn, None)
            else:
                if is_dir:
                    self.dirs[frn] = full
                self.removed.discard(full)
                self.added[key] = (full, frn, is_dir)
                if len(self.added) > 400000:
                    for k in list(self.added)[:100000]:
                        del self.added[k]
        event = {"t": time.time(), "tags": me._reason_text(reason), "name": name,
                 "path": full, "reason": reason, "vol": self.letter}
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # ---------------------------------------------------------------- queries
    @staticmethod
    def _hidden(path, removed, prefix_tuple):
        """True when the path was removed (or sits inside a removed subtree)."""
        if removed and path in removed:
            return True
        return bool(prefix_tuple) and path.startswith(prefix_tuple)

    def matches(self, needle, regex, case):
        with self.lock:
            paths, lower, frns, isdirs = self.paths, self.lower, self.frns, self.isdirs
            added = list(self.added.items())
            added_keys = set(self.added)
            removed = self.removed.copy()
            prefixes = tuple(self.removed_prefixes)
        if regex:
            rx = re.compile(needle, 0 if case else re.IGNORECASE)
            for i, p in enumerate(paths):
                if lower[i] in added_keys:
                    continue
                if rx.search(p) and not self._hidden(p, removed, prefixes):
                    yield p, frns[i], bool(isdirs[i])
            for key, (p, frn, isd) in added:
                if rx.search(p) and not self._hidden(p, removed, prefixes):
                    yield p, frn, isd
            return
        if case:
            for i, p in enumerate(paths):
                if lower[i] in added_keys:
                    continue
                if needle in p and not self._hidden(p, removed, prefixes):
                    yield p, frns[i], bool(isdirs[i])
            for key, (p, frn, isd) in added:
                if needle in p and not self._hidden(p, removed, prefixes):
                    yield p, frn, isd
            return
        # case-insensitive substring: the hot path, kept free of per-item
        # Python calls so it runs at C speed over the whole index
        for i, low in enumerate(lower):
            if needle in low:
                if added_keys and low in added_keys:
                    continue
                p = paths[i]
                if not self._hidden(p, removed, prefixes):
                    yield p, frns[i], bool(isdirs[i])
        for key, (p, frn, isd) in added:
            if needle in key and not self._hidden(p, removed, prefixes):
                yield p, frn, isd

    def stat(self, path, frn):
        key = path.lower()
        with self.lock:
            hit = self.stat_cache.get(key)
            generation = self._stat_generation
            if hit is not None:
                return hit
        res = None
        if self.handle is not None and frn:
            try:
                res = me.mft_stat(self.handle, frn, path.rsplit("\\", 1)[-1])
            except Exception:
                res = None
        if res is None:
            try:
                st = os.stat(path)          # single syscall: AV hooks make this costly
                isd = stat.S_ISDIR(st.st_mode)
                res = (0 if isd else st.st_size, st.st_mtime, isd)
            except OSError:
                res = (None, None, False)
        with self.lock:
            # A change while stat was in flight must not restore stale metadata.
            if generation == self._stat_generation:
                if len(self.stat_cache) > 300000:
                    self.stat_cache.clear()
                self.stat_cache[key] = res
        return res

    def pause(self):
        self.stop.set()

    def resume(self):
        if self.journal_id is not None and self.stop.is_set():
            self._start_watch()

    @property
    def paused(self):
        return self.journal_id is not None and self.stop.is_set()

    def stats(self):
        with self.lock:
            return {"letter": self.letter, "paused": self.paused,
                    "count": len(self.paths) + len(self.added),
                    "method": self.method, "seconds": self.seconds,
                    "built_at": self.built_at, "building": self.building,
                    "deltas": self.deltas, "live": self.journal_id is not None,
                    "removed": len(self.removed) + len(self.removed_prefixes),
                    "error": self.error}


class Service:
    def __init__(self, letters):
        self.volumes = {l: Volume(l) for l in letters}

    def build_all(self):
        threads = [threading.Thread(target=v.build, daemon=True) for v in self.volumes.values()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def selected(self, which):
        if not which or which.upper() == "ALL":
            return list(self.volumes.values())
        return [self.volumes[c] for c in which.upper() if c in self.volumes]

    def set_listen(self, enabled):
        for v in self.volumes.values():
            v.resume() if enabled else v.pause()
        return {l: v.stats()["paused"] for l, v in self.volumes.items()}

    def stats(self):
        vols = [v.stats() for v in self.volumes.values()]
        return {"volumes": vols, "count": sum(v["count"] for v in vols),
                "building": any(v["building"] for v in vols),
                "deltas": sum(v["deltas"] for v in vols)}

    def search(self, q, limit, regex, case, dirs_only, sort, order, which):
        t0 = time.time()
        needle = q if (case or regex) else q.lower()
        total = 0

        def candidates():
            nonlocal total
            for vol in self.selected(which):
                for path, frn, is_dir in vol.matches(needle, regex, case):
                    if dirs_only and not is_dir:
                        continue
                    total += 1
                    yield path, vol, frn, is_dir

        truncated = False
        if sort in ("size", "date"):
            pool = []
            for row in candidates():
                if len(pool) < POOL_CAP:
                    pool.append(row)
            truncated = total > len(pool)
            rows = []
            for path, vol, frn, is_dir in pool:
                size, mtime, isd = vol.stat(path, frn)
                rows.append({"p": path, "d": 1 if (is_dir or isd) else 0, "s": size, "t": mtime})
            if sort == "size":
                rows.sort(key=lambda r: (r["s"] if r["s"] is not None else -1), reverse=(order == "desc"))
            else:
                rows.sort(key=lambda r: (r["t"] if r["t"] is not None else 0), reverse=(order == "desc"))
            rows = rows[:limit]
        else:
            # Pick the global top N before truncation, including every volume
            # and live overlay. Memory stays bounded by the requested limit.
            pick = heapq.nlargest if order == "desc" else heapq.nsmallest
            pool = pick(limit, candidates(), key=lambda row: row[0].lower())
            rows = []
            for path, vol, frn, is_dir in pool:
                size, mtime, isd = vol.stat(path, frn)
                rows.append({"p": path, "d": 1 if (is_dir or isd) else 0, "s": size, "t": mtime})
        return {"hits": rows, "total": total, "ms": round((time.time() - t0) * 1000, 2),
                "sort": sort, "order": order, "pool": len(pool), "sort_truncated": truncated}


def make_handler(svc):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "mini-everything"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                return self._file("index.html", "text/html; charset=utf-8")
            if u.path == "/api/stats":
                return self._json(svc.stats())
            if u.path == "/api/search":
                q = (qs.get("q") or [""])[0]
                try:
                    out = svc.search(
                        q,
                        limit=max(1, min(5000, int((qs.get("limit") or ["300"])[0]))),
                        regex=(qs.get("regex") or ["0"])[0] == "1",
                        case=(qs.get("case") or ["0"])[0] == "1",
                        dirs_only=(qs.get("dirs") or ["0"])[0] == "1",
                        sort=(qs.get("sort") or ["name"])[0],
                        order=(qs.get("order") or ["asc"])[0],
                        which=(qs.get("vol") or ["ALL"])[0])
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                except re.error as exc:
                    return self._json({"error": "bad regex: %s" % exc}, 400)
                return self._json(out)
            if u.path == "/api/watch":
                letter = (qs.get("drive") or ["C"])[0].upper()
                grep = (qs.get("grep") or [""])[0]
                vol = svc.volumes.get(letter)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                if vol is None:
                    return
                return self._sse(vol, grep)
            return self._json({"error": "not found"}, 404)

        def _sse(self, vol, grep):
            q = queue.Queue(maxsize=2000)
            vol.subscribers.append(q)
            try:
                self.wfile.write(b"event: hello\ndata: {\"ok\": true}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=0.5)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if grep and grep.lower() not in ev["name"].lower():
                        continue
                    self.wfile.write(("data: %s\n\n" % json.dumps(ev)).encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    vol.subscribers.remove(q)
                except ValueError:
                    pass

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/listen":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                return self._json(svc.set_listen(bool(body.get("enabled", True))))
            if u.path == "/api/rebuild":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                letter = (body.get("drive") or "C").upper()
                vol = svc.volumes.get(letter)
                if vol is None:
                    return self._json({"error": "unknown volume"}, 400)
                threading.Thread(target=vol.build, daemon=True).start()
                return self._json({"started": letter})
            return self._json({"error": "not found"}, 404)

        def _file(self, name, ctype):
            path = os.path.join(HERE, name)
            if not os.path.exists(path):
                return self._json({"error": "missing %s" % name}, 404)
            with open(path, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--volumes", default="CD")
    ap.add_argument("--demo", action="store_true",
                    help="serve a synthetic sample index instead of reading a real volume")
    args = ap.parse_args()
    svc = Service([c for c in args.volumes.upper() if c.isalpha()])
    if args.demo:
        import demo
        demo.install(svc)
        print("demo mode: synthetic sample index", flush=True)
    else:
        print("building %s ..." % ",".join(svc.volumes), flush=True)
        svc.build_all()
    print("ready: %s" % json.dumps(svc.stats()), flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(svc))
    srv.daemon_threads = True
    print("serving on http://127.0.0.1:%d/" % args.port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
