"""Regression checks; no administrator privileges or real volume reads needed."""
import array
import json
import struct
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import demo
import mini_everything as me
import webapp


def seed(vol, rows):
    """rows: (path, frn, directory), deliberately not required to be sorted."""
    vol.paths = [r[0] for r in rows]
    vol.lower = [r[0].lower() for r in rows]
    vol.frns = array.array("Q", (r[1] for r in rows))
    vol.isdirs = bytearray(r[2] for r in rows)
    vol.stat_cache = {r[0].lower(): (10, 100, r[2]) for r in rows}


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.svc = webapp.Service(["D", "C"])
        seed(self.svc.volumes["D"], [(r"D:\b.txt", 20, False)])
        seed(self.svc.volumes["C"], [(r"C:\a.txt", 10, False),
                                     (r"C:\c.txt", 11, False)])

    def search(self, limit=2, order="asc", sort="name", which="ALL"):
        return self.svc.search("", limit, False, False, False, sort, order, which)

    def test_global_name_sort_and_limit(self):
        self.assertEqual([r["p"] for r in self.search()["hits"]],
                         [r"C:\a.txt", r"C:\c.txt"])
        self.assertEqual([r["p"] for r in self.search(order="desc")["hits"]],
                         [r"D:\b.txt", r"C:\c.txt"])

    def test_overlay_participates_in_sort(self):
        vol = self.svc.volumes["C"]
        vol.apply_event(30, 5, 0, webapp.R_FILE_CREATE, "0.txt")
        with patch.object(vol, "stat", return_value=(5, 100, False)):
            self.assertEqual(self.search(limit=1)["hits"][0]["p"], r"C:\0.txt")

    def test_name_limit_above_metadata_sampling_cap(self):
        vol = self.svc.volumes["C"]
        seed(vol, [(r"C:\%05d" % i, i + 10, False) for i in range(5100)])
        out = self.search(limit=5000, order="desc", which="C")
        self.assertEqual(len(out["hits"]), 5000)
        self.assertEqual(out["hits"][0]["p"], r"C:\05099")
        self.assertEqual(out["total"], 5100)
        self.assertFalse(out["sort_truncated"])
        sampled = self.search(limit=5000, sort="size", which="C")
        self.assertEqual(len(sampled["hits"]), webapp.POOL_CAP)
        self.assertTrue(sampled["sort_truncated"])

    def test_overlay_dedupes_all_search_modes(self):
        vol = self.svc.volumes["C"]
        # A same-path create after the MFT snapshot is common during journal replay.
        vol.apply_event(10, 5, 0, webapp.R_FILE_CREATE, "a.txt")
        for regex, case in [(False, False), (False, True), (True, False), (True, True)]:
            with self.subTest(regex=regex, case=case):
                hits = list(vol.matches("a", regex, case))
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0][0], r"C:\a.txt")

    def test_delete_and_directory_filters(self):
        vol = self.svc.volumes["C"]
        vol.apply_event(10, 5, 0, webapp.R_FILE_DELETE, "a.txt")
        self.assertEqual([r["p"] for r in self.search(which="C")["hits"]], [r"C:\c.txt"])
        vol.apply_event(40, 5, me.FILE_ATTRIBUTE_DIRECTORY, webapp.R_FILE_CREATE, "docs.v1")
        with patch.object(vol, "stat", return_value=(0, 100, True)):
            out = self.svc.search("", 10, False, False, True, "name", "asc", "C")
        self.assertEqual([r["p"] for r in out["hits"]], [r"C:\docs.v1"])


