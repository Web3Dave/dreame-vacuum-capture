"""The configuration file: tasks, tags and classifications.

These are *settings* - things a person authored and would reasonably want to
read, diff, back up and paste into a support thread. Frigate keeps that kind
of thing in one YAML file rather than a database, and for the same reasons it
lives here too.

What stays in SQLite (store.py) is everything that is *not* settings: the
device list the integration pushes, and the run history. Neither is authored,
both regenerate themselves, and mixing an append-only activity log into a file
someone hand-edits would be unpleasant.

Round-tripped with ruamel rather than PyYAML so that a comment someone wrote
survives a save made from the UI. Losing them would train people not to
annotate their config, which is most of the point of it being a file.
"""
from __future__ import annotations

import copy
import io
import threading
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

import steps as step_schema

CONFIG_PATH = Path("/data/config.yaml")

TOP_LEVEL_KEYS = ("tags", "tasks", "classifications", "settings")

# The two shapes a classification can learn, straight from Frigate's own
# distinction: a tracked object has one sub_label (a specific identity - a
# name, a finer type) but can carry several independent attributes at once
# (helmet: yes, vest: yes). Neither applies to *us* in Frigate's sense - we
# have no tracked object to attach a field to - but the vocabulary is worth
# keeping, because it is the same choice a person is making: "is this one
# fact replacing the last" vs "are these facts that can coexist".
CLASSIFICATION_TYPES = ("sub_label", "attribute")

# What classify_train / classify_infer can actually run on today. Frigate
# offers a real choice here (cpu, onnx, openvino, edgetpu, hailo, ...)
# because it ships a plugin per backend; this add-on has one working path -
# TensorFlow Lite on CPU - so that is the only value this will accept. Kept
# as a real set rather than a single hardcoded string so adding a second
# backend later is a matter of implementing it and appending here, not
# reworking how the setting is stored or validated.
SUPPORTED_DEVICES = ("cpu",)

STARTER_CONFIG = """\
# Dreame Vacuum Companion configuration.
#
# Everything here can also be edited from the UI - the two are the same data.
# Comments you add are preserved when the UI writes to this file.

# Tags group the photos a task takes. The key is the id: it is the folder
# snapshots are saved under and what a task step refers to, so renaming a tag
# means changing its name, not its key.
tags: {}

# Tasks are sequences of moves - drive somewhere, face a direction, photograph
# it. The key is the id an automation calls with dreame_vacuum_core.start_task.
tasks: {}

# Classifications read a state from the snapshots of the tags they are linked
# to. Each link carries its own crop, because the same state seen from two
# viewpoints needs two different squares. A classification does nothing until
# it is configured with a type (sub_label or attribute) and at least two
# classes - the UI prompts for these when they are missing.
classifications: {}

# Add-on-wide settings, not tied to any one classification.
settings: {}
"""

_lock = threading.RLock()
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # do not fold long lines - a wrapped step reads badly

# Parsed config, with the mtime and size it was read at. Re-read when the file
# changes underneath us, which it does whenever someone edits it by hand.
_cache: dict | None = None
_cache_stamp: tuple[float, int] | None = None


class ConfigError(ValueError):
    """A configuration that cannot be used, with a reason worth showing."""


