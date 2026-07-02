"""Wipe all ic- (iCloud-injected) rows + files from the frame for a clean re-sync.

Leaves any non-ic (cloud/Skylight) photos alone. Run, then restart the sync
service to repopulate from the album.

Usage:  FRAME_HOST=192.168.1.50:5555 python3 reset.py
"""
import os
from frame import Frame, PICDIR, MEDIA_NAMES

f = Frame(os.environ.get("FRAME_HOST", "192.168.1.50:5555"))
f.connect()
n = len(f.asset_ids(prefix="ic-"))
print(f"removing {n} ic- rows + files")
f.sql("DELETE FROM SlideshowAsset WHERE serverAssetId LIKE 'ic-%'")
# Sweep every media name for ic- ids (image + video + both posters).
globs = " ".join(f"{PICDIR}/{name.format(id='ic-*')}" for name in MEDIA_NAMES.values())
f.shell("rm -rf " + globs)
print("remaining ic-:", len(f.asset_ids(prefix="ic-")))