class MetadataTests(unittest.TestCase):
    def test_data_and_basic_info_events_invalidate_cache(self):
        for reason in (1, 2, 4, 0x8000):
            vol = webapp.Volume("C")
            seed(vol, [(r"C:\Report.TXT", 10, False)])
            vol.apply_event(10, 5, 0, reason, "report.txt")
            self.assertNotIn(r"c:\report.txt", vol.stat_cache)
            self.assertEqual(vol.deltas, 0)
            with patch.object(webapp.os, "stat") as fake_stat:
                fake_stat.return_value.st_mode = 0o100644
                fake_stat.return_value.st_size = 99
                fake_stat.return_value.st_mtime = 200
                self.assertEqual(vol.stat(r"C:\Report.TXT", 10), (99, 200, False))

    def test_inflight_stat_cannot_restore_invalidated_value(self):
        vol = webapp.Volume("C")
        vol.handle = 1

        def read_then_change(*args):
            vol.apply_event(10, 5, 0, 2, "a.txt")
            return 10, 100, False

        with patch.object(me, "mft_stat", side_effect=read_then_change):
            vol.stat(r"C:\a.txt", 10)
        self.assertNotIn(r"c:\a.txt", vol.stat_cache)


class BuildTests(unittest.TestCase):
    def test_permission_and_unsupported_volume_fall_back(self):
        for stage in ("open", "enum"):
            with self.subTest(stage=stage):
                vol = webapp.Volume("C")
                with patch.object(me, "open_volume", side_effect=PermissionError("denied") if stage == "open" else None,
                                  return_value=1), \
                     patch.object(me, "enum_volume", side_effect=OSError("unsupported")), \
                     patch.object(me, "device_io", return_value=struct.pack("<7Q", 1, 0, 100, 0, 0, 0, 0)), \
                     patch.object(webapp.os.path, "isdir", return_value=True), \
                     patch.object(me, "walk_volume_entries", return_value=iter([
                         ("C:\\", True), (r"C:\dir.v1", True), (r"C:\README", False)])):
                    vol.build()
                self.assertIsNone(vol.error)
                self.assertIn("recursive", vol.method)
                self.assertIsNone(vol.journal_id)
                self.assertEqual(vol.isdirs, bytearray([1, 1, 0]))
                self.assertEqual(len(vol.paths), 3)

    def test_missing_volume_reports_error(self):
        vol = webapp.Volume("Z")
        with patch.object(me, "open_volume", side_effect=OSError("missing")), \
             patch.object(webapp.os.path, "isdir", return_value=False):
            vol.build()
        self.assertIn("unavailable", vol.error)
        self.assertFalse(vol.building)

    def test_build_replays_from_cursor_before_enumeration(self):
        vol = webapp.Volume("C")
        calls = []

        def query(*args):
            calls.append("cursor")
            return struct.pack("<7Q", 7, 0, 100, 0, 0, 0, 0)

        def enum(*args, **kwargs):
            calls.append("enumerate")
            return {5: (".", 5, 16), 10: ("a.txt", 5, 0)}

        with patch.object(me, "open_volume", return_value=1), \
             patch.object(me, "device_io", side_effect=query), \
             patch.object(me, "enum_volume", side_effect=enum), \
             patch.object(vol, "_start_watch") as watch:
            vol.build()
        self.assertEqual(calls, ["cursor", "enumerate"])
        self.assertEqual(vol.next_usn, 100)
        watch.assert_called_once()

    def test_rebuild_preserves_pause(self):
        vol = webapp.Volume("C")
        vol.pause()
        with patch.object(me, "open_volume", return_value=1), \
             patch.object(me, "device_io", return_value=struct.pack("<7Q", 7, 0, 100, 0, 0, 0, 0)), \
             patch.object(me, "enum_volume", return_value={}), \
             patch.object(vol, "_start_watch") as watch:
            vol.build()
        watch.assert_not_called()
        self.assertTrue(vol.paused)

    def test_retired_reader_cannot_apply_inflight_batch(self):
        vol = webapp.Volume("C")
        vol.journal_id, vol.next_usn, vol.handle = 1, 100, 1
        vol._watch_gen = 1

        def read(*args, **kwargs):
            vol._watch_gen = 2
            return struct.pack("<Q", 200)

        with patch.object(me, "device_io", side_effect=read):
            vol._watch_loop(1)
        self.assertEqual(vol.next_usn, 100)