def _stamp() -> tuple[float, int] | None:
    try:
        stat = CONFIG_PATH.stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def _parse(text: str) -> dict:
    try:
        data = _yaml.load(io.StringIO(text))
    except YAMLError as err:
        raise ConfigError(f"Not valid YAML: {err}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("The configuration must be a mapping of sections")
    for key in TOP_LEVEL_KEYS:
        if data.get(key) is None:
            data[key] = {}
        elif not isinstance(data[key], dict):
            raise ConfigError(f"'{key}' must be a mapping keyed by id")
    unknown = [k for k in data if k not in TOP_LEVEL_KEYS]
    if unknown:
        raise ConfigError(
            f"Unknown section(s): {', '.join(sorted(unknown))}. "
            f"Expected: {', '.join(TOP_LEVEL_KEYS)}"
        )
    return data


def load() -> dict:
    """The parsed config, re-read when the file has changed on disk."""
    global _cache, _cache_stamp
    with _lock:
        stamp = _stamp()
        if stamp is None:
            # No file yet: hand back an empty shape rather than failing, so a
            # fresh install renders empty pages instead of an error.
            return _parse("")
        if _cache is None or stamp != _cache_stamp:
            _cache = _parse(CONFIG_PATH.read_text())
            _cache_stamp = stamp
        return _cache


def raw() -> str:
    """The file as written, for the editor. Never reformatted on the way out."""
    with _lock:
        if not CONFIG_PATH.exists():
            return STARTER_CONFIG
        return CONFIG_PATH.read_text()


def _write(data: dict) -> None:
    """Serialise and replace the file atomically.

    Written to a sibling temp file and renamed so a crash mid-write cannot
    leave a half-file that fails to parse on the next boot.
    """
    global _cache, _cache_stamp
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    _yaml.dump(data, buffer)
    temp = CONFIG_PATH.with_suffix(".yaml.tmp")
    temp.write_text(buffer.getvalue())
    temp.replace(CONFIG_PATH)
    _cache = data
    _cache_stamp = _stamp()


def save_raw(text: str) -> None:
    """Validate then write the editor's text verbatim.

    Verbatim matters: the file keeps the author's formatting and comments
    exactly, rather than being round-tripped through the serialiser.
    """
    data = _parse(text)
    validate(data)
    with _lock:
        global _cache, _cache_stamp
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = CONFIG_PATH.with_suffix(".yaml.tmp")
        temp.write_text(text)
        temp.replace(CONFIG_PATH)
        _cache = data
        _cache_stamp = _stamp()


def validate(data: dict) -> None:
    """Raise ConfigError describing everything wrong with a parsed config.

    All problems at once rather than the first: fixing a config one error per
    save is a miserable way to spend an evening.
    """
    problems: list[str] = []

    tags = data.get("tags") or {}
    for tag_id, body in tags.items():
        where = f"tags.{tag_id}"
        if not _is_id(tag_id):
            problems.append(f"{where}: ids may only use a-z, 0-9 and underscore")
        if not isinstance(body, dict):
            problems.append(f"{where}: must be a mapping with a 'name'")
            continue
        if not str(body.get("name") or "").strip():
            problems.append(f"{where}: 'name' is required")

    tasks = data.get("tasks") or {}
    for slug, body in tasks.items():
        where = f"tasks.{slug}"
        if not _is_id(slug):
            problems.append(f"{where}: ids may only use a-z, 0-9 and underscore")
        if not isinstance(body, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        if not str(body.get("name") or "").strip():
            problems.append(f"{where}: 'name' is required")
        if not str(body.get("did") or "").strip():
            problems.append(f"{where}: 'did' is required - which vacuum runs this")
        try:
            step_schema.validate_steps(_plain(body.get("steps")))
        except step_schema.StepError as err:
            problems.append(f"{where}: {err}")

    classifications = data.get("classifications") or {}
    for cid, body in classifications.items():
        where = f"classifications.{cid}"
        if not _is_id(cid):
            problems.append(f"{where}: ids may only use a-z, 0-9 and underscore")
        if not isinstance(body, dict):
            problems.append(f"{where}: must be a mapping with a 'name'")
            continue
        if not str(body.get("name") or "").strip():
            problems.append(f"{where}: 'name' is required")
        links = body.get("tags") or {}
        if not isinstance(links, dict):
            problems.append(f"{where}.tags: must be a mapping keyed by tag id")
            continue
        for tag_id, link in links.items():
            link_where = f"{where}.tags.{tag_id}"
            if tag_id not in tags:
                # A crop against a tag that does not exist can never fire, and
                # the silence would be baffling.
                problems.append(f"{link_where}: no tag with this id")
            crop = (link or {}).get("crop") if isinstance(link, dict) else None
            if _bad_crop(crop):
                problems.append(
                    f"{link_where}.crop: must be [x1, y1, x2, y2] as fractions "
                    "of the image between 0 and 1, with some area to it"
                )

        # These three only matter once someone has set at least one of them -
        # an unconfigured classification (freshly created, nothing chosen
        # yet) is a valid, ordinary state, not an error.
        want_type = body.get("classification_type")
        classes = body.get("classes")
        touched = want_type is not None or classes is not None or "threshold" in body
        if touched:
            if want_type not in CLASSIFICATION_TYPES:
                problems.append(
                    f"{where}.classification_type: must be one of "
                    f"{', '.join(CLASSIFICATION_TYPES)}"
                )
            if not isinstance(classes, list) or len(classes) < 2:
                problems.append(f"{where}.classes: needs at least two classes")
            elif len({str(c).strip().lower() for c in classes}) != len(classes):
                problems.append(f"{where}.classes: class names must be distinct")
            elif any(not str(c).strip() for c in classes):
                problems.append(f"{where}.classes: a class name cannot be empty")
            threshold = body.get("threshold", 0.8)
            try:
                if not (0.0 < float(threshold) <= 1.0):
                    raise ValueError
            except (TypeError, ValueError):
                problems.append(f"{where}.threshold: must be a number between 0 and 1")

    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        problems.append("settings: must be a mapping")
    else:
        if "mobilenet_weights_path" in settings and settings["mobilenet_weights_path"] is not None:
            if not isinstance(settings["mobilenet_weights_path"], str):
                problems.append("settings.mobilenet_weights_path: must be a text path")
        device = settings.get("device")
        if device is not None and device not in SUPPORTED_DEVICES:
            # Validated here too, not just left to the dropdown to enforce -
            # an API call can bypass a disabled <option> the UI would never
            # let a person pick.
            problems.append(
                f"settings.device: '{device}' is not available in this add-on "
                f"yet. Supported: {', '.join(SUPPORTED_DEVICES)}"
            )

    if problems:
        raise ConfigError("\n".join(problems))


def _is_id(value) -> bool:
    text = str(value or "")
    return bool(text) and all(c.isascii() and (c.isdigit() or c.islower() or c == "_")
                              for c in text)


def _bad_crop(crop) -> bool:
    if not isinstance(crop, (list, tuple)) or len(crop) != 4:
        return True
    try:
        x1, y1, x2, y2 = (float(v) for v in crop)
    except (TypeError, ValueError):
        return True
    if not all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2)):
        return True
    return x2 - x1 < 0.01 or y2 - y1 < 0.01


