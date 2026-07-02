"""Tiny stdlib HTTP status surface for the sync daemon.

The sync is a background loop with no web UI, but a deployed service should still
expose its health. `State` holds a snapshot of the last cycle; `serve` runs a
threaded HTTP server that answers:

    GET /status   -> JSON snapshot; 200 when healthy, 503 when not.

"Healthy" = the last cycle finished without a fatal error and recently enough
(within ~2 poll intervals). During the very first cycle there is a grace window
so the container isn't killed before its first sync completes. The container
HEALTHCHECK and any LAN/tailnet probe read the same endpoint.
"""
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _iso(epoch):
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    """Thread-safe holder for the latest sync snapshot."""

    def __init__(self, poll_interval):
        self._lock = threading.Lock()
        self._started = time.time()
        # A cycle is "fresh" if it finished within this many seconds.
        self._stale_after = max(poll_interval * 2 + 60, 180)
        self._d = {
            "service": "skylight-icloud-sync",
            "poll_interval": poll_interval,
            "frame_host": None,
            "frame_reachable": False,
            "album": None,
            "album_count": None,
            "album_photos": None,
            "album_videos": None,
            "frame_ic_count": None,
            "last_added": 0,
            "last_removed": 0,
            "last_error": None,
            "cycles": 0,
        }
        self._last_sync = None  # epoch of the last completed cycle

    def update(self, **kw):
        with self._lock:
            self._d.update(kw)

    def mark_synced(self):
        with self._lock:
            self._last_sync = time.time()
            self._d["cycles"] += 1

    def snapshot(self):
        with self._lock:
            d = dict(self._d)
            last_sync = self._last_sync
            started = self._started
            stale_after = self._stale_after
        now = time.time()
        if last_sync is None:
            # No cycle yet — healthy only inside the startup grace window.
            healthy = (now - started) < stale_after and d["last_error"] is None
        else:
            healthy = d["last_error"] is None and (now - last_sync) < stale_after
        d["healthy"] = healthy
        d["last_sync"] = _iso(last_sync)
        d["started_at"] = _iso(started)
        d["uptime_s"] = int(now - started)
        return d


def serve(state, addr, port):
    """Start the status HTTP server on a daemon thread and return it."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] not in ("/status", "/", "/healthz"):
                self.send_response(404)
                self.end_headers()
                return
            snap = state.snapshot()
            body = json.dumps(snap, indent=2).encode() + b"\n"
            self.send_response(200 if snap["healthy"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # keep status probes out of the container log
            pass

    httpd = ThreadingHTTPServer((addr, port), Handler)
    t = threading.Thread(target=httpd.serve_forever, name="status", daemon=True)
    t.start()
    return httpd
