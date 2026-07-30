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

-- What happened on each errand the integration ran. Kept here rather than only
-- in Home Assistant's log because a robot in another room is much easier to
-- debug from a page than from a log file, and the trace is short-lived
-- information that does not belong in the recorder database.
CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    did      TEXT NOT NULL,
    command  TEXT NOT NULL,
    ok       INTEGER NOT NULL,
    at       INTEGER NOT NULL,
    summary  TEXT,
    detail   TEXT            -- json: {"trace": [...], "error": "...", ...}
);

CREATE INDEX IF NOT EXISTS idx_runs_at ON runs(at DESC);

-- Steps arrive while the errand is still running, so the UI can follow along
-- rather than waiting for a result.
CREATE TABLE IF NOT EXISTS run_steps (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    at     REAL NOT NULL,
    text   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps(run_id, id);
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


# -- runs -----------------------------------------------------------------
RUN_HISTORY = 200


def start_run(did, command):
    """Open a run and return its id. Steps and an outcome follow."""
    with _lock, _connect() as db:
        cur = db.execute(
            "INSERT INTO runs (did, command, ok, at, summary, detail) VALUES (?,?,?,?,?,?)",
            (str(did), str(command), -1, int(time.time()), None, "{}"),
        )
        run_id = cur.lastrowid
        db.execute(
            "DELETE FROM run_steps WHERE run_id NOT IN "
            "(SELECT id FROM runs ORDER BY at DESC, id DESC LIMIT ?)",
            (RUN_HISTORY,),
        )
        db.execute(
            "DELETE FROM runs WHERE id NOT IN "
            "(SELECT id FROM runs ORDER BY at DESC, id DESC LIMIT ?)",
            (RUN_HISTORY,),
        )
    return run_id


def add_step(run_id, text):
    with _lock, _connect() as db:
        db.execute(
            "INSERT INTO run_steps (run_id, at, text) VALUES (?,?,?)",
            (int(run_id), time.time(), str(text)),
        )


def finish_run(run_id, ok, summary, detail):
    with _lock, _connect() as db:
        db.execute(
            "UPDATE runs SET ok = ?, summary = ?, detail = ? WHERE id = ?",
            (1 if ok else 0, summary, json.dumps(detail or {}), int(run_id)),
        )


def add_run(did, command, ok, summary, detail):
    """Record one errand. Trimmed to the most recent RUN_HISTORY rows so this
    cannot grow without bound on a device that patrols on a schedule."""
    with _lock, _connect() as db:
        db.execute(
            "INSERT INTO runs (did, command, ok, at, summary, detail) VALUES (?,?,?,?,?,?)",
            (str(did), str(command), 1 if ok else 0, int(time.time()),
             summary, json.dumps(detail or {})),
        )
        db.execute(
            "DELETE FROM runs WHERE id NOT IN "
            "(SELECT id FROM runs ORDER BY at DESC, id DESC LIMIT ?)",
            (RUN_HISTORY,),
        )


def list_runs(did=None, limit=50):
    query = "SELECT id, did, command, ok, at, summary, detail FROM runs"
    params = []
    if did:
        query += " WHERE did = ?"
        params.append(str(did))
    query += " ORDER BY at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with _lock, _connect() as db:
        rows = db.execute(query, params).fetchall()
    out = []
    with _lock, _connect() as db:
        for row in rows:
            try:
                detail = json.loads(row[6] or "{}")
            except ValueError:
                detail = {}
            steps = db.execute(
                "SELECT at, text FROM run_steps WHERE run_id = ? ORDER BY id", (row[0],)
            ).fetchall()
            out.append({
                "id": row[0], "did": row[1], "command": row[2],
                # -1 means still running - distinct from finished-and-failed.
                "ok": None if row[3] == -1 else bool(row[3]),
                "running": row[3] == -1,
                "at": row[4], "summary": row[5], "detail": detail,
                "steps": [{"at": s_at, "text": s_text} for s_at, s_text in steps],
            })
    return out
