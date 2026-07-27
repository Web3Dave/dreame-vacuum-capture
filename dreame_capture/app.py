#!/usr/bin/env python3
"""
Dreame Vacuum Camera Capture - Home Assistant add-on HTTP API.

POST /capture  -> logs in, runs the camera activation sequence, fetches a
                  fresh xp2p_info credential, starts the local P2P client,
                  grabs one JPEG frame into /media/dreame-capture/, returns
                  its path.
GET  /latest.jpg -> serves the most recent snapshot.
GET  /health      -> liveness check.

See README.md for the HA-side rest_command / generic camera wiring.
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

from flask import Flask, jsonify, send_file, abort

sys.path.insert(0, os.path.dirname(__file__))
from dreame_lib.protocol import DreameVacuumProtocol
from dreame_sign import sign_params

OPTIONS_PATH = "/data/options.json"
MEDIA_DIR = "/media/dreame-capture"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
P2P_BINARY = os.path.join(SCRIPT_DIR, "p2p_sample")
P2P_CONFIG = "/tmp/p2p_config.txt"

SIID_CAMERA_SERVICE = 10001
AIID_STREAM_CODE = 4
AIID_STREAM_VIDEO = 1
PIID_STREAM_CODE_OPEN = 1100
PIID_STREAM_VERIFY_CODE = 1102
PIID_STREAM_VIDEO_TRIGGER = 1

app = Flask(__name__)
os.makedirs(MEDIA_DIR, exist_ok=True)


def load_options():
    if not os.path.exists(OPTIONS_PATH):
        abort(500, "options.json not found - is this running as a Home Assistant add-on?")
    with open(OPTIONS_PATH) as f:
        opts = json.load(f)
    required = ["account_username", "account_password", "four_digit_code"]
    missing = [k for k in required if not opts.get(k)]
    if missing:
        abort(400, f"Missing add-on configuration: {missing}")
    return opts


def login(opts):
    protocol = DreameVacuumProtocol(
        username=opts["account_username"],
        password=opts["account_password"],
        country=opts.get("account_country", "eu"),
        prefer_cloud=True,
        account_type="dreame",
    )
    if not protocol.cloud.login():
        abort(502, "Dreame login failed - check account_username/account_password/account_country")

    devices = protocol.cloud.get_devices()
    records = devices.get("page", {}).get("records", [])
    if not records:
        abort(502, "No devices found on this Dreame account")
    did = str(records[0]["did"])
    protocol.cloud._did = did
    protocol.connect()
    return protocol, did


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


def write_p2p_config(product_id, device_name):
    with open(P2P_CONFIG, "w") as f:
        f.write(f"product_id={product_id}\n")
        f.write(f"device_name={device_name}\n")
        f.write("app_id=\n")
        f.write("app_key=\n")
        f.write("lan_host=\n")
        f.write("lan_port=\n")


def run_p2p_and_get_live_url(p2p_info, timeout=20):
    env = dict(os.environ)
    env["XP2P_INFO"] = p2p_info
    proc = subprocess.Popen(
        [P2P_BINARY, P2P_CONFIG], env=env,
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


@app.route("/capture", methods=["POST"])
def capture():
    opts = load_options()
    protocol, did = login(opts)
    product_id, device_name = get_identity(protocol, did)

    start_camera_session(protocol, did, opts["four_digit_code"], product_id, device_name)
    time.sleep(1)
    p2p_info = get_p2p_info(protocol, did)

    write_p2p_config(product_id, device_name)
    proc, live_url = run_p2p_and_get_live_url(p2p_info)

    if not live_url:
        proc.terminate()
        abort(504, "Timed out waiting for the P2P client to report a stream URL")

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    snapshot_path = os.path.join(MEDIA_DIR, f"snapshot_{timestamp}.jpg")
    latest_path = os.path.join(MEDIA_DIR, "latest.jpg")

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", live_url, "-frames:v", "1", snapshot_path],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 or not os.path.exists(snapshot_path):
            abort(502, f"ffmpeg failed to capture a frame: {result.stderr[-500:]}")
        with open(snapshot_path, "rb") as src, open(latest_path, "wb") as dst:
            dst.write(src.read())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return jsonify({"success": True, "path": snapshot_path})


@app.route("/latest.jpg", methods=["GET"])
def latest():
    path = os.path.join(MEDIA_DIR, "latest.jpg")
    if not os.path.exists(path):
        abort(404, "No snapshot has been captured yet")
    return send_file(path, mimetype="image/jpeg")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
