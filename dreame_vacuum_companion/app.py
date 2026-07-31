#!/usr/bin/env python3
"""
Dreame Vacuum Camera Capture - Home Assistant add-on HTTP API.

Stateless w.r.t. Dreame account identity: every request supplies its own
credentials/did. The only thing this add-on itself is configured with is
`api_token`, a shared secret required on every request (see README.md) -
this is meant to be called by a companion Home Assistant integration, not
used directly by end users.

POST /devices       {username, password, country}
                    -> discovers every device on the account
POST /capture       {username, password, country, four_digit_code, did,
                     tag?}
                    -> one-shot: activation sequence -> grab one JPEG frame
                       -> /media/dreame-capture/snapshots/<tag>/<ts>.jpg
                          plus latest.jpg in the same folder -> tear down
POST /stream/start  {username, password, country, four_digit_code, did}
                    -> activation sequence -> keeps the P2P session alive,
                       republishing it as RTSP via a bundled MediaMTX server
POST /stream/stop   {did}
                    -> tears down a stream started above
GET  /stream/status?did=...
GET  /latest.jpg?did=...
POST /runs          {did, command} -> {id}
                    -> opens an errand record for the UI's Activity page
POST /runs/<id>/steps   {text}          -> appends a step while it runs
POST /runs/<id>/finish  {ok, summary, detail}
GET  /runs?did=&limit=
GET  /snapshots?tag=&limit=
                    -> what has been captured, newest first
GET  /snapshots/<tag>/<file>
POST /map           multipart: did, meta (json), image (png)
                    -> the rendered map used to pick coordinates
GET  /map/<did>     -> geometry: origin, grid_size, scale, size
GET  /map/<did>/document -> the grid itself, for a client that renders it
GET  /map/<did>.png
GET  /health        (no auth - liveness only)
"""
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, send_file, abort, request

sys.path.insert(0, os.path.dirname(__file__))
import steps as step_schema
import store
from dreame_lib.protocol import DreameVacuumProtocol
from dreame_sign import sign_params

OPTIONS_PATH = "/data/options.json"
MEDIA_ROOT = "/media/dreame-capture"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
P2P_BINARY = os.path.join(SCRIPT_DIR, "p2p_sample")
RTSP_HOST_PORT = 8554
MEDIAMTX_API = "http://127.0.0.1:9997"
STALL_THRESHOLD_SECONDS = 15

# The device treats KEEP_ALIVE as "an app is currently watching me". It lapses
# on its own if not refreshed, and when it does the device stops sending
# non-essential data to the cloud - which includes the camera feed. The real
# app refreshes it continuously while open; the Dreame HA integration does the
# same on a 25s timer. Note the camera-service variant (siid 10001/piid 6) is
# NOT implemented on this vacuum (returns code -1), but this general one is.
SIID_KEEP_ALIVE = 14
PIID_DEVICE_KEEP_ALIVE = 4
KEEP_ALIVE_INTERVAL_SECONDS = 20

SIID_CAMERA_SERVICE = 10001
AIID_STREAM_CODE = 4
# The device stops sending video ~60s after activation unless the client keeps
# telling it someone is watching. Recovered verbatim from the app's own
# downloadable vacuum plugin bundle (Monitor model):
#
#   SIID = 10001
#   PIID = { TAKE_PHOTO: 5, KEEP_ALIVE: 6, GET_PROPERTY: 99, PERSON_DATA: 110 }
#   AIID = { CAMERA_OPERATE: 1, VOICE_OPERATE: 2, PROPERTY_OPERATE: 3,
#            ACCESS_CODE_OPERATE: 4, VIDEO_VENDOR: 7 }
#
#   checkAlive(videoStatus) ->
#     sendAction(AIID.CAMERA_OPERATE, PIID.KEEP_ALIVE,
#                {operType: 'keep_alive', videoStatus: videoStatus})
#   ...on setInterval(..., 20000)   // 20s for the Tencent path
#
# A healthy reply carries out[0].value == 'ok'. Note it goes through
# CAMERA_OPERATE (aiid 1), NOT PROPERTY_OPERATE - and reading siid 10001/piid 6
# as a plain property just returns -1, which is what made it look unsupported.
AIID_CAMERA_OPERATE = 1
PIID_CAMERA_KEEP_ALIVE = 6
KEEP_ALIVE_VIDEO_STATUS = "opened"
AIID_STREAM_VIDEO = 1
PIID_STREAM_CODE_OPEN = 1100
PIID_STREAM_VERIFY_CODE = 1102
PIID_STREAM_VIDEO_TRIGGER = 1

app = Flask(__name__)
os.makedirs(MEDIA_ROOT, exist_ok=True)

