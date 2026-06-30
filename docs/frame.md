# The frame — state, mods, recovery

The Skylight Frame is a rooted Android device we drive directly over network-ADB.
Everything below is persistent on the frame itself; the sync service
(`docker/icloud-frame-sync`) just reconciles photos into it.

## Facts
| thing | value |
|-------|-------|
| Frame IP | set via `FRAME_HOST` (e.g. `192.168.1.50:5555`) — **set a DHCP reservation** |
| Frame model / OS | D106 (Skylight Frame) · Android 7.1.2 · Rockchip RK3126C · userdebug |
| ADB | network: `adb connect 192.168.1.50:5555` → root shell (no `adb root` needed) |
| Slideshow app | `com.skylight` |
| Photo DB | `/data/data/com.skylight/databases/skylight_v2.db` → table `SlideshowAsset` |
| Media files | `…/com.skylight/files/pictures/` — photos `image-<id>.jpg`; videos `video-<id>.mp4` + poster `video-{small,full}-thumbnail-<id>.jpg` |
| Our injected ids | `ic-<sanitized CloudKit photoGuid>` — only these are ever touched |
| Album | public iCloud Shared Album — link set via `ALBUM_URL` in `.env` |

> For the DHCP reservation, get the frame's MAC from your router's client list.

## Persistent mods already applied to the frame (one-time)
- **Rooted ADB** — userdebug build, shell is uid 0.
- **Persistent network-ADB** — `service.adb.tcp.port=5555` in `/system/build.prop`,
  so port 5555 auto-opens on every boot.
- **Cloud + trackers blocked** in `/system/etc/hosts`: `app.ourskylight.com`,
  `*.launchdarkly.com`, `api2.amplitude.com`, `cdn.onesignal.com`, `*.sentry.io`.
  The slideshow renders **locally** with the cloud blocked (photos live on the
  frame's `/data`), so it works fully offline.
- **Our CA** at `/system/etc/security/cacerts/da4eea83.0` — installed during
  recon, not used by the current setup; harmless, can be removed when reverting.

## Recovery / troubleshooting
- **ADB won't connect (5555 closed)** — the frame rebooted and persistent-ADB
  didn't hold. Recover once over USB: `adb tcpip 5555` (build.prop should prevent
  recurrence).
- **`device offline`** — usually two ADB masters fighting for the frame. Only one
  syncer should run. If it persists, reboot the frame's ADB / power-cycle it.
- **Frame IP changed** — update `FRAME_HOST` in `.env` and
  `docker compose up -d`. Fix permanently with a DHCP reservation.
- **Slideshow shows a setup/error screen** (not photos) — the cloud block is too
  aggressive for this app build. Remount rw and drop the `app.ourskylight.com`
  line (photos are local, so they keep working):
  ```
  adb shell "mount -o rw,remount /system; sed -i '/app.ourskylight.com/d' /system/etc/hosts"
  ```
- **Photos vanish** — only if the cloud becomes reachable and reconciles them
  away; keep it blocked. The sync loop also re-adds them next poll (self-healing).
- **HEIC** — shared albums may serve JPEG or HEIC originals. Android 7 may not
  render HEIC; add JPEGs, or extend the fetcher to pick a JPEG derivative.
- **Video** — the sync pushes Apple's H.264 mp4 derivative (the Rockchip chip
  hardware-decodes it; the HEVC original is never used) and inserts an
  `assetType='video'` row. The slide's still is the iCloud poster written as
  `video-small-thumbnail-<id>.jpg` and referenced by the row's `smallThumbnail`
  column. Skylight paywalls video in the app, but the local renderer plays it.
  ExoPlayer logs a harmless "Format exceeds selected codec's capabilities" warning
  (Rockchip under-reports OMX caps) and decodes 720p fine anyway.

## Reverting the frame mods (if ever undoing this)
Remount `/system` rw, then remove: the CA (`da4eea83.0`), the `/system/etc/hosts`
lines, and the `service.adb.tcp.port` line in `build.prop`. Run the sync service's
`reset.py` to drop the injected `ic-` photos.