class WalkTests(unittest.TestCase):
    def test_dotted_directories_and_extensionless_files(self):
        with patch.object(me.os, "walk", return_value=iter([
                ("C:\\", ["docs.v1"], ["README"]),
                (r"C:\docs.v1", [], ["x.py"])])):
            rows = list(me.walk_volume_entries("C"))
        self.assertEqual(rows, [("C:\\", True), (r"C:\docs.v1", True),
                                (r"C:\README", False), (r"C:\docs.v1\x.py", False)])


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.svc = webapp.Service(["C", "D"])
        seed(self.svc.volumes["C"], [(r"C:\dev", 10, True),
             (r"C:\dev\a.py", 11, False), (r"C:\dev\empty.dir", 12, True),
             (r"C:\device\b.JPG", 13, False), (r"C:\dev\sub\readme.md", 14, False)])
        seed(self.svc.volumes["D"], [(r"D:\music\song.mp3", 20, False)])

    def test_complete_counts_without_metadata_reads(self):
        with patch.object(webapp.Volume, "stat", side_effect=AssertionError("must not read metadata")):
            out = self.svc.overview()
        self.assertEqual((out["files"], out["directories"], out["groups"]), (4, 2, 3))
        self.assertEqual(out["types"]["code"], 1)
        self.assertEqual(out["types"]["image"], 1)
        self.assertEqual(out["types"]["media"], 1)
        self.assertEqual(sum(n["files"] for n in out["nodes"]), out["files"])

    def test_scope_boundary_and_empty_directory(self):
        out = self.svc.overview(scope=r"c:/dev")
        self.assertEqual((out["files"], out["directories"]), (2, 1))
        self.assertEqual({n["name"] for n in out["nodes"]}, {"代码文件", "empty.dir", "sub"})
        empty = self.svc.overview(scope=r"C:\dev\empty.dir")
        self.assertEqual(empty["nodes"], [])
        files = self.svc.search("", 10, False, False, False, "name", "asc", "ALL", scope=r"C:\dev")
        self.assertEqual(files["total"], 3)

    def test_category_islands_link_to_exact_direct_files(self):
        out = self.svc.overview(scope=r"C:\dev")
        node = next(n for n in out["nodes"] if n["kind"] == "category")
        found = self.svc.search("", 10, False, False, False, "name", "asc", "ALL",
                                scope=node["path"], category=node["category"])
        self.assertEqual(found["total"], node["files"])
        self.assertEqual(found["hits"][0]["p"], r"C:\dev\a.py")

    def test_limit_keeps_full_summary(self):
        out = self.svc.overview(limit=1)
        self.assertEqual(len(out["nodes"]), 1)
        self.assertEqual(out["omitted_groups"], 2)
        self.assertEqual(sum(n["files"] for n in out["nodes"]) + out["omitted_files"], out["files"])

    def test_counts_are_not_limited_by_search_sampling(self):
        seed(self.svc.volumes["C"], [(r"C:\dev\f%d.py" % i, i + 10, False) for i in range(5100)])
        out = self.svc.overview(which="C", limit=1)
        self.assertEqual(out["files"], 5100)
        self.assertEqual(out["nodes"][0]["files"], 5100)
        self.assertEqual(out["types"]["code"], 5100)

    def test_search_filters_and_volume_selection(self):
        self.assertEqual(self.svc.overview(q=r"\.py$", regex=True)["files"], 1)
        self.assertEqual(self.svc.overview(q="A.PY", case=True)["files"], 0)
        self.assertEqual(self.svc.overview(q="A.PY")["files"], 1)
        self.assertEqual(self.svc.overview(dirs_only=True)["files"], 0)
        self.assertEqual(self.svc.overview(which="D")["files"], 1)

    def test_cache_invalidated_by_live_overlay_and_rebuild_state(self):
        self.assertEqual(self.svc.overview()["files"], 4)
        vol = self.svc.volumes["C"]
        vol.dirs[10] = r"C:\dev"
        vol.apply_event(30, 10, 0, webapp.R_FILE_CREATE, "new.py")
        self.assertEqual(self.svc.overview()["files"], 5)
        vol.apply_event(30, 10, 0, webapp.R_FILE_DELETE, "new.py")
        self.assertEqual(self.svc.overview()["files"], 4)
        vol.building = True
        self.assertTrue(self.svc.overview()["building"])

    def test_fallback_volume_root_is_not_a_phantom_island(self):
        seed(self.svc.volumes["D"], [("D:\\", 0, True), (r"D:\file.txt", 0, False)])
        out = self.svc.overview(which="D")
        self.assertEqual((out["files"], out["directories"]), (1, 0))
        self.assertEqual(out["nodes"][0]["category"], "document")


