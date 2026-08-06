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
                       -> /media/dreame_vacuum_unlocked/snapshots/<tag>/<ts>.jpg
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
import base64

import requests
from flask import Flask, jsonify, send_file, abort, request

sys.path.insert(0, os.path.dirname(__file__))
import steps as step_schema
import config_store
import store
from dreame_lib.protocol import DreameVacuumProtocol
from dreame_lib import flv_audio
from dreame_sign import sign_params

OPTIONS_PATH = "/data/options.json"
MEDIA_ROOT = "/media/dreame_vacuum_unlocked"
AUDIO_ROOT = os.path.join(MEDIA_ROOT, "audio")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
P2P_BINARY = os.path.join(SCRIPT_DIR, "p2p_sample")
P2P_SPEAK_BINARY = os.path.join(SCRIPT_DIR, "p2p_speak")
P2P_INTERCOM_BINARY = os.path.join(SCRIPT_DIR, "p2p_intercom")
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

# Monitor.startVoice / VOICE_OPERATE - arms/disarms talk-back on a device
# that already has an active monitor (video) session. See _read_monitor_audio.
AIID_VOICE_OPERATE = 2
PIID_VOICE_OPERATE = 2

# The runSendService params string the real app sends when arming the voice
# send channel (Command.getTwoWayRadio(channel) in the app's RN bridge) -
# confirmed byte-for-byte against a live capture of the app's own traffic.
VOICE_SEND_CMD = "channel=0"

app = Flask(__name__)
os.makedirs(MEDIA_ROOT, exist_ok=True)
os.makedirs(AUDIO_ROOT, exist_ok=True)

# did -> {"p2p_proc": Popen, "ffmpeg_proc": Popen, "live_url": str}
_active_streams = {}
_streams_lock = threading.Lock()

# did -> {"p2p_proc": Popen (stdin=PIPE, stream mode), "protocol", "session",
#         "product_id", "device_name", "started_at", "line_q": Queue}
_active_audio_streams = {}
_audio_streams_lock = threading.Lock()

# did -> {"proc": Popen (ffmpeg, stdin=PIPE), "temp": str, "tag": str,
#         "audio": bool, "started_at": int}. A clip spans two steps: record_clip
# opens one, end_clip closes it and moves the finished mp4 into the tag's
# folder. Only one recording per device at a time.
_active_recordings = {}
_recordings_lock = threading.Lock()


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


def _read_monitor_audio(protocol, did):
    """Read Monitor siid 10001 / piid 2 (PropMonitorAudioStatus).

    The device reports its intercom state here. When it arms talk-back it sets
    a JSON string like {"result":0,"operation":"start","session":"<sid>"}
    (the session it got in the VOICE_OPERATE value). Returns a parsed dict (or
    the raw scalar) or None on any failure.
    """
    try:
        resp = protocol.get_properties([{"did": did, "siid": SIID_CAMERA_SERVICE, "piid": 2}])
        # Accept either a list of results or the raw response.
        if isinstance(resp, list) and resp:
            entry = resp[0] if isinstance(resp[0], dict) else resp
            raw = entry.get("value") if isinstance(entry, dict) else None
        else:
            raw = resp
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return raw
        return raw
    except Exception as err:  # noqa: BLE001 - best effort probe
        app.logger.warning("could not read 10001.2: %s", err)
        return None


def _cloud_props(protocol, did, keys):
    """Read props via the app's real channel: POST dreame-user-iot/iotstatus/props.

    The device does NOT serve some Monitor props (incl. 10001.2 intercom status)
    on the data bus (`code:-1`); the app reads them from this cloud endpoint.
    """
    try:
        return protocol.cloud._api_call(
            "dreame-user-iot/iotstatus/props", {"did": did, "keys": keys}
        )
    except Exception as err:  # noqa: BLE001 - best effort
        app.logger.warning("cloud props read failed: %s", err)
        return None


