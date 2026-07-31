"""Persistent state for the companion app.

SQLite rather than Postgres deliberately: the data here is device registrations
and patrol routes - kilobytes, single writer. A bundled Postgres would mean a
much larger image and more failure modes, and depending on a *separate*
Postgres add-on would make installation fragile. This lives in /data, so it is
covered by the add-on's normal backup.

Vacuum *state* is intentionally not stored here. Home Assistant already owns
that; the UI reads it live over the HA API so there is no second copy to drift.

Tags, tasks and classifications used to live here too, but they are settings a
person authors - the kind of thing you want to read, diff and back up as text -
so they moved to config_store.py, a YAML file, the same way Frigate keeps
config.yml rather than a database. The tables and the functions below that
read them (list_tags, list_tasks, list_classifiers, and the rest of that
group) are kept only so config_store.migrate_from_sqlite can do a one-time
export on first boot after the upgrade; nothing else calls them any more, and
they are not exercised by fresh installs at all.
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
    detail   TEXT,           -- json: {"trace": [...], "error": "...", ...}
    run_uid  TEXT            -- the integration's id for this run, so the
                             -- vacuum's live state and this row agree
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

-- Tasks are authored here and stay here: the app is the source of truth, and
-- exporting to a Home Assistant script is a one-way snapshot the user then
-- owns. Nothing writes to Home Assistant's config, so there is no second copy
-- to reconcile.
--
-- A task belongs to one vacuum, because its coordinates are millimetres in
-- that vacuum's map frame and mean nothing on another robot.
CREATE TABLE IF NOT EXISTS tasks (
    slug       TEXT PRIMARY KEY,
    did        TEXT NOT NULL,
    name       TEXT NOT NULL,
    steps      TEXT NOT NULL,     -- json: [{"type": "...", ...}]
    created_at INTEGER,
    updated_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tasks_did ON tasks(did);

-- Snapshot tags. Global rather than per-vacuum: "poop_check" means the same
-- thing whichever robot takes the photo, and the snapshot folders on disk are
-- already keyed by tag alone. The id is derived from the name and is what a
-- step stores; renaming a tag re-derives it (the edit UI's job, later).
CREATE TABLE IF NOT EXISTS tags (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at INTEGER,
    updated_at INTEGER
);

-- Classifications: a state a model will learn to read from snapshots (the
-- model itself comes later - this is the authoring side). A classification
-- is linked to one or more tags, and each link carries its own crop: the
-- same state can be judged from two viewpoints, and the square that frames
-- it is different in each.
CREATE TABLE IF NOT EXISTS classifiers (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS classifier_tags (
    classifier_id TEXT NOT NULL,
    tag_id        TEXT NOT NULL,
    crop          TEXT NOT NULL,   -- json [x1,y1,x2,y2], normalised 0-1
    created_at    INTEGER,
    PRIMARY KEY (classifier_id, tag_id)
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# Columns added after a release. CREATE TABLE IF NOT EXISTS does nothing to a
# table that already exists, so an upgrade needs these applied explicitly -
# without them the first insert against an older database fails.
MIGRATIONS = [
    ("runs", "run_uid", "ALTER TABLE runs ADD COLUMN run_uid TEXT"),
]


def init() -> None:
    with _lock, _connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, statement in MIGRATIONS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(statement)


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


def start_run(did, command, run_uid=None):
    """Open a run and return its id. Steps and an outcome follow."""
    with _lock, _connect() as db:
        cur = db.execute(
            "INSERT INTO runs (did, command, ok, at, summary, detail, run_uid) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(did), str(command), -1, int(time.time()), None, "{}", run_uid),
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
    query = "SELECT id, did, command, ok, at, summary, detail, run_uid FROM runs"
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
                "run_uid": row[7],
                "steps": [{"at": s_at, "text": s_text} for s_at, s_text in steps],
            })
    return out


# -- tasks ----------------------------------------------------------------
def slugify(value):
    """A stable, typeable handle - this is what an automation refers to.

    Lowercase letters, digits and underscores only, matching what the editor
    shows as it autofills the id from the name. Hyphens used to be allowed;
    they now fold to underscores like every other separator, which is why
    save accepts a previous_slug - an id that changes must rename the row,
    not duplicate it.
    """
    lowered = (value or "").strip().lower()
    cleaned = "".join(c if (c.isalnum() and c.isascii()) or c == "_" else "_" for c in lowered)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:48]


def save_task(slug, did, name, steps):
    now = int(time.time())
    with _lock, _connect() as db:
        db.execute(
            "INSERT INTO tasks (slug, did, name, steps, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET did=excluded.did, name=excluded.name, "
            "steps=excluded.steps, updated_at=excluded.updated_at",
            (slug, str(did), name, json.dumps(steps), now, now),
        )


def get_task(slug):
    with _lock, _connect() as db:
        row = db.execute(
            "SELECT slug, did, name, steps, created_at, updated_at FROM tasks WHERE slug = ?",
            (slug,),
        ).fetchone()
    return _task_row(row) if row else None


def list_tasks(did=None):
    query = "SELECT slug, did, name, steps, created_at, updated_at FROM tasks"
    params = []
    if did:
        query += " WHERE did = ?"
        params.append(str(did))
    query += " ORDER BY name COLLATE NOCASE"
    with _lock, _connect() as db:
        rows = db.execute(query, params).fetchall()
    return [_task_row(row) for row in rows]


def delete_task(slug):
    with _lock, _connect() as db:
        return db.execute("DELETE FROM tasks WHERE slug = ?", (slug,)).rowcount > 0


def _task_row(row):
    try:
        steps = json.loads(row[3] or "[]")
    except ValueError:
        steps = []
    return {
        "slug": row[0], "did": row[1], "name": row[2], "steps": steps,
        "created_at": row[4], "updated_at": row[5],
    }


# -- tags -----------------------------------------------------------------
def list_tags():
    with _lock, _connect() as db:
        rows = db.execute("SELECT id, name FROM tags ORDER BY name COLLATE NOCASE").fetchall()
    return [{"id": r[0], "name": r[1]} for r in rows]


def save_tag(name):
    """Create (or refresh the name of) a tag. Returns it, or None for a name
    that reduces to nothing."""
    tag_id = slugify(name)
    if not tag_id:
        return None
    now = int(time.time())
    with _lock, _connect() as db:
        db.execute(
            "INSERT INTO tags (id, name, created_at, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
            (tag_id, str(name).strip(), now, now),
        )
    return {"id": tag_id, "name": str(name).strip()}


def ensure_tags(ids):
    """Adopt tags that exist only as folders on disk.

    Snapshots taken before this table existed (or via the service with an
    ad-hoc tag) already have folders; the dropdown should offer them rather
    than pretend they are not there. The display name is the id with its
    underscores read as spaces - the best available guess, editable later.
    """
    now = int(time.time())
    with _lock, _connect() as db:
        for raw in ids:
            tag_id = slugify(raw)
            if not tag_id:
                continue
            db.execute(
                "INSERT OR IGNORE INTO tags (id, name, created_at, updated_at) VALUES (?,?,?,?)",
                (tag_id, tag_id.replace("_", " "), now, now),
            )


def rename_tag(tag_id, name):
    """Change a tag's display name. The id is untouched deliberately: it is
    the snapshot folder name and what a task step's tag field stores, so
    changing it would orphan every photo already taken and every step that
    references it. Returns the updated tag, or None if the name is empty or
    the tag does not exist."""
    name = str(name or "").strip()
    if not name:
        return None
    now = int(time.time())
    with _lock, _connect() as db:
        updated = db.execute(
            "UPDATE tags SET name = ?, updated_at = ? WHERE id = ?",
            (name, now, tag_id),
        ).rowcount
    return {"id": tag_id, "name": name} if updated else None


def delete_tag(tag_id):
    """Remove a tag and its classifier links. The caller owns the snapshot
    folder - files are not this module's business."""
    with _lock, _connect() as db:
        db.execute("DELETE FROM classifier_tags WHERE tag_id = ?", (tag_id,))
        return db.execute("DELETE FROM tags WHERE id = ?", (tag_id,)).rowcount > 0


