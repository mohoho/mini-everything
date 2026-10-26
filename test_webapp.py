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


if __name__ == "__main__":
    unittest.main()
