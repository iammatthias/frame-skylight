"""Container HEALTHCHECK: probe the local /status endpoint.

Exit 0 when /status returns 200 (last sync healthy), 1 otherwise. Stdlib only,
so the image needs no curl.
"""
import os
import sys
import urllib.request

port = os.environ.get("STATUS_PORT", "8780")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
