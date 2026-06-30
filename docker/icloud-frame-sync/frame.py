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


def _esc(s):
    return s.replace("'", "''")


class Frame:
    def __init__(self, host):
        self.host = host                       # e.g. "192.168.50.72:5555"

    def _adb(self, *args):
        return subprocess.run(["adb", "-s", self.host, *args],
                              capture_output=True, text=True)

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
        return self.shell(f'sqlite3 {DB} "{q}"').strip()

    def asset_ids(self, prefix=None):
        out = self.sql("SELECT serverAssetId FROM SlideshowAsset")
        ids = {x.strip() for x in out.splitlines() if x.strip()}
        return {i for i in ids if i.startswith(prefix)} if prefix else ids

    def push_photo(self, local_path, asset_id):
        remote = f"{PICDIR}/image-{asset_id}.jpg"
        self._adb("push", local_path, remote)
        self.shell(f"chmod 644 '{remote}'; chown media_rw:media_rw '{remote}'")
        return f"{PATHPREFIX}/image-{asset_id}.jpg"

    def insert_asset(self, asset_id, path, width, height, caption="", sender="icloud@local"):
        now = int(time.time() * 1000)
        q = ("INSERT OR REPLACE INTO SlideshowAsset "
             "(serverAssetId,syncToken,caption,url,senderAddress,createdAt,isLiked,"
             "hasBeenSeen,pathToAsset,updatedAt,assetType,smallThumbnail,rotation,"
             "markedForDeletion,imageFileHeight,imageFileWidth,timesFailedToDownload) "
             f"VALUES ('{_esc(asset_id)}',0,'{_esc(caption)}','','{_esc(sender)}',{now},0,1,"
             f"'{_esc(path)}',{now},'photo','',0,0,{int(height)},{int(width)},0)")
        self.sql(q)

    def remove_asset(self, asset_id):
        self.sql(f"DELETE FROM SlideshowAsset WHERE serverAssetId='{_esc(asset_id)}'")
        self.shell(f"rm -f '{PICDIR}/image-{asset_id}.jpg'")
