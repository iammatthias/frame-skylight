"""Reconcile a public iCloud Shared Album into the Skylight frame, on a loop.

Adds photos new in the album, removes ones deleted from it. Idempotent: each
frame asset id is "ic-<sanitized photoGuid>", so re-runs never duplicate and only
our own ic- rows are touched — any cloud/Skylight photos are left alone.

Config (env):
  ALBUM_URL       public iCloud Shared Album link (required)
  FRAME_HOST      frame network-adb endpoint, ip:port (default 192.168.1.50:5555)
  POLL_INTERVAL   reconcile period in seconds; <= 0 runs one cycle and exits
  DRY_RUN         "1" to log the plan without pushing/inserting/removing anything
  STATUS_ADDR     status HTTP bind address (default 0.0.0.0)
  STATUS_PORT     status HTTP port (default 8780); GET /status

Health/observability live at http://STATUS_ADDR:STATUS_PORT/status.
"""
import os
import re
import signal
import sys
import tempfile
import threading
import urllib.request
from collections import Counter
from datetime import datetime, timezone

from icloud_album import fetch_album
from frame import Frame
from status import State, serve

ALBUM = os.environ.get("ALBUM_URL", "")
FRAME_HOST = os.environ.get("FRAME_HOST", "192.168.1.50:5555")
INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))
DRY_RUN = os.environ.get("DRY_RUN", "") in ("1", "true", "yes")
STATUS_ADDR = os.environ.get("STATUS_ADDR", "0.0.0.0")
STATUS_PORT = int(os.environ.get("STATUS_PORT", "8780"))
ID_PREFIX = "ic-"

_stop = threading.Event()


def log(*a):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(ts, *a, flush=True)


def _aid(guid):
    # Filesystem- and shell-safe, stable id. GUIDs can contain / + : etc.
    return ID_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", guid)


def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r, open(path, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)


def _download_tmp(url, suffix):
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False).name
    download(url, tmp)
    return tmp


def _add_asset(frame, aid, p):
    """Fetch one album asset onto the frame and insert its slideshow row. Photos
    push a single JPEG; videos push the H.264 mp4 plus a poster JPEG (the still
    the frame shows while the clip buffers). Temp files are always cleaned up."""
    tmps = []
    try:
        if p["kind"] == "video":
            path = frame.push_video(_keep(tmps, _download_tmp(p["url"], ".mp4")), aid)
            thumb = ""
            if p.get("poster_url"):
                thumb = frame.push_poster(_keep(tmps, _download_tmp(p["poster_url"], ".jpg")), aid)
            frame.insert_asset(aid, path, p["width"], p["height"], p["caption"],
                               asset_type="video", small_thumbnail=thumb)
        else:
            path = frame.push_photo(_keep(tmps, _download_tmp(p["url"], ".jpg")), aid)
            frame.insert_asset(aid, path, p["width"], p["height"], p["caption"])
    finally:
        for t in tmps:
            try:
                os.unlink(t)
            except OSError:
                pass


def _keep(bucket, path):
    bucket.append(path)
    return path


def sync_once(frame, state):
    frame.connect()
    state.update(frame_reachable=True)
    album = fetch_album(ALBUM)
    want = {_aid(p["guid"]): p for p in album["assets"] if p["url"]}
    have = frame.asset_ids(prefix=ID_PREFIX)
    kinds = Counter(p["kind"] for p in album["assets"])
    state.update(album=album["name"], album_count=len(album["assets"]),
                 album_photos=kinds.get("photo", 0), album_videos=kinds.get("video", 0))
    added = removed = 0

    for aid, p in want.items():
        if aid in have:
            continue  # already on the frame. NB: in-place album edits (same guid,
                      # re-cropped) are not re-fetched -- id presence wins here.
        if DRY_RUN:
            log(f"would add [{p['kind']}] {aid}  {p['width']}x{p['height']}  {p['filename']}")
            added += 1
            continue
        try:
            _add_asset(frame, aid, p)
            added += 1
            log(f"+ [{p['kind']}] {aid}  {p['width']}x{p['height']}  {p['filename']}")
        except Exception as e:
            log(f"! failed {aid}: {e}")

    to_remove = have - set(want)
    # Safety valve: a *successful* fetch returning zero assets is almost always an
    # API/token hiccup, not "the user emptied the album" -- removing then would
    # wipe (and re-download) the whole frame. Skip removals in that case; a
    # genuinely emptied album is cleared intentionally with reset.py.
    if not want and to_remove:
        log(f"! album returned 0 assets but frame has {len(to_remove)} ic- items; "
            f"skipping removals (run reset.py to clear intentionally)")
        to_remove = set()
    for aid in to_remove:
        if DRY_RUN:
            log(f"would remove {aid}")
            removed += 1
            continue
        frame.remove_asset(aid)
        removed += 1
        log(f"- {aid}")

    # The slideshow app caches its playlist at process start and ignores our
    # external DB writes, so new/removed photos only show after a restart. Kick
    # it once per cycle that actually changed something (keeps the screen steady
    # on the common no-op cycle).
    if not DRY_RUN and (added or removed):
        frame.refresh_app()
        log("refreshed slideshow app to load DB changes")

    # Real current count: re-query after a live cycle; in dry-run nothing
    # changed, so report what the frame holds now (len(have)), not the plan.
    total_ic = len(have) if DRY_RUN else len(frame.asset_ids(prefix=ID_PREFIX))
    state.update(frame_ic_count=total_ic, last_added=added, last_removed=removed,
                 last_error=None)
    state.mark_synced()
    log(f"sync done: album={len(album['assets'])} added={added} removed={removed} "
        f"ic_total={total_ic}{' (dry-run)' if DRY_RUN else ''}")


def _handle_signal(signum, _frame):
    log(f"received signal {signum}, shutting down")
    _stop.set()


def main():
    if not ALBUM:
        sys.exit("ALBUM_URL is required (the public iCloud Shared Album link)")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle_signal)

    state = State(INTERVAL)
    state.update(frame_host=FRAME_HOST)
    serve(state, STATUS_ADDR, STATUS_PORT)
    log(f"skylight-icloud-sync: album -> {FRAME_HOST} every {INTERVAL}s; "
        f"status on :{STATUS_PORT}/status{'; DRY_RUN' if DRY_RUN else ''}")

    frame = Frame(FRAME_HOST)
    while not _stop.is_set():
        try:
            sync_once(frame, state)
        except Exception as e:
            state.update(frame_reachable=False, last_error=str(e))
            log("sync error:", e)
        if INTERVAL <= 0:
            break
        _stop.wait(INTERVAL)  # wakes immediately on a shutdown signal
    log("stopped")


if __name__ == "__main__":
    main()
