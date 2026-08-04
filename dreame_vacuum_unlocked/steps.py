"""The step vocabulary a task is built from.

One schema, three consumers: the editor renders fields from it, the YAML view
serialises against it, and the export turns steps into Home Assistant service
calls. Keeping them from drifting apart is the whole reason this is data rather
than three hand-written translations.

Deliberately a closed vocabulary of movement primitives. A task is "drive
here, look there, photograph it" - not a general automation language. Anything
conditional belongs in the Home Assistant automation that starts the task.

Note `use_camera_session` on rotate_to_heading: the vacuum allows one camera
session at a time, so a turn that happens while a stream is open must not try
to open its own. The executor infers this from whether a stream is running,
and the export writes it out explicitly - otherwise exported YAML would turn
noisily where the task did not.
"""
from __future__ import annotations

import json

# Each field: (name, type, required, default, help)
STEP_TYPES = {
    "start_stream": {
        "label": "Start camera stream",
        "help": "Holds one camera session open. Keeps turns silent and lets "
                "snapshots come off the live feed.",
        "fields": [],
        "service": ("switch", "turn_on"),
    },
    "stop_stream": {
        "label": "Stop camera stream",
        "help": "Releases the camera so the phone app can use it again.",
        "fields": [],
        "service": ("switch", "turn_off"),
    },
    "go_to_point": {
        "label": "Go to point",
        "help": "Drive to a coordinate on the current map.",
        "fields": [
            ("x", "int", True, None, "Millimetres, as reported by position_x"),
            ("y", "int", True, None, "Millimetres, as reported by position_y"),
            ("arrival_tolerance", "int", False, 150, "How close counts as arrived, in mm"),
            ("timeout", "float", False, 180, "Give up after this many seconds"),
        ],
        "service": ("dreame_vacuum_unlocked_integration", "go_to_point"),
    },
    "rotate_to_heading": {
        "label": "Rotate to heading",
        "help": "Turn on the spot to face a compass heading.",
        "fields": [
            ("heading", "float", True, None, "Degrees, 0-359"),
            ("tolerance", "float", False, 5, "How close counts as facing it, in degrees"),
            ("max_attempts", "int", False, 8, "Give up after this many corrections"),
        ],
        "service": ("dreame_vacuum_unlocked_integration", "rotate_to_heading"),
    },
    "take_snapshot": {
        "label": "Take snapshot",
        "help": "Photograph what the vacuum is looking at.",
        "fields": [
            ("tag", "str", False, "general", "Groups snapshots, e.g. poop_check"),
        ],
        "service": ("dreame_vacuum_unlocked_integration", "take_snapshot"),
    },
    "return_to_dock": {
        "label": "Return to dock",
        "help": "Send the vacuum home.",
        "fields": [],
        "service": ("vacuum", "return_to_base"),
    },
    "clean_rooms": {
        "label": "Clean rooms",
        "help": "Clean the chosen rooms in the order listed. The vacuum visits "
                "them in that order.",
        "fields": [
            ("rooms", "list_int", True, None,
             "Room ids to clean, in order: first id is cleaned first"),
            ("times", "int", False, 1, "How many times to clean each room"),
        ],
        "service": ("dreame_vacuum_unlocked_integration", "clean_rooms"),
    },
}


class StepError(ValueError):
    """A step the vocabulary cannot express, with a reason worth showing."""


def _coerce(value, kind, where):
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise StepError(f"{where} must be a whole number, got {value!r}") from None
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise StepError(f"{where} must be a number, got {value!r}") from None
    if kind == "list_int":
        if isinstance(value, str):
            # Accept a comma/space separated list typed into the field, or JSON.
            value = value.strip()
            if value.startswith("["):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = [part for part in value.strip("[]").split(",") if part.strip()]
            else:
                value = [part for part in value.replace(" ", ",").split(",") if part.strip()]
        if not isinstance(value, (list, tuple)):
            raise StepError(f"{where} must be a list of room ids, got {value!r}")
        out = []
        for item in value:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                raise StepError(
                    f"{where} must be whole room ids, got {item!r}"
                ) from None
        return out
    return str(value)


def validate_step(step, index=0):
    """Return a normalised step, or raise StepError explaining what is wrong."""
    if not isinstance(step, dict):
        raise StepError(f"Step {index + 1} is not a mapping")
    kind = step.get("type")
    if kind not in STEP_TYPES:
        raise StepError(
            f"Step {index + 1}: unknown type {kind!r}. "
            f"Expected one of: {', '.join(sorted(STEP_TYPES))}"
        )

    spec = STEP_TYPES[kind]
    out = {"type": kind}
    known = {name for name, *_ in spec["fields"]}
    for name in step:
        if name not in known and name != "type":
            raise StepError(
                f"Step {index + 1} ({kind}): unknown field {name!r}"
                + (f". Expected: {', '.join(sorted(known))}" if known else " - it takes none")
            )
    for name, kind_, required, default, _help in spec["fields"]:
        if name in step and step[name] not in (None, ""):
            out[name] = _coerce(step[name], kind_, f"Step {index + 1} ({kind}) field '{name}'")
        elif required:
            raise StepError(f"Step {index + 1} ({kind}): '{name}' is required")
        elif default is not None:
            out[name] = default
    return out


def validate_steps(steps):
    if not isinstance(steps, list) or not steps:
        raise StepError("A task needs at least one step")
    return [validate_step(step, i) for i, step in enumerate(steps)]


def describe(step):
    """One line for a list or a log."""
    kind = step.get("type")
    spec = STEP_TYPES.get(kind, {})
    label = spec.get("label", kind)
    detail = ", ".join(
        f"{name} {step[name]}" for name, *_ in spec.get("fields", []) if name in step
    )
    return f"{label}" + (f" ({detail})" if detail else "")


def to_service_calls(steps, entity_id, stream_switch=None):
    """Steps as Home Assistant service calls, ready to export or execute.

    `use_camera_session: false` is written out on any turn that happens while a
    stream is open, so the exported script behaves exactly as the task does.
    """
    calls = []
    streaming = False
    for step in steps:
        kind = step["type"]
        domain, service = STEP_TYPES[kind]["service"]
        data = {k: v for k, v in step.items() if k != "type"}
        target = entity_id

        if kind in ("start_stream", "stop_stream"):
            if not stream_switch:
                raise StepError(
                    f"'{kind}' needs the vacuum's stream switch, which this "
                    "device has not reported - is the camera set up?"
                )
            target = stream_switch
            streaming = kind == "start_stream"
        elif kind == "rotate_to_heading" and streaming:
            data["use_camera_session"] = False

        calls.append({"action": f"{domain}.{service}", "target": {"entity_id": target},
                      "data": data} if data else
                     {"action": f"{domain}.{service}", "target": {"entity_id": target}})
    return calls
