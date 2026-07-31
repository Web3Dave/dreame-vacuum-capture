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

import json
import os
import shutil
import time


from flask import Flask, abort, jsonify, redirect, render_template, request, send_file

import ha_client
import yaml

import steps as step_schema
import store

app = Flask(__name__)

UI_PORT = int(os.environ.get("COMPANION_UI_PORT", "8100"))


def _addon_version() -> str:
    """Our own version, used to bust caches on the JavaScript we serve.

    Read from config.yaml rather than duplicated in code, so bumping the
    add-on is the only place a version has to change. Falls back to a clock
    reading: a cache that is never reused beats a stale one that breaks a
    page in a way nobody can diagnose.
    """
    try:
        with open(os.path.join(os.path.dirname(__file__), "config.yaml")) as handle:
            return str(yaml.safe_load(handle).get("version") or int(time.time()))
    except Exception:  # noqa: BLE001
        return str(int(time.time()))


ADDON_VERSION = _addon_version()


def _ingress_base() -> str:
    """Ingress serves us under a generated path prefix; links must respect it."""
    return request.headers.get("X-Ingress-Path", "")


def _viewer() -> str | None:
    """Ingress passes the authenticated HA user - no separate login needed."""
    return request.headers.get("X-Remote-User-Display-Name")


SNAPSHOT_ROOT = "/media/dreame-capture/snapshots"

# How many snapshots the Tags page shows per tag before "view all" takes over.
TAG_PREVIEW_COUNT = 20
# Page size for the tag detail view's scroll-to-load-more.
TAG_PAGE_SIZE = 40


def _safe_tag(value):
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (value or "").strip())
    return cleaned.strip("_")[:48].lower() or "general"


