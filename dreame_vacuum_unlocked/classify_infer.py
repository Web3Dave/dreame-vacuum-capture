"""Run a trained classification model against a snapshot.

Uses tensorflow.lite.Interpreter rather than the separate tflite_runtime
package Frigate reaches for: Frigate's inference process is never supposed to
carry the full TensorFlow install, but this add-on already does (training,
in classify_train.py), so a second, smaller runtime package would only be
saving space nobody is spending. One dependency, two jobs.

Interpreters are cached per classification and rebuilt when the model file's
mtime changes - training a classification again replaces model.tflite, and
the next classification afterward should use the new one without the add-on
needing a restart.
"""
from __future__ import annotations

import logging
import os
import threading

import numpy as np
from PIL import Image

import classify_train

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# classifier_id -> (mtime, interpreter, input_index, output_index, labelmap)
_cache: dict[str, tuple] = {}


def _load(classifier_id: str):
    model_file = classify_train.model_path(classifier_id)
    label_file = classify_train.labelmap_path(classifier_id)
    if not os.path.exists(model_file) or not os.path.exists(label_file):
        return None

    mtime = os.path.getmtime(model_file)
    cached = _cache.get(classifier_id)
    if cached and cached[0] == mtime:
        return cached[1:]

    from tensorflow import lite as tflite

    interpreter = tflite.Interpreter(model_path=model_file, num_threads=2)
    interpreter.allocate_tensors()
    input_index = interpreter.get_input_details()[0]["index"]
    output_index = interpreter.get_output_details()[0]["index"]
    with open(label_file) as handle:
        labelmap = [line.strip() for line in handle if line.strip()]

    entry = (interpreter, input_index, output_index, labelmap)
    _cache[classifier_id] = (mtime, *entry)
    return entry


def classify(classifier_id: str, image_path: str, crop: list[float]) -> tuple[str, float] | None:
    """(label, score) for the classification's crop of an image, or None if
    no trained model exists yet. Thread-safe: interpreter reuse is cached,
    but a single tf.lite.Interpreter is not safe to run concurrently, so
    invocation itself is serialised.
    """
    with _lock:
        loaded = _load(classifier_id)
        if loaded is None:
            return None
        interpreter, input_index, output_index, labelmap = loaded

        try:
            with Image.open(image_path) as image:
                image = image.convert("RGB")
                width, height = image.size
                x1, y1, x2, y2 = crop
                box = (round(x1 * width), round(y1 * height),
                       round(x2 * width), round(y2 * height))
                cropped = image.crop(box).resize(classify_train.IMAGE_SIZE, Image.BILINEAR)
                array = np.asarray(cropped, dtype=np.uint8)
        except Exception:  # noqa: BLE001 - a bad image must not break capture
            logger.exception("Could not prepare %s for classification", image_path)
            return None

        interpreter.set_tensor(input_index, array[None, ...])
        interpreter.invoke()
        result = interpreter.get_tensor(output_index)[0]

    total = int(result.sum())
    if total == 0:
        return None
    probabilities = result / total
    best = int(np.argmax(probabilities))
    if best >= len(labelmap):
        logger.warning("Model output for %s does not match its labelmap", classifier_id)
        return None
    return labelmap[best], round(float(probabilities[best]), 3)
