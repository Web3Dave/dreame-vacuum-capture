# Dreame Vacuum Camera Capture — Home Assistant Add-on

Captures a still snapshot from your own Dreame vacuum's onboard camera, authenticated
with your own Dreame account credentials — no phone, no manual network capture.

This is the result of reverse-engineering the Dreamehome app's camera-activation and
P2P streaming protocol (Tencent's XP2P/LiteAV SDK) for personal use. It talks to
Dreame's own cloud API and Tencent's P2P relay using the same requests the real app
makes, authenticated as you, then pulls one JPEG frame out of the live feed via
`ffmpeg` and drops it in Home Assistant's Media folder.

**This only works for a device and account you own.** Don't point it at anyone else's
vacuum or account. The signing algorithm and API endpoints this relies on are
undocumented and reverse-engineered — Dreame could change them at any time without
notice.

## How it works

1. Logs into your Dreame account
2. Runs the camera-activation sequence the app performs when you enter the camera's
   4-digit PIN (open a stream session → verify the PIN → start the video encoder)
3. Fetches a fresh `xp2p_info` P2P session credential from Dreame's backend
4. Runs Tencent's actual XP2P SDK binary (the real, official build — not a
   reimplementation) to negotiate the P2P/relay connection and expose a local FLV feed
5. Grabs one frame via `ffmpeg` and saves it to `/media/dreame-capture/`

Everything runs inside a single add-on container — no nested Docker, no separate VM.

## Installation

### 1. Add this repository to Home Assistant

> **Note:** Add-ons were renamed to **Apps** in Home Assistant 2026.2. The steps are
> the same either way.

1. In Home Assistant, go to **Settings → Apps** (older versions: **Settings → Add-ons**).
2. Click **Install app** to open the App Store, then click the **⋮** menu (top-right)
   and choose **Repositories**.
3. Paste this repository URL and click **Add**:
   ```
   https://github.com/Web3Dave/dreame-vacuum-video-capture
   ```
4. Refresh the page. **Dreame Vacuum Camera Capture** will appear in the store.

### 2. Install and configure

Click **Dreame Vacuum Camera Capture → Install**, then go to the **Configuration** tab:

| Option | Description |
|---|---|
| `account_username` | Your Dreame account login (same as the Dreamehome app) |
| `account_password` | Your Dreame account password |
| `account_country` | The region your account is registered in (check Settings → Region in the app — most EU accounts use `eu`) |
| `four_digit_code` | The camera privacy PIN you set in the app for this vacuum |
| `product_id` | See below |
| `device_name` | See below |

Click **Save**, then switch to the **Info** tab and click **Start**. Check the **Log**
tab to confirm it started cleanly.

#### Finding `product_id` and `device_name`

These identify your specific vacuum model/unit and aren't exposed anywhere in the
app's UI. The easiest way to find them once: capture your phone's HTTPS traffic with
mitmproxy while opening the vacuum's live camera view, and look at any POST to
`applog.iotcloud.tencentiotcloud.com/api/xp2p_ops/applog` — the JSON body includes
both `"ProductId"` and `"DeviceName"` fields directly.

### 3. Wire up a service and a camera entity

The add-on itself only exposes a local HTTP API — it doesn't register HA entities or
services directly. Add this to your `configuration.yaml` (replace the IP with your
actual Home Assistant host if calling from elsewhere, `localhost` works if
Home Assistant Core and Supervisor share the host network):

```yaml
rest_command:
  dreame_capture_photo:
    url: "http://localhost:8099/capture"
    method: POST
    timeout: 30

camera:
  - platform: generic
    name: Dreame Vacuum Camera
    still_image_url: "http://localhost:8099/latest.jpg"
```

Restart Home Assistant. You'll now have:
- A callable service, **`rest_command.dreame_capture_photo`** — call it from
  Developer Tools → Actions, or from an automation, to trigger a new capture.
- A **camera entity**, `camera.dreame_vacuum_camera`, showing the most recent
  snapshot (refreshes each time you call the service above).

Every snapshot is also saved under **Media → dreame-capture** in Home Assistant's
Media Browser (`/media/dreame-capture/snapshot_<timestamp>.jpg`), so you keep a
timestamped history, not just the latest frame.

## Files

- `dreame_capture/config.yaml` — add-on manifest (options schema, port, media mount)
- `dreame_capture/Dockerfile` — multi-stage build (Ubuntu 20.04 builder, matching the
  toolchain Tencent's vendored static libraries were built with — a newer GCC will
  fail to link them)
- `dreame_capture/app.py` — the HTTP API: login → activation sequence → xp2p_info →
  P2P client → ffmpeg snapshot
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
- If a capture times out waiting for a stream URL, check the add-on log — the
  activation sequence returns a `code` for each step, and a non-zero code usually
  means a wrong PIN, wrong `product_id`/`device_name`, or an expired session.
- The `xp2p_info` credential appears to be a longer-lived, cached value rather than
  single-use, but the add-on re-runs the full activation sequence on every capture
  for reliability, since the video encoder needs to be actively started each time.
- `secrets` never touch disk as a file here — credentials live in HA's add-on
  options store (`/data/options.json` inside the container), not a repo-tracked file.