def _snapshot_index(tag=None, limit=None):
    """Snapshots grouped by tag, newest first within each.

    latest.jpg is skipped: it duplicates whichever timestamped file is newest.

    `limit` caps how many snapshots are returned per tag - `count` is always
    the true total, so a caller that only wants a preview row (the Tags page)
    can still tell the user there are more without fetching them.
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
        total = len(shots)
        groups.append({
            "tag": name, "count": total,
            "snapshots": shots[:limit] if limit is not None else shots,
        })
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


MAP_ROOT = "/media/dreame-capture/maps"


@app.route("/api/map/<did>")
def api_map(did):
    """Geometry for the picker, refreshing the image first if asked.

    The refresh goes through the integration: it is the only side that can
    fetch a frame from the vacuum.
    """
    if request.args.get("refresh"):
        device = store.get_device(did) or {}
        entity = (device.get("entities") or {}).get("vacuum")
        if entity:
            started = time.time()
            ok, detail = ha_client.call_service_result(
                "dreame_vacuum_core", "publish_map", {"entity_id": entity}, timeout=90
            )
            if not ok:
                # Prefer the integration's own recorded reason: Home Assistant's
                # is always "Server got itself in trouble".
                detail = _last_failure_reason(did, started) or detail
                return jsonify({"error": detail or "Could not refresh the map"}), 502
    path = os.path.join(MAP_ROOT, f"{_safe_tag(did)}.json")
    if not os.path.exists(path):
        return jsonify({"error": "No map yet - try Refresh"}), 404
    with open(path) as handle:
        return jsonify({"meta": json.load(handle)})


@app.route("/map/<did>/document")
def map_document(did):
    path = os.path.join(MAP_ROOT, f"{_safe_tag(did)}.map.json")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="application/json")


@app.route("/map/<did>.png")
def map_image(did):
    path = os.path.join(MAP_ROOT, f"{_safe_tag(did)}.png")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/png")


@app.route("/tasks")
def tasks():
    return render_template("tasks.html", base=_ingress_base(), viewer=_viewer(),
                           page="tasks", addon_version=ADDON_VERSION)


@app.route("/tasks/new")
def task_new():
    return render_template("task_editor.html", base=_ingress_base(), viewer=_viewer(),
                           page="tasks", addon_version=ADDON_VERSION, task=None)


@app.route("/tasks/<slug>/edit")
def task_edit(slug):
    """A real URL rather than a modal: refresh keeps your place, the browser
    back button is honest navigation, and an edit screen can be linked to."""
    task = store.get_task(slug)
    if not task:
        abort(404)
    return render_template("task_editor.html", base=_ingress_base(), viewer=_viewer(),
                           page="tasks", addon_version=ADDON_VERSION, task=task)


@app.route("/api/tags")
def api_tags():
    # Folders on disk are tags in practice - snapshots taken before the table
    # existed, or via the service with an ad-hoc tag. Adopt them so the
    # dropdown offers what the media browser already shows.
    if os.path.isdir(SNAPSHOT_ROOT):
        store.ensure_tags(
            name for name in os.listdir(SNAPSHOT_ROOT)
            if os.path.isdir(os.path.join(SNAPSHOT_ROOT, name))
        )
    return jsonify({"tags": store.list_tags()})


@app.route("/api/tags", methods=["POST"])
def api_create_tag():
    body = request.get_json(silent=True) or {}
    tag = store.save_tag(body.get("name") or "")
    if not tag:
        return jsonify({"error": "A tag needs letters or numbers in its name"}), 400
    return jsonify({"tag": tag})


@app.route("/api/tags/<tag>/latest")
def api_tag_latest(tag):
    """The newest snapshot for a tag - what the crop is drawn on.

    The timestamped file rather than latest.jpg, so the browser's cache can
    never show yesterday's photo under today's name.
    """
    groups = _snapshot_index(tag)
    if not groups or not groups[0]["snapshots"]:
        return jsonify({"error": "No snapshots with this tag yet - run a task "
                                 "that takes one first"}), 404
    newest = groups[0]["snapshots"][0]
    return jsonify({"tag": _safe_tag(tag), "filename": newest["filename"],
                    "taken_at": newest["taken_at"]})


@app.route("/classifications")
def classifications():
    return render_template("classifications.html", base=_ingress_base(),
                           viewer=_viewer(), page="classifications")


@app.route("/api/classifications")
def api_classifications():
    # Tag names ride along so the page needs no second fetch to label chips.
    return jsonify({
        "classifications": store.list_classifiers(),
        "tags": store.list_tags(),
    })


@app.route("/api/classifications", methods=["POST"])
def api_create_classification():
    body = request.get_json(silent=True) or {}
    try:
        made = store.create_classifier(body.get("name") or "")
    except ValueError as err:
        return jsonify({"error": str(err)}), 409
    if not made:
        return jsonify({"error": "A classification needs letters or numbers "
                                 "in its name"}), 400
    return jsonify({"classification": made})


@app.route("/api/classifications/<cid>", methods=["DELETE"])
def api_delete_classification(cid):
    if not store.delete_classifier(cid):
        return jsonify({"error": "No such classification"}), 404
    return jsonify({"success": True})


def _valid_crop(crop):
    """A normalised square-ish region: four floats in [0,1] with real area.

    Squareness is not checked here - it is enforced in image-pixel space by
    the UI, and normalised coordinates of a pixel square are only equal-sided
    when the image happens to be square itself.
    """
    if not (isinstance(crop, list) and len(crop) == 4):
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in crop)
    except (TypeError, ValueError):
        return None
    if not all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2)):
        return None
    if x2 - x1 < 0.01 or y2 - y1 < 0.01:
        return None
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


@app.route("/api/classifications/<cid>/tags/<tag_id>", methods=["PUT"])
def api_link_classification_tag(cid, tag_id):
    if not store.get_classifier(cid):
        return jsonify({"error": "No such classification"}), 404
    if not any(t["id"] == tag_id for t in store.list_tags()):
        return jsonify({"error": "No such tag"}), 404
    body = request.get_json(silent=True) or {}
    crop = _valid_crop(body.get("crop"))
    if crop is None:
        return jsonify({"error": "crop must be [x1, y1, x2, y2] as fractions "
                                 "of the image, with some area to it"}), 400
    store.set_classifier_tag(cid, tag_id, crop)
    return jsonify({"classification": store.get_classifier(cid)})


@app.route("/api/classifications/<cid>/tags/<tag_id>", methods=["DELETE"])
def api_unlink_classification_tag(cid, tag_id):
    if not store.unlink_classifier_tag(cid, tag_id):
        return jsonify({"error": "That tag is not linked"}), 404
    return jsonify({"classification": store.get_classifier(cid)})


def _busy_by_device():
    """What each vacuum is doing, from the integration's own live state.

    Read from Home Assistant rather than tracked here: the integration
    performs the errands, so it is the only thing that actually knows. A
    vacuum we cannot read is reported as not busy - refusing to let someone
    press Run because the API is briefly unavailable would be worse than
    letting the integration refuse it properly.
    """
    busy = {}
    for device in store.list_devices():
        entity = (device.get("entities") or {}).get("vacuum")
        if not entity:
            continue
        state = ha_client.get_state(entity)
        attrs = (state or {}).get("attributes") or {}
        busy[device["did"]] = {
            "running": bool(attrs.get("task_running")),
            "task": attrs.get("task_id"),
            "run_id": attrs.get("task_run_id"),
            "command": attrs.get("task_command"),
            "step": attrs.get("task_step"),
            "steps": attrs.get("task_steps"),
            "detail": attrs.get("task_detail"),
            "vacuum": entity,
            "name": device.get("name") or device["did"],
        }
    return busy


@app.route("/api/tasks", methods=["GET"])
def api_tasks():
    busy = _busy_by_device()
    tasks = store.list_tasks()
    for task in tasks:
        state = busy.get(task["did"]) or {}
        # Two different reasons a task cannot start: it is itself running, or
        # its vacuum is busy with something else. The UI says which.
        task["running"] = bool(state.get("running") and state.get("task") == task["slug"])
        task["device_busy"] = bool(state.get("running")) and not task["running"]
        task["busy_with"] = state.get("task") or state.get("command") if task["device_busy"] else None
        task["progress"] = (
            {"step": state.get("step"), "steps": state.get("steps"),
             "detail": state.get("detail"), "run_id": state.get("run_id")}
            if task["running"] else None
        )
    return jsonify({
        "tasks": tasks,
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
    # The id is editable, so two cases need telling apart: a *new* task whose
    # id collides with an existing one (refused - saving would silently
    # overwrite someone else's task), and an *edit* that changed the id
    # (allowed - the old row is renamed away, because an automation calls a
    # task by id and a duplicate under the old id would keep answering).
    previous = store.slugify(body.get("previous_slug") or "")
    if not previous and store.get_task(slug):
        return jsonify({"error": f"The id '{slug}' is already in use"}), 409
    if previous and previous != slug and store.get_task(slug):
        return jsonify({"error": f"The id '{slug}' is already in use"}), 409
    try:
        validated = step_schema.validate_steps(body["steps"])
    except step_schema.StepError as err:
        return jsonify({"error": str(err)}), 400
    store.save_task(slug, body["did"], body["name"], validated)
    if previous and previous != slug:
        store.delete_task(previous)
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
    started = time.time()
    ok, detail = ha_client.call_service_result(
        "dreame_vacuum_core", "start_task", {"entity_id": vacuum, "task": slug}
    )
    if not ok:
        # Home Assistant answers any service error with a bare 500 and keeps the
        # reason in its own log. The integration records that reason here as it
        # refuses, so prefer our own copy over HA's "Server got itself in
        # trouble".
        detail = _last_failure_reason(task["did"], started) or detail
    return jsonify({"success": ok, "error": detail}), (200 if ok else 502)


def _last_failure_reason(did, since):
    """The error from this device's newest run, if it just failed.

    Only the newest is considered: skipping over a later success to find an
    older failure would report a reason from a different run entirely.
    """
    runs = store.list_runs(did, limit=1)
    if not runs:
        return None
    run = runs[0]
    if run.get("running") or run.get("ok"):
        return None
    if run.get("at", 0) < int(since) - 5:
        return None
    return (run.get("detail") or {}).get("error") or run.get("summary")


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


@app.route("/tags")
def tags_page():
    return render_template("tags.html", base=_ingress_base(), viewer=_viewer(), page="tags")


@app.route("/snapshots")
def snapshots_redirect():
    """The tab this page used to be. Bookmarks keep working."""
    return redirect(f"{_ingress_base()}/tags")


@app.route("/api/snapshots")
def api_snapshots():
    return jsonify({"snapshots": _snapshot_index(request.args.get("tag"))})


@app.route("/api/tags/overview")
def api_tags_overview():
    """Everything the Tags page shows in one fetch: each tag with its
    snapshots and the classifications watching it.

    Driven by the tag table rather than the snapshot folders, so a tag
    created but never photographed still appears - it is a manageable thing,
    not just a folder that happens to exist.
    """
    if os.path.isdir(SNAPSHOT_ROOT):
        store.ensure_tags(
            name for name in os.listdir(SNAPSHOT_ROOT)
            if os.path.isdir(os.path.join(SNAPSHOT_ROOT, name))
        )
    snaps = {g["tag"]: g for g in _snapshot_index(limit=TAG_PREVIEW_COUNT)}
    watching = {}
    for c in store.list_classifiers():
        for link in c["tags"]:
            watching.setdefault(link["tag_id"], []).append(
                {"id": c["id"], "name": c["name"]}
            )
    return jsonify({"tags": [
        {
            **tag,
            "count": snaps.get(tag["id"], {}).get("count", 0),
            "snapshots": snaps.get(tag["id"], {}).get("snapshots", []),
            "classifications": watching.get(tag["id"], []),
        }
        for tag in store.list_tags()
    ]})


@app.route("/tags/<tag_id>")
def tag_detail(tag_id):
    """All of a tag's snapshots, loaded a page at a time as the user scrolls -
    the Tags page itself only ever shows a preview row."""
    safe = _safe_tag(tag_id)
    tag = next((t for t in store.list_tags() if t["id"] == safe), None)
    if not tag:
        abort(404)
    return render_template("tag_detail.html", base=_ingress_base(), viewer=_viewer(),
                           page="tags", tag=tag, page_size=TAG_PAGE_SIZE)


@app.route("/api/tags/<tag_id>/snapshots")
def api_tag_snapshots(tag_id):
    """One page of a tag's snapshots, newest first, for scroll-to-load-more."""
    try:
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", TAG_PAGE_SIZE))
    except (TypeError, ValueError):
        return jsonify({"error": "offset and limit must be numbers"}), 400
    if offset < 0 or limit < 1:
        return jsonify({"error": "offset must be >= 0 and limit must be >= 1"}), 400
    limit = min(limit, 200)

    groups = _snapshot_index(tag_id)
    all_shots = groups[0]["snapshots"] if groups else []
    page = all_shots[offset:offset + limit]
    return jsonify({
        "snapshots": page,
        "total": len(all_shots),
        "has_more": offset + limit < len(all_shots),
    })


@app.route("/api/tags/<tag_id>", methods=["PATCH"])
def api_rename_tag(tag_id):
    """Rename a tag. The id (the folder name, and what a step's tag field
    stores) does not change - see store.rename_tag for why."""
    body = request.get_json(silent=True) or {}
    safe = _safe_tag(tag_id)
    if not any(t["id"] == safe for t in store.list_tags()):
        return jsonify({"error": "No such tag"}), 404
    tag = store.rename_tag(safe, body.get("name") or "")
    if not tag:
        return jsonify({"error": "A tag needs letters or numbers in its name"}), 400
    return jsonify({"tag": tag})


@app.route("/api/tags/<tag_id>", methods=["DELETE"])
def api_delete_tag(tag_id):
    """Delete a tag, its classifier links, and its snapshots.

    The folder goes too, deliberately: leaving it would resurrect the tag on
    the next seed from disk, which reads as a delete that did not work.
    """
    safe = _safe_tag(tag_id)
    if not store.delete_tag(safe):
        return jsonify({"error": "No such tag"}), 404
    folder = os.path.join(SNAPSHOT_ROOT, safe)
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
    return jsonify({"success": True})


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
