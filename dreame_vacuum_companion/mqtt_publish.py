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

Each classification is announced as MQTT-discovered entities, grouped under
a device of its own - state, last-updated, and one binary sensor per class -
so an automation can be built by picking one from Home Assistant's entity
list, and a classifier's sensors sit together rather than in one long flat
list for the whole add-on. Frigate does not do this: it groups a model's
sensor under whichever camera it runs on, or under the whole server for a
"global" one, because a model is not a first-class thing in its device
hierarchy. A classifier is, here - so it gets a device to itself.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

BASE_TOPIC = "dreame_vacuum_companion"
DISCOVERY_PREFIX = "homeassistant"

_lock = threading.Lock()
_client: mqtt.Client | None = None
# classifier_id -> the class list it was last announced with. Re-announcing
# whenever this changes (rather than once ever) is what lets renaming a
# class, or adding one, show up without an add-on restart.
_announced: dict[str, list[str]] = {}
_warned_unavailable = False


def _on_connect(client, userdata, connect_flags, reason_code, properties=None) -> None:
    """The half of a connection failure `connect()` cannot see.

    A TCP handshake can succeed and still be rejected at the MQTT protocol
    level - wrong or missing credentials, most commonly - and that rejection
    only ever arrives here, asynchronously, well after connect() has already
    returned normally. Without this callback that rejection was invisible:
    no exception, no log line, just a client that looked usable and quietly
    never delivered anything.
    """
    if getattr(reason_code, "is_failure", reason_code != 0):
        logger.warning(
            "MQTT broker rejected the connection (%s) - check MQTT_USERNAME/"
            "MQTT_PASSWORD are actually set (Settings > Add-ons > this add-on "
            "> should show them once a broker is discovered) and that the "
            "broker add-on is running", reason_code,
        )
    else:
        logger.info("Connected to MQTT broker")


def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None) -> None:
    # Expected during shutdown; anything else is the broker or network
    # dropping a connection that was working, worth knowing about rather
    # than just going quiet.
    if getattr(reason_code, "is_failure", reason_code not in (0, None)):
        logger.warning("Disconnected from the MQTT broker (%s)", reason_code)


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
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    username = os.environ.get("MQTT_USERNAME")
    password = os.environ.get("MQTT_PASSWORD")
    if username:
        client.username_pw_set(username, password or None)
    else:
        logger.info("Connecting to MQTT broker %s with no username set "
                   "(MQTT_USERNAME was empty) - most brokers will refuse this", host)
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


def _slug(value: str) -> str:
    """A class name as an MQTT topic segment and entity id suffix - not
    stored anywhere, just kept stable and free of characters a topic or a
    unique_id should not carry."""
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in value.strip())
    return cleaned.strip("_").lower()[:48] or "class"


def _device(classifier_id: str, name: str) -> dict:
    return {
        "identifiers": [f"dreame_classify_{classifier_id}"],
        "name": name,
        "model": "Snapshot classification",
        "manufacturer": "Dreame Vacuum Companion",
    }