# did -> {"p2p_proc": Popen, "ffmpeg_proc": Popen, "live_url": str}
_active_streams = {}
_streams_lock = threading.Lock()


def _addon_options():
    if not os.path.exists(OPTIONS_PATH):
        return {}
    with open(OPTIONS_PATH) as f:
        return json.load(f)


def _stream_timeout_minutes():
    """None means "no timeout" - the stream_timeout_minutes option is
    optional and left unset by default means don't auto-stop.
    """
    value = _addon_options().get("stream_timeout_minutes")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@app.before_request
def _require_token():
    if request.path == "/health":
        return None
    expected = _addon_options().get("api_token")
    if not expected:
        abort(500, "api_token is not configured for this add-on - set it in the add-on's Configuration tab")
    if request.headers.get("X-Api-Token") != expected:
        abort(401, "Missing or incorrect X-Api-Token header")


def _media_dir(did):
    path = os.path.join(MEDIA_ROOT, did)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_tag(value):
    """A filesystem-safe folder name from caller-supplied text.

    Whitelisted rather than escaped: this becomes a path segment, and the
    caller is a network client, so anything resembling "../" must not survive.
    """
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (value or "").strip())
    cleaned = cleaned.strip("_")[:48]
    return cleaned.lower() or "general"


def _snapshot_dir(tag):
    """Snapshots live under a tag rather than per device: a tag like
    'poop_check' is what someone actually looks for, and a did is not."""
    path = os.path.join(MEDIA_ROOT, "snapshots", _safe_tag(tag))
    os.makedirs(path, exist_ok=True)
    return path


def _require_body(*keys):
    body = request.get_json(silent=True) or {}
    missing = [k for k in keys if not body.get(k)]
    if missing:
        abort(400, f"Missing required field(s): {missing}")
    return body


def login(username, password, country):
    protocol = DreameVacuumProtocol(
        username=username, password=password, country=country or "eu",
        prefer_cloud=True, account_type="dreame",
    )
    if not protocol.cloud.login():
        abort(502, "Dreame login failed - check username/password/country")
    return protocol


def list_devices(protocol):
    devices = protocol.cloud.get_devices()
    records = (devices or {}).get("page", {}).get("records", [])
    return [
        {"did": str(d["did"]), "mac": d.get("mac"), "name": d.get("customName") or d.get("model")}
        for d in records
    ]


def connect_device(protocol, did):
    protocol.cloud._did = did
    protocol.connect()


def signed_call(protocol, path, body):
    signed_body, _ = sign_params(body)
    return protocol.cloud._api_call(path, signed_body)


def send_command_url(protocol):
    strings = protocol.cloud._strings
    host = f"-{protocol.cloud._host.split('.')[0]}" if protocol.cloud._host else ""
    return f"{strings[37]}{host}/{strings[27]}/{strings[38]}"


def camera_action(protocol, did, aiid, piid, value):
    req_id = int(time.time() * 1000) % 1000000
    body = {
        "did": did, "id": req_id,
        "data": {
            "did": did, "id": req_id, "method": "action",
            "params": {
                "did": did, "siid": SIID_CAMERA_SERVICE, "aiid": aiid,
                "in": [{"piid": piid, "value": json.dumps(value, separators=(",", ":"))}],
            },
        },
    }
    return signed_call(protocol, send_command_url(protocol), body)


def _check(resp, step):
    ok = resp and resp.get("success") and resp.get("data", {}).get("result", {}).get("code") == 0
    if not ok:
        app.logger.warning("unexpected response for %s: %s", step, resp)
    return ok


def start_camera_session(protocol, did, four_digit_code, product_id, device_name):
    session = uuid.uuid4().hex

    r1 = camera_action(protocol, did, AIID_STREAM_CODE, PIID_STREAM_CODE_OPEN, {"open": True, "session": session})
    _check(r1, "open session")

    oldcode = hashlib.sha256(four_digit_code.encode()).hexdigest()
    r2 = camera_action(protocol, did, AIID_STREAM_CODE, PIID_STREAM_VERIFY_CODE,
                        {"oldcode": oldcode, "lazymode": 0, "session": session})
    _check(r2, "verify PIN")

    r3 = trigger_stream_video(protocol, did, product_id, device_name, session)
    _check(r3, "start video")
    return session


def trigger_stream_video(protocol, did, product_id, device_name, session):
    """The camera-video-session trigger itself (as opposed to a generic
    liveness signal). Re-issuing this with the same session is exactly what
    the watchdog's full-session restart relies on to bring a dead feed back.
    """
    return camera_action(protocol, did, AIID_STREAM_VIDEO, PIID_STREAM_VIDEO_TRIGGER, {
        "token": "tx",
        "channelId": f"{product_id}/{device_name}",
        "operType": "monitor",
        "operation": "start",
        "session": session,
    })