def _wait_intercom_cloud(protocol, did, session, seconds=4):
    """Confirm via the CLOUD props channel that the device armed intercom for
    `session` (10001.2 echoes {"result":0,"operation":"start","session":<sid>}),
    polling briefly. Returns True as soon as it's confirmed - IMPORTANT to keep
    this short so audio is pushed while intercom is still freshly armed."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        resp = _cloud_props(protocol, did, "10001.2")
        try:
            entries = (resp or {}).get("data") or []
            if entries:
                val = json.loads(entries[0].get("value", "{}"))
                app.logger.info("10001.2 cloud -> %s", val)
                if (
                    val.get("result") == 0
                    and val.get("operation") == "start"
                    and val.get("session") == session
                ):
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return False


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


def start_p2p_client(did, product_id, device_name, p2p_info, timeout=30):
    """Start the stream's P2P client. Uses the intercom-capable binary
    (p2p_intercom) in streaming mode (reads pre-muxed FLV paths from stdin to
    talk) so video, vacuum-mic downlink and talk all share ONE session - the
    Stream + Intercom switches only arm/disarm different layers of it.

    Returns (proc, live_url, audio_url, stdin) or (None, None, None, None)."""
    config_path = write_p2p_config(did, product_id, device_name)
    env = dict(os.environ)
    env["XP2P_INFO"] = p2p_info
    proc = subprocess.Popen(
        [P2P_INTERCOM_BINARY, config_path, "-", VOICE_SEND_CMD, "0"],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    line_q = queue.Queue()

    def _reader():
        for line in proc.stdout:
            line_q.put(line)

    threading.Thread(target=_reader, daemon=True).start()

    tags = _read_stream_tags(line_q, ("LIVE_URL", "AUDIO_URL"), timeout=timeout)
    live_url = tags.get("LIVE_URL")
    audio_url = tags.get("AUDIO_URL")
    if not live_url:
        proc.terminate()
        return None, None, None, None, None
    return proc, live_url, audio_url, proc.stdin, line_q


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

    proc, live_url, audio_url, stdin, line_q = start_p2p_client(did, product_id, device_name, p2p_info)
    if not live_url:
        proc.terminate()
        _safe_disconnect(protocol)
        abort(504, "Timed out waiting for the P2P client to report a stream URL")
    return {
        "protocol": protocol,
        "p2p_proc": proc,
        "live_url": live_url,
        "audio_url": audio_url,
        "stdin": stdin,
        "line_q": line_q,
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


AUDIO_EXTS = (".mp3",)


def _safe_audio_name(value):
    """Keep the file name within AUDIO_ROOT - never allow path traversal.
    Kept in sync with ui.py's copy (separate process, same directory)."""
    name = os.path.basename((value or "").strip())
    if not name or not name.lower().endswith(AUDIO_EXTS):
        raise ValueError("Not an mp3 file name")
    return "".join(c if (c.isalnum() or c in " ._-") else "_" for c in name)


def _mux_audio_to_flv(audio_bytes, tag):
    """Any ffmpeg-readable audio bytes -> a temp .flv file muxed exactly like
    the app's own AudioRecordUtil/PCMEncoder/FLVPacker pipeline (AAC-LC,
    16kHz, mono; see dreame_lib/flv_audio.py). Caller owns cleanup."""
    src_path = f"/tmp/speak_src_{tag}"
    flv_path = f"/tmp/speak_{tag}.flv"
    with open(src_path, "wb") as fh:
        fh.write(audio_bytes)
    try:
        n = flv_audio.build_send_file(src_path, flv_path)
        app.logger.info("muxed %d bytes -> %s (%d FLV packets)", len(audio_bytes), flv_path, n)
    finally:
        try:
            os.remove(src_path)
        except OSError:
            pass
    return flv_path