def _plain(value):
    """A ruamel structure as ordinary dicts/lists.

    Anything leaving this module is plain Python: ruamel's CommentedMap is a
    dict subclass that carries formatting with it, and letting that reach
    jsonify or the step validator invites surprises.
    """
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def slugify(value):
    """A stable, typeable handle - the key a section is stored under."""
    lowered = str(value or "").strip().lower()
    cleaned = "".join(c if (c.isalnum() and c.isascii()) or c == "_" else "_"
                      for c in lowered)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:48]


def _section(data: dict, key: str) -> dict:
    if data.get(key) is None:
        data[key] = {}
    return data[key]


# -- tags -----------------------------------------------------------------
def list_tags():
    tags = load().get("tags") or {}
    return sorted(
        ({"id": str(tid), "name": str((body or {}).get("name") or tid)}
         for tid, body in tags.items()),
        key=lambda t: t["name"].lower(),
    )


def save_tag(name):
    tag_id = slugify(name)
    if not tag_id:
        return None
    with _lock:
        data = load()
        _section(data, "tags")[tag_id] = {"name": str(name).strip()}
        _write(data)
    return {"id": tag_id, "name": str(name).strip()}


def rename_tag(tag_id, name):
    """Change a tag's display name. The id is untouched: it is the snapshot
    folder name and what a task step stores, so changing it would orphan every
    photo already taken."""
    name = str(name or "").strip()
    if not name:
        return None
    with _lock:
        data = load()
        tags = data.get("tags") or {}
        if tag_id not in tags:
            return None
        entry = tags[tag_id]
        if isinstance(entry, dict):
            entry["name"] = name
        else:
            tags[tag_id] = {"name": name}
        _write(data)
    return {"id": tag_id, "name": name}


def ensure_tags(ids):
    """Adopt tags that exist only as folders on disk.

    Snapshots taken before a tag was declared already have folders; the UI
    should offer them rather than pretend they are not there.
    """
    with _lock:
        data = load()
        tags = _section(data, "tags")
        added = False
        for raw_id in ids:
            tag_id = slugify(raw_id)
            if tag_id and tag_id not in tags:
                tags[tag_id] = {"name": tag_id.replace("_", " ")}
                added = True
        if added:
            _write(data)


