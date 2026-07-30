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
import yaml

import steps as step_schema
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
        page="devices",
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


@app.route("/tasks")
def tasks():
    return render_template("tasks.html", base=_ingress_base(), viewer=_viewer(), page="tasks")


@app.route("/api/tasks", methods=["GET"])
def api_tasks():
    return jsonify({
        "tasks": store.list_tasks(),
        "devices": [
            {"did": d["did"], "name": d.get("name") or d["did"]}
            for d in store.list_devices()
        ],
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


@app.route("/api/tasks/yaml", methods=["POST"])
def api_steps_yaml():
    """Convert between steps and the YAML the editor shows.

    Round-tripped here rather than in the browser so both directions use the
    same validator the save path does - a YAML view that accepts something
    the save rejects would be worse than no YAML view.
    """
    body = request.get_json(silent=True) or {}
    if "yaml" in body:
        try:
            parsed = yaml.safe_load(body["yaml"]) or []
        except yaml.YAMLError as err:
            return jsonify({"error": f"Not valid YAML: {err}"}), 400
        try:
            return jsonify({"steps": step_schema.validate_steps(parsed)})
        except step_schema.StepError as err:
            return jsonify({"error": str(err)}), 400
    # type leads each step: it decides what the other keys mean, so reading it
    # third is needlessly hard.
    ordered = [
        {"type": step.get("type"), **{k: v for k, v in step.items() if k != "type"}}
        for step in (body.get("steps") or [])
    ]
    return jsonify({
        "yaml": yaml.safe_dump(ordered, sort_keys=False, default_flow_style=False)
    })


@app.route("/api/tasks", methods=["POST"])
def api_save_task():
    body = request.get_json(silent=True) or {}
    for field in ("did", "name", "steps"):
        if not body.get(field):
            return jsonify({"error": f"{field} is required"}), 400
    slug = store.slugify(body.get("slug") or body["name"])
    if not slug:
        return jsonify({"error": "Use letters or numbers in the name"}), 400
    try:
        validated = step_schema.validate_steps(body["steps"])
    except step_schema.StepError as err:
        return jsonify({"error": str(err)}), 400
    store.save_task(slug, body["did"], body["name"], validated)
    return jsonify({"task": store.get_task(slug)})


@app.route("/api/tasks/<slug>", methods=["DELETE"])
def api_delete_task(slug):
    if not store.delete_task(slug):
        return jsonify({"error": "No such task"}), 404
    return jsonify({"success": True})


@app.route("/api/tasks/<slug>/export")
def api_export_task(slug):
    """A scripts.yaml entry with the steps expanded.

    One-way on purpose: once pasted into Home Assistant it is the user's, and
    nothing here tries to keep the two in step.
    """
    task = store.get_task(slug)
    if not task:
        return jsonify({"error": "No such task"}), 404
    entities = (store.get_device(task["did"]) or {}).get("entities") or {}
    if not entities.get("vacuum"):
        return jsonify({"error": "This vacuum has not registered its entities yet"}), 409
    try:
        calls = step_schema.to_service_calls(
            task["steps"], entities["vacuum"], entities.get("stream")
        )
    except step_schema.StepError as err:
        return jsonify({"error": str(err)}), 409
    return jsonify({"yaml": _script_yaml(task, calls)})


@app.route("/api/tasks/<slug>/run", methods=["POST"])
def api_run_task(slug):
    task = store.get_task(slug)
    if not task:
        return jsonify({"error": "No such task"}), 404
    entities = (store.get_device(task["did"]) or {}).get("entities") or {}
    vacuum = entities.get("vacuum")
    if not vacuum:
        return jsonify({"error": "This vacuum has not registered its entities yet"}), 409
    # Runs through the integration rather than firing the steps from here, so
    # a run started in the UI is narrated and guarded exactly like one started
    # from an automation.
    ok, detail = ha_client.call_service_result(
        "dreame_vacuum_core", "start_task", {"entity_id": vacuum, "task": slug}
    )
    return jsonify({"success": ok, "error": detail}), (200 if ok else 502)


def _script_yaml(task, calls):
    """Hand-rolled rather than via a yaml library: the add-on image has no
    PyYAML, and the shape here is small and fixed."""
    lines = [
        f"{task['slug']}:",
        f"  alias: {task['name']}",
        "  mode: single",
        "  sequence:",
    ]
    for call in calls:
        lines.append(f"    - action: {call['action']}")
        target = call.get("target") or {}
        if target:
            lines.append("      target:")
            lines.append(f"        entity_id: {target['entity_id']}")
        data = call.get("data") or {}
        if data:
            lines.append("      data:")
            for key, value in data.items():
                if isinstance(value, bool):
                    rendered = "true" if value else "false"
                elif isinstance(value, str):
                    rendered = value
                elif isinstance(value, float) and value.is_integer():
                    rendered = str(int(value))
                else:
                    rendered = str(value)
                lines.append(f"        {key}: {rendered}")
    return "\n".join(lines) + "\n"


@app.route("/snapshots")
def snapshots():
    return render_template("snapshots.html", base=_ingress_base(), viewer=_viewer(), page="snapshots")


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
        page="activity",
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
