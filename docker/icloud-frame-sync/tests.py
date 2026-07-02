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

    def test_asset_from_master_photo(self):
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
        p = icloud_album.asset_from_master(m)
        self.assertEqual(p["kind"], "photo")
        self.assertEqual(p["guid"], "Ah7p/x:1")
        self.assertEqual(p["filename"], "My Photo.JPG")
        self.assertEqual((p["width"], p["height"]), (1620, 1080))
        self.assertEqual(p["poster_url"], "")
        # ${f} replaced with the URL-encoded filename.
        self.assertIn("My%20Photo.JPG", p["url"])
        self.assertNotIn("${f}", p["url"])

    def test_asset_from_master_skips(self):
        # Non-master records and masters without a download URL are skipped.
        self.assertIsNone(icloud_album.asset_from_master({"recordType": "CPLAsset"}))
        self.assertIsNone(icloud_album.asset_from_master(
            {"recordType": "CPLMaster", "recordName": "x", "fields": {}}))

    @staticmethod
    def _video_master(med=True, poster=True):
        # Mirrors a real iCloud video CPLMaster: a quicktime itemType, the huge
        # original we must ignore, H.264 mp4 derivatives, and JPEG posters.
        f = {
            "filenameEnc": {"value": base64.b64encode(b"IMG_1.MOV").decode()},
            "itemType": {"value": "com.apple.quicktime-movie"},
            "resOriginalFileType": {"value": "com.apple.quicktime-movie"},
            "resOriginalRes": {"value": {"downloadURL": "https://x/ORIGINAL/${f}"}},
            "resVidSmallRes": {"value": {"downloadURL": "https://x/SMALL/${f}"}},
            "resVidSmallWidth": {"value": 360}, "resVidSmallHeight": {"value": 640},
            "resJPEGThumbRes": {"value": {"downloadURL": "https://x/THUMB/${f}"}},
        }
        if med:
            f["resVidMedRes"] = {"value": {"downloadURL": "https://x/MED/${f}"}}
            f["resVidMedWidth"] = {"value": 720}
            f["resVidMedHeight"] = {"value": 1280}
        if poster:
            f["resJPEGMedRes"] = {"value": {"downloadURL": "https://x/POSTER/${f}"}}
        return {"recordType": "CPLMaster", "recordName": "vguid", "fields": f}

    def test_asset_from_master_video_prefers_med(self):
        v = icloud_album.asset_from_master(self._video_master())
        self.assertEqual(v["kind"], "video")
        self.assertEqual((v["width"], v["height"]), (720, 1280))
        self.assertIn("/MED/", v["url"])
        self.assertIn("public.mp4", v["url"])           # never the .MOV filename
        self.assertNotIn("/ORIGINAL/", v["url"])        # the huge HEVC original is skipped
        self.assertIn("/POSTER/", v["poster_url"])      # resJPEGMedRes still
        self.assertIn("public.jpeg", v["poster_url"])
        self.assertNotIn("${f}", v["url"] + v["poster_url"])

    def test_asset_from_master_video_falls_back_to_small(self):
        v = icloud_album.asset_from_master(self._video_master(med=False))
        self.assertEqual(v["kind"], "video")
        self.assertEqual((v["width"], v["height"]), (360, 640))
        self.assertIn("/SMALL/", v["url"])

    def test_asset_from_master_video_poster_optional(self):
        v = icloud_album.asset_from_master(self._video_master(poster=False))
        self.assertEqual(v["kind"], "video")
        self.assertIn("/THUMB/", v["poster_url"])       # falls back to resJPEGThumbRes

    def test_live_photo_stays_a_photo(self):
        # A Live Photo is an image that also carries resVid* (its motion clip);
        # it must sync as a still, using resOriginalRes -- not become a video.
        m = {
            "recordType": "CPLMaster", "recordName": "live1",
            "fields": {
                "itemType": {"value": "public.heic"},
                "filenameEnc": {"value": base64.b64encode(b"IMG_9.HEIC").decode()},
                "resOriginalRes": {"value": {"downloadURL": "https://x/STILL/${f}"}},
                "resOriginalWidth": {"value": 4032}, "resOriginalHeight": {"value": 3024},
                "resVidMedRes": {"value": {"downloadURL": "https://x/LIVEMOTION/${f}"}},
                "resVidMedWidth": {"value": 1440}, "resVidMedHeight": {"value": 1080},
            },
        }
        p = icloud_album.asset_from_master(m)
        self.assertEqual(p["kind"], "photo")
        self.assertIn("/STILL/", p["url"])
        self.assertNotIn("/LIVEMOTION/", p["url"])
        self.assertEqual(p["poster_url"], "")


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

        self.assertEqual(len(album["assets"]), TOTAL)            # not 50
        self.assertEqual(album["assets"][0]["guid"], "guid-0000")
        self.assertEqual(album["assets"][-1]["guid"], f"guid-{TOTAL - 1:04d}")  # newest present