def delete_tag(tag_id):
    """Remove a tag and any classification links to it. The caller owns the
    snapshot folder - files are not this module's business."""
    with _lock:
        data = load()
        tags = data.get("tags") or {}
        if tag_id not in tags:
            return False
        del tags[tag_id]
        for body in (data.get("classifications") or {}).values():
            links = (body or {}).get("tags") or {}
            links.pop(tag_id, None)
        _write(data)
    return True


# -- tasks ----------------------------------------------------------------
def _task(slug, body):
    body = body or {}
    return {
        "slug": str(slug),
        "did": str(body.get("did") or ""),
        "name": str(body.get("name") or slug),
        "steps": _plain(body.get("steps")) or [],
    }


def list_tasks(did=None):
    tasks = load().get("tasks") or {}
    out = [_task(slug, body) for slug, body in tasks.items()]
    if did:
        out = [t for t in out if t["did"] == str(did)]
    return sorted(out, key=lambda t: t["name"].lower())


def get_task(slug):
    tasks = load().get("tasks") or {}
    return _task(slug, tasks[slug]) if slug in tasks else None


def save_task(slug, did, name, steps):
    with _lock:
        data = load()
        tasks = _section(data, "tasks")
        existing = tasks.get(slug)
        if isinstance(existing, dict):
            # Update in place so any comments attached to this task's keys stay
            # attached to them.
            existing["name"] = name
            existing["did"] = str(did)
            existing["steps"] = _plain(steps)
        else:
            tasks[slug] = {"name": name, "did": str(did), "steps": _plain(steps)}
        _write(data)


def delete_task(slug):
    with _lock:
        data = load()
        tasks = data.get("tasks") or {}
        if slug not in tasks:
            return False
        del tasks[slug]
        _write(data)
    return True


# -- classifications ------------------------------------------------------
def _classifier(cid, body):
    body = body or {}
    links = body.get("tags") or {}
    classes = _plain(body.get("classes")) or []
    classification_type = body.get("classification_type")
    return {
        "id": str(cid),
        "name": str(body.get("name") or cid),
        "enabled": bool(body.get("enabled", False)),
        "classification_type": str(classification_type) if classification_type else None,
        "classes": [str(c) for c in classes],
        "threshold": float(body.get("threshold", 0.8)),
        # Derived, not stored: a classification with a type and at least two
        # classes can be trained on and used; one missing either cannot, and
        # the UI uses this one flag to decide whether to show the "finish
        # setting this up" prompt or the ordinary card.
        "configured": bool(classification_type) and len(classes) >= 2,
        "tags": [
            {"tag_id": str(tid), "crop": _plain((link or {}).get("crop")) or []}
            for tid, link in sorted(links.items())
        ],
    }


def list_classifiers():
    found = load().get("classifications") or {}
    return sorted(
        (_classifier(cid, body) for cid, body in found.items()),
        key=lambda c: c["name"].lower(),
    )


def get_classifier(classifier_id):
    found = load().get("classifications") or {}
    return _classifier(classifier_id, found[classifier_id]) if classifier_id in found else None


def create_classifier(name):
    classifier_id = slugify(name)
    if not classifier_id:
        return None
    with _lock:
        data = load()
        found = _section(data, "classifications")
        if classifier_id in found:
            raise ValueError(f"The id '{classifier_id}' is already in use")
        # enabled defaults to False deliberately: an unconfigured classifier
        # has no type and no classes, so there is nothing yet for "enabled"
        # to mean - and a freshly created classification should not start
        # doing something the moment its first tag is linked.
        found[classifier_id] = {
            "name": str(name).strip(), "enabled": False,
            "classification_type": None, "classes": [], "threshold": 0.8,
            "tags": {},
        }
        _write(data)
    return get_classifier(classifier_id)