@app.route("/speak", methods=["POST"])
def speak():
    """One-shot: stream audio to the vacuum's speaker over the XP2P
    talk-back (voice intercom) channel, then close the channel again.

    Request: JSON {username, password, country?, four_digit_code, did,
    audio} - `audio` is base64-encoded bytes of ANY ffmpeg-readable audio
    format (mp3, wav, ...); this endpoint transcodes/muxes it to the exact
    AAC-LC/16kHz/mono FLV container the device's speaker expects.

    Wires up the same login -> identity -> PIN camera session -> p2p_info
    chain the live camera uses, then hands the muxed audio to the
    `p2p_intercom` binary which opens the SDK's send-voice service and
    pushes it tag-by-tag.
    """
    body = _require_body("username", "password", "four_digit_code", "did", "audio")
    did = body["did"]
    try:
        audio = base64.b64decode(body["audio"])
    except Exception:
        abort(400, "audio field must be base64")
    if not audio:
        abort(400, "Empty audio body")

    protocol = login(body["username"], body["password"], body.get("country", "eu"))
    try:
        connect_device(protocol, did)
        product_id, device_name = get_identity(protocol, did)
        session = start_camera_session(protocol, did, body["four_digit_code"], product_id, device_name)
        # Enter intercom mode WITH the active session. The app's Monitor module
        # always injects `session` into the VOICE_OPERATE value (Monitor.
        # startVoice -> sendAction adds session:this.session); the device only
        # arms talk-back and echoes that session back on 10001.2 when it matches
        # the active monitor session. Sending it without a session never arms it.
        start_voice = camera_action(
            protocol, did, AIID_VOICE_OPERATE, PIID_VOICE_OPERATE,
            {"session": session, "operType": "intercom", "operation": "start"},
        )
        app.logger.info("/speak VOICE_OPERATE start(session=%s) -> %s", session, start_voice)
        confirmed = _wait_intercom_cloud(protocol, did, session)
        app.logger.info("/speak intercom confirmed=%s", confirmed)
        p2p_info = get_p2p_info(protocol, did)
    except Exception:
        _safe_disconnect(protocol)
        raise

    config_path = write_p2p_config(did, product_id, device_name)
    try:
        flv_path = _mux_audio_to_flv(audio, did)
    except Exception as err:
        _safe_disconnect(protocol)
        abort(400, f"Could not mux audio (is it a valid audio file?): {err}")

    env = dict(os.environ)
    env["XP2P_INFO"] = p2p_info
    timeout = 120
    try:
        result = subprocess.run(
            [P2P_INTERCOM_BINARY, config_path, flv_path, body.get("cmd", VOICE_SEND_CMD), body.get("crypto", "0")],
            env=env, capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        app.logger.info("/speak returncode=%s audio_bytes=%d", result.returncode, len(audio))
        return jsonify({
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "audio_bytes": len(audio),
            "intercom_confirmed": confirmed,
            "output": output[-4000:],
        })
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": f"p2p_intercom timed out after {timeout}s"})
    finally:
        try:
            os.remove(flv_path)
        except OSError:
            pass
        # Leave intercom mode (with the same session it was entered with).
        try:
            camera_action(
                protocol, did, AIID_VOICE_OPERATE, PIID_VOICE_OPERATE,
                {"session": session, "operType": "intercom", "operation": "end"},
            )
        except Exception:
            app.logger.warning("/speak VOICE_OPERATE end failed", exc_info=True)
        _safe_disconnect(protocol)


def _read_stream_tags(line_q, wanted, timeout=30):
    """Read lines from a p2p client's output queue until it has reached each
    wanted prefix (e.g. LIVE_URL, AUDIO_URL). Returns {prefix: value}."""
    found = {}
    deadline = time.time() + timeout
    while time.time() < deadline and len(found) < len(wanted):
        try:
            line = line_q.get(timeout=0.5)
        except queue.Empty:
            continue
        for prefix in wanted:
            if prefix in found:
                continue
            if line.startswith(prefix + ":"):
                val = line.split(":", 1)[1].strip()
                if val and val != "(null)":
                    found[prefix] = val
    return found


@app.route("/speak/start", methods=["POST"])
def speak_start():
    """Open a persistent talk-back session: arm intercom, open the p2p send
    channel, and keep it open (backed by `p2p_intercom` in streaming mode,
    reading file paths from stdin) so multiple clips can be pushed with
    /speak/send without re-arming intercom between each one. Mirrors
    /stream/start's shape. When `rtsp` is truthy in the body it also pulls
    the vacuum's mic ("live-audio") and muxes video + mic into the {did} RTSP
    so the HA camera widget gets sound while intercom is armed."""
    body = _require_body("username", "password", "four_digit_code", "did")
    did = body["did"]

    with _audio_streams_lock:
        existing = _active_audio_streams.get(did)
        if existing and existing["p2p_proc"].poll() is None:
            return jsonify({"success": True, "already_running": True})

    protocol = login(body["username"], body["password"], body.get("country", "eu"))
    try:
        connect_device(protocol, did)
        product_id, device_name = get_identity(protocol, did)
        session = start_camera_session(protocol, did, body["four_digit_code"], product_id, device_name)
        start_voice = camera_action(
            protocol, did, AIID_VOICE_OPERATE, PIID_VOICE_OPERATE,
            {"session": session, "operType": "intercom", "operation": "start"},
        )
        app.logger.info("/speak/start VOICE_OPERATE start(session=%s) -> %s", session, start_voice)
        confirmed = _wait_intercom_cloud(protocol, did, session)
        app.logger.info("/speak/start intercom confirmed=%s", confirmed)
        p2p_info = get_p2p_info(protocol, did)
    except Exception:
        _safe_disconnect(protocol)
        raise

    config_path = write_p2p_config(did, product_id, device_name)
    env = dict(os.environ)
    env["XP2P_INFO"] = p2p_info
    proc = subprocess.Popen(
        [P2P_INTERCOM_BINARY, config_path, "-", body.get("cmd", VOICE_SEND_CMD), body.get("crypto", "0")],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    line_q = queue.Queue()

    def _reader():
        for line in proc.stdout:
            line_q.put(line)

    threading.Thread(target=_reader, daemon=True).start()

    # Optionally republish live_url as RTSP. Single input only - confirmed
    # against a real device that the "live" FLV feed already carries AAC
    # audio tags once intercom is armed (which it already is by this point,
    # since VOICE_OPERATE ran before p2p_intercom was even started above), so
    # there's no separate mic stream to mux in and no timing dance needed:
    # by the time ffmpeg probes live_url here, the audio tags are already
    # flowing. See _spawn_ffmpeg_republish's docstring.
    rtsp_url = None
    rtsp_proc = None
    if body.get("rtsp"):
        tags = _read_stream_tags(line_q, ("LIVE_URL",), timeout=30)
        live_url = tags.get("LIVE_URL")
        if live_url:
            rtsp_url = f"rtsp://127.0.0.1:{RTSP_HOST_PORT}/{did}"
            rtsp_proc = _spawn_ffmpeg_republish(live_url, rtsp_url)
            app.logger.info("/speak/start republishing -> %s", rtsp_url)
        else:
            app.logger.warning("/speak/start rtsp requested but got no LIVE_URL")

    with _audio_streams_lock:
        _active_audio_streams[did] = {
            "p2p_proc": proc, "protocol": protocol, "session": session,
            "product_id": product_id, "device_name": device_name,
            "started_at": time.time(), "line_q": line_q,
            "rtsp_url": rtsp_url, "rtsp_proc": rtsp_proc,
        }

    return jsonify({"success": True, "intercom_confirmed": confirmed, "rtsp_url": rtsp_url})


@app.route("/speak/send", methods=["POST"])
def speak_send():
    """Push one clip through an already-open talk-back session (see
    /speak/start). Request: JSON {did, filename} to send a clip already
    uploaded to the add-on's audio library (AUDIO_ROOT - what the UI's Audio
    page shows), or JSON {did, audio} (base64, any ffmpeg-readable format) to
    send bytes directly. Reuses the open channel instead of re-arming
    intercom - only one of `filename`/`audio` is required."""
    body = _require_body("did")
    did = body["did"]

    audio_size_hint = 0
    if body.get("filename"):
        try:
            safe = _safe_audio_name(body["filename"])
        except ValueError as err:
            abort(400, str(err))
        src_path = os.path.join(AUDIO_ROOT, safe)
        if not os.path.isfile(src_path):
            abort(404, f"No such clip: {safe}")
        audio_size_hint = os.path.getsize(src_path)
        mux_input = ("path", src_path)
    elif body.get("audio"):
        try:
            audio = base64.b64decode(body["audio"])
        except Exception:
            abort(400, "audio field must be base64")
        if not audio:
            abort(400, "Empty audio body")
        audio_size_hint = len(audio)
        mux_input = ("bytes", audio)
    else:
        abort(400, "Provide either 'filename' or 'audio'")

    # Talk routes through the RUNNING stream's session when one is active and
    # intercom is armed (video + mic + talk all on one session); otherwise
    # fall back to a standalone /speak/start session.
    entry = None
    with _streams_lock:
        st = _active_streams.get(did)
        if st and st["p2p_proc"].poll() is None and st.get("intercom_armed") and st.get("stdin"):
            entry = {"p2p_proc": st["p2p_proc"], "line_q": st.get("line_q")}
    if entry is None:
        with _audio_streams_lock:
            entry = _active_audio_streams.get(did)
        if not entry or entry["p2p_proc"].poll() is not None:
            abort(409, "No active intercom on this device - start it first")

    tag = f"{did}_{uuid.uuid4().hex[:8]}"
    try:
        if mux_input[0] == "path":
            flv_path = f"/tmp/speak_{tag}.flv"
            flv_audio.build_send_file(mux_input[1], flv_path)
        else:
            flv_path = _mux_audio_to_flv(mux_input[1], tag)
    except Exception as err:
        abort(400, f"Could not mux audio (is it a valid audio file?): {err}")

    proc = entry["p2p_proc"]
    proc.stdin.write(flv_path + "\n")
    proc.stdin.flush()

    # Wait for the matching "SENT <flv_path> rc=<n>" line so the caller
    # knows the clip actually finished before deciding whether to /speak/stop.
    timeout = max(10.0, audio_size_hint / 4000.0)  # rough floor scaled to clip size
    deadline = time.time() + timeout
    rc = None
    while time.time() < deadline:
        try:
            line = entry["line_q"].get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if line.startswith("SENT ") and flv_path in line:
            try:
                rc = int(line.strip().rsplit("rc=", 1)[1])
            except (IndexError, ValueError):
                rc = -1
            break

    try:
        os.remove(flv_path)
    except OSError:
        pass

    if rc is None:
        return jsonify({"success": False, "error": f"timed out after {timeout:.0f}s waiting for send confirmation"})
    return jsonify({"success": rc == 0, "returncode": rc, "audio_bytes": audio_size_hint})


@app.route("/speak/stop", methods=["POST"])
def speak_stop():
    """Close a talk-back session opened by /speak/start. Mirrors
    /stream/stop's shape."""
    body = _require_body("did")
    did = body["did"]

    with _audio_streams_lock:
        entry = _active_audio_streams.pop(did, None)

    if not entry:
        return jsonify({"success": True, "was_running": False})

    # Tear down the optional video+mic RTSP republish leg first.
    rtsp_proc = entry.get("rtsp_proc")
    if rtsp_proc and rtsp_proc.poll() is None:
        try:
            rtsp_proc.terminate()
            rtsp_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rtsp_proc.kill()

    proc = entry["p2p_proc"]
    try:
        if proc.poll() is None:
            proc.stdin.write("STOP\n")
            proc.stdin.flush()
        proc.wait(timeout=5)
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    try:
        camera_action(
            entry["protocol"], did, AIID_VOICE_OPERATE, PIID_VOICE_OPERATE,
            {"session": entry["session"], "operType": "intercom", "operation": "end"},
        )
    except Exception:
        app.logger.warning("/speak/stop VOICE_OPERATE end failed", exc_info=True)
    _safe_disconnect(entry.get("protocol"))

    return jsonify({"success": True, "was_running": True})


@app.route("/speak/status", methods=["GET"])
def speak_status():
    did = request.args.get("did")
    if not did:
        abort(400, "Missing required query param: did")
    with _audio_streams_lock:
        entry = _active_audio_streams.get(did)
        running = bool(entry and entry["p2p_proc"].poll() is None)
        rtsp_proc = entry.get("rtsp_proc") if entry else None
        rtsp_running = bool(rtsp_proc and rtsp_proc.poll() is None)
        rtsp_url = entry.get("rtsp_url") if entry else None
    mux_log = f"/tmp/dreame_mux_{did}.log"
    mux_tail = None
    try:
        if os.path.isfile(mux_log):
            with open(mux_log, "r", errors="replace") as fh:
                mux_tail = "".join(fh.readlines()[-40:])
    except Exception:
        pass
    return jsonify({
        "running": running,
        "rtsp_running": rtsp_running,
        "rtsp_url": rtsp_url if rtsp_running else None,
        "mux_log": mux_tail,
    })


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

    classifications = _classify_snapshot(tag, snapshot_path)

    return jsonify({
        "success": True,
        "path": snapshot_path,
        "latest": latest_path,
        "tag": tag,
        # Relative to the media root, which is what a media-source or www
        # path needs - callers should not have to strip a prefix themselves.
        "media_path": os.path.relpath(snapshot_path, MEDIA_ROOT),
        "latest_media_path": os.path.relpath(latest_path, MEDIA_ROOT),
        # The dreame_vacuum_unlocked_integration integration is always the one that asked for
        # this snapshot - even a task run from this add-on's own UI still
        # takes the photo via the `vacuum.take_snapshot` Home Assistant
        # service, which the integration alone can fulfil. So riding the
        # result back on this response, rather than the add-on pushing it
        # out separately afterward, reaches Home Assistant with no second
        # connection and no separate credential to manage.
        "classifications": classifications,
    })


def _record_ffmpeg(rtsp_url, temp_path):
    """An ffmpeg that records the running stream's RTSP to a fragmented mp4.

    The stream's RTSP is already h264 video (+ AAC audio when the mic layer is
    armed), so both streams are *copied* (`-c copy`) into the mp4 rather than
    re-encoded - a genuine h264 mp4 with essentially no CPU, and none of the
    frame drops a live libx264 re-encode causes alongside the republisher.
    Audio is recorded by default: the optional `-map 0:a:0?` never fails a
    video-only flow, and the fragmenting mp4 muxer (see below) is always used.
    `frag_keyframe+empty_moov` interleaves the moov data so the file is already
    valid if cut short, and streams over HTTP.
    """
    args = ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", rtsp_url,
            "-map", "0:v:0", "-c:v", "copy",
            "-map", "0:a:0?", "-c:a", "copy",
            "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", temp_path]
    # stdin stays open so end_clip can send 'q' for a clean finalisation.
    # text=True: the stdin is how we stop it, and on py3.8 the pipe is binary
    # by default -> "a bytes-like object is required" (hit live, 2026-08).
    return subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, text=True,
    )


