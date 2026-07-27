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
POST /capture       {username, password, country, four_digit_code, did}
                    -> one-shot: activation sequence -> grab one JPEG frame
                       -> save to /media/dreame-capture/<did>/ -> tear down
POST /stream/start  {username, password, country, four_digit_code, did}
                    -> activation sequence -> keeps the P2P session alive,
                       republishing it as RTSP via a bundled MediaMTX server
POST /stream/stop   {did}
                    -> tears down a stream started above
GET  /stream/status?did=...
GET  /latest.jpg?did=...
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
from dreame_lib.protocol import DreameVacuumProtocol
from dreame_sign import sign_params

OPTIONS_PATH = "/data/options.json"
MEDIA_ROOT = "/media/dreame-capture"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
P2P_BINARY = os.path.join(SCRIPT_DIR, "p2p_sample")
RTSP_HOST_PORT = 8554
MEDIAMTX_API = "http://127.0.0.1:9997"
STALL_THRESHOLD_SECONDS = 15

SIID_CAMERA_SERVICE = 10001
AIID_STREAM_CODE = 4
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

    r3 = camera_action(protocol, did, AIID_STREAM_VIDEO, PIID_STREAM_VIDEO_TRIGGER, {
        "token": "tx",
        "channelId": f"{product_id}/{device_name}",
        "operType": "monitor",
        "operation": "start",
        "session": session,
    })
    _check(r3, "start video")


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
    Returns (p2p_proc, live_url).
    """
    protocol = login(username, password, country)
    connect_device(protocol, did)
    product_id, device_name = get_identity(protocol, did)

    start_camera_session(protocol, did, four_digit_code, product_id, device_name)
    time.sleep(1)
    p2p_info = get_p2p_info(protocol, did)

    proc, live_url = start_p2p_client(did, product_id, device_name, p2p_info)
    if not live_url:
        proc.terminate()
        abort(504, "Timed out waiting for the P2P client to report a stream URL")
    return proc, live_url


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

    media_dir = _media_dir(did)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    snapshot_path = os.path.join(media_dir, f"snapshot_{timestamp}.jpg")
    latest_path = os.path.join(media_dir, "latest.jpg")

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
            proc, live_url = run_activation(
                body["username"], body["password"], body.get("country", "eu"), body["four_digit_code"], did,
            )
            ok, stderr = _grab_frame(live_url, snapshot_path)
            if not ok:
                abort(502, f"ffmpeg failed to capture a frame: {stderr[-500:]}")

        with open(snapshot_path, "rb") as src, open(latest_path, "wb") as dst:
            dst.write(src.read())
    finally:
        if owns_session and proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return jsonify({"success": True, "path": snapshot_path})


def _spawn_ffmpeg_republish(live_url, rtsp_url):
    return subprocess.Popen(
        ["ffmpeg", "-y", "-i", live_url, "-c", "copy", "-f", "rtsp", rtsp_url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _path_inbound_bytes(did):
    """None means "no active publisher on this path" (dead), not "unknown"."""
    try:
        resp = requests.get(f"{MEDIAMTX_API}/v3/paths/get/{did}", timeout=3)
        if resp.status_code != 200:
            return None
        return resp.json().get("inboundBytes")
    except requests.RequestException:
        return None


def _kill(proc):
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


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

        timeout_minutes = _stream_timeout_minutes()
        if timeout_minutes is not None and time.time() - started_at > timeout_minutes * 60:
            with _streams_lock:
                _active_streams.pop(did, None)
            _kill(ffmpeg_proc)
            _kill(p2p_proc)
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
                new_p2p_proc, new_live_url = run_activation(
                    creds["username"], creds["password"], creds["country"], creds["four_digit_code"], did,
                )
            except Exception:
                app.logger.warning("Full session restart failed for %s, will retry", did, exc_info=True)
                continue
            new_ffmpeg = _spawn_ffmpeg_republish(new_live_url, rtsp_url)
            last_bytes, last_progress, respawns_without_progress = None, time.time(), 0
            with _streams_lock:
                current = _active_streams.get(did)
                if current is None:
                    new_ffmpeg.terminate()
                    new_p2p_proc.terminate()
                    return
                current.update({"p2p_proc": new_p2p_proc, "ffmpeg_proc": new_ffmpeg, "live_url": new_live_url})
            continue

        _kill(ffmpeg_proc)
        new_ffmpeg = _spawn_ffmpeg_republish(live_url, rtsp_url)
        last_bytes, last_progress = None, time.time()
        respawns_without_progress += 1

        with _streams_lock:
            current = _active_streams.get(did)
            if current is None or current["p2p_proc"].poll() is not None:
                new_ffmpeg.terminate()
                return
            current["ffmpeg_proc"] = new_ffmpeg


@app.route("/stream/start", methods=["POST"])
def stream_start():
    body = _require_body("username", "password", "four_digit_code", "did")
    did = body["did"]

    with _streams_lock:
        existing = _active_streams.get(did)
        if existing and existing["p2p_proc"].poll() is None:
            return jsonify({"success": True, "rtsp_url": existing["rtsp_url"], "already_running": True})

    p2p_proc, live_url = run_activation(
        body["username"], body["password"], body.get("country", "eu"), body["four_digit_code"], did,
    )

    rtsp_url = f"rtsp://127.0.0.1:{RTSP_HOST_PORT}/{did}"
    ffmpeg_proc = _spawn_ffmpeg_republish(live_url, rtsp_url)

    with _streams_lock:
        _active_streams[did] = {
            "p2p_proc": p2p_proc, "ffmpeg_proc": ffmpeg_proc, "rtsp_url": rtsp_url, "live_url": live_url,
            "started_at": time.time(),
        }
    creds = {
        "username": body["username"], "password": body["password"],
        "country": body.get("country", "eu"), "four_digit_code": body["four_digit_code"],
    }
    threading.Thread(target=_ffmpeg_watchdog, args=(did, creds), daemon=True).start()

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

    return jsonify({"success": True, "was_running": True})


@app.route("/stream/status", methods=["GET"])
def stream_status():
    did = request.args.get("did")
    if not did:
        abort(400, "Missing required query param: did")
    with _streams_lock:
        entry = _active_streams.get(did)
        running = bool(entry and entry["p2p_proc"].poll() is None)
    return jsonify({"running": running})


@app.route("/latest.jpg", methods=["GET"])
def latest():
    did = request.args.get("did")
    if not did:
        abort(400, "Missing required query param: did")
    path = os.path.join(_media_dir(did), "latest.jpg")
    if not os.path.exists(path):
        abort(404, "No snapshot has been captured yet for this device")
    return send_file(path, mimetype="image/jpeg")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099, threaded=True)
