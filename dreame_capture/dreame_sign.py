from __future__ import annotations

import hashlib
import json
import time

SALT = "EETjszu*XI5znHsI"


def _sort_recursive(obj):
    if isinstance(obj, dict):
        return {k: _sort_recursive(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_sort_recursive(v) for v in obj]
    return obj


def _splice(obj):
    parts = []
    for k, v in obj.items():
        if isinstance(v, dict):
            parts.append(f"{k}={_sub_splice(v)}")
        elif isinstance(v, list):
            parts.append(f"{k}={_java_tostring(v)}")
        else:
            parts.append(f"{k}={_java_tostring(v)}")
    return "&".join(parts)


def _sub_splice(obj):
    parts = []
    for k, v in obj.items():
        if isinstance(v, dict):
            parts.append(f"{k}={_sub_splice(v)}")
        else:
            parts.append(f"{k}={_java_tostring(v)}")
    return "[" + "&".join(parts) + "]"


def _java_tostring(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        # Java Gson JsonElement toString-ish for arrays used via getJsonValue -> just returns element itself
        # RequestParamsUtil.getJsonValue only special-cases primitives; arrays fall through to element.toString()
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def sign_params(data: dict, timestamp_ms: int | None = None):
    """Reimplements RequestParamsUtil.signParams(String, false) end to end.
    `data` is the JSON object to sign (the full body dict, e.g. {"did":..,"id":..,"data":{...}})
    Returns (signed_dict, timestamp_ms).
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    sorted_obj = _sort_recursive(data)
    canonical = _splice(sorted_obj) + str(timestamp_ms)
    sign = hashlib.md5((canonical + SALT).encode()).hexdigest()
    out = dict(data)
    out["sign"] = sign
    out["timestamp"] = timestamp_ms
    return out, canonical


if __name__ == "__main__":
    # sanity check against captured ground truth
    tests = [
        ({"appVer": 1, "os": 1, "pluginType": "3dmap"}, 1785173621081, "6014b9b4d2282d92e20acd37c2ef302e"),
        ({"appVer": 1, "os": 1, "pluginType": "3dmap"}, 1785173493391, "883691a61700e851dd3eb76dc3761683"),
    ]
    for data, ts, expected in tests:
        _, canonical = sign_params(data, ts)
        actual = hashlib.md5((canonical + SALT).encode()).hexdigest()
        status = "OK" if actual == expected else "MISMATCH"
        print(status, canonical, "->", actual, "expected", expected)