class SizeOverviewTests(unittest.TestCase):
    def setUp(self):
        self.svc = webapp.Service(["C"])
        self.vol = self.svc.volumes["C"]
        seed(self.vol, [(r"C:\many\a.py", 10, False), (r"C:\many\b.py", 11, False),
                        (r"C:\large\movie.mp4", 12, False), (r"C:\empty", 13, True)])
        self.vol.stat_cache[r"c:\large\movie.mp4"] = (1000, 100, False)

    def complete(self, **args):
        self.svc.overview(mode="size", **args)
        job = list(self.svc._size_jobs.values())[-1]
        self.assertTrue(job.done.wait(3), "size scan did not finish")
        out = self.svc.overview(mode="size", **args)
        self.assertEqual(out["size_state"], "complete", out)
        return out

    def test_sizes_drive_order_instead_of_file_counts(self):
        out = self.complete()
        self.assertEqual(out["nodes"][0]["name"], "large")
        self.assertEqual((out["bytes"], out["files"], out["known_files"]), (1020, 3, 3))
        self.assertEqual(out["type_bytes"]["media"], 1000)
        self.assertEqual(out["nodes"][-1]["bytes"], 0)
        self.assertEqual(out["pending_files"], 0)

    def test_zero_and_unknown_sizes_are_distinct(self):
        seed(self.vol, [(r"C:\zero.txt", 10, False), (r"C:\missing.py", 11, False)])
        with patch.object(self.vol, "stat", side_effect=[(0, 100, False), (None, None, False)]) as read:
            out = self.complete()
        self.assertEqual((out["bytes"], out["known_files"], out["failed_files"], out["unknown_files"]), (0, 1, 1, 1))
        self.assertEqual(read.call_count, 2)
        known = next(n for n in out["nodes"] if n["category"] == "document")
        unknown = next(n for n in out["nodes"] if n["category"] == "code")
        self.assertEqual((known["known_files"], unknown["known_files"]), (1, 0))

    def test_all_files_are_measured_not_just_search_pool(self):
        seed(self.vol, [(r"C:\all\f%d.py" % i, i + 10, False) for i in range(5100)])
        out = self.complete()
        self.assertEqual((out["bytes"], out["scanned_files"]), (51000, 5100))

    def test_directory_filter_keeps_descendant_sizes(self):
        seed(self.vol, [(r"C:\dev", 10, True), (r"C:\dev\file.py", 11, False),
                        (r"C:\root.txt", 12, False)])
        out = self.complete(dirs_only=True)
        self.assertEqual((out["bytes"], out["files"]), (10, 1))
        self.assertEqual([n["name"] for n in out["nodes"]], ["dev"])

    def test_scope_and_query_apply_to_sizes(self):
        out = self.complete(scope=r"C:\many", q=r"a\.py$", regex=True)
        self.assertEqual((out["bytes"], out["files"]), (10, 1))
        self.assertEqual(out["nodes"][0]["category"], "code")

    def test_slow_metadata_does_not_block_overview_or_counts(self):
        entered, release = threading.Event(), threading.Event()

        def slow_stat(*_args):
            entered.set()
            release.wait(3)
            return 5, 100, False

        with patch.object(self.vol, "stat", side_effect=slow_stat):
            try:
                self.svc.overview(mode="size")
                self.assertTrue(entered.wait(2))
                partial = self.svc.overview(mode="size")
                self.assertEqual(partial["size_state"], "scanning")
                self.assertGreater(partial["pending_files"], 0)
                self.assertEqual(self.svc.overview(mode="count")["files"], 3)
            finally:
                release.set()
                self.assertTrue(list(self.svc._size_jobs.values())[-1].done.wait(3))

    def test_limits_share_scan_and_keep_full_totals(self):
        out = self.complete(limit=1)
        self.assertEqual(out["nodes"][0]["bytes"], 1000)
        self.assertEqual(out["bytes"], 1020)
        self.assertEqual(out["omitted_bytes"], 20)
        full = self.svc.overview(mode="size", limit=12)
        self.assertEqual(len(self.svc._size_jobs), 1)
        self.assertEqual(len(full["nodes"]), 3)

    def test_changes_mark_snapshot_stale_and_refresh_rescans(self):
        current_size = [20]
        with patch.object(self.vol, "stat", side_effect=lambda *_: (current_size[0], 100, False)):
            first = self.complete()
            self.vol.apply_event(10, 5, 0, 2, r"many\a.py")
            current_size[0] = 50
            stale = self.svc.overview(mode="size")
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["bytes"], first["bytes"])
            self.svc.overview(mode="size", refresh=True)
            job = list(self.svc._size_jobs.values())[-1]
            self.assertTrue(job.done.wait(3))
            updated = self.svc.overview(mode="size")
        self.assertEqual(updated["bytes"], 150)
        self.assertFalse(updated["stale"])

    def test_evicted_scans_are_cancelled(self):
        self.complete(q="many")
        first = list(self.svc._size_jobs.values())[0]
        for query in ("large", "empty", "a.py", "b.py"):
            self.complete(q=query)
        self.assertEqual(len(self.svc._size_jobs), 4)
        self.assertTrue(first.cancel.is_set())


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        svc = webapp.Service(["C", "D"])
        demo.install(svc, emit_events=False)
        cls.server = webapp.ThreadingHTTPServer(("127.0.0.1", 0), webapp.make_handler(svc))
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_demo_search_and_stats(self):
        for route in ("/api/stats", "/api/search?q=main", "/api/search?q=main&sort=size&order=desc"):
            with urllib.request.urlopen(self.base + route) as response:
                self.assertEqual(response.status, 200)
                out = json.load(response)
                if "hits" in out:
                    self.assertGreater(out["total"], 0)
                    self.assertTrue(all(r["s"] is not None for r in out["hits"]))
                else:
                    self.assertGreater(out["count"], 0)

    def test_invalid_regex_returns_400(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(self.base + "/api/search?q=%5B&regex=1")
        self.assertEqual(exc.exception.code, 400)
        exc.exception.close()

    def test_overview_and_static_assets(self):
        with urllib.request.urlopen(self.base + "/api/overview?scope=C%3A%5Cdev") as response:
            out = json.load(response)
            self.assertGreater(out["files"], 0)
            self.assertTrue(out["demo"])
        for asset, content_type in [("islands.css", "text/css"), ("islands.js", "text/javascript")]:
            with urllib.request.urlopen(self.base + "/" + asset) as response:
                self.assertIn(content_type, response.headers["Content-Type"])
                self.assertGreater(len(response.read()), 100)

    def test_overview_rejects_invalid_scope_and_regex(self):
        for query in ("scope=relative", "q=%5B&regex=1", "mode=invalid", "mode=size&q=%5B&regex=1"):
            with self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(self.base + "/api/overview?" + query)
            self.assertEqual(exc.exception.code, 400)
            exc.exception.close()


if __name__ == "__main__":
    unittest.main()
