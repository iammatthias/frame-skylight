# skylight-icloud-sync

Drive a (rooted) Skylight Frame from a **public iCloud Shared Album**. A small
container polls the album and reconciles it into the frame's local slideshow DB
over network-ADB. Add a photo or video on your iPhone → it appears on the frame.
No Skylight cloud, no subscription — **including video**, which the Skylight app
paywalls but the on-device slideshow renderer plays for free.

```
 iCloud Shared Album  --CloudKit Web Services-->  [ container ]  --adb tcpip-->  Skylight Frame
 (you add photos)                                 (Docker host)                  (com.skylight slideshow)
```

Runs as its own standalone Docker project on any host that can reach the frame
over the LAN. Stdlib Python + the `adb` client — no other dependencies.

## What it does
- `icloud_album.py` — resolves the album via **CloudKit Web Services**
  (`ckdatabasews`: `resolve` → `records/query CPLAssetAndMasterByAddedDate` →
  download URLs). Works with the public `photos.icloud.com/shared/album/<token>`
  link directly, and pages through the whole album. Photos use the original;
  **videos use Apple's H.264 mp4 derivative** (`resVidMedRes` ~720p, else
  `resVidSmallRes`) plus a JPEG poster (`resJPEGMedRes`) — never the huge HEVC
  original the frame can't hardware-decode.
- `frame.py` — `adb push` the file + `INSERT` a `SlideshowAsset` row (`pathToAsset`
  → the local file), with connect retries for flaky network-ADB. Asset id =
  `ic-<sanitized photoGuid>`; idempotent, and only its own `ic-` rows are touched.
  Photos push as `image-<id>.jpg`; **videos push as `video-<id>.mp4` with
  `assetType='video'` and a poster `video-{small,full}-thumbnail-<id>.jpg`** so the
  slide shows a still. Restarts the slideshow app after a changed cycle so new
  items actually appear.
- `sync.py` — every `POLL_INTERVAL`s: add new album photos/videos, remove deleted
  ones. Self-healing (a re-appearing/removed row is reconciled next cycle),
  graceful on `SIGTERM`, and serves a `/status` endpoint.
- `status.py` — in-process HTTP `/status` (JSON; 200 healthy / 503 not), which the
  container HEALTHCHECK and any LAN/tailnet probe read.

## Deploy
Clone the repo, then from `docker/icloud-frame-sync/`:
```bash
cp .env.example .env          # set ALBUM_URL (+ FRAME_HOST for your frame)
docker compose up -d --build
docker compose logs -f        # expect: "sync done: album=N added=… removed=…"
curl -s localhost:8780/status # JSON health snapshot
```
Update later with `git pull && docker compose up -d --build`. Host networking is
required so `adb` reaches the frame on the LAN; with host net the `/status` port
binds straight onto the host (no published ports).

## Config (env / `.env`)
| var | default | meaning |
|-----|---------|---------|
| `ALBUM_URL` | — | public album link, `photos.icloud.com/shared/album/<token>` |
| `FRAME_HOST` | `192.168.1.50:5555` | frame LAN IP : adb port (set a DHCP reservation) |
| `POLL_INTERVAL` | `300` | reconcile interval (s); `0` = run once and exit |
| `DRY_RUN` | _(off)_ | `1` to log the plan without writing to the frame |
| `STATUS_ADDR` / `STATUS_PORT` | `0.0.0.0` / `8780` | `/status` bind |

## Operations
- **Add/remove photos**: edit the iCloud album; reflects within `POLL_INTERVAL`.
- **Health/observability**: `GET /status` (`:8780`) or
  `docker inspect --format '{{.State.Health.Status}}' skylight-icloud-sync`.
- **Logs**: `docker compose logs -f` (capped at 10m×3 via the compose `x-logging`).
- **Dry run** (plan only): `DRY_RUN=1` in `.env`, `docker compose up -d`.
- **Clean re-sync**: `FRAME_HOST=… python3 reset.py` (drops only `ic-` rows), then
  restart the service to repopulate from the album.

## Tests
Offline unit tests (no network, no adb) cover album-link parsing, full-album
pagination, CloudKit record → photo/video mapping (derivative + poster
selection), frame SQL escaping + id sanitization, video file/thumbnail naming,
the slideshow-refresh trigger, and the status health state machine:
```bash
python3 -m unittest tests
```

## One-off / debug (no Docker)
```bash
# single reconcile against a frame:
ALBUM_URL='…/shared/album/<token>' FRAME_HOST=192.168.1.50:5555 POLL_INTERVAL=0 python3 sync.py
# just list what the album exposes:
python3 icloud_album.py '…/shared/album/<token>'
```

## Notes
- The frame renders the slideshow **locally** with the Skylight cloud blocked
  (photos live on the frame's `/data`), so it works offline.
- The slideshow app reads its playlist once at startup, so the sync restarts
  `com.skylight` after any cycle that added or removed items.
- **Safety valve**: a cycle whose album fetch returns *zero* assets is treated as
  an anomaly (token/API hiccup), not a mass delete — removals are skipped so a
  transient glitch can't wipe the frame. Clear the frame intentionally with
  `reset.py`.
- HEIC: if the album serves HEIC originals (`resOriginalRes`), Android 7 may not
  render them — add JPEGs or extend the fetcher to pick a JPEG derivative.
- **Video**: Apple serves an H.264 mp4 derivative that the frame's Rockchip chip
  hardware-decodes; the renderer plays any `assetType='video'` row (no Skylight
  subscription). The slide's still comes from the iCloud poster, written as
  `video-small-thumbnail-<id>.jpg` and pointed at by the row's `smallThumbnail`
  column — without it the app's Glide loader has nothing to show. Audio is muted
  by the app on the slideshow. The HEVC original is never downloaded.
