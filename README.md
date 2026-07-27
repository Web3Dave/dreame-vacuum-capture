# Dreame Vacuum Camera Capture — Home Assistant Add-on (backend)

The backend engine behind the **Dreame Vacuum Camera Capture** Home Assistant
integration ([dreame-vacuum-capture-integration](https://github.com/Web3Dave/dreame-vacuum-capture-integration)).
It talks to Dreame's cloud API and Tencent's XP2P SDK to pull snapshots and RTSP
streams from your vacuum's onboard camera, using your own Dreame account credentials.

**This add-on is not meant to be used directly.** It has no credentials or device
config of its own - install the companion integration, which drives this add-on's
HTTP API on your behalf (login, device discovery, per-device entities). This add-on
just needs a shared secret token so only that integration can call it.

This is the result of reverse-engineering the Dreamehome app's camera-activation and
P2P streaming protocol (Tencent's XP2P/LiteAV SDK) for personal use.

**This only works for a device and account you own.** Don't point it at anyone else's
vacuum or account. The signing algorithm and API endpoints this relies on are
undocumented and reverse-engineered — Dreame could change them at any time without
notice.

## How it works

Every request to this add-on supplies its own Dreame account credentials and a
target device (`did`) - the add-on itself is stateless about identity. For each
request it:

1. Logs into the given Dreame account
2. Resolves the vacuum's Tencent XP2P `product_id`/`device_name` automatically via
   Dreame's own `getIdentity` endpoint - the same call the app makes
3. Runs the camera-activation sequence the app performs when you enter the camera's
   4-digit PIN (open a stream session → verify the PIN → start the video encoder)
4. Fetches a fresh `xp2p_info` P2P session credential from Dreame's backend
5. Runs Tencent's actual XP2P SDK binary (the real, official build — not a
   reimplementation) to negotiate the P2P/relay connection and expose a local FLV feed

From there, depending on the endpoint called:
- **`/capture`** grabs one frame via `ffmpeg`, saves it under `/media/dreame-capture/<did>/`,
  and tears the whole session down immediately.
- **`/stream/start`** instead republishes that local feed as RTSP (via a bundled
  [MediaMTX](https://github.com/bluenviron/mediamtx) server) at
  `rtsp://<host>:8554/<did>`, and keeps it running until `/stream/stop` is called.

Everything runs inside a single add-on container — no nested Docker, no separate VM.

## HTTP API

Every endpoint except `/health` requires an `X-Api-Token` header matching the
add-on's configured `api_token`.

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/health` | - | Liveness check, no auth |
| POST | `/devices` | `{username, password, country}` | Lists every device on the account |
| POST | `/capture` | `{username, password, country, four_digit_code, did}` | One-shot snapshot |
| POST | `/stream/start` | `{username, password, country, four_digit_code, did}` | Starts an RTSP stream for `did`, returns `{rtsp_url}` |
| POST | `/stream/stop` | `{did}` | Stops the stream for `did` |
| GET | `/stream/status?did=` | - | `{running: bool}` |
| GET | `/latest.jpg?did=` | - | Serves the most recent snapshot for `did` |

## Installation

> **Note:** Add-ons were renamed to **Apps** in Home Assistant 2026.2. The steps are
> the same either way.

1. In Home Assistant, go to **Settings → Apps** (older versions: **Settings → Add-ons**).
2. Click **Install app** to open the App Store, then click the **⋮** menu (top-right)
   and choose **Repositories**.
3. Paste this repository URL and click **Add**:
   ```
   https://github.com/Web3Dave/dreame-vacuum-video-capture
   ```
4. Refresh the page, install **Dreame Vacuum Camera Capture**.
5. On the **Configuration** tab, set `api_token` to any random string (e.g.
   `openssl rand -hex 32`). You'll enter this same value into the companion
   integration's setup flow.
6. Start the add-on and check the **Log** tab for a clean startup.
7. Install the [companion integration](https://github.com/Web3Dave/dreame-vacuum-capture-integration)
   - that's where your Dreame credentials, region, PIN, and device selection actually
   go, and where the camera entities get created.

## Files

- `dreame_capture/config.yaml` — add-on manifest (`api_token` option, HTTP + RTSP
  ports, media mount)
- `dreame_capture/Dockerfile` — multi-stage build (Ubuntu 20.04 builder, matching the
  toolchain Tencent's vendored static libraries were built with — a newer GCC will
  fail to link them) plus a bundled MediaMTX RTSP server
- `dreame_capture/run.sh` — starts MediaMTX in the background, then the HTTP API in
  the foreground
- `dreame_capture/mediamtx.yml` — minimal MediaMTX config (RTSP only, dynamic paths)
- `dreame_capture/app.py` — the HTTP API described above
- `dreame_capture/dreame_sign.py` — the reverse-engineered Dreame request-signing
  algorithm
- `dreame_capture/dreame_lib/` — a trimmed-down copy of the Dreame Home Assistant
  integration's cloud-protocol client (login, device discovery, signed API calls) —
  just the network layer
- `dreame_capture/pc_client/` — Tencent's real, publicly-published XP2P SDK static
  libraries (`tencentyun/iot-p2p-build` on GitHub) plus a small C wrapper
  (`p2p_sample.c`) from Tencent's own reference sample

## Notes / troubleshooting

- **x86_64 only.** Tencent only publishes Linux static libraries for this SDK as
  x86_64 (`-DSYSTEM_ARCH=x86` in their own CI) — there's no ARM64 build available
  upstream, so this won't run on Raspberry Pi / HA Green / HA Yellow.
- If a request times out waiting for a stream URL, check the add-on log — the
  activation sequence returns a `code` for each step, and a non-zero code usually
  means a wrong PIN or an expired session.
- The `xp2p_info` credential appears to be a longer-lived, cached value rather than
  single-use, but the add-on re-runs the full activation sequence on every
  `/capture`/`/stream/start` call for reliability, since the video encoder needs to
  be actively started each time.
- Credentials are never persisted by this add-on - they arrive per-request from the
  integration and are only ever held in memory for the duration of that request.
