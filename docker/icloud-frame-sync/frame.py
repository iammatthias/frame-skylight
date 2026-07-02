"""Inject/reconcile photos into the Skylight frame's local slideshow DB via adb.

The frame (com.skylight) renders rows of SlideshowAsset in skylight_v2.db, each
pointing at a local image-<id>.jpg. We add our own rows (id prefixed so we never
collide with cloud photos) and push the matching JPGs. No network/cert needed —
the frame must just be reachable via network-adb (adb tcpip 5555).
"""
import subprocess
import time

DB = "/data/data/com.skylight/databases/skylight_v2.db"
PICDIR = "/data/media/0/Android/data/com.skylight/files/pictures"          # writable path
PATHPREFIX = "/storage/emulated/0/Android/data/com.skylight/files/pictures"  # path the app reads

# Every file name an asset id can map to. Only one kind exists per row (photo or
# video + posters), but a remove/reset must sweep them all -- the single source
# of truth so cleanup never drifts from what push_* writes ({id} is the asset id,
# or an "ic-*" glob for a bulk reset).
MEDIA_NAMES = {
    "photo": "image-{id}.jpg",
    "video": "video-{id}.mp4",
    "poster_small": "video-small-thumbnail-{id}.jpg",
    "poster_full": "video-full-thumbnail-{id}.jpg",
}


def _esc(s):
    return s.replace("'", "''")


class Frame:
    def __init__(self, host):
        self.host = host                       # e.g. "192.168.1.50:5555"

    def _adb(self, *args, text_input=None):
        return subprocess.run(["adb", "-s", self.host, *args],
                              input=text_input, capture_output=True, text=True)

    def connect(self, attempts=3, backoff=2.0):
        # Network-adb drops are common (frame sleeps, wifi blips). Retry a few
        # times before treating the frame as unreachable for this cycle.
        last = ""
        for i in range(attempts):
            subprocess.run(["adb", "connect", self.host], capture_output=True, text=True)
            r = self._adb("get-state")
            if "device" in r.stdout:
                return
            last = r.stdout.strip() or r.stderr.strip()
            self._adb("disconnect")  # clear a half-open/offline entry before retrying
            if i < attempts - 1:
                time.sleep(backoff * (i + 1))
        raise RuntimeError(f"frame {self.host} not reachable via adb ({last})")

    def shell(self, cmd):
        return self._adb("shell", cmd).stdout

    def sql(self, q):
        # Pipe the statement to sqlite3 over stdin rather than embedding it in a
        # shell command line -- no shell quoting, so a value containing " $ ` etc.
        # can't break out of the command (SQL literals are still escaped by _esc).
        if not q.rstrip().endswith(";"):
            q += ";"
        return self._adb("shell", "sqlite3", DB, text_input=q).stdout.strip()

    def asset_ids(self, prefix=None):
        out = self.sql("SELECT serverAssetId FROM SlideshowAsset")
        ids = {x.strip() for x in out.splitlines() if x.strip()}
        return {i for i in ids if i.startswith(prefix)} if prefix else ids

    def _push(self, local_path, remote_name):
        """adb push a local file into the frame's pictures dir, fix perms, and
        return the path the slideshow app reads it from. Raises on push failure
        so a missing file never gets a slideshow row (it's retried next cycle)."""
        remote = f"{PICDIR}/{remote_name}"
        r = self._adb("push", local_path, remote)
        if r.returncode != 0 or "error:" in (r.stderr or "").lower():
            raise RuntimeError(f"adb push failed for {remote_name}: "
                               f"{(r.stderr or r.stdout).strip()}")
        self.shell(f"chmod 644 '{remote}'; chown media_rw:media_rw '{remote}'")
        return f"{PATHPREFIX}/{remote_name}"

    def push_photo(self, local_path, asset_id):
        return self._push(local_path, MEDIA_NAMES["photo"].format(id=asset_id))

    def push_video(self, local_path, asset_id):
        return self._push(local_path, MEDIA_NAMES["video"].format(id=asset_id))

    def push_poster(self, local_path, asset_id):
        """A video slide shows its still from a sibling thumbnail JPEG, not the
        video itself. The app names that file video-{small,full}-thumbnail-<id>;
        write both, and return the small one's app path for the smallThumbnail
        column (the poster the slideshow loads via Glide while the clip buffers)."""
        small = self._push(local_path, MEDIA_NAMES["poster_small"].format(id=asset_id))
        self._push(local_path, MEDIA_NAMES["poster_full"].format(id=asset_id))
        return small

    def insert_asset(self, asset_id, path, width, height, caption="", sender="icloud@local",
                     asset_type="photo", small_thumbnail=""):
        now = int(time.time() * 1000)
        q = ("INSERT OR REPLACE INTO SlideshowAsset "
             "(serverAssetId,syncToken,caption,url,senderAddress,createdAt,isLiked,"
             "hasBeenSeen,pathToAsset,updatedAt,assetType,smallThumbnail,rotation,"
             "markedForDeletion,imageFileHeight,imageFileWidth,timesFailedToDownload) "
             f"VALUES ('{_esc(asset_id)}',0,'{_esc(caption)}','','{_esc(sender)}',{now},0,1,"
             f"'{_esc(path)}',{now},'{_esc(asset_type)}','{_esc(small_thumbnail)}',0,0,"
             f"{int(height)},{int(width)},0)")
        self.sql(q)

    def remove_asset(self, asset_id):
        self.sql(f"DELETE FROM SlideshowAsset WHERE serverAssetId='{_esc(asset_id)}'")
        files = " ".join(f"'{PICDIR}/{n.format(id=asset_id)}'" for n in MEDIA_NAMES.values())
        self.shell("rm -f " + files)

    def refresh_app(self, pkg="com.skylight"):
        """Make the slideshow reload its playlist from the DB.

        The app reads SlideshowAsset once at process start and ignores external
        INSERT/DELETEs, so freshly synced photos don't appear until it restarts.
        force-stop + relaunch the launcher activity; the frame keeps the app as
        its foreground/home, so it comes straight back into the slideshow with
        the current DB contents."""
        self.shell(f"am force-stop {pkg}")
        self.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