@app.route("/record/start", methods=["POST"])
def record_start():
    """Begin a clip recording of the running stream, to be ended by /record/stop.

    Request: JSON {did, tag?, audio?}. Needs an active stream (its RTSP is
    what gets recorded). Records deliberately loose: no snapshot name yet,
    because clips are named from the moment they *finish*, and no classifier
    runs on a video - the classifier is for photos only.
    """
    body = _require_body("did")
    did = body["did"]
    tag = _safe_tag(body.get("tag"))

    with _streams_lock:
        existing = _active_streams.get(did)
        stream_active = existing is not None and existing["p2p_proc"].poll() is None
        rtsp_url = existing["rtsp_url"] if stream_active else None
    if not stream_active or not rtsp_url:
        return jsonify({
            "success": False,
            "error": "No active stream to record - start one (a start_stream "
                     "step) before recording a clip.",
        }), 409

    with _recordings_lock:
        running = _active_recordings.get(did)
        if running and running["proc"].poll() is None:
            return jsonify({"success": False, "error": "Already recording a clip for this device"}), 409
        # Record to a temp file on the SAME filesystem as the final snapshots
        # dir. The final save uses os.replace (an atomic rename), which FAILS
        # across mount points with Errno 18 EXDEV (e.g. /tmp -> /media) - a
        # recording captured fine but never landed (hit live 2026-08). The
        # `.part` suffix is not a media extension, so a stray copy from an
        # abandoned recording is invisible to the Tags index.
        temp_path = os.path.join(MEDIA_ROOT, f".record-{did}-{int(time.time())}.part")
        proc = _record_ffmpeg(rtsp_url, temp_path)
        _active_recordings[did] = {
            "proc": proc, "temp": temp_path, "tag": tag,
            "audio": True, "started_at": time.time(),
        }
    app.logger.info("/record/start did=%s tag=%s (audio on) rtsp=%s", did, tag, rtsp_url)
    return jsonify({"success": True, "tag": tag, "temp": temp_path})


