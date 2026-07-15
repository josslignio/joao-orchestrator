"""SQLite-backed index for the global registry + task lookups.

Dual-write model: JSON/JSONL artifact files remain the source of truth for
human-readable task data; SQLite is a fast indexed view used for registry
queries, task listing, and (in V0.2) recovery scans.

stdlib sqlite3 only. Schema is versioned in the `schema_meta` table.

V1.4.1 schema_version = 1. V0.2 will add the steps/events tables and the
`storage doctor` / `storage migrate --dry-run` migration logic.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    project_id      TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    profile_path    TEXT,
    registered_at   TEXT NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    title       TEXT,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    branch      TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
"""


class SqliteStore:
    """Thin wrapper over a sqlite3 connection for the registry index."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA_V1)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    # ------------------------------------------------------------------ #
    # Schema introspection (V0.2 will expand into migrators)
    # ------------------------------------------------------------------ #
    def schema_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            return int(row["value"]) if row else 0

    def doctor(self) -> dict:
        """Return a read-only health report. No writes, no migrations."""
        report = {
            "db_path": str(self.db_path),
            "schema_version": self.schema_version(),
            "expected_schema_version": SCHEMA_VERSION,
            "projects": 0,
            "tasks": 0,
            "ok": True,
            "notes": [],
        }
        with self._conn() as conn:
            report["projects"] = conn.execute(
                "SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
            report["tasks"] = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        if report["schema_version"] != SCHEMA_VERSION:
            report["ok"] = False
            report["notes"].append(
                f"schema mismatch: db={report['schema_version']} "
                f"expected={SCHEMA_VERSION}")
        return report

    # ------------------------------------------------------------------ #
    # Projects
    # ------------------------------------------------------------------ #
    def upsert_project(self, project_id: str, display_name: str,
                       repository_root: str, registered_at: str,
                       profile_path: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO projects(project_id, display_name, repository_root,
                                        profile_path, registered_at, schema_version)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE SET
                     display_name=excluded.display_name,
                     repository_root=excluded.repository_root,
                     profile_path=excluded.profile_path""",
                (project_id, display_name, repository_root, profile_path,
                 registered_at, SCHEMA_VERSION),
            )

    def remove_project(self, project_id: str) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM projects WHERE project_id=?",
                               (project_id,))
            return cur.rowcount

    def get_project(self, project_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_projects(self) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY project_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def has_project(self, project_id: str) -> bool:
        return self.get_project(project_id) is not None

    # ------------------------------------------------------------------ #
    # Tasks
    # ------------------------------------------------------------------ #
    def upsert_task(self, task_id: str, project_id: str, title: str,
                    state: str, created_at: str, updated_at: str,
                    branch: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO tasks(task_id, project_id, title, state,
                                      created_at, updated_at, branch, schema_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, task_id) DO UPDATE SET
                     title=excluded.title,
                     state=excluded.state,
                     updated_at=excluded.updated_at,
                     branch=excluded.branch""",
                (task_id, project_id, title, state, created_at, updated_at,
                 branch, SCHEMA_VERSION),
            )

    def list_tasks(self, project_id: Optional[str] = None) -> List[dict]:
        with self._conn() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE project_id=? ORDER BY task_id DESC",
                    (project_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY task_id DESC").fetchall()
            return [dict(r) for r in rows]

    def get_task(self, project_id: str, task_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE project_id=? AND task_id=?",
                (project_id, task_id)).fetchone()
            return dict(row) if row else None


# ---------------------------------------------------------------------------
# V1.4.2 — Durable queue index (Phase A)
# ---------------------------------------------------------------------------
# SQLite is the runtime index ONLY. JSON snapshots (queue.json) remain the
# canonical source of truth; JSONL (queue_events.jsonl) remains the append-only
# audit log. SQLite provides atomic claim/lease/ack/nack under contention and a
# fast recovery scan on startup.
#
# Inspired by litequeue's lease/claim/ack/nack semantics (BSD-3-Clause). No code
# copied — the schema and SQL here are purpose-built for JOSS's project-lease
# model. See THIRD_PARTY_NOTICES.

_QUEUE_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS queue_items (
    queue_id       TEXT NOT NULL,
    item_id        TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    priority       INTEGER NOT NULL DEFAULT 50,
    status         TEXT NOT NULL DEFAULT 'QUEUED',
    not_before     TEXT,
    lease_owner    TEXT,
    lease_expires  TEXT,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (queue_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_qi_status ON queue_items(status);
CREATE INDEX IF NOT EXISTS idx_qi_project ON queue_items(project_id);
CREATE INDEX IF NOT EXISTS idx_qi_claim ON queue_items(status, not_before, priority, created_at);
"""


class SqliteQueueIndex:
    """SQLite-backed durable queue index for atomic claim/lease/ack/nack.

    Single-writer safe via WAL + short transactions. The JSON snapshot remains
    canonical; this index mirrors item state for fast atomic operations and
    startup recovery.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Use immediate transaction for write safety.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_QUEUE_SCHEMA_V1)

    # -- Mirror / sync ---------------------------------------------------- #

    def upsert_item(self, queue_id: str, item_id: str, project_id: str,
                    priority: int, status: str,
                    not_before: Optional[str], lease_owner: Optional[str],
                    lease_expires: Optional[str], attempt_count: int,
                    created_at: str, updated_at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO queue_items(
                       queue_id, item_id, project_id, priority, status,
                       not_before, lease_owner, lease_expires, attempt_count,
                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(queue_id, item_id) DO UPDATE SET
                     project_id=excluded.project_id,
                     priority=excluded.priority,
                     status=excluded.status,
                     not_before=excluded.not_before,
                     lease_owner=excluded.lease_owner,
                     lease_expires=excluded.lease_expires,
                     attempt_count=excluded.attempt_count,
                     updated_at=excluded.updated_at""",
                (queue_id, item_id, project_id, priority, status,
                 not_before, lease_owner, lease_expires, attempt_count,
                 created_at, updated_at),
            )

    def update_status(self, queue_id: str, item_id: str, status: str,
                      updated_at: str,
                      lease_owner: Optional[str] = None,
                      lease_expires: Optional[str] = None) -> bool:
        """Atomically update status. Returns True if a row was updated."""
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE queue_items
                   SET status=?, lease_owner=?, lease_expires=?, updated_at=?
                   WHERE queue_id=? AND item_id=?""",
                (status, lease_owner, lease_expires, updated_at,
                 queue_id, item_id),
            )
            return cur.rowcount > 0

    def delete_item(self, queue_id: str, item_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM queue_items WHERE queue_id=? AND item_id=?",
                (queue_id, item_id))

    # -- Atomic claim / ack / nack ---------------------------------------- #

    def claim(self, queue_id: str, item_id: str, lease_owner: str,
              lease_expires: str, expected_status: str = "QUEUED",
              now: Optional[str] = None) -> bool:
        """Atomically claim an item for execution.

        Succeeds only if the item is still in ``expected_status`` (default
        QUEUED) and either has no active lease or its lease has expired
        (``lease_expires < now`` when ``now`` is provided).
        Returns True if the claim succeeded.
        """
        sql = (
            "UPDATE queue_items "
            "SET status='RUNNING', lease_owner=?, lease_expires=?, updated_at=? "
            "WHERE queue_id=? AND item_id=? AND status=?"
        )
        params: list = [lease_owner, lease_expires, lease_expires,
                        queue_id, item_id, expected_status]
        if now is not None:
            # Allow claim if lease expired. Two OR branches: no lease, or
            # expired lease.
            sql = (
                "UPDATE queue_items "
                "SET status='RUNNING', lease_owner=?, lease_expires=?, "
                "    updated_at=? "
                "WHERE queue_id=? AND item_id=? AND status=? "
                "  AND (lease_expires IS NULL OR lease_expires < ?)"
            )
            params = [lease_owner, lease_expires, lease_expires,
                      queue_id, item_id, expected_status, now]
        with self._conn() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount > 0

    def ack(self, queue_id: str, item_id: str,
            final_status: str = "AWAITING_APPROVAL",
            updated_at: Optional[str] = None) -> bool:
        """Acknowledge successful execution. Clears the lease."""
        updated_at = updated_at or ""
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE queue_items
                   SET status=?, lease_owner=NULL, lease_expires=NULL,
                       updated_at=?
                   WHERE queue_id=? AND item_id=?""",
                (final_status, updated_at, queue_id, item_id),
            )
            return cur.rowcount > 0

    def nack(self, queue_id: str, item_id: str,
             updated_at: Optional[str] = None) -> bool:
        """Negatively acknowledge: mark FAILED, clear lease."""
        updated_at = updated_at or ""
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE queue_items
                   SET status='FAILED', lease_owner=NULL, lease_expires=NULL,
                       updated_at=?
                   WHERE queue_id=? AND item_id=?""",
                (updated_at, queue_id, item_id),
            )
            return cur.rowcount > 0

    # -- Queries ---------------------------------------------------------- #

    def next_claimable(self, queue_id: str, now: str,
                       project_ids_available: Optional[set] = None,
                       ) -> Optional[dict]:
        """Return the next claimable item for a queue, or None.

        Deterministic order: highest priority → oldest created_at → lexical
        item_id. Only items in QUEUED status whose not_before has passed and
        whose project is not currently leased.
        """
        sql = (
            "SELECT * FROM queue_items "
            "WHERE queue_id=? AND status='QUEUED' "
            "  AND (not_before IS NULL OR not_before <= ?) "
            "  AND (lease_expires IS NULL OR lease_expires < ?) "
            "ORDER BY priority DESC, created_at ASC, item_id ASC "
            "LIMIT 1"
        )
        with self._conn() as conn:
            row = conn.execute(sql, (queue_id, now, now)).fetchone()
            if row is None:
                return None
            d = dict(row)
        if project_ids_available is not None:
            if d["project_id"] not in project_ids_available:
                return None
        return d

    def leased_projects(self, now: str) -> set:
        """Return the set of project_ids with an active (non-expired) lease."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT project_id FROM queue_items "
                "WHERE lease_owner IS NOT NULL AND lease_expires > ?",
                (now,)).fetchall()
            return {r["project_id"] for r in rows}

    def stale_leases(self, now: str) -> List[dict]:
        """Return items whose leases have expired but are still RUNNING."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM queue_items "
                "WHERE status='RUNNING' AND lease_expires IS NOT NULL "
                "  AND lease_expires < ?",
                (now,)).fetchall()
            return [dict(r) for r in rows]

    def running_items(self, queue_id: Optional[str] = None) -> List[dict]:
        sql = "SELECT * FROM queue_items WHERE status='RUNNING'"
        params: tuple = ()
        if queue_id:
            sql += " AND queue_id=?"
            params = (queue_id,)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def list_items(self, queue_id: Optional[str] = None,
                   status: Optional[str] = None) -> List[dict]:
        sql = "SELECT * FROM queue_items WHERE 1=1"
        params: list = []
        if queue_id:
            sql += " AND queue_id=?"
            params.append(queue_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at ASC, item_id ASC"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def doctor(self) -> dict:
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM queue_items").fetchone()["n"]
            by_status = {
                r["status"]: r["n"] for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM queue_items "
                    "GROUP BY status").fetchall()}
        return {"queue_items": total, "by_status": by_status, "ok": True}
