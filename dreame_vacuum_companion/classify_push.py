"""Push a classification result to the dreame_vacuum_core integration.

Replaces the earlier MQTT broadcast: that needed a broker, credentials, and
Home Assistant's MQTT discovery to all line up before a single entity showed
up, and any one of those being wrong failed silently. This instead POSTs
straight to a webhook the integration registers with Home Assistant and
pushes to this add-on's /register endpoint on every startup - nothing for a
person to install, configure, or debug.
"""
from __future__ import annotations

import logging
import time

import requests

import config_store

logger = logging.getLogger(__name__)

_warned_unavailable = False


def publish_result(classifier_id: str, name: str, classification_type: str,
                    classes: list[str], label: str, score: float, *,
                    tag_id: str, filename: str) -> None:
    """Best-effort: the label is already saved to the dataset by this point
    regardless, so a Home Assistant restart or a slow response here must
    never be the reason the caller's request fails.
    """
    global _warned_unavailable
    url = config_store.get_classification_webhook_url()
    if not url:
        if not _warned_unavailable:
            logger.info(
                "No classification webhook registered yet - the "
                "dreame_vacuum_core integration pushes one to this add-on's "
                "/register endpoint on startup, so this should resolve "
                "itself once Home Assistant has loaded that integration"
            )
            _warned_unavailable = True
        return

    payload = {
        "classifier_id": classifier_id,
        "name": name,
        "classification_type": classification_type,
        "classes": classes,
        "label": label,
        "score": score,
        "tag_id": tag_id,
        "filename": filename,
        "ran_at": int(time.time()),
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code >= 300:
            logger.warning(
                "Integration rejected classification result: HTTP %s", resp.status_code
            )
        else:
            _warned_unavailable = False
    except requests.RequestException as err:
        logger.warning("Could not reach the integration's classification webhook: %s", err)
