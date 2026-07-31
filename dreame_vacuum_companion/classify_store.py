"""The labelled dataset a classification trains on.

Mirrors Frigate's own layout - `dataset/<label>/*.jpg` under a folder per
classification - deliberately: it needs no index of its own, a folder listing
*is* the dataset, and it is the same shape Frigate's own training code and
dataset endpoints expect. Training (a later piece) can read this directly.

Unlike Frigate, there is no automatic sampling from review items or events -
a label here comes from one deliberate action: a person opens a snapshot they
already have (already framed the way the classification will always see it,
by construction of go_to_point + rotate_to_heading + take_snapshot) and says
what it shows. That action is `assign_label`.
"""
from __future__ import annotations

import logging
import os
import time

from PIL import Image

import config_store

DATASET_ROOT = "/media/dreame-capture/classify"

# The size Frigate trains and infers MobileNetV2 at. Matched exactly: a
# dataset image saved at a different size would need resizing again at train
# time anyway, and matching now means what you see when reviewing a labelled
# example is pixel-for-pixel what the model will be trained on.
TRAIN_SIZE = (224, 224)

logger = logging.getLogger(__name__)


class AssignError(ValueError):
    """A label could not be assigned, with a reason worth showing."""


def safe_component(value: str) -> str:
    """A single path segment, not a path - no slashes, no traversal.

    Public: classify_train and classify_infer use the same rule to name a
    classification's model directory, and it needs to agree with this
    module's dataset directory exactly, or a model would train against one
    folder and load from another.
    """
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (value or "").strip())
    return cleaned.strip("_")[:64]


def dataset_dir(classifier_id: str) -> str:
    return os.path.join(DATASET_ROOT, safe_component(classifier_id), "dataset")


def label_dir(classifier_id: str, label: str) -> str:
    return os.path.join(dataset_dir(classifier_id), safe_component(label))


def dataset_counts(classifier_id: str) -> dict[str, int]:
    """How many labelled examples exist per class - what a "ready to train"
    indicator in the UI would read from, once there is one."""
    root = dataset_dir(classifier_id)
    if not os.path.isdir(root):
        return {}
    counts = {}
    for label in sorted(os.listdir(root)):
        folder = os.path.join(root, label)
        if not os.path.isdir(folder):
            continue
        counts[label] = sum(
            1 for f in os.listdir(folder) if f.lower().endswith(".jpg")
        )
    return counts


def assign_label(classifier_id: str, tag_id: str, snapshot_path: str, label: str) -> dict:
    """Crop a snapshot per the classification's link to this tag, and file it
    under the chosen label.

    The crop comes from the (classification, tag) link, not from the
    snapshot itself - that link is what says "this rectangle is the part of
    the frame this classification reads", true for every photo taken under
    this tag because the task that takes them always frames the shot the
    same way.

    Raises AssignError for every reason this can fail, so the route handler
    only has to catch one exception type and return its message.
    """
    classifier = config_store.get_classifier(classifier_id)
    if not classifier:
        raise AssignError("No such classification")
    if not classifier["configured"]:
        raise AssignError(
            f"'{classifier['name']}' is not configured yet - set its type and "
            "classes on the Classifications tab first"
        )
    if label not in classifier["classes"]:
        raise AssignError(
            f"'{label}' is not one of {classifier['name']}'s classes "
            f"({', '.join(classifier['classes'])})"
        )
    link = next((t for t in classifier["tags"] if t["tag_id"] == tag_id), None)
    if not link:
        raise AssignError(f"'{classifier['name']}' is not linked to this tag")
    if not os.path.exists(snapshot_path):
        raise AssignError("That snapshot no longer exists")

    try:
        with Image.open(snapshot_path) as image:
            image = image.convert("RGB")
            width, height = image.size
            x1, y1, x2, y2 = link["crop"]
            box = (
                round(x1 * width), round(y1 * height),
                round(x2 * width), round(y2 * height),
            )
            cropped = image.crop(box).resize(TRAIN_SIZE, Image.BILINEAR)
    except Exception as err:  # noqa: BLE001 - a corrupt image must not 500
        raise AssignError(f"Could not read that snapshot: {err}") from None

    folder = label_dir(classifier_id, label)
    os.makedirs(folder, exist_ok=True)
    # The original filename ties a dataset image back to the snapshot it came
    # from - useful when reviewing what was labelled, and re-assigning the
    # same snapshot under a corrected label overwrites rather than
    # duplicating it under the old one.
    stem = os.path.splitext(os.path.basename(snapshot_path))[0]
    dest = os.path.join(folder, f"{stem}.jpg")

    # Assigning a different label for a snapshot already labelled elsewhere
    # in this classification must move it, not leave two copies training two
    # different classes on the same photo.
    for other_label in classifier["classes"]:
        if other_label == label:
            continue
        stray = os.path.join(label_dir(classifier_id, other_label), f"{stem}.jpg")
        if os.path.exists(stray):
            os.remove(stray)

    cropped.save(dest, "JPEG", quality=90)
    logger.info("Labelled %s as '%s' for classification '%s'",
                os.path.basename(snapshot_path), label, classifier_id)
    return {
        "classifier_id": classifier_id, "label": label,
        "filename": os.path.basename(dest), "assigned_at": int(time.time()),
    }