def get_identity(protocol, did):
    """Auto-discover this device's Tencent XP2P product_id/device_name - the app
    fetches these the same way rather than a user ever typing them in.
    """
    resp = signed_call(protocol, "dreame-third-video/tx/mgr/dev/getIdentity", {"did": did, "os": "ios"})
    if not resp or not resp.get("success"):
        abort(502, f"getIdentity failed: {resp}")
    data = resp["data"]["data"]
    return data["productId"], data["deviceName"]


def get_p2p_info(protocol, did):
    resp = signed_call(protocol, "dreame-third-video/tx/dev/getP2PInfo", {"did": did})
    if not resp or not resp.get("success"):
        abort(502, f"getP2PInfo failed: {resp}")
    return resp["data"]["data"]["p2pInfo"]


def write_p2p_config(did, product_id, device_name):
    config_path = f"/tmp/p2p_config_{did}.txt"
    with open(config_path, "w") as f:
        f.write(f"product_id={product_id}\n")
        f.write(f"device_name={device_name}\n")
        f.write("app_id=\n")
        f.write("app_key=\n")
        f.write("lan_host=\n")
        f.write("lan_port=\n")
    return config_path


def start_p2p_client(did, product_id, device_name, p2p_info, timeout=20):
    config_path = write_p2p_config(did, product_id, device_name)
    env = dict(os.environ)
    env["XP2P_INFO"] = p2p_info
    proc = subprocess.Popen(
        [P2P_BINARY, config_path], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )

    line_q = queue.Queue()

    def _reader():
        for line in proc.stdout:
            line_q.put(line)

    threading.Thread(target=_reader, daemon=True).start()

    live_url = None
    deadline = time.time() + timeout
    while time.time() < deadline and live_url is None:
        try:
            line = line_q.get(timeout=0.5)
        except queue.Empty:
            continue
        if line.startswith("LIVE_URL:"):
            url = line.split("LIVE_URL:", 1)[1].strip()
            if url and url != "(null)":
                live_url = url

    return proc, live_url


def run_activation(username, password, country, four_digit_code, did):
    """Shared setup for both /capture and /stream/start: login, resolve identity,
    run the PIN-activation sequence, fetch xp2p_info, start the P2P client.
    Returns (protocol, p2p_proc, live_url).

    The returned `protocol` owns a live MQTT session (subscribed to the
    device's /status/ topic) - the real app keeps this open the whole time
    it's running, which is how it receives continuous position/status
    updates. Long-lived callers (/stream/start) must hold onto it and call
    protocol.disconnect() at teardown; if it's simply dropped, Python GCs it
    and the MQTT session goes away moments after activation.
    """
    protocol = login(username, password, country)
    connect_device(protocol, did)
    product_id, device_name = get_identity(protocol, did)

    session = start_camera_session(protocol, did, four_digit_code, product_id, device_name)
    time.sleep(1)
    p2p_info = get_p2p_info(protocol, did)

    proc, live_url = start_p2p_client(did, product_id, device_name, p2p_info)
    if not live_url:
        proc.terminate()
        _safe_disconnect(protocol)
        abort(504, "Timed out waiting for the P2P client to report a stream URL")
    return {
        "protocol": protocol,
        "p2p_proc": proc,
        "live_url": live_url,
        "session": session,
        "product_id": product_id,
        "device_name": device_name,
    }


def _safe_disconnect(protocol):
    if protocol is None:
        return
    try:
        protocol.disconnect()
    except Exception:
        app.logger.warning("Failed to cleanly disconnect protocol/MQTT session", exc_info=True)


@app.route("/devices", methods=["POST"])
def devices():
    body = _require_body("username", "password")
    protocol = login(body["username"], body["password"], body.get("country", "eu"))
    return jsonify({"success": True, "devices": list_devices(protocol)})


def _grab_frame(input_url, snapshot_path):
    transport_args = ["-rtsp_transport", "tcp"] if input_url.startswith("rtsp://") else []
    result = subprocess.run(
        ["ffmpeg", "-y", *transport_args, "-i", input_url, "-frames:v", "1", snapshot_path],
        capture_output=True, text=True, timeout=15,
    )
    ok = result.returncode == 0 and os.path.exists(snapshot_path)
    return ok, result.stderr


