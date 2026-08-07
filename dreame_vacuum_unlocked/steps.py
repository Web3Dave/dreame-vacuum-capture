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
    "record_clip": {
        "label": "Record clip",
        "help": "Start recording from the camera's live stream into a video "
                "clip. Sound is recorded by default. Must be followed by an "
                "'End clip' step, which stops and saves it as an h264 mp4 "
                "under this step's tag.",
        "fields": [
            ("tag", "str", False, "general",
             "Groups clips alongside snapshots, e.g. poop_check"),
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
    # A control step, not a service call: `service` is deliberately None. It
    # holds a classifier id plus a list of steps per classification label, and
    # the task runner picks which branch to run based on the classifier's most
    # recent snapshot result (see to_service_calls -> {branch: ...} nodes).
    "if_classification": {
        "label": "If classification",
        "help": "Branch on a trained classifier's result. Put a 'Take snapshot' "
                "step whose tag this classification is linked to before it, "
                "then pick the label to match for each branch.",
        "fields": [
            ("classifier", "str", True, None,
             "The classification (by id) whose last result decides the branch"),
            ("cases", "steps_map", True, None,
             "Which steps run when the result equals each label"),
            ("default", "steps_list", False, [],
             "Steps that run when the result matches no case"),
        ],
        "service": None,
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
    if kind in ("steps_map", "steps_list"):
        # Pass through untouched; the deep validation (each nested step, and
        # pairing inside each branch) happens in validate_step's
        # if_classification block, which alone understands the nesting.
        return value
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

    if kind == "if_classification":
        # Deep-validate the nested branches. `cases` is a mapping of
        # classification label -> list of steps; `default` is an optional list.
        # Passed through _coerce untouched (kind "steps_map"/"steps_list");
        # this is the only place that knows the shape, so it owns the errors.
        cases = out.get("cases")
        if not isinstance(cases, dict) or not cases:
            raise StepError(
                f"Step {index + 1} (if_classification): 'cases' must be a "
                "non-empty mapping of classification label to a list of steps"
            )
        out["cases"] = {
            str(label): validate_steps(cases[label], allow_empty=True)
            for label in cases
        }
        default_steps = out.get("default")
        if default_steps:
            out["default"] = validate_steps(default_steps, allow_empty=True)
        else:
            out.pop("default", None)
    return out


def validate_steps(steps, allow_empty=False):
    if not isinstance(steps, list):
        raise StepError("A task's steps must be a list")
    if not steps and not allow_empty:
        raise StepError("A task needs at least one step")
    out = [validate_step(step, i) for i, step in enumerate(steps)]
    return _validate_pairings(out)


def _validate_pairings(steps):
    """Resources opened by a step must be closed by another step.

    A recording left running ('record_clip' with no 'end_clip') would grow on
    disk until it fills the card. Catching that here rather than hours later
    behind a running task keeps the mistake cheap. The camera stream is NOT
    in this list: the task runner opens and closes it around the whole task,
    so no step has to.
    """
    open_clips = 0
    for i, step in enumerate(steps):
        kind = step["type"]
        if kind == "if_classification":
            # Each branch already pair-checks itself (validate_step recurses
            # through validate_steps), and the conditional as a whole neither
            # opens nor closes a clip. A clip left running before it must be
            # matched after it at this same level, so no counter change here.
            continue
        if kind == "record_clip":
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
    if kind == "if_classification":
        n = len(step.get("cases") or {})
        return f"If classification ({n} case{'s' if n != 1 else ''})"
    detail = ", ".join(
        f"{name} {step[name]}" for name, *_ in spec.get("fields", []) if name in step
    )
    return f"{label}" + (f" ({detail})" if detail else "")


def to_service_calls(steps, entity_id):
    """Steps as Home Assistant service calls, ready to export or execute.

    `use_camera_session: false` is written out on every turn: the task runner
    holds one camera stream open for the whole task (opening and closing it
    around the steps), so a turn must never try to open its own second session.
    """
    calls = []
    for step in steps:
        kind = step["type"]

        if kind == "if_classification":
            # A control node, not a service call: the runner reads the
            # classifier's current result and runs exactly one branch, so the
            # branches cannot be flattened ahead of time. Recursing here keeps
            # every level of nesting expanded by the same rule.
            node = {
                "branch": True,
                "classifier": step["classifier"],
                "cases": {
                    label: to_service_calls(branch, entity_id)
                    for label, branch in step["cases"].items()
                },
            }
            if step.get("default"):
                node["default"] = to_service_calls(step["default"], entity_id)
            calls.append(node)
            continue

        domain, service = STEP_TYPES[kind]["service"]
        data = {k: v for k, v in step.items() if k != "type"}

        if kind == "rotate_to_heading":
            # The task's stream already holds the single camera session.
            data["use_camera_session"] = False

        calls.append({"action": f"{domain}.{service}", "target": {"entity_id": entity_id},
                      "data": data} if data else
                     {"action": f"{domain}.{service}", "target": {"entity_id": entity_id}})
    return calls