def _announce(classifier_id: str, name: str, classes: list[str]) -> None:
    """Publish the discovery configs that turn this classification's topics
    into Home Assistant entities: a state sensor, a last-updated timestamp,
    and one binary sensor per class - all grouped under one device.

    Re-announced whenever the class list actually changes, not only the
    first time - editing a classifier's classes on the Classifications tab
    should not need an add-on restart to show up as different entities.
    """
    if _announced.get(classifier_id) == classes:
        return
    client = _client_or_none()
    if client is None:
        return

    device = _device(classifier_id, name)
    base = f"{BASE_TOPIC}/classification/{classifier_id}"

    state_uid = f"dreame_classify_{classifier_id}"
    client.publish(
        f"{DISCOVERY_PREFIX}/sensor/{state_uid}/config",
        json.dumps({
            "name": "State",
            "unique_id": state_uid,
            "state_topic": f"{base}/state",
            "json_attributes_topic": f"{base}/attributes",
            "icon": "mdi:tag-text-outline",
            "device": device,
            # A generic MQTT-discovered entity only fires state_changed when
            # the value actually differs from last time - unlike Frigate's
            # own classification sensor, which is a native entity that calls
            # async_write_ha_state() straight from its MQTT callback and so
            # has no such filter. Without this, a `state` trigger automation
            # never fires on a second consecutive identical result (a door
            # classified Closed twice in a row, say) even though the
            # classifier genuinely ran and published both times.
            "force_update": True,
        }),
        qos=0, retain=True,
    )

    updated_uid = f"{state_uid}_last_updated"
    client.publish(
        f"{DISCOVERY_PREFIX}/sensor/{updated_uid}/config",
        json.dumps({
            "name": "Last updated",
            "unique_id": updated_uid,
            "state_topic": f"{base}/last_updated",
            "device_class": "timestamp",
            "entity_category": "diagnostic",
            "device": device,
            "force_update": True,
        }),
        qos=0, retain=True,
    )

    # A class dropped from the classifier's list stops being announced, but
    # its old discovery config is not retracted here - doing that safely
    # needs the *previous* class list, which the caller does not have once
    # config_store has already been edited. A stray disabled-looking entity
    # for a renamed-away class is the acceptable side of that trade.
    for class_name in classes:
        uid = f"{state_uid}_{_slug(class_name)}"
        client.publish(
            f"{DISCOVERY_PREFIX}/binary_sensor/{uid}/config",
            json.dumps({
                "name": class_name,
                "unique_id": uid,
                "state_topic": f"{base}/class/{_slug(class_name)}/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": device,
                # Same reasoning as the state sensor above: a run that
                # confirms Closed twice in a row must fire this ON twice,
                # not be swallowed as "no change" - a state trigger on this
                # binary sensor is exactly what "run every time" needs.
                "force_update": True,
            }),
            qos=0, retain=True,
        )

    _announced[classifier_id] = list(classes)


def publish_result(classifier_id: str, name: str, classification_type: str,
                    classes: list[str], label: str, score: float, *,
                    tag_id: str, filename: str) -> None:
    """Best-effort: a broker that is briefly unreachable must never be the
    reason a classification result is lost from the caller's point of view -
    the label is already saved to the dataset by this point regardless.
    """
    client = _client_or_none()
    if client is None:
        return
    try:
        _announce(classifier_id, name, classes)
        now = datetime.datetime.now(datetime.timezone.utc)
        base = f"{BASE_TOPIC}/classification/{classifier_id}"

        client.publish(f"{base}/state", label, qos=0, retain=True)
        client.publish(
            f"{base}/attributes",
            json.dumps({
                "score": score, "tag": tag_id, "filename": filename,
                "classification_type": classification_type,
                "ran_at": int(now.timestamp()),
            }),
            qos=0, retain=True,
        )
        # ISO 8601 on its own topic rather than a Jinja value_template
        # reading the timestamp back out of the attributes JSON - simpler to
        # read on the wire, and avoids the discovery config needing to agree
        # with this function about the attributes payload's exact shape.
        client.publish(f"{base}/last_updated", now.isoformat(), qos=0, retain=True)

        # One retained ON/OFF per class, computed here rather than as an MQTT
        # value_template comparing against the class name: a class name is
        # free text a person typed into a form, and safely embedding it in a
        # Jinja string literal (quotes, braces) is a problem worth just not
        # having. Every class gets a message, including the ones that did
        # not win, so a class that used to be true and no longer is actually
        # turns off instead of sitting stale at its last ON.
        for class_name in classes:
            state = "ON" if class_name == label else "OFF"
            client.publish(f"{base}/class/{_slug(class_name)}/state",
                           state, qos=0, retain=True)
    except Exception:  # noqa: BLE001 - publishing must never break the caller
        logger.exception("Could not publish classification result for %s", classifier_id)