@app.route("/capture", methods=["POST"])
def capture():
    body = _require_body("username", "password", "four_digit_code", "did")
    did = body["did"]

    tag = _safe_tag(body.get("tag"))
    snapshot_dir = _snapshot_dir(tag)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    snapshot_path = os.path.join(snapshot_dir, f"{timestamp}.jpg")
    latest_path = os.path.join(snapshot_dir, "latest.jpg")
    # Also kept per device, because /latest.jpg (and so the camera entity's
    # thumbnail) is addressed by did and knows nothing about categories.
    device_latest = os.path.join(_media_dir(did), "latest.jpg")

    # The vacuum's camera only supports one live encoder session at a time -
    # starting a second one (via run_activation) would kill whatever session
    # /stream/start already has running. And p2p_sample's own local FLV proxy
    # appears to only tolerate one direct HTTP client - opening a second raw
    # connection to it (rather than to MediaMTX) kicks out the republish
    # ffmpeg's existing one, confirmed directly from the add-on log: the
    # republish connection died at the exact moment a /capture grabbed the
    # live_url directly. So if a stream is active, read the frame back via
    # MediaMTX's RTSP output instead (an ordinary reader connection, already
    # proven not to disturb the publisher) rather than the raw feed.
    #
    # If that RTSP read fails, the stream itself is genuinely down and its
    # own watchdog is already working on recovering it - don't start a
    # competing independent session that would fight with that recovery.
    # Only run an independent session when no stream is active at all.
    proc = None
    protocol = None
    owns_session = False
    with _streams_lock:
        existing = _active_streams.get(did)
        stream_active = existing is not None and existing["p2p_proc"].poll() is None
        rtsp_url = existing["rtsp_url"] if stream_active else None

    try:
        if stream_active:
            ok, stderr = _grab_frame(rtsp_url, snapshot_path)
            if not ok:
                abort(502, f"Active stream isn't producing frames right now (it should self-recover shortly): {stderr[-300:]}")
        else:
            owns_session = True
            act = run_activation(
                body["username"], body["password"], body.get("country", "eu"), body["four_digit_code"], did,
            )
            protocol, proc = act["protocol"], act["p2p_proc"]
            ok, stderr = _grab_frame(act["live_url"], snapshot_path)
            if not ok:
                abort(502, f"ffmpeg failed to capture a frame: {stderr[-500:]}")

        with open(snapshot_path, "rb") as src:
            image = src.read()
        for destination in (latest_path, device_latest):
            with open(destination, "wb") as dst:
                dst.write(image)
    finally:
        if owns_session and proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if owns_session:
            _safe_disconnect(protocol)

    _classify_snapshot_async(tag, snapshot_path)

    return jsonify({
        "success": True,
        "path": snapshot_path,
        "latest": latest_path,
        "tag": tag,
        # Relative to the media root, which is what a media-source or www
        # path needs - callers should not have to strip a prefix themselves.
        "media_path": os.path.relpath(snapshot_path, MEDIA_ROOT),
        "latest_media_path": os.path.relpath(latest_path, MEDIA_ROOT),
    })


def _classify_snapshot_async(tag: str, snapshot_path: str) -> None:
    """Run every enabled, trained classification linked to this tag and
    broadcast the result over MQTT.

    Backgrounded rather than run inline: the vacuum's integration is waiting
    on this request, and while TFLite inference itself is fast, an MQTT
    broker that is slow or unreachable must not be the reason a snapshot
    capture takes longer to answer. Nothing here writes to the training
    dataset - that only happens when a person assigns a label by hand, so an
    uncertain guess can never quietly teach the model to repeat itself.
    """
    def run():
        try:
            import classify_infer
            import config_store
            import mqtt_publish

            for classifier in config_store.list_classifiers():
                if not (classifier["enabled"] and classifier["configured"]):
                    continue
                link = next((t for t in classifier["tags"] if t["tag_id"] == tag), None)
                if not link:
                    continue
                result = classify_infer.classify(classifier["id"], snapshot_path, link["crop"])
                if result is None:
                    continue
                label, score = result
                if score < classifier["threshold"]:
                    continue
                mqtt_publish.publish_result(
                    classifier["id"], classifier["name"], classifier["classification_type"],
                    label, score, tag_id=tag, filename=os.path.basename(snapshot_path),
                )
        except Exception:  # noqa: BLE001 - a background task must not crash the process
            app.logger.warning(
                "Classification failed for tag %s (snapshot capture already succeeded)",
                tag, exc_info=True)

    threading.Thread(target=run, daemon=True).start()