class TestFrameSQL(unittest.TestCase):
    def setUp(self):
        # A Frame that records adb commands instead of running them. SQL now goes
        # to sqlite3 over stdin, so it lands in sql_inputs, not the argv calls.
        self.calls = []
        self.sql_inputs = []

        class FakeFrame(frame.Frame):
            def _adb(inner, *args, text_input=None):
                self.calls.append(args)
                if text_input is not None:
                    self.sql_inputs.append(text_input)

                class R:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return R()

        self.frame = FakeFrame("1.2.3.4:5555")

    def _all_args(self):
        return " ".join(" ".join(str(a) for a in c) for c in self.calls)

    def test_aid_is_shell_and_fs_safe(self):
        aid = sync._aid("Ah7p/x:1+2")
        self.assertTrue(aid.startswith("ic-"))
        # Only [A-Za-z0-9_] after the prefix — nothing that breaks a path or shell.
        self.assertRegex(aid, r"^ic-[A-Za-z0-9_]+$")

    def test_caption_quotes_escaped(self):
        self.frame.insert_asset("ic-x", "/p/img.jpg", 100, 200, caption="O'Brien's trip")
        sql = self.sql_inputs[-1]
        self.assertIn("O''Brien''s trip", sql)   # '' = escaped single quote
        self.assertIn("ic-x", sql)
        self.assertIn("200", sql)                 # height
        self.assertIn("100", sql)                 # width

    def test_sql_piped_on_stdin_not_argv(self):
        # The statement must travel on stdin (shell can't reinterpret it), never
        # embedded in the adb argv -- that's what defuses the injection.
        self.frame.sql("SELECT 1")
        self.assertIn("SELECT 1;", self.sql_inputs[-1])
        self.assertNotIn("SELECT 1", self._all_args())

    def test_remove_deletes_row_and_all_files(self):
        self.frame.remove_asset("ic-x")
        self.assertIn("DELETE FROM SlideshowAsset", " ".join(self.sql_inputs))
        rm = self._all_args()   # the rm runs as a shell command (argv)
        # Photo, video, and both poster thumbnails are all swept (only one exists).
        self.assertIn("image-ic-x.jpg", rm)
        self.assertIn("video-ic-x.mp4", rm)
        self.assertIn("video-small-thumbnail-ic-x.jpg", rm)
        self.assertIn("video-full-thumbnail-ic-x.jpg", rm)

    def test_push_video_and_poster_naming(self):
        vpath = self.frame.push_video("/tmp/a.mp4", "ic-x")
        ppath = self.frame.push_poster("/tmp/a.jpg", "ic-x")
        pushes = " ".join(" ".join(str(a) for a in c) for c in self.calls if c and c[0] == "push")
        self.assertIn("video-ic-x.mp4", pushes)
        self.assertIn("video-small-thumbnail-ic-x.jpg", pushes)
        self.assertIn("video-full-thumbnail-ic-x.jpg", pushes)   # both posters written
        self.assertTrue(vpath.endswith("video-ic-x.mp4"))
        self.assertTrue(ppath.endswith("video-small-thumbnail-ic-x.jpg"))

    def test_push_raises_on_adb_failure(self):
        # A failed push must raise so no slideshow row points at a missing file.
        class Failing(frame.Frame):
            def _adb(inner, *args, text_input=None):
                class R:
                    returncode = 1
                    stdout = ""
                    stderr = "adb: error: failed to copy"
                return R()
        with self.assertRaises(RuntimeError):
            Failing("h:5555").push_photo("/tmp/x.jpg", "ic-x")

    def test_insert_video_sets_type_and_thumbnail(self):
        self.frame.insert_asset("ic-x", "/p/video-ic-x.mp4", 720, 1280,
                                asset_type="video",
                                small_thumbnail="/p/video-small-thumbnail-ic-x.jpg")
        sql = self.sql_inputs[-1]
        self.assertIn("'video'", sql)                             # assetType column
        self.assertIn("video-small-thumbnail-ic-x.jpg", sql)     # smallThumbnail column
        self.assertIn("1280", sql)                               # height
        self.assertIn("720", sql)                                # width

    def test_insert_photo_defaults_unchanged(self):
        self.frame.insert_asset("ic-x", "/p/image-ic-x.jpg", 100, 200)
        sql = self.sql_inputs[-1]
        self.assertIn("'photo'", sql)                            # default assetType
        # smallThumbnail stays empty for photos: ...,'photo','',0,...
        self.assertIn("'photo','',", sql)

    def test_refresh_restarts_slideshow(self):
        self.frame.refresh_app()
        joined = " ".join(" ".join(c) for c in self.calls)
        self.assertIn("force-stop com.skylight", joined)   # reload the playlist
        self.assertIn("monkey -p com.skylight", joined)    # relaunch into slideshow