@app.route("/record/stop", methods=["POST"])
def record_stop():
    """End the clip recording for a did and save it under its tag.

    Stops ffmpeg cleanly (a 'q' on its stdin), then moves the finished
    fragmented mp4 into `snapshots/<tag>/<ts>.mp4`. No classifier runs here:
    a video is not a photo and is exempt from classification on purpose.
    """
    body = _require_body("did")
    did = body["did"]

    with _recordings_lock:
        entry = _active_recordings.pop(did, None)
    if not entry:
        return jsonify({"success": False, "error": "No clip is recording for this device"}), 409

    proc = entry["proc"]
    temp_path = entry["temp"]
    tag = entry["tag"]
    try:
        if proc.poll() is None:
            # Best-effort graceful stop; never let a broken pipe/short write
            # turn a successful recording into a failure, and never guess at
            # the failure cause. text=True above makes 'q' the right thing.
            try:
                proc.stdin.write("q\n")
                proc.stdin.flush()
            except Exception:  # noqa: BLE001 - stopping is best-effort
                app.logger.debug("/record/stop failed to signal ffmpeg for %s", did)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

        if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
            app.logger.warning("/record/stop did=%s produced no file %s", did, temp_path)
            try:
                os.remove(temp_path)
            except OSError:
                pass
            return jsonify({
                "success": False,
                "error": "The recording produced no video - did the stream die mid-clip?",
            }), 502

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        snapshot_dir = _snapshot_dir(tag)
        os.makedirs(snapshot_dir, exist_ok=True)
        final_name = f"{timestamp}.mp4"
        final_path = os.path.join(snapshot_dir, final_name)
        os.replace(temp_path, final_path)
        elapsed = int(time.time() - entry["started_at"])
        app.logger.info("/record/stop did=%s -> %s (%ds)", did, final_path, elapsed)
        return jsonify({
            "success": True,
            "path": final_path,
            "tag": tag,
            "filename": final_name,
            "media_path": os.path.relpath(final_path, MEDIA_ROOT),
            "audio": entry["audio"],
            "seconds": elapsed,
        })
    except Exception:  # noqa: BLE001
        # Leave temp_path in place (os.replace moves it on success) so a
        # finalisation glitch never silently discards the captured footage.
        app.logger.exception("/record/stop failed for %s (temp %s left for recovery)", did, temp_path)
        return jsonify({
            "success": False,
            "error": "Could not finalise the clip - see the add-on log",
        }), 502


