#!/usr/bin/env python3
"""Companion control panel - served through Home Assistant Ingress.

Runs on a *different* port from the machine API in app.py. That separation is
deliberate: the API is token-authenticated for the integration, while this UI
is reachable only through Ingress, which Home Assistant has already
authenticated. Nothing here is exposed to the LAN.

This is the placeholder shell: it proves the Ingress plumbing, the SQLite
device registry, and live state reads from Home Assistant. Patrol/security
route editing goes here next.
"""
from __future__ import annotations

import os

import os

from flask import Flask, abort, jsonify, render_template, request, send_file

import ha_client
import store

app = Flask(__name__)

UI_PORT = int(os.environ.get("COMPANION_UI_PORT", "8100"))


def _ingress_base() -> str:
    """Ingress serves us under a generated path prefix; links must respect it."""
    return request.headers.get("X-Ingress-Path", "")


def _viewer() -> str | None:
    """Ingress passes the authenticated HA user - no separate login needed."""
    return request.headers.get("X-Remote-User-Display-Name")


SNAPSHOT_ROOT = "/media/dreame-capture/snapshots"


def _safe_tag(value):
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (value or "").strip())
    return cleaned.strip("_")[:48].lower() or "general"


def _snapshot_index(tag=None):
    """Snapshots grouped by tag, newest first within each.

    latest.jpg is skipped: it duplicates whichever timestamped file is newest.
    """
    if not os.path.isdir(SNAPSHOT_ROOT):
        return []
    wanted = _safe_tag(tag) if tag else None
    groups = []
    for name in sorted(os.listdir(SNAPSHOT_ROOT)):
        folder = os.path.join(SNAPSHOT_ROOT, name)
        if not os.path.isdir(folder) or (wanted and name != wanted):
            continue
        shots = []
        for entry in os.listdir(folder):
            if not entry.lower().endswith(".jpg") or entry == "latest.jpg":
                continue
            try:
                taken = int(os.stat(os.path.join(folder, entry)).st_mtime)
            except OSError:
                continue
            shots.append({"filename": entry, "taken_at": taken})
        if not shots:
            continue
        shots.sort(key=lambda item: item["taken_at"], reverse=True)
        groups.append({"tag": name, "count": len(shots), "snapshots": shots})
    return groups


@app.route("/")
def index():
    devices = store.list_devices()
    ha_up = ha_client.available()

    # Enrich with live HA state rather than caching it here.
    for dev in devices:
        dev["state"] = {}
        if ha_up:
            for role, entity_id in (dev.get("entities") or {}).items():
                st = ha_client.get_state(entity_id)
                if st:
                    dev["state"][role] = {
                        "entity_id": entity_id,
                        "state": st.get("state"),
                        "attributes": st.get("attributes", {}),
                    }

    return render_template(
        "index.html",
        devices=devices,
        ha_up=ha_up,
        viewer=_viewer(),
        base=_ingress_base(),
        routes=store.list_routes(),
    )


@app.route("/api/devices")
def api_devices():
    """Also useful for debugging the registration handshake."""
    return jsonify({"devices": store.list_devices()})


@app.route("/api/routes")
def api_routes():
    return jsonify({"routes": store.list_routes(request.args.get("did"))})


@app.route("/api/service", methods=["POST"])
def api_service():
    """Proxy a HA service call.

    The UI drives the vacuum through Home Assistant rather than talking to the
    device directly, so there is exactly one control path and HA stays the
    source of truth.
    """
    body = request.get_json(silent=True) or {}
    domain = body.get("domain")
    service = body.get("service")
    if not domain or not service:
        return jsonify({"error": "domain and service are required"}), 400
    ok = ha_client.call_service(domain, service, body.get("data") or {})
    return jsonify({"success": ok}), (200 if ok else 502)


@app.route("/snapshots")
def snapshots():
    return render_template("snapshots.html", base=_ingress_base(), viewer=_viewer())


@app.route("/api/snapshots")
def api_snapshots():
    return jsonify({"snapshots": _snapshot_index(request.args.get("tag"))})


@app.route("/snapshot/<tag>/<filename>")
def snapshot_image(tag, filename):
    """Served through Ingress, so Home Assistant has already authenticated the
    viewer - no token handling needed here."""
    safe = os.path.basename(filename)
    if not safe.lower().endswith(".jpg"):
        abort(404)
    path = os.path.join(SNAPSHOT_ROOT, _safe_tag(tag), safe)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")


@app.route("/activity")
def activity():
    return render_template(
        "activity.html",
        base=_ingress_base(),
        viewer=_viewer(),
        runs=store.list_runs(limit=50),
    )


@app.route("/api/runs")
def api_runs():
    return jsonify({"runs": store.list_runs(request.args.get("did"), 50)})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "devices": len(store.list_devices()), "ha": ha_client.available()})


if __name__ == "__main__":
    store.init()
    from waitress import serve

    serve(app, host="0.0.0.0", port=UI_PORT, threads=4)