# -- classifications -------------------------------------------------------
def list_classifiers():
    """Every classification, each with its tag links and their crops."""
    with _lock, _connect() as db:
        rows = db.execute(
            "SELECT id, name FROM classifiers ORDER BY name COLLATE NOCASE"
        ).fetchall()
        links = db.execute(
            "SELECT classifier_id, tag_id, crop FROM classifier_tags ORDER BY tag_id"
        ).fetchall()
    by_classifier = {}
    for classifier_id, tag_id, crop in links:
        try:
            parsed = json.loads(crop)
        except ValueError:
            continue
        by_classifier.setdefault(classifier_id, []).append(
            {"tag_id": tag_id, "crop": parsed}
        )
    return [
        {"id": r[0], "name": r[1], "tags": by_classifier.get(r[0], [])} for r in rows
    ]


def get_classifier(classifier_id):
    for c in list_classifiers():
        if c["id"] == classifier_id:
            return c
    return None


def create_classifier(name):
    """Create a classification. Returns it, or None for an empty name, or
    raises ValueError if the id is already taken - a silent upsert here would
    quietly merge two classifications someone named alike."""
    classifier_id = slugify(name)
    if not classifier_id:
        return None
    now = int(time.time())
    with _lock, _connect() as db:
        exists = db.execute(
            "SELECT 1 FROM classifiers WHERE id = ?", (classifier_id,)
        ).fetchone()
        if exists:
            raise ValueError(f"The id '{classifier_id}' is already in use")
        db.execute(
            "INSERT INTO classifiers (id, name, created_at, updated_at) VALUES (?,?,?,?)",
            (classifier_id, str(name).strip(), now, now),
        )
    return {"id": classifier_id, "name": str(name).strip(), "tags": []}


