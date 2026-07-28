"""Persistent state for the companion app.

SQLite rather than Postgres deliberately: the data here is device registrations
and patrol routes - kilobytes, single writer. A bundled Postgres would mean a
much larger image and more failure modes, and depending on a *separate*
Postgres add-on would make installation fragile. This lives in /data, so it is
covered by the add-on's normal backup.

Vacuum *state* is intentionally not stored here. Home Assistant already owns
that; the UI reads it live over the HA API so there is no second copy to drift.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path("/data/companion.db")

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    did           TEXT PRIMARY KEY,
    name          TEXT,
    model         TEXT,
    entry_id      TEXT,
    entities      TEXT,      -- json: {"vacuum": "vacuum.x", ...}
    registered_at INTEGER,
    last_seen     INTEGER
);

CREATE TABLE IF NOT EXISTS routes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    did        TEXT NOT NULL,
    name       TEXT NOT NULL,
    waypoints  TEXT NOT NULL,   -- json: [{"x":..,"y":..,"heading":..,"dwell":..}]
    created_at INTEGER,
    updated_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_routes_did ON routes(did);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, _connect() as conn:
        conn.executescript(SCHEMA)


def register_devices(entry_id: str, devices: list[dict]) -> int:
    """Upsert the device list the integration reports.

    The integration is authoritative about which devices are 'ours' - this
    avoids the UI having to guess from an entity-registry dump.
    """
    now = int(time.time())
    with _lock, _connect() as conn:
        for dev in devices:
            did = str(dev.get("did") or "").strip()
            if not did:
                continue
            conn.execute(
                """
                INSERT INTO devices (did, name, model, entry_id, entities, registered_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(did) DO UPDATE SET
                    name=excluded.name,
                    model=excluded.model,
                    entry_id=excluded.entry_id,
                    entities=excluded.entities,
                    last_seen=excluded.last_seen
                """,
                (
                    did,
                    dev.get("name"),
                    dev.get("model"),
                    entry_id,
                    json.dumps(dev.get("entities") or {}, separators=(",", ":")),
                    now,
                    now,
                ),
            )
    return len(devices)


def list_devices() -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY name IS NULL, name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["entities"] = json.loads(d.get("entities") or "{}")
        except json.JSONDecodeError:
            d["entities"] = {}
        out.append(d)
    return out


def get_device(did: str) -> dict | None:
    for dev in list_devices():
        if dev["did"] == did:
            return dev
    return None


def list_routes(did: str | None = None) -> list[dict]:
    with _lock, _connect() as conn:
        if did:
            rows = conn.execute("SELECT * FROM routes WHERE did = ? ORDER BY name", (did,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM routes ORDER BY did, name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["waypoints"] = json.loads(d["waypoints"])
        except json.JSONDecodeError:
            d["waypoints"] = []
        out.append(d)
    return out


def save_route(did: str, name: str, waypoints: list[dict], route_id: int | None = None) -> int:
    now = int(time.time())
    payload = json.dumps(waypoints, separators=(",", ":"))
    with _lock, _connect() as conn:
        if route_id:
            conn.execute(
                "UPDATE routes SET did=?, name=?, waypoints=?, updated_at=? WHERE id=?",
                (did, name, payload, now, route_id),
            )
            return route_id
        cur = conn.execute(
            "INSERT INTO routes (did, name, waypoints, created_at, updated_at) VALUES (?,?,?,?,?)",
            (did, name, payload, now, now),
        )
        return int(cur.lastrowid)


def delete_route(route_id: int) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))
