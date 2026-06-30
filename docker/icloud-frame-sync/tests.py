"""Unit tests for the offline logic — no network, no adb, no frame.

Run:  python3 -m unittest tests   (from this directory)
Covers: album link parsing, CloudKit record -> photo mapping, frame SQL
escaping + id sanitization, and the status health state machine.
"""
import base64
import unittest

import icloud_album
import frame
import sync
import status


class TestAlbumParsing(unittest.TestCase):
    def test_token_from_url_forms(self):
        cases = {
            "https://photos.icloud.com/shared/album/0b19ABC": "0b19ABC",
            "https://photos.icloud.com/shared/album/0b19ABC/": "0b19ABC",
            "https://www.icloud.com/sharedalbum/#0b19ABC": "0b19ABC",
            "https://www.icloud.com/sharedalbum/#0b19ABC;extra": "0b19ABC;extra",
            "0b19ABC": "0b19ABC",
            "0b19ABC?foo=bar": "0b19ABC",
        }
        for url, want in cases.items():
            self.assertEqual(icloud_album.token_from_url(url), want, url)

    def test_photo_from_master(self):
        m = {
            "recordType": "CPLMaster",
            "recordName": "Ah7p/x:1",
            "fields": {
                "resOriginalRes": {"value": {"downloadURL": "https://x/B/abc/${f}?o=1"}},
                "resOriginalWidth": {"value": 1620},
                "resOriginalHeight": {"value": 1080},
                "filenameEnc": {"value": base64.b64encode(b"My Photo.JPG").decode()},
            },
        }
        p = icloud_album.photo_from_master(m)
        self.assertEqual(p["guid"], "Ah7p/x:1")
        self.assertEqual(p["filename"], "My Photo.JPG")
        self.assertEqual((p["width"], p["height"]), (1620, 1080))
        # ${f} replaced with the URL-encoded filename.
        self.assertIn("My%20Photo.JPG", p["url"])
        self.assertNotIn("${f}", p["url"])

    def test_photo_from_master_skips(self):
        # Non-master records and masters without a download URL are skipped.
        self.assertIsNone(icloud_album.photo_from_master({"recordType": "CPLAsset"}))
        self.assertIsNone(icloud_album.photo_from_master(
            {"recordType": "CPLMaster", "recordName": "x", "fields": {}}))


class TestFrameSQL(unittest.TestCase):
    def setUp(self):
        # A Frame that records the adb commands instead of running them.
        self.calls = []

        class FakeFrame(frame.Frame):
            def _adb(inner, *args):
                self.calls.append(args)

                class R:
                    stdout = ""
                    stderr = ""

                return R()

        self.frame = FakeFrame("1.2.3.4:5555")

    def test_aid_is_shell_and_fs_safe(self):
        aid = sync._aid("Ah7p/x:1+2")
        self.assertTrue(aid.startswith("ic-"))
        # Only [A-Za-z0-9_] after the prefix — nothing that breaks a path or shell.
        self.assertRegex(aid, r"^ic-[A-Za-z0-9_]+$")

    def test_caption_quotes_escaped(self):
        self.frame.insert_asset("ic-x", "/p/img.jpg", 100, 200, caption="O'Brien's trip")
        sql = " ".join(self.calls[-1])
        self.assertIn("O''Brien''s trip", sql)   # '' = escaped single quote
        self.assertIn("ic-x", sql)
        self.assertIn("200", sql)                 # height
        self.assertIn("100", sql)                 # width

    def test_remove_deletes_row_and_file(self):
        self.frame.remove_asset("ic-x")
        joined = " ".join(" ".join(c) for c in self.calls)
        self.assertIn("DELETE FROM SlideshowAsset", joined)
        self.assertIn("image-ic-x.jpg", joined)


class TestStatusHealth(unittest.TestCase):
    def test_health_lifecycle(self):
        st = status.State(poll_interval=300)
        # Before any sync: healthy during the startup grace window.
        self.assertTrue(st.snapshot()["healthy"])
        # After a clean cycle: healthy, counters reflected.
        st.update(album="Skylight Frame", album_count=14, last_error=None)
        st.mark_synced()
        snap = st.snapshot()
        self.assertTrue(snap["healthy"])
        self.assertEqual(snap["album_count"], 14)
        self.assertEqual(snap["cycles"], 1)
        self.assertIsNotNone(snap["last_sync"])
        # A recorded error makes it unhealthy.
        st.update(last_error="frame unreachable")
        self.assertFalse(st.snapshot()["healthy"])


if __name__ == "__main__":
    unittest.main()
