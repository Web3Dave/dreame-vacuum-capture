"""Train a classification model - the same architecture and procedure Frigate
uses for its own custom classifiers, adapted to read from classify_store's
dataset layout instead of Frigate's CLIPS_DIR.

Runs in a subprocess, also as Frigate does it: TensorFlow does not fully free
GPU/CPU memory back to the OS when a model goes out of scope in the same
process, so the process itself is the unit of cleanup - it trains, writes the
model, and exits.

The base weights are the one thing genuinely worth not re-fetching per
classification: KERAS_HOME is pointed at /data before TensorFlow is ever
imported, so Keras's own standard download-and-cache mechanism keeps its copy
under the add-on's persistent volume rather than the container's home
directory, which does not survive an add-on update. A training run for a
second classification then costs no download at all - not because this code
does anything clever, but because Keras already does the right thing once
told where "persistent" is.
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import time

# Must happen before TensorFlow (or anything importing it) is loaded -
# Keras reads this at import time to decide where its weights cache lives.
os.environ.setdefault("KERAS_HOME", "/data/keras_cache")

import classify_store
import config_store

logger = logging.getLogger(__name__)

BATCH_SIZE = 16
EPOCHS = 50
LEARNING_RATE = 0.001
IMAGE_SIZE = classify_store.TRAIN_SIZE  # (224, 224), matched to the dataset

MODEL_CACHE_ROOT = "/data/classify_models"
STATUS_FILE = "status.json"

# Fewer than this many total labelled images and MobileNetV2 has nothing
# meaningful to learn from - Frigate does not enforce a floor here, but an
# add-on user is far more likely than a Frigate operator to hit "I made two
# classes and one snapshot each" by trying the feature, and a clear refusal
# beats a model that has memorised two photos.
MIN_IMAGES_PER_CLASS = 1


def model_dir(classifier_id: str) -> str:
    return os.path.join(MODEL_CACHE_ROOT, classify_store.safe_component(classifier_id))


def _status_path(classifier_id: str) -> str:
    return os.path.join(model_dir(classifier_id), STATUS_FILE)


def _write_status(classifier_id: str, **fields) -> None:
    path = _status_path(classifier_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    current = read_status(classifier_id)
    current.update(fields)
    with open(path, "w") as handle:
        json.dump(current, handle)


def read_status(classifier_id: str) -> dict:
    """state: none | training | complete | failed."""
    try:
        with open(_status_path(classifier_id)) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"state": "none"}


def model_path(classifier_id: str) -> str:
    return os.path.join(model_dir(classifier_id), "model.tflite")


def labelmap_path(classifier_id: str) -> str:
    return os.path.join(model_dir(classifier_id), "labelmap.txt")


def dataset_readiness(classifier_id: str, classes: list[str]) -> dict:
    """Per-class example counts against MIN_IMAGES_PER_CLASS, and whether
    there is enough to train on - what the UI's Train button is enabled by."""
    counts = classify_store.dataset_counts(classifier_id)
    per_class = {c: counts.get(c, 0) for c in classes}
    short = [c for c, n in per_class.items() if n < MIN_IMAGES_PER_CLASS]
    return {
        "counts": per_class,
        "ready": len(classes) >= 2 and not short,
        "short": short,
        "minimum": MIN_IMAGES_PER_CLASS,
    }


def start_training(classifier_id: str) -> tuple[bool, str]:
    """Kick off training in a background process. Returns (started, message)
    - message is the reason when it did not start, so the route handler has
    nothing to compute itself."""
    classifier = config_store.get_classifier(classifier_id)
    if not classifier:
        return False, "No such classification"
    if not classifier["configured"]:
        return False, "Set a type and classes for this classification first"

    status = read_status(classifier_id)
    if status.get("state") == "training":
        return False, "Already training"

    readiness = dataset_readiness(classifier_id, classifier["classes"])
    if not readiness["ready"]:
        short = ", ".join(readiness["short"])
        return False, (
            f"Not enough labelled examples yet - need at least "
            f"{MIN_IMAGES_PER_CLASS} per class, short on: {short}"
        )

    _write_status(classifier_id, state="training", started_at=int(time.time()), error=None)
    process = multiprocessing.Process(
        target=_train_worker, args=(classifier_id, classifier["classes"]), daemon=True
    )
    process.start()
    return True, "Training started"