@app.route("/record/status", methods=["GET"])
def record_status():
    did = request.args.get("did")
    if not did:
        abort(400, "Missing required query param: did")
    with _recordings_lock:
        entry = _active_recordings.get(did)
        running = bool(entry and entry["proc"].poll() is None)
        return jsonify({
            "running": running,
            "tag": (entry or {}).get("tag"),
            "audio": (entry or {}).get("audio"),
            "started_at": (entry or {}).get("started_at"),
        })


def _classify_snapshot(tag: str, snapshot_path: str) -> list:
    """Report what happened for every classification linked to this tag -
    not just the ones that produced a usable result. The caller (the
    dreame_vacuum_unlocked_integration integration) logs this to the task's activity trace,
    so "nothing happened" has to be distinguishable from "ran and scored
    below threshold" and from "no trained model yet", rather than all three
    silently looking identical.

    Run inline rather than backgrounded: TFLite inference is local and fast,
    and the caller needs the results to include in its own response. Nothing
    here writes to the training dataset - that only happens when a person
    assigns a label by hand, so an uncertain guess can never quietly teach
    the model to repeat itself.
    """
    reports = []
    try:
        import classify_infer
        import classify_store
        import config_store

        for classifier in config_store.list_classifiers():
            link = next((t for t in classifier["tags"] if t["tag_id"] == tag), None)
            if not link:
                continue  # not linked to this tag at all - not this classifier's business
            base = {"classifier_id": classifier["id"], "name": classifier["name"]}
            if not classifier["configured"]:
                reports.append({**base, "skipped": "not configured yet"})
                continue
            if not classifier["enabled"]:
                reports.append({**base, "skipped": "disabled"})
                continue
            result = classify_infer.classify(classifier["id"], snapshot_path, link["crop"])
            if result is None:
                reports.append({**base, "skipped": "no trained model yet"})
                continue
            label, score = result
            # Recorded regardless of threshold - View classifications on the
            # tag detail page is exactly where a below-threshold result
            # (shown there in red) is supposed to be visible, not somewhere
            # that quietly never learns it happened.
            classify_store.save_result(
                tag, os.path.basename(snapshot_path), classifier["id"], classifier["name"],
                label, score, classifier["threshold"],
            )
            reports.append({
                **base,
                "classification_type": classifier["classification_type"],
                "classes": classifier["classes"],
                "label": label,
                "score": score,
                "threshold": classifier["threshold"],
                "passed_threshold": score >= classifier["threshold"],
                "tag_id": tag,
                "filename": os.path.basename(snapshot_path),
                "ran_at": int(time.time()),
            })
    except Exception:  # noqa: BLE001 - classification must never break a capture
        app.logger.warning(
            "Classification failed for tag %s (snapshot capture already succeeded)",
            tag, exc_info=True)
        reports.append({"error": "Classification crashed - see the add-on's own log"})
    return reports


def _spawn_ffmpeg_republish(live_url, rtsp_url):
    """Single input, `-c copy`, no explicit -map: ffmpeg picks up every
    elementary stream present in the source FLV. This is deliberately NOT a
    two-input mux against a separate mic URL - confirmed directly against a
    real device that the "live" feed already carries AAC audio tags
    (interleaved with the video tags) once intercom is armed, on the SAME
    URL. The separate "live-audio" endpoint p2p_intercom also derives
    (AUDIO_URL) returns an immediate empty response and isn't needed.

    Because RTSP's SDP is negotiated once at startup from ffmpeg's initial
    probe, this must be (re)started AFTER intercom is armed for the audio
    track to be included - a process started before arming, or before the
    device's audio tags begin flowing, will probe video-only and never add
    audio retroactively. See /stream/intercom, which respawns this on every
    arm/disarm toggle for exactly that reason.
    """
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


