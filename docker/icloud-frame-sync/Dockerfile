# amd64 (Beelink) — stdlib Python + adb client only.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends adb \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY icloud_album.py frame.py status.py sync.py healthcheck.py /app/

# No pip deps — everything is stdlib + the adb binary.
# Healthcheck probes the in-process /status endpoint (200 = last sync healthy).
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD ["python", "/app/healthcheck.py"]

CMD ["python", "-u", "sync.py"]
