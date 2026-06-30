"""Wipe all ic- (iCloud-injected) rows + files from the frame for a clean re-sync.

Leaves any non-ic (cloud/Skylight) photos alone. Run, then restart the sync
service to repopulate from the album.

Usage:  FRAME_HOST=192.168.1.50:5555 python3 reset.py
"""
import os
from frame import Frame

f = Frame(os.environ.get("FRAME_HOST", "192.168.1.50:5555"))
f.connect()
n = len(f.asset_ids(prefix="ic-"))
print(f"removing {n} ic- rows + files")
f.sql("DELETE FROM SlideshowAsset WHERE serverAssetId LIKE 'ic-%'")
f.shell("rm -rf /data/media/0/Android/data/com.skylight/files/pictures/image-ic-*")
print("remaining ic-:", len(f.asset_ids(prefix="ic-")))
