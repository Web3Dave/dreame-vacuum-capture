"""Fetch and cache the shared MobileNetV2 base weights ahead of time.

Frigate itself has no dedicated "download the model" control anywhere in its
UI - its detector models are either baked into its Docker image or point at
a path the user supplies, and the base weights other features fetch (custom
classification, face recognition) download silently on first use with no
progress of their own. This gives that step a visible status instead,
because the first Train click otherwise looks like nothing is happening for
however long the download takes.

Triggering the same MobileNetV2(weights="imagenet", ...) call training itself
makes, rather than hand-building the download URL Keras would use: Keras's
own weight-file naming has changed across releases before, and asking Keras
to do its own lookup is the only way this cannot drift out of sync with what
classify_train.py actually loads at training time.
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import time

os.environ.setdefault("KERAS_HOME", "/data/keras_cache")

logger = logging.getLogger(__name__)

STATUS_PATH = os.path.join(os.environ.get("KERAS_HOME", "/data/keras_cache"),
                           "base_model_status.json")

# The exact call classify_train._train makes for the frozen backbone -
# kept identical on purpose, see the module docstring.
_MOBILENET_KWARGS = {"include_top": False, "alpha": 0.35, "input_shape": (224, 224, 3)}


def read_status() -> dict:
    """state: none | downloading | ready | failed."""
    try:
        with open(STATUS_PATH) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"state": "none"}


def _write_status(**fields) -> None:
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    current = read_status()
    current.update(fields)
    with open(STATUS_PATH, "w") as handle:
        json.dump(current, handle)


def start_download() -> tuple[bool, str]:
    status = read_status()
    if status.get("state") == "downloading":
        return False, "Already downloading"
    _write_status(state="downloading", started_at=int(time.time()), error=None)
    process = multiprocessing.Process(target=_download_worker, daemon=True)
    process.start()
    return True, "Download started"


def _download_worker() -> None:
    try:
        _download()
        _write_status(state="ready", finished_at=int(time.time()), error=None)
    except Exception as err:  # noqa: BLE001 - reported via status, not re-raised
        logger.exception("Could not download the base model")
        _write_status(state="failed", finished_at=int(time.time()), error=str(err)[:400])


def _download() -> None:
    # Imported in the child process only - see classify_train._train for why.
    from tensorflow.keras.applications import MobileNetV2

    model = MobileNetV2(weights="imagenet", **_MOBILENET_KWARGS)
    size = sum(w.size for w in model.get_weights())
    logger.info("Base model ready (%d parameters)", size)