def _spawn_ffmpeg_republish(live_url, rtsp_url):
    return subprocess.Popen(
        ["ffmpeg", "-y", "-i", live_url, "-c", "copy", "-f", "rtsp", rtsp_url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _path_inbound_bytes(did):
    """None means "no active publisher on this path" (dead), not "unknown".

    Uses the list endpoint rather than /v3/paths/get/<name>: the latter 404s
    when a path is absent, and MediaMTX logs every one of those at ERR level.
    Since this is polled twice a second while a stream starts, that produced a
    stream of alarming-looking "path not found" errors during entirely normal
    startup. The list endpoint returns 200 with an empty array instead.
    """
    try:
        resp = requests.get(f"{MEDIAMTX_API}/v3/paths/list", timeout=3)
        if resp.status_code != 200:
            return None
        for item in resp.json().get("items") or []:
            if item.get("name") == did:
                return item.get("inboundBytes")
        return None
    except (requests.RequestException, ValueError):
        return None


def _kill(proc):
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _refresh_keep_alive(protocol, did):
    """Returns the device's reported value, or None on failure. The app always
    sends 1; the device answers with its total connected-client count.
    """
    resp = protocol.get_properties([{"did": did, "siid": SIID_KEEP_ALIVE, "piid": PIID_DEVICE_KEEP_ALIVE}])
    value = None
    if resp and isinstance(resp, list) and resp[0].get("code") == 0:
        value = resp[0].get("value")
    if not value:
        protocol.set_property(SIID_KEEP_ALIVE, PIID_DEVICE_KEEP_ALIVE, 1)
    return value


def _keep_alive_loop(did):
    """Without this the video path goes dead after ~60-115s while the P2P
    command channel stays perfectly healthy - the device has simply decided
    nobody is watching and stopped sending. Re-reads the protocol from the
    stream entry each pass, since the watchdog can swap it during a full
    session restart.
    """
    while True:
        with _streams_lock:
            entry = _active_streams.get(did)
            if entry is None:
                return  # stream stopped
            protocol = entry.get("protocol")
            session = entry.get("session")

        if protocol is not None:
            try:
                _refresh_keep_alive(protocol, did)
            except Exception:
                app.logger.warning("KEEP_ALIVE refresh failed for %s", did, exc_info=True)

            # Tell the device someone is still watching, exactly as the app's
            # own checkAlive() does. Without this it stops sending video after
            # ~60s while the P2P channel stays perfectly healthy.
            try:
                resp = camera_action(
                    protocol, did, AIID_CAMERA_OPERATE, PIID_CAMERA_KEEP_ALIVE,
                    {
                        "operType": "keep_alive",
                        "videoStatus": KEEP_ALIVE_VIDEO_STATUS,
                        # sendAction() injects the session into every action
                        # payload; omitting it gets the call rejected (-1).
                        "session": session,
                    },
                )
                result = (resp or {}).get("data", {}).get("result", {}) or {}
                out = result.get("out") or [{}]
                app.logger.warning(
                    "camera keep_alive for %s -> code=%s value=%s",
                    did, result.get("code"), out[0].get("value"),
                )
            except Exception:
                app.logger.warning("camera keep_alive failed for %s", did, exc_info=True)

        time.sleep(KEEP_ALIVE_INTERVAL_SECONDS)


def _ffmpeg_watchdog(did, creds):
    """ffmpeg can hang alive (never exits) when the vacuum's XP2P feed stalls
    mid-stream - observed directly: same PID for minutes with zero new data,
    long after MediaMTX had already torn down the RTSP session as dead. Process
    liveness alone can't detect this, so instead we track MediaMTX's own
    inboundBytes counter for the path and force-kill+respawn ffmpeg if it stops
    advancing for STALL_THRESHOLD_SECONDS.

    Also observed: sometimes respawning ffmpeg alone doesn't help, because
    p2p_proc's own XP2P session has silently gone stale (still alive, but no
    longer relaying data) - not just the ffmpeg leg. If one ffmpeg-only
    respawn in a row still doesn't produce progress, escalate to a full
    session restart (fresh run_activation, new p2p_proc and live_url).
    """
    last_bytes = None
    last_progress = time.time()
    respawns_without_progress = 0

    while True:
        time.sleep(3)
        with _streams_lock:
            entry = _active_streams.get(did)
            if entry is None:
                return  # stream was explicitly stopped
            live_url, rtsp_url = entry["live_url"], entry["rtsp_url"]
            p2p_proc, ffmpeg_proc = entry["p2p_proc"], entry["ffmpeg_proc"]
            started_at = entry["started_at"]
            protocol = entry.get("protocol")

        timeout_minutes = _stream_timeout_minutes()
        if timeout_minutes is not None and time.time() - started_at > timeout_minutes * 60:
            with _streams_lock:
                _active_streams.pop(did, None)
            _kill(ffmpeg_proc)
            _kill(p2p_proc)
            _safe_disconnect(protocol)
            app.logger.warning("Stream for %s auto-stopped after %s minutes (stream_timeout_minutes)", did, timeout_minutes)
            return

        now = time.time()
        inbound = _path_inbound_bytes(did)
        if inbound is not None and inbound != last_bytes:
            last_bytes, last_progress = inbound, now
            respawns_without_progress = 0
            continue

        exited = ffmpeg_proc.poll() is not None
        stalled = now - last_progress > STALL_THRESHOLD_SECONDS
        if not (exited or stalled):
            continue

        if respawns_without_progress >= 1:
            _kill(ffmpeg_proc)
            _kill(p2p_proc)
            try:
                act = run_activation(
                    creds["username"], creds["password"], creds["country"], creds["four_digit_code"], did,
                )
            except Exception:
                app.logger.warning("Full session restart failed for %s, will retry", did, exc_info=True)
                continue
            # The old MQTT session is superseded - drop it only once the
            # replacement is established, so there's no window with none.
            _safe_disconnect(protocol)
            new_ffmpeg = _spawn_ffmpeg_republish(act["live_url"], rtsp_url)
            last_bytes, last_progress, respawns_without_progress = None, time.time(), 0
            with _streams_lock:
                current = _active_streams.get(did)
                if current is None:
                    new_ffmpeg.terminate()
                    act["p2p_proc"].terminate()
                    _safe_disconnect(act["protocol"])
                    return
                current.update({
                    "p2p_proc": act["p2p_proc"], "ffmpeg_proc": new_ffmpeg,
                    "live_url": act["live_url"], "protocol": act["protocol"],
                    "session": act["session"], "product_id": act["product_id"],
                    "device_name": act["device_name"],
                })
            continue

        _kill(ffmpeg_proc)
        new_ffmpeg = _spawn_ffmpeg_republish(live_url, rtsp_url)
        last_bytes, last_progress = None, time.time()
        respawns_without_progress += 1

        with _streams_lock:
            current = _active_streams.get(did)
            if current is None or current["p2p_proc"].poll() is not None:
                new_ffmpeg.terminate()
                if current is not None:
                    _active_streams.pop(did, None)
                    _safe_disconnect(current.get("protocol"))
                return
            current["ffmpeg_proc"] = new_ffmpeg


def _wait_for_path_ready(did, timeout=15):
    """Callers (HA's go2rtc in particular) connect the instant they see a
    success response - if the RTSP path isn't actually publishing yet, that
    shows up as a burst of "no stream is available"/DESCRIBE 404 failures on
    their end. Block here until MediaMTX confirms real data is flowing,
    rather than just "we spawned the process".
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _path_inbound_bytes(did) is not None:
            return True
        time.sleep(0.5)
    return False


@app.route("/stream/start", methods=["POST"])
def stream_start():
    body = _require_body("username", "password", "four_digit_code", "did")
    did = body["did"]

    with _streams_lock:
        existing = _active_streams.get(did)
        if existing and existing["p2p_proc"].poll() is None:
            _wait_for_path_ready(did)
            return jsonify({"success": True, "rtsp_url": existing["rtsp_url"], "already_running": True})

    act = run_activation(
        body["username"], body["password"], body.get("country", "eu"), body["four_digit_code"], did,
    )

    rtsp_url = f"rtsp://127.0.0.1:{RTSP_HOST_PORT}/{did}"
    ffmpeg_proc = _spawn_ffmpeg_republish(act["live_url"], rtsp_url)

    with _streams_lock:
        _active_streams[did] = {
            "p2p_proc": act["p2p_proc"], "ffmpeg_proc": ffmpeg_proc, "rtsp_url": rtsp_url,
            "live_url": act["live_url"], "started_at": time.time(), "protocol": act["protocol"],
            "session": act["session"], "product_id": act["product_id"], "device_name": act["device_name"],
        }
    creds = {
        "username": body["username"], "password": body["password"],
        "country": body.get("country", "eu"), "four_digit_code": body["four_digit_code"],
    }
    threading.Thread(target=_ffmpeg_watchdog, args=(did, creds), daemon=True).start()
    threading.Thread(target=_keep_alive_loop, args=(did,), daemon=True).start()

    if not _wait_for_path_ready(did):
        abort(504, "P2P client started but the RTSP path never came up")

    return jsonify({"success": True, "rtsp_url": rtsp_url})


@app.route("/stream/stop", methods=["POST"])
def stream_stop():
    body = _require_body("did")
    did = body["did"]

    with _streams_lock:
        entry = _active_streams.pop(did, None)

    if not entry:
        return jsonify({"success": True, "was_running": False})

    for proc in (entry["ffmpeg_proc"], entry["p2p_proc"]):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _safe_disconnect(entry.get("protocol"))

    return jsonify({"success": True, "was_running": True})


@app.route("/stream/status", methods=["GET"])
def stream_status():
    did = request.args.get("did")
    if not did:
        abort(400, "Missing required query param: did")
    with _streams_lock:
        entry = _active_streams.get(did)
        running = bool(entry and entry["p2p_proc"].poll() is None)
        # The url is reported here so a client can attach to a stream without
        # being able to start one: /stream/start is the only way to open a
        # camera session on the device.
        rtsp_url = entry["rtsp_url"] if running else None
    return jsonify({"running": running, "rtsp_url": rtsp_url})


@app.route("/latest.jpg", methods=["GET"])
def latest():
    did = request.args.get("did")
    if not did:
        abort(400, "Missing required query param: did")
    path = os.path.join(_media_dir(did), "latest.jpg")
    if not os.path.exists(path):
        abort(404, "No snapshot has been captured yet for this device")
    return send_file(path, mimetype="image/jpeg")


@app.route("/register", methods=["POST"])
def register():
    """Device registration pushed by the dreame_vacuum_core integration.

    The integration is authoritative about which devices belong to it, so the
    companion UI never has to infer ownership from an entity-registry dump.
    Expected body:
      {"entry_id": "...", "devices": [{"did","name","model","entities":{...}}]}
    """
    body = _require_body("entry_id", "devices")
    devices = body["devices"]
    if not isinstance(devices, list):
        abort(400, "devices must be a list")
    count = store.register_devices(str(body["entry_id"]), devices)
    app.logger.warning("registered %d device(s) from entry %s", count, body["entry_id"])
    return jsonify({"success": True, "registered": count})


@app.route("/registered", methods=["GET"])
def registered():
    return jsonify({"devices": store.list_devices()})


def _list_snapshots(tag=None):
    """Snapshots on disk, newest first. latest.jpg is excluded - it is a copy
    of whichever timestamped file is newest, not a capture of its own."""
    root = os.path.join(MEDIA_ROOT, "snapshots")
    if not os.path.isdir(root):
        return []
    tags = [_safe_tag(tag)] if tag else sorted(os.listdir(root))
    out = []
    for name in tags:
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        for entry in os.listdir(folder):
            if not entry.lower().endswith(".jpg") or entry == "latest.jpg":
                continue
            full = os.path.join(folder, entry)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            out.append({
                "tag": name,
                "filename": entry,
                "media_path": os.path.relpath(full, MEDIA_ROOT),
                "taken_at": int(stat.st_mtime),
                "bytes": stat.st_size,
            })
    out.sort(key=lambda item: item["taken_at"], reverse=True)
    return out


@app.route("/snapshots", methods=["GET"])
def snapshots():
    tag = request.args.get("tag")
    limit = int(request.args.get("limit", 100))
    items = _list_snapshots(tag)
    counts = {}
    for item in items:
        counts[item["tag"]] = counts.get(item["tag"], 0) + 1
    return jsonify({"tags": counts, "snapshots": items[:limit]})


@app.route("/snapshots/<tag>/<filename>", methods=["GET"])
def snapshot_file(tag, filename):
    """Serve one snapshot. Both segments are re-sanitised rather than trusted:
    they arrive in a URL and are used to build a path."""
    safe_name = os.path.basename(filename)
    if not safe_name.lower().endswith(".jpg"):
        abort(404)
    path = os.path.join(_snapshot_dir(tag), safe_name)
    if not os.path.exists(path):
        abort(404, "No such snapshot")
    return send_file(path, mimetype="image/jpeg")


MAP_ROOT = os.path.join(MEDIA_ROOT, "maps")


@app.route("/map", methods=["POST"])
def put_map():
    """Store a rendered map and its geometry, uploaded by the integration.

    Kept as a file plus a JSON sidecar rather than in the database: it is an
    image, and the media folder is already where images live.
    """
    did = request.form.get("did")
    image = request.files.get("image")
    if not did or image is None:
        abort(400, "did and image are required")
    try:
        meta = json.loads(request.form.get("meta") or "{}")
    except ValueError:
        abort(400, "meta is not valid JSON")

    os.makedirs(MAP_ROOT, exist_ok=True)
    safe = _safe_tag(did)
    image.save(os.path.join(MAP_ROOT, f"{safe}.png"))
    meta["updated_at"] = int(time.time())
    with open(os.path.join(MAP_ROOT, f"{safe}.json"), "w") as handle:
        json.dump(meta, handle)

    document = request.form.get("document")
    if document:
        # Kept separate from the image: a client that renders the grid itself
        # never fetches the picture, and one that only wants a picture never
        # downloads 17KB of grid.
        with open(os.path.join(MAP_ROOT, f"{safe}.map.json"), "w") as handle:
            handle.write(document)
    return jsonify({"success": True, "meta": meta})


@app.route("/map/<did>", methods=["GET"])
def get_map_meta(did):
    path = os.path.join(MAP_ROOT, f"{_safe_tag(did)}.json")
    if not os.path.exists(path):
        abort(404, "No map has been published for this vacuum yet")
    with open(path) as handle:
        return jsonify({"meta": json.load(handle)})


@app.route("/map/<did>/document", methods=["GET"])
def get_map_document(did):
    path = os.path.join(MAP_ROOT, f"{_safe_tag(did)}.map.json")
    if not os.path.exists(path):
        abort(404, "No map document has been published for this vacuum yet")
    return send_file(path, mimetype="application/json")


@app.route("/map/<did>.png", methods=["GET"])
def get_map_image(did):
    path = os.path.join(MAP_ROOT, f"{_safe_tag(did)}.png")
    if not os.path.exists(path):
        abort(404, "No map has been published for this vacuum yet")
    return send_file(path, mimetype="image/png")


@app.route("/tasks", methods=["GET"])
def get_tasks():
    return jsonify({
        "tasks": store.list_tasks(request.args.get("did")),
        "step_types": {
            kind: {
                "label": spec["label"], "help": spec["help"],
                "fields": [
                    {"name": n, "type": t, "required": r, "default": d, "help": h}
                    for n, t, r, d, h in spec["fields"]
                ],
            }
            for kind, spec in step_schema.STEP_TYPES.items()
        },
    })


@app.route("/tasks", methods=["POST"])
def put_task():
    """Create or update a task. The slug is derived from the name unless given,
    because it is what automations refer to and should not change silently."""
    body = _require_body("did", "name", "steps")
    slug = store.slugify(body.get("slug") or body["name"])
    if not slug:
        abort(400, "Could not make an id from that name - use letters or numbers")
    try:
        validated = step_schema.validate_steps(body["steps"])
    except step_schema.StepError as err:
        abort(400, str(err))
    store.save_task(slug, body["did"], body["name"], validated)
    return jsonify({"success": True, "task": store.get_task(slug)})


@app.route("/tasks/<slug>", methods=["GET"])
def get_task(slug):
    task = store.get_task(slug)
    if not task:
        abort(404, f"No task '{slug}'")
    return jsonify({"task": task})


@app.route("/tasks/<slug>", methods=["DELETE"])
def remove_task(slug):
    if not store.delete_task(slug):
        abort(404, f"No task '{slug}'")
    return jsonify({"success": True})


@app.route("/tasks/<slug>/calls", methods=["GET"])
def task_calls(slug):
    """The task as Home Assistant service calls - what the integration runs and
    what the export writes out, from one place so they cannot diverge."""
    task = store.get_task(slug)
    if not task:
        abort(404, f"No task '{slug}'")
    device = store.get_device(task["did"]) or {}
    entities = device.get("entities") or {}
    vacuum = request.args.get("vacuum") or entities.get("vacuum")
    if not vacuum:
        abort(409, "This vacuum has not registered its entities with the add-on yet")
    try:
        calls = step_schema.to_service_calls(
            task["steps"], vacuum, request.args.get("stream") or entities.get("stream")
        )
    except step_schema.StepError as err:
        abort(409, str(err))
    return jsonify({"task": task, "calls": calls})


@app.route("/runs", methods=["POST"])
def start_run():
    """Open a run. Steps stream in against the returned id while it works."""
    body = _require_body("did", "command")
    return jsonify({
        "success": True,
        "id": store.start_run(body["did"], body["command"], body.get("run_uid")),
    })


@app.route("/runs/reconcile", methods=["POST"])
def reconcile_runs():
    body = request.get_json(silent=True) or {}
    return jsonify({"success": True, "closed": store.close_orphaned_runs(body.get("did"))})


@app.route("/runs/<int:run_id>/steps", methods=["POST"])
def add_run_step(run_id):
    body = _require_body("text")
    store.add_step(run_id, body["text"])
    return jsonify({"success": True})


@app.route("/runs/<int:run_id>/finish", methods=["POST"])
def finish_run(run_id):
    body = request.get_json(silent=True) or {}
    store.finish_run(run_id, bool(body.get("ok")), body.get("summary"), body.get("detail"))
    return jsonify({"success": True})


@app.route("/runs", methods=["GET"])
def get_runs():
    return jsonify({"runs": store.list_runs(request.args.get("did"),
                                            int(request.args.get("limit", 50)))})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.after_request
def _log_request(response):
    """Waitress doesn't log requests the way the dev server did, and that log
    has been the main tool for telling "the client never called" apart from
    "the call failed"."""
    app.logger.info("%s %s -> %s", request.method, request.full_path.rstrip("?"), response.status_code)
    return response


if __name__ == "__main__":
    store.init()
    # Not Flask's dev server: it mishandles HTTP keep-alive, which surfaced as
    # aiohttp in Home Assistant raising "Server disconnected" when it reused a
    # pooled connection the server had already dropped. Long requests
    # (/stream/start blocks for ~10s) also need real concurrency, or a single
    # start would stall every status poll behind it.
    from waitress import serve

    serve(app, host="0.0.0.0", port=8099, threads=8)
