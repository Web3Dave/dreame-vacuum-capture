"""Read Home Assistant state / call services from inside the add-on.

Supervisor injects SUPERVISOR_TOKEN and proxies Home Assistant at
http://supervisor/core/api/ when `homeassistant_api: true` is set in
config.yaml.

Deliberately read-mostly: HA owns vacuum state, so the UI queries it live
rather than the add-on keeping its own copy.

Note the REST API exposes entity *states* but not which integration owns an
entity - that mapping only exists in the entity registry, which is WebSocket
only. We avoid needing it by having the integration register its devices with
us explicitly (see store.register_devices); `discover_by_platform` is the
fallback for when that hasn't happened.
"""
from __future__ import annotations

import json
import os

import requests

SUPERVISOR_CORE = "http://supervisor/core/api"
WS_URL = "ws://supervisor/core/websocket"


class HomeAssistantUnavailable(RuntimeError):
    pass


def _token() -> str:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise HomeAssistantUnavailable(
            "SUPERVISOR_TOKEN missing - is this running as a Home Assistant add-on "
            "with homeassistant_api: true?"
        )
    return token


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def available() -> bool:
    try:
        r = requests.get(f"{SUPERVISOR_CORE}/", headers=_headers(), timeout=5)
        return r.status_code == 200
    except (requests.RequestException, HomeAssistantUnavailable):
        return False


def get_state(entity_id: str) -> dict | None:
    try:
        r = requests.get(f"{SUPERVISOR_CORE}/states/{entity_id}", headers=_headers(), timeout=8)
    except (requests.RequestException, HomeAssistantUnavailable):
        return None
    if r.status_code != 200:
        return None
    return r.json()


def get_api(path: str, timeout: int = 15) -> dict | None:
    """Anything else under Home Assistant's own /api/ namespace.

    For the integration's own custom views (e.g. /dreame_vacuum_unlocked_integration/maps/
    <did>) - the Dreame cloud client and login session live in the
    integration, not here, so this add-on asks Home Assistant for
    already-decoded data rather than duplicating that client. `path` is
    relative to /api, matching how get_state's own /states/<id> is built.
    Returns None on anything but a 200 - the caller cannot usefully tell
    "not found" from "vacuum unreachable" apart anyway, and does not need to.
    """
    try:
        r = requests.get(f"{SUPERVISOR_CORE}{path}", headers=_headers(), timeout=timeout)
    except (requests.RequestException, HomeAssistantUnavailable):
        return None
    if r.status_code != 200:
        return None
    return r.json()


def get_states(entity_ids: list[str]) -> dict[str, dict]:
    """Individual lookups - the full /states dump is large and we only ever
    care about a handful of known entities."""
    out = {}
    for eid in entity_ids:
        st = get_state(eid)
        if st:
            out[eid] = st
    return out


def call_service(domain: str, service: str, data: dict | None = None) -> bool:
    return call_service_result(domain, service, data)[0]


def call_service_result(
    domain: str, service: str, data: dict | None = None, timeout: int = 120
) -> tuple[bool, str]:
    """Call a service and return why it failed, not just that it did.

    Home Assistant's reason is the useful part - "Service not found" means the
    integration needs updating, which is invisible if the caller only sees a
    boolean.
    """
    try:
        r = requests.post(
            f"{SUPERVISOR_CORE}/services/{domain}/{service}",
            headers=_headers(),
            data=json.dumps(data or {}),
            timeout=timeout,
        )
        if r.status_code in (200, 201):
            return True, ""
        detail = (r.text or "").strip()
        if r.status_code == 400 and "not found" in detail.lower():
            detail = (
                f"Home Assistant has no {domain}.{service} service. "
                "Update the integration and restart Home Assistant."
            )
        return False, detail[:300] or f"HTTP {r.status_code}"
    except requests.RequestException as err:
        return False, f"Could not reach Home Assistant: {err}"
    except HomeAssistantUnavailable as err:
        return False, str(err)


def discover_by_platform(platform: str = "dreame_vacuum_unlocked_integration") -> list[dict]:
    """Fallback device discovery via the entity registry (WebSocket only).

    Used when the integration hasn't registered with us - e.g. the add-on was
    installed standalone, or the integration was removed and re-added.
    Returns [] rather than raising if anything is unavailable; the UI treats
    an empty list as "nothing registered yet".
    """
    try:
        from websocket import create_connection  # type: ignore
    except ImportError:
        return []

    try:
        ws = create_connection(WS_URL, timeout=10)
    except Exception:  # noqa: BLE001 - discovery is best-effort
        return []

    try:
        ws.recv()  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": _token()}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            return []
        ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1 and msg.get("type") == "result":
                entries = msg.get("result") or []
                break
        else:  # pragma: no cover
            return []
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass

    return [e for e in entries if e.get("platform") == platform]