def delete_classifier(classifier_id):
    with _lock, _connect() as db:
        db.execute("DELETE FROM classifier_tags WHERE classifier_id = ?", (classifier_id,))
        return db.execute(
            "DELETE FROM classifiers WHERE id = ?", (classifier_id,)
        ).rowcount > 0


def set_classifier_tag(classifier_id, tag_id, crop):
    """Link a tag (or update its crop). The crop travels with the link, not
    the classification: the same state seen from two viewpoints needs two
    different squares."""
    now = int(time.time())
    with _lock, _connect() as db:
        db.execute(
            "INSERT INTO classifier_tags (classifier_id, tag_id, crop, created_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(classifier_id, tag_id) DO UPDATE SET crop=excluded.crop",
            (classifier_id, tag_id, json.dumps(list(crop)), now),
        )


def unlink_classifier_tag(classifier_id, tag_id):
    with _lock, _connect() as db:
        return db.execute(
            "DELETE FROM classifier_tags WHERE classifier_id = ? AND tag_id = ?",
            (classifier_id, tag_id),
        ).rowcount > 0


def close_orphaned_runs(did=None, summary="Abandoned - Home Assistant restarted"):
    """Close runs still marked in progress.

    Only the integration can know an errand ended, and it calls this at
    startup. Without it a row stays 'running' forever and a task looks
    permanently busy.
    """
    query = "UPDATE runs SET ok = 0, summary = COALESCE(summary, ?) WHERE ok = -1"
    params = [summary]
    if did:
        query += " AND did = ?"
        params.append(str(did))
    with _lock, _connect() as db:
        return db.execute(query, params).rowcount