def _path_reader_count(did):
    """How many RTSP clients are currently reading this path, or None if that
    could not be determined.

    None is deliberately not treated as "zero" by the caller: if MediaMTX's
    API is briefly unreachable, that must not look identical to "nobody is
    watching" and auto-stop a stream someone actually has open.
    """
    try:
        resp = requests.get(f"{MEDIAMTX_API}/v3/paths/list", timeout=3)
        if resp.status_code != 200:
            return None
        for item in resp.json().get("items") or []:
            if item.get("name") == did:
                readers = item.get("readers")
                return len(readers) if isinstance(readers, list) else None
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
                code = result.get("code")
                # Fires every 20s for as long as a stream is open - a routine
                # "yes, still here" is not a warning. Only a non-zero code,
                # which the device answers with when it has stopped listening
                # to this session, is worth a log line at all.
                if code != 0:
                    app.logger.warning(
                        "camera keep_alive for %s -> code=%s value=%s",
                        did, code, out[0].get("value"),
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
            # A live view left open (a dashboard card, HA's own camera
            # stream) keeps at least one RTSP reader attached for as long as
            # it is actually being watched. Killing the path under it left
            # Home Assistant with no way to tell "deliberately stopped" from
            # "network fault" - it just retried forever with growing backoff,
            # which is what this timer was supposed to prevent, not cause.
            # None (API unreachable) is treated as "someone might still be
            # watching", the same caution _wait_for_path_ready already uses -
            # a stop that should not have happened is worse than one delayed
            # a few seconds until the next check.
            readers = _path_reader_count(did)
            if readers != 0:
                continue
            with _streams_lock:
                _active_streams.pop(did, None)
            _kill(ffmpeg_proc)
            _kill(p2p_proc)
            _safe_disconnect(protocol)
            app.logger.warning("Stream for %s auto-stopped after %s minutes with nobody watching (stream_timeout_minutes)", did, timeout_minutes)
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
            # A fresh session means a fresh p2p handle - intercom (VOICE_OPERATE)
            # was armed against the OLD session and does not carry over, so the
            # device won't be sending audio tags on the new live_url until
            # re-armed. Re-toggling the Intercom switch re-arms it (and
            # respawns ffmpeg, picking the now-present audio tags up).
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
                    "live_url": act["live_url"], "audio_url": act.get("audio_url"),
                    "protocol": act["protocol"], "stdin": act.get("stdin"),
                    "line_q": act.get("line_q"),
                    "session": act["session"], "product_id": act["product_id"],
                    "device_name": act["device_name"],
                    "intercom_armed": False,
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
    # Video-only at start: intercom isn't armed yet, so the device isn't
    # putting audio tags into live_url yet either - ffmpeg's initial probe
    # will see video only. /stream/intercom respawns this once armed, so a
    # fresh probe picks up the now-present audio tags (see
    # _spawn_ffmpeg_republish's docstring for why a respawn is required).
    ffmpeg_proc = _spawn_ffmpeg_republish(act["live_url"], rtsp_url)

    with _streams_lock:
        _active_streams[did] = {
            "p2p_proc": act["p2p_proc"], "ffmpeg_proc": ffmpeg_proc, "rtsp_url": rtsp_url,
            "live_url": act["live_url"], "audio_url": act.get("audio_url"),
            "started_at": time.time(), "protocol": act["protocol"],
            "session": act["session"], "product_id": act["product_id"],
            "device_name": act["device_name"], "line_q": act.get("line_q"),
            "stdin": act.get("stdin"),
            "intercom_armed": False,
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

    # If the mic/intercom was armed, disarm it before tearing down.
    if entry.get("intercom_armed"):
        try:
            camera_action(
                entry["protocol"], did, AIID_VOICE_OPERATE, PIID_VOICE_OPERATE,
                {"session": entry["session"], "operType": "intercom", "operation": "end"},
            )
        except Exception:
            app.logger.warning("stream/stop VOICE_OPERATE end failed", exc_info=True)

    stdout = entry.get("stdin")
    if stdout:
        try:
            stdout.close()
        except Exception:
            pass

    for proc in (entry["ffmpeg_proc"], entry["p2p_proc"]):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _safe_disconnect(entry.get("protocol"))

    return jsonify({"success": True, "was_running": True})


@app.route("/stream/intercom", methods=["POST"])
def stream_intercom():
    """Arm/disarm the intercom (vacuum-mic) layer on a RUNNING stream. This is
    the audio-out toggle: on arms the mic, which makes the device start muxing
    AAC audio tags directly into the SAME live_url FLV feed /stream/start
    already opened (confirmed against a real device - there's no separate mic
    stream to open); off disarms it. The video stream itself keeps running
    either way - call /stream/start to (re)open it."""
    body = _require_body("did", "on")
    did = body["did"]
    on = bool(body.get("on"))

    with _streams_lock:
        entry = _active_streams.get(did)
        running = bool(entry and entry["p2p_proc"].poll() is None)
        session = entry["session"] if entry else None
        protocol = entry["protocol"] if entry else None

    if not running or not session:
        return jsonify({"success": False, "error": "No running stream to toggle intercom on. Start /stream/start first."}), 409

    operation = "start" if on else "end"
    resp = camera_action(
        protocol, did, AIID_VOICE_OPERATE, PIID_VOICE_OPERATE,
        {"session": session, "operType": "intercom", "operation": operation},
    )
    confirmed = False
    if on:
        confirmed = _wait_intercom_cloud(protocol, did, session)
    armed = confirmed if on else False
    app.logger.info("stream/intercom %s for %s -> resp=%s confirmed=%s", operation, did, resp, confirmed)

    # ffmpeg has to be restarted for its RTSP output to pick up the change:
    # RTSP's SDP is negotiated once from ffmpeg's initial probe of live_url,
    # so a process that started before arming (video-only at the time) never
    # retroactively adds the audio track once the device starts sending it,
    # and one that started while armed keeps announcing audio even after
    # disarming (silence, not a dropped track). A fresh probe right now
    # reflects whatever the device is actually sending at this instant.
    with _streams_lock:
        cur = _active_streams.get(did)
        if cur:
            cur["intercom_armed"] = armed
            old_ffmpeg = cur["ffmpeg_proc"]
            live_url, rtsp_url = cur["live_url"], cur["rtsp_url"]
        else:
            old_ffmpeg = None

    if old_ffmpeg is not None:
        # Kill before spawning the replacement (outside the lock - this can
        # block up to 5s): MediaMTX only accepts one publisher per path, so a
        # brief overlap would make the new ffmpeg fail to publish rather than
        # take over cleanly.
        _kill(old_ffmpeg)
        new_ffmpeg = _spawn_ffmpeg_republish(live_url, rtsp_url)
        with _streams_lock:
            cur = _active_streams.get(did)
            if cur is None or cur["p2p_proc"].poll() is not None:
                new_ffmpeg.terminate()
            else:
                cur["ffmpeg_proc"] = new_ffmpeg

    return jsonify({"success": True, "intercom_armed": armed})


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
        intercom = bool(entry.get("intercom_armed")) if entry else False
    return jsonify({"running": running, "rtsp_url": rtsp_url, "intercom_armed": intercom})


@app.route("/latest.jpg", methods=["GET"])
def latest():
    did = request.args.get("did")
    if not did:
        abort(400, "Missing required query param: did")
    path = os.path.join(_media_dir(did), "latest.jpg")
    if not os.path.exists(path):
        abort(404, "No snapshot has been captured yet for this device")
    return send_file(path, mimetype="image/jpeg")


@app.route("/api/audio/pack", methods=["GET"])
def audio_pack():
    """Serve the built custom voice pack (upload.tar.gz) to the integration,
    which places it under Home Assistant's config/www so the vacuum can fetch it.

    The add-on (which owns ffmpeg + the audio files) builds the pack on Apply;
    the integration pulls it from here and writes it to the /local URL the
    robot downloads.
    """
    path = os.path.join(AUDIO_ROOT, "upload.tar.gz")
    if not os.path.exists(path):
        abort(404, "No voice pack built yet")
    return send_file(path, mimetype="application/gzip")


@app.route("/register", methods=["POST"])
def register():
    """Device registration pushed by the dreame_vacuum_unlocked_integration integration.

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
        "tasks": config_store.list_tasks(request.args.get("did")),
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
    slug = config_store.slugify(body.get("slug") or body["name"])
    if not slug:
        abort(400, "Could not make an id from that name - use letters or numbers")
    try:
        validated = step_schema.validate_steps(body["steps"])
    except step_schema.StepError as err:
        abort(400, str(err))
    config_store.save_task(slug, body["did"], body["name"], validated)
    return jsonify({"success": True, "task": config_store.get_task(slug)})


@app.route("/tasks/<slug>", methods=["GET"])
def get_task(slug):
    task = config_store.get_task(slug)
    if not task:
        abort(404, f"No task '{slug}'")
    return jsonify({"task": task})


@app.route("/tasks/<slug>", methods=["DELETE"])
def remove_task(slug):
    if not config_store.delete_task(slug):
        abort(404, f"No task '{slug}'")
    return jsonify({"success": True})


@app.route("/tasks/<slug>/calls", methods=["GET"])
def task_calls(slug):
    """The task as Home Assistant service calls - what the integration runs and
    what the export writes out, from one place so they cannot diverge."""
    task = config_store.get_task(slug)
    if not task:
        abort(404, f"No task '{slug}'")
    device = store.get_device(task["did"]) or {}
    entities = device.get("entities") or {}
    vacuum = request.args.get("vacuum") or entities.get("vacuum")
    if not vacuum:
        abort(409, "This vacuum has not registered its entities with the add-on yet")
    try:
        calls = step_schema.to_service_calls(task["steps"], vacuum)
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
