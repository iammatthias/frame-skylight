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


class TestAlbumPagination(unittest.TestCase):
    def test_fetches_every_page(self):
        # An album of 130 photos. CloudKit returns each photo as a CPLMaster +
        # CPLAsset pair (so a 100-record page = 50 photos), added-date ASCENDING,
        # paged by startRank. The old loop stopped after page 1 and returned only
        # the oldest 50 -- dropping the newest 80, which is the live symptom.
        PAGE, TOTAL = icloud_album.PAGE, 130

        def master(i):
            return {
                "recordType": "CPLMaster", "recordName": f"guid-{i:04d}",
                "fields": {
                    "resOriginalRes": {"value": {"downloadURL": f"https://x/{i}/${{f}}"}},
                    "resOriginalWidth": {"value": 100}, "resOriginalHeight": {"value": 100},
                    "filenameEnc": {"value": base64.b64encode(f"p{i}.jpg".encode()).decode()},
                },
            }

        # Full record stream: master+asset per photo, ascending.
        stream = []
        for i in range(TOTAL):
            stream += [master(i), {"recordType": "CPLAsset", "recordName": f"a-{i}", "fields": {}}]

        orig_q, orig_r = icloud_album._query, icloud_album._resolve
        # startRank is in photo units; two records per photo => slice at start*2.
        icloud_album._query = lambda ctx, start: {"records": stream[start * 2: start * 2 + PAGE]}
        icloud_album._resolve = lambda token: {"zoneID": {}, "authToken": "", "host": "", "title": "T"}
        try:
            album = icloud_album.fetch_album("0bTOKEN")
        finally:
            icloud_album._query, icloud_album._resolve = orig_q, orig_r

        self.assertEqual(len(album["photos"]), TOTAL)            # not 50
        self.assertEqual(album["photos"][0]["guid"], "guid-0000")
        self.assertEqual(album["photos"][-1]["guid"], f"guid-{TOTAL - 1:04d}")  # newest present


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

    def test_refresh_restarts_slideshow(self):
        self.frame.refresh_app()
        joined = " ".join(" ".join(c) for c in self.calls)
        self.assertIn("force-stop com.skylight", joined)   # reload the playlist
        self.assertIn("monkey -p com.skylight", joined)    # relaunch into slideshow


class TestSyncRefresh(unittest.TestCase):
    """The slideshow only reloads on restart, so a cycle that changes the photo
    set must kick the app -- and a no-op cycle must not (keep the screen steady)."""

    def _run(self, album_photos, have_ids):
        calls = {"refresh": 0, "added": [], "removed": []}

        class FakeFrame:
            def connect(self):
                pass

            def asset_ids(self, prefix=None):
                return set(have_ids)

            def push_photo(self, path, aid):
                return f"/p/image-{aid}.jpg"

            def insert_asset(self, aid, *a, **k):
                calls["added"].append(aid)

            def remove_asset(self, aid):
                calls["removed"].append(aid)

            def refresh_app(self):
                calls["refresh"] += 1

        orig = (sync.fetch_album, sync.download, sync.DRY_RUN)
        sync.fetch_album = lambda url: {"name": "T", "photos": album_photos}
        sync.download = lambda url, path: open(path, "wb").close()
        sync.DRY_RUN = False
        try:
            sync.sync_once(FakeFrame(), status.State(300))
        finally:
            sync.fetch_album, sync.download, sync.DRY_RUN = orig
        return calls

    @staticmethod
    def _photo(guid):
        return {"guid": guid, "url": f"https://x/{guid}", "width": 1, "height": 1,
                "caption": "", "filename": f"{guid}.jpg"}

    def test_refresh_when_photo_added(self):
        calls = self._run([self._photo("g1")], have_ids=set())
        self.assertEqual(calls["added"], ["ic-g1"])
        self.assertEqual(calls["refresh"], 1)

    def test_no_refresh_when_unchanged(self):
        calls = self._run([self._photo("g1")], have_ids={sync._aid("g1")})
        self.assertEqual(calls["added"], [])
        self.assertEqual(calls["refresh"], 0)


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
