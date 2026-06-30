# frame-skylight

Drive a rooted **Skylight Frame** from a **public iCloud Shared Album** — no
Skylight cloud, no subscription, no trackers. Add a photo on your iPhone and it
shows up on the frame within a poll interval.

```
 iCloud Shared Album  --CloudKit Web Services-->  [ icloud-frame-sync ]  --adb tcpip-->  Skylight Frame
 (you add photos)                                 (Beelink, Docker)                      (com.skylight slideshow)
```

A small container polls the album and reconciles photos into the frame's local
slideshow DB over network-ADB. It runs on the **Beelink** as its own standalone
Docker project (separate from farfield), reaching the frame across the LAN.

## Repo layout
- **`docker/icloud-frame-sync/`** — the service: the sync daemon, Dockerfile,
  compose, tests, and its own [README](docker/icloud-frame-sync/README.md). This
  is the only thing you deploy.
- **`docs/frame.md`** — the frame itself: its persistent mods, ADB/recovery, and
  how to revert. Read this if the frame misbehaves.

## Quick start
On the Beelink, from `docker/icloud-frame-sync/`:
```bash
cp .env.example .env           # set ALBUM_URL (+ FRAME_HOST if it moved)
docker compose up -d --build
curl -s localhost:8780/status  # JSON health snapshot
```
Add/remove photos by editing the iCloud album — the frame reflects it within
`POLL_INTERVAL`. Full config, ops, and tests are in the service README.

## Status
Live on the Beelink (`~/projects/frame-skylight`). The frame is reconciled to the
album; health is at `http://<beelink>:8780/status`. Deploys are `git pull` +
`docker compose up -d --build` (see the service README).