def _train_worker(classifier_id: str, classes: list[str]) -> None:
    """Runs in the child process. Any exception here must still leave a
    readable status behind - a training run that silently vanishes looks
    identical to one that is still running."""
    try:
        _train(classifier_id, classes)
        _write_status(classifier_id, state="complete", finished_at=int(time.time()), error=None)
    except Exception as err:  # noqa: BLE001 - reported via status, not re-raised
        logger.exception("Training failed for %s", classifier_id)
        _write_status(classifier_id, state="failed", finished_at=int(time.time()),
                      error=str(err)[:400])


def _train(classifier_id: str, classes: list[str]) -> None:
    # Imported inside the function, in the child process only, exactly as
    # Frigate does it - so the parent process (serving the UI while this
    # trains) never pays TensorFlow's import cost or memory footprint.
    import tensorflow as tf
    from tensorflow.keras import layers, models, optimizers
    from tensorflow.keras.applications import MobileNetV2
    from tensorflow.keras.preprocessing.image import ImageDataGenerator

    dataset_dir = classify_store.dataset_dir(classifier_id)
    out_dir = model_dir(classifier_id)
    os.makedirs(out_dir, exist_ok=True)

    settings = config_store.get_settings()
    weights = settings.get("mobilenet_weights_path") or "imagenet"
    if weights != "imagenet" and not os.path.exists(weights):
        logger.warning("Configured MobileNet weights path does not exist, "
                       "falling back to the standard download: %s", weights)
        weights = "imagenet"

    base_model = MobileNetV2(
        input_shape=(*IMAGE_SIZE, 3), include_top=False, weights=weights, alpha=0.35,
    )
    base_model.trainable = False

    model = models.Sequential([
        base_model,
        layers.GlobalAveragePooling2D(),
        layers.Dense(128, activation="relu"),
        layers.Dropout(0.3),
        layers.Dense(len(classes), activation="softmax"),
    ])
    model.compile(
        optimizer=optimizers.Adam(learning_rate=LEARNING_RATE),
        loss="categorical_crossentropy", metrics=["accuracy"],
    )

    datagen = ImageDataGenerator(rescale=1.0 / 255, validation_split=0.2)
    train_gen = datagen.flow_from_directory(
        dataset_dir, target_size=IMAGE_SIZE, batch_size=BATCH_SIZE,
        class_mode="categorical", subset="training",
    )

    # The label -> index mapping Keras derives from directory names, written
    # out so inference can turn a predicted index back into a class name -
    # this is the file inference reads, and it must agree with the model
    # exactly or every prediction will be labelled wrong but confidently so.
    class_indices = train_gen.class_indices
    index_to_class = {v: k for k, v in class_indices.items()}
    sorted_classes = [index_to_class[i] for i in range(len(index_to_class))]
    with open(labelmap_path(classifier_id), "w") as handle:
        for name in sorted_classes:
            handle.write(f"{name}\n")

    model.fit(train_gen, epochs=EPOCHS, verbose=0)

    def representative_dataset():
        # PIL rather than Frigate's cv2, which this add-on has no other use
        # for - Pillow is already a dependency (classify_store's crop step),
        # and this is the only place that would otherwise need OpenCV.
        from PIL import Image as PILImage

        image_paths = []
        for root, _dirs, files in os.walk(dataset_dir):
            for file in files:
                if file.lower().endswith((".jpg", ".jpeg", ".png")):
                    image_paths.append(os.path.join(root, file))
        for path in image_paths[:300]:
            with PILImage.open(path) as img:
                img = img.convert("RGB").resize(IMAGE_SIZE, PILImage.BILINEAR)
                array = tf.keras.utils.img_to_array(img, dtype="float32") / 255.0
                yield [array[None, ...]]

    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    converter.representative_dataset = representative_dataset
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    converter.inference_input_type = tf.uint8
    converter.inference_output_type = tf.uint8
    tflite_model = converter.convert()

    with open(model_path(classifier_id), "wb") as handle:
        handle.write(tflite_model)

    if not os.path.exists(model_path(classifier_id)) or os.path.getsize(model_path(classifier_id)) == 0:
        raise RuntimeError("Model file was not written")

    counts = classify_store.dataset_counts(classifier_id)
    total_images = sum(counts.values())
    _write_status(classifier_id, image_count=total_images, classes=sorted_classes)
    logger.info("Finished training %s on %d images", classifier_id, total_images)