def configure_classifier(classifier_id, *, enabled, classification_type, classes, threshold):
    """Set the type, classes, threshold and enabled flag that turn a bare
    classification into one that can actually be trained and used.

    Validated the same way a config file save is - config_store.validate
    covers the whole document, so a bad value here is reported the same way
    a bad value in the editor would be.

    Mutates a deep copy, not the live cache: `load()` hands back the same
    object every call until the file changes, so editing it in place and
    then failing validation would leave the rejected values sitting in
    memory - accepted by every reader, written to disk by none - until
    something else happened to invalidate the cache.
    """
    with _lock:
        data = load()
        if classifier_id not in (data.get("classifications") or {}):
            return None
        candidate = copy.deepcopy(data)
        body = candidate["classifications"][classifier_id]
        if not isinstance(body, dict):
            body = candidate["classifications"][classifier_id] = {"name": classifier_id}
        body["enabled"] = bool(enabled)
        body["classification_type"] = classification_type
        body["classes"] = list(classes)
        body["threshold"] = float(threshold)
        validate(candidate)
        _write(candidate)
    return get_classifier(classifier_id)


def delete_classifier(classifier_id):
    with _lock:
        data = load()
        found = data.get("classifications") or {}
        if classifier_id not in found:
            return False
        del found[classifier_id]
        _write(data)
    return True


def set_classifier_tag(classifier_id, tag_id, crop):
    with _lock:
        data = load()
        found = data.get("classifications") or {}
        if classifier_id not in found:
            return False
        body = found[classifier_id]
        if not isinstance(body, dict):
            body = found[classifier_id] = {"name": classifier_id}
        if body.get("tags") is None:
            body["tags"] = {}
        body["tags"][tag_id] = {"crop": [float(v) for v in crop]}
        _write(data)
    return True


def unlink_classifier_tag(classifier_id, tag_id):
    with _lock:
        data = load()
        body = (data.get("classifications") or {}).get(classifier_id) or {}
        links = body.get("tags") or {}
        if tag_id not in links:
            return False
        del links[tag_id]
        _write(data)
    return True


# -- settings ---------------------------------------------------------------
def get_settings():
    settings = load().get("settings") or {}
    return {
        "mobilenet_weights_path": settings.get("mobilenet_weights_path") or "",
        "device": settings.get("device") or "cpu",
    }


def save_settings(mobilenet_weights_path, device=None):
    with _lock:
        data = load()
        candidate = copy.deepcopy(data)
        section = candidate.get("settings")
        if not isinstance(section, dict):
            section = candidate["settings"] = {}
        path = str(mobilenet_weights_path or "").strip()
        section["mobilenet_weights_path"] = path or None
        section["device"] = device or "cpu"
        validate(candidate)
        _write(candidate)
    return get_settings()


def get_classification_webhook_url():
    """Where to POST classification results, pushed by the dreame_vacuum_core
    integration itself on startup - nothing for a person to configure.

    Kept out of get_settings()/save_settings() deliberately: those are what
    the Settings tab reads and writes, and a save from that form must not be
    able to wipe out a value only the integration is meant to set.
    """
    settings = load().get("settings") or {}
    return settings.get("classification_webhook_url") or None


def save_classification_webhook_url(url):
    with _lock:
        data = load()
        candidate = copy.deepcopy(data)
        section = candidate.get("settings")
        if not isinstance(section, dict):
            section = candidate["settings"] = {}
        section["classification_webhook_url"] = str(url or "").strip() or None
        validate(candidate)
        _write(candidate)


# -- migration ------------------------------------------------------------
def migrate_from_sqlite(store) -> bool:
    """Write the config file from the old SQLite tables, once.

    Only runs when there is no config file at all, so it cannot overwrite an
    edited one. The tables are read but never dropped: if this export has a
    bug, the original data is still sitting there to re-run against.
    """
    with _lock:
        if CONFIG_PATH.exists():
            return False
        try:
            tags = {t["id"]: {"name": t["name"]} for t in store.list_tags()}
            tasks = {
                t["slug"]: {"name": t["name"], "did": t["did"], "steps": t["steps"]}
                for t in store.list_tasks()
            }
            classifications = {
                c["id"]: {
                    "name": c["name"],
                    "tags": {link["tag_id"]: {"crop": link["crop"]} for link in c["tags"]},
                }
                for c in store.list_classifiers()
            }
        except Exception:  # noqa: BLE001 - a missing table must not block startup
            tags, tasks, classifications = {}, {}, {}

        data = _parse(STARTER_CONFIG)
        data["tags"] = tags
        data["tasks"] = tasks
        data["classifications"] = classifications
        _write(data)
    return bool(tags or tasks or classifications)