class TestSyncRefresh(unittest.TestCase):
    """The slideshow only reloads on restart, so a cycle that changes the photo
    set must kick the app -- and a no-op cycle must not (keep the screen steady)."""

    def _run(self, album_assets, have_ids):
        calls = {"refresh": 0, "added": [], "removed": [], "inserts": []}

        class FakeFrame:
            def connect(self):
                pass

            def asset_ids(self, prefix=None):
                return set(have_ids)

            def push_photo(self, path, aid):
                return f"/p/image-{aid}.jpg"

            def push_video(self, path, aid):
                return f"/p/video-{aid}.mp4"

            def push_poster(self, path, aid):
                return f"/p/video-small-thumbnail-{aid}.jpg"

            def insert_asset(self, aid, path, w, h, caption="", sender="icloud@local",
                             asset_type="photo", small_thumbnail=""):
                calls["added"].append(aid)
                calls["inserts"].append({"aid": aid, "type": asset_type,
                                         "thumb": small_thumbnail, "path": path})

            def remove_asset(self, aid):
                calls["removed"].append(aid)

            def refresh_app(self):
                calls["refresh"] += 1

        orig = (sync.fetch_album, sync.download, sync.DRY_RUN)
        sync.fetch_album = lambda url: {"name": "T", "assets": album_assets}
        sync.download = lambda url, path: open(path, "wb").close()
        sync.DRY_RUN = False
        try:
            sync.sync_once(FakeFrame(), status.State(300))
        finally:
            sync.fetch_album, sync.download, sync.DRY_RUN = orig
        return calls

    @staticmethod
    def _photo(guid):
        return {"guid": guid, "kind": "photo", "url": f"https://x/{guid}", "width": 1,
                "height": 1, "caption": "", "filename": f"{guid}.jpg", "poster_url": ""}

    @staticmethod
    def _video(guid, poster=True):
        return {"guid": guid, "kind": "video", "url": f"https://x/{guid}.mp4",
                "width": 720, "height": 1280, "caption": "", "filename": f"{guid}.MOV",
                "poster_url": f"https://x/{guid}-poster.jpg" if poster else ""}

    def test_refresh_when_photo_added(self):
        calls = self._run([self._photo("g1")], have_ids=set())
        self.assertEqual(calls["added"], ["ic-g1"])
        self.assertEqual(calls["inserts"][0]["type"], "photo")
        self.assertEqual(calls["refresh"], 1)

    def test_no_refresh_when_unchanged(self):
        calls = self._run([self._photo("g1")], have_ids={sync._aid("g1")})
        self.assertEqual(calls["added"], [])
        self.assertEqual(calls["refresh"], 0)

    def test_video_added_with_poster(self):
        calls = self._run([self._video("v1")], have_ids=set())
        self.assertEqual(calls["added"], ["ic-v1"])
        ins = calls["inserts"][0]
        self.assertEqual(ins["type"], "video")
        self.assertEqual(ins["path"], "/p/video-ic-v1.mp4")
        self.assertEqual(ins["thumb"], "/p/video-small-thumbnail-ic-v1.jpg")
        self.assertEqual(calls["refresh"], 1)

    def test_video_added_without_poster(self):
        calls = self._run([self._video("v1", poster=False)], have_ids=set())
        ins = calls["inserts"][0]
        self.assertEqual(ins["type"], "video")
        self.assertEqual(ins["thumb"], "")          # no poster pushed, smallThumbnail stays empty

    def test_removes_asset_absent_from_nonempty_album(self):
        # Normal deletion still works: g2 is on the frame but no longer in the album.
        calls = self._run([self._photo("g1")],
                          have_ids={sync._aid("g1"), sync._aid("g2")})
        self.assertEqual(calls["removed"], [sync._aid("g2")])
        self.assertEqual(calls["refresh"], 1)

    def test_empty_album_does_not_wipe_frame(self):
        # A fetch returning zero assets is treated as an anomaly, not a mass
        # delete -- the frame's content is left intact and the app isn't kicked.
        calls = self._run([], have_ids={sync._aid("g1"), sync._aid("g2")})
        self.assertEqual(calls["removed"], [])
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
