# skylight-icloud-sync

Drive a (rooted) Skylight Frame from a **public iCloud Shared Album**. A small
container polls the album and reconciles it into the frame's local slideshow DB
over network-ADB. Add a photo on your iPhone → it appears on the frame. No
Skylight cloud, no subscription.

```
 iCloud Shared Album  --CloudKit Web Services-->  [ container ]  --adb tcpip-->  Skylight Frame
 (you add photos)                                 (Beelink, amd64)               (com.skylight slideshow)
```

Runs on the **Beelink** (amd64) as its own standalone Docker project, separate
from farfield. Stdlib Python + the `adb` client; it talks to the frame over the
LAN, so it can live on any host that can reach the frame.

## What it does
- `icloud_album.py` — resolves the album via **CloudKit Web Services**
  (`ckdatabasews`: `resolve` → `records/query CPLAssetAndMasterByAddedDate` →
  `resOriginalRes` download URLs). Works with the public
  `photos.icloud.com/shared/album/<token>` link directly.
- `frame.py` — `adb push` the JPG + `INSERT` a `SlideshowAsset` row (`pathToAsset`
  → the local file), with connect retries for flaky network-ADB. Asset id =
  `ic-<sanitized photoGuid>`; idempotent, and only its own `ic-` rows are touched.
- `sync.py` — every `POLL_INTERVAL`s: add new album photos, remove deleted ones.
  Self-healing (a re-appearing/removed row is reconciled next cycle), graceful on
  `SIGTERM`, and serves a `/status` endpoint.
- `status.py` — in-process HTTP `/status` (JSON; 200 healthy / 503 not), which the
  container HEALTHCHECK and any LAN/tailnet probe read.

## Deploy (Beelink)
Cloned at `~/projects/frame-skylight` on the Beelink
(`github.com/iammatthias/frame-skylight`). From `docker/icloud-frame-sync/`:
```bash
cp .env.example .env          # set ALBUM_URL (+ FRAME_HOST if it moved)
docker compose up -d --build
docker compose logs -f        # expect: "sync done: album=N added=… removed=…"
curl -s localhost:8780/status # JSON health snapshot
```
Update later with `git pull && docker compose up -d --build`. Host networking is
required so `adb` reaches the frame on the LAN; with host net the `/status` port
binds straight onto the Beelink (no published ports).

## Config (env / `.env`)
| var | default | meaning |
|-----|---------|---------|
| `ALBUM_URL` | — | public album link, `photos.icloud.com/shared/album/<token>` |
| `FRAME_HOST` | `192.168.50.72:5555` | frame LAN IP : adb port (set a DHCP reservation) |
| `POLL_INTERVAL` | `300` | reconcile interval (s); `0` = run once and exit |
| `DRY_RUN` | _(off)_ | `1` to log the plan without writing to the frame |
| `STATUS_ADDR` / `STATUS_PORT` | `0.0.0.0` / `8780` | `/status` bind |

## Operations
- **Add/remove photos**: edit the iCloud album; reflects within `POLL_INTERVAL`.
- **Health/observability**: `GET /status` on the Beelink (`:8780`) or
  `docker inspect --format '{{.State.Health.Status}}' skylight-icloud-sync`.
- **Logs**: `docker compose logs -f` (capped at 10m×3 via the compose `x-logging`).
- **Dry run** (plan only): `DRY_RUN=1` in `.env`, `docker compose up -d`.
- **Clean re-sync**: `FRAME_HOST=… python3 reset.py` (drops only `ic-` rows), then
  restart the service to repopulate from the album.

## Tests
Offline unit tests (no network, no adb) cover album-link parsing, CloudKit
record → photo mapping, frame SQL escaping + id sanitization, and the status
health state machine:
```bash
python3 -m unittest tests
```

## One-off / debug (no Docker)
```bash
# single reconcile against a frame:
ALBUM_URL='…/shared/album/<token>' FRAME_HOST=192.168.50.72:5555 POLL_INTERVAL=0 python3 sync.py
# just list what the album exposes:
python3 icloud_album.py '…/shared/album/<token>'
```

## Notes
- The frame renders the slideshow **locally** with the Skylight cloud blocked
  (photos live on the frame's `/data`), so it works offline — verified.
- HEIC: the album currently serves JPEG originals (`resOriginalRes`). If you ever
  add HEIC originals, Android 7 may not render them — add JPEGs or extend the
  fetcher to pick a JPEG derivative.
