"""Broadcast a classification result over MQTT.

Connection details come from Home Assistant's own MQTT service discovery,
not from add-on options: `services: [mqtt:want]` in config.yaml makes the
Supervisor inject MQTT_HOST/MQTT_PORT/MQTT_USERNAME/MQTT_PASSWORD into the
container automatically whenever an MQTT broker add-on (Mosquitto, most
commonly) is running - the same mechanism Frigate's own add-on relies on.
Nobody has to go find a password and paste it into a text field; if the
broker is present, this just works, and if it is not, classification results
are logged once and otherwise skipped rather than the add-on refusing to
start.

Each classification is also announced once as an MQTT-discovered sensor, so
an automation can be built by picking it from Home Assistant's entity list
rather than having to know the raw topic.
"""
from __future__ import annotations

import json
import logging
import os
import threading

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

BASE_TOPIC = "dreame_vacuum_companion"
DISCOVERY_PREFIX = "homeassistant"

# One virtual device groups every classification's sensor together in Home
# Assistant's UI, rather than each showing up as its own unrelated device.
DEVICE = {
    "identifiers": ["dreame_vacuum_companion_classify"],
    "name": "Dreame Vacuum Companion",
    "model": "Snapshot classification",
}

_lock = threading.Lock()
_client: mqtt.Client | None = None
_announced: set[str] = set()
_warned_unavailable = False


def _connect() -> mqtt.Client | None:
    global _client, _warned_unavailable
    host = os.environ.get("MQTT_HOST")
    if not host:
        if not _warned_unavailable:
            logger.info("No MQTT broker available (Supervisor reported none) - "
                       "classification results will not be broadcast")
            _warned_unavailable = True
        return None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    username = os.environ.get("MQTT_USERNAME")
    password = os.environ.get("MQTT_PASSWORD")
    if username:
        client.username_pw_set(username, password or None)
    try:
        client.connect(host, int(os.environ.get("MQTT_PORT", 1883)), keepalive=60)
        client.loop_start()
    except OSError as err:
        logger.warning("Could not connect to MQTT broker at %s: %s", host, err)
        return None
    return client


def _client_or_none() -> mqtt.Client | None:
    global _client
    with _lock:
        if _client is None:
            _client = _connect()
        return _client


def _announce(classifier_id: str, name: str, classification_type: str) -> None:
    """Publish (once per process) the discovery config that turns this
    classification's topic into a proper Home Assistant sensor entity."""
    if classifier_id in _announced:
        return
    client = _client_or_none()
    if client is None:
        return
    unique_id = f"dreame_classify_{classifier_id}"
    payload = {
        "name": name,
        "unique_id": unique_id,
        "state_topic": f"{BASE_TOPIC}/classification/{classifier_id}/state",
        "json_attributes_topic": f"{BASE_TOPIC}/classification/{classifier_id}/attributes",
        "icon": "mdi:tag-text-outline",
        "device": DEVICE,
    }
    # classification_type does not change what gets published - see
    # config_store's CLASSIFICATION_TYPES docstring for why: unlike Frigate,
    # nothing here shares one slot on a tracked object, so sub_label and
    # attribute publish identically. It travels in the attributes payload
    # purely as information for whoever is looking at the entity.
    client.publish(
        f"{DISCOVERY_PREFIX}/sensor/{unique_id}/config",
        json.dumps(payload), qos=0, retain=True,
    )
    _announced.add(classifier_id)


def publish_result(classifier_id: str, name: str, classification_type: str,
                    label: str, score: float, *, tag_id: str, filename: str) -> None:
    """Best-effort: a broker that is briefly unreachable must never be the
    reason a classification result is lost from the caller's point of view -
    the label is already saved to the dataset by this point regardless."""
    client = _client_or_none()
    if client is None:
        return
    try:
        _announce(classifier_id, name, classification_type)
        client.publish(f"{BASE_TOPIC}/classification/{classifier_id}/state",
                       label, qos=0, retain=True)
        client.publish(
            f"{BASE_TOPIC}/classification/{classifier_id}/attributes",
            json.dumps({
                "score": score, "tag": tag_id, "filename": filename,
                "classification_type": classification_type,
            }),
            qos=0, retain=True,
        )
    except Exception:  # noqa: BLE001 - publishing must never break the caller
        logger.exception("Could not publish classification result for %s", classifier_id)
