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
        "fields": [
            ("audio", "bool", False, False,
             "Also stream sound from the vacuum's mic (arms the intercom), "
             "so clips recorded with sound capture it"),
        ],
        "service": ("switch", "turn_on"),
    },
    "stop_stream": {
        "label": "Stop camera stream",
        "help": "Releases the camera so the phone app can use it again.",
        "fields": [],
        "service": ("switch", "turn_off"),
    },
    "record_clip": {
        "label": "Record clip",
        "help": "Start recording from the camera's live stream into a video "
                "clip. Must be followed by an 'End clip' step, which stops "
                "and saves it as an h264 mp4 under this step's tag.",
        "fields": [
            ("tag", "str", False, "general",
             "Groups clips alongside snapshots, e.g. poop_check"),
            ("audio", "bool", False, False,
             "Also record the vacuum's mic into the clip - needs the stream "
             "to have sound (a start_stream with the audio option)"),
        ],
        "service": ("dreame_vacuum_unlocked_integration", "record_clip"),
    },
    "end_clip": {
        "label": "End clip",
        "help": "Stop the recording started by a 'Record clip' step and save "
                "it as an h264 mp4 under that step's tag. Clips are never run "
                "through the classifier - that is for photos only.",
        "fields": [],
        "service": ("dreame_vacuum_unlocked_integration", "end_clip"),
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
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "yes", "on", "y"):
                return True
            if v in ("false", "0", "no", "off", "n", ""):
                return False
        raise StepError(f"{where} must be yes or no, got {value!r}")
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
    out = [validate_step(step, i) for i, step in enumerate(steps)]
    return _validate_pairings(out)


def _validate_pairings(steps):
    """Resources that are turned on must be turned off within the same task.

    A stream left open holds the device's single camera session forever -
    the phone app can't use it until something closes the task. A recording
    left running ('record_clip' with no 'end_clip') would grow on disk until
    it fills the card. Both are structural mistakes that show up as soon as
    the steps are read, so catch them here rather than hours later behind a
    running task.
    """
    open_streams = 0
    open_clips = 0
    for i, step in enumerate(steps):
        kind = step["type"]
        if kind == "start_stream":
            if open_streams:
                raise StepError(
                    f"Step {i + 1} (start_stream): a stream is already running "
                    "from an earlier step - add a stop_stream step first (the "
                    "vacuum only allows one camera session at a time)"
                )
            open_streams += 1
        elif kind == "stop_stream":
            if open_streams == 0:
                raise StepError(
                    f"Step {i + 1} (stop_stream): there is no stream running "
                    "to stop - a stop_stream must follow a start_stream step"
                )
            open_streams -= 1
        elif kind == "record_clip":
            if open_clips:
                raise StepError(
                    f"Step {i + 1} (record_clip): a clip is already recording "
                    "from an earlier step - add an end_clip step before "
                    "starting another"
                )
            open_clips += 1
        elif kind == "end_clip":
            if open_clips == 0:
                raise StepError(
                    f"Step {i + 1} (end_clip): there is no clip recording "
                    "to end - an end_clip must follow a record_clip step"
                )
            open_clips -= 1
    if open_streams:
        raise StepError(
            "A start_stream step has no matching stop_stream - a stream left "
            "open holds the camera. Add a stop_stream to the end of the task."
        )
    if open_clips:
        raise StepError(
            "A record_clip step has no matching end_clip - the recording "
            "would never be saved. Add an end_clip step to stop it."
        )
    return steps


def describe(step):
    """One line for a list or a log."""
    kind = step.get("type")
    spec = STEP_TYPES.get(kind, {})
    label = spec.get("label", kind)
    detail = ", ".join(
        f"{name} {step[name]}" for name, *_ in spec.get("fields", []) if name in step
    )
    return f"{label}" + (f" ({detail})" if detail else "")


def to_service_calls(steps, entity_id, stream_switch=None, intercom_switch=None):
    """Steps as Home Assistant service calls, ready to export or execute.

    `use_camera_session: false` is written out on any turn that happens while a
    stream is open, so the exported script behaves exactly as the task does.

    A `start_stream` with `audio` also turns on the intercom switch (the
    vacuum-mic layer), so the stream carries sound for downstream clips - and
    the exported script reads back as exactly the two switches a task that
    streams sound actually flips.
    """
    calls = []
    streaming = False
    for step in steps:
        kind = step["type"]
        domain, service = STEP_TYPES[kind]["service"]
        data = {k: v for k, v in step.items() if k != "type"}
        target = entity_id

        if kind == "start_stream":
            if not stream_switch:
                raise StepError(
                    "'start_stream' needs the vacuum's stream switch, which this "
                    "device has not reported - is the camera set up?"
                )
            # `audio` is dropped from the switch data: switch.turn_on has no
            # such attribute. Sound is armed as its own switch instead.
            calls.append({"action": "switch.turn_on", "target": {"entity_id": stream_switch}})
            streaming = True
            if step.get("audio"):
                if not intercom_switch:
                    raise StepError(
                        "start_stream with its audio option on needs the vacuum's "
                        "intercom switch, which this device has not reported - "
                        "is the camera set up?"
                    )
                calls.append({"action": "switch.turn_on", "target": {"entity_id": intercom_switch}})
            continue
        if kind == "stop_stream":
            if not stream_switch:
                raise StepError(
                    "'stop_stream' needs the vacuum's stream switch, which this "
                    "device has not reported - is the camera set up?"
                )
            calls.append({"action": "switch.turn_off", "target": {"entity_id": stream_switch}})
            streaming = False
            continue

        if kind == "rotate_to_heading" and streaming:
            data["use_camera_session"] = False

        calls.append({"action": f"{domain}.{service}", "target": {"entity_id": target},
                      "data": data} if data else
                     {"action": f"{domain}.{service}", "target": {"entity_id": target}})
    return calls
