"""SQLite-authoritative queue state and lease enforcement.

All queue item status, queue claims, run ownership, and project leases are
committed in one SQLite database. JSON/JSONL files are derived display/audit
artifacts only and are never consulted as an alternate runtime authority after
one-time migration.
"""
from __future__ import annotations

import fcntl
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from ..storage.persistence_errors import (
    LeaseAuthorityError,
    MigrationError,
    SQLiteConstraintError,
    SQLiteCorruptionError,
    SQLiteInitializationError,
    SQLiteLockTimeoutError,
    SQLiteTransactionError,
    StaleLeaseError,
)


@dataclass(frozen=True)
class QueueClaim:
    claim_id: str
    queue_id: str
    item_id: str
    run_id: str
    owner_pid: int
    lease_token: str
    generation: int
    claimed_at: float
    expires_at: float
    heartbeat_at: Optional[float] = None
    project_id: str = ""


@dataclass(frozen=True)
class ProjectLease:
    project_id: str
    owner_run_id: str
    lease_token: str
    generation: int
    leased_at: float
    expires_at: float
    heartbeat_at: Optional[float] = None
    queue_id: str = ""
    item_id: str = ""
    owner_pid: int = 0


@dataclass(frozen=True)
class CombinedLease:
    queue_claim: QueueClaim
    project_lease: ProjectLease


class LeaseAuthority:
    """Fail-closed SQLite authority for queue state and ownership."""

    _SCHEMA_VERSION = 4

    def __init__(self, db_path: Path, timeout_seconds: float = 30.0):
        self.db_path = Path(db_path)
        self.timeout = float(timeout_seconds)
        self._local = threading.local()
        self._ensure_initialized()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(
                    str(self.db_path),
                    timeout=self.timeout,
                    isolation_level=None,
                    check_same_thread=False,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
                # Detect obvious corruption at connection creation, not later.
                result = conn.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise SQLiteCorruptionError(
                        f"lease authority quick_check failed: {result[0] if result else 'no result'}"
                    )
            except SQLiteCorruptionError:
                raise
            except sqlite3.DatabaseError as exc:
                raise self._map_sqlite_error("open", exc) from exc
            self._local.conn = conn
        return conn

    def _map_sqlite_error(self, operation: str, exc: sqlite3.Error) -> Exception:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            return SQLiteLockTimeoutError(f"{operation}: SQLite lock timeout: {exc}")
        if "malformed" in text or "corrupt" in text or "not a database" in text:
            return SQLiteCorruptionError(f"{operation}: SQLite corruption: {exc}")
        if isinstance(exc, sqlite3.IntegrityError):
            return SQLiteConstraintError(f"{operation}: SQLite constraint: {exc}")
        return SQLiteTransactionError(f"{operation}: SQLite failure: {exc}")

    @contextmanager
    def _transaction(self, immediate: bool = False):
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            cur = conn.cursor()
            yield cur
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            if isinstance(exc, LeaseAuthorityError):
                raise
            if isinstance(exc, sqlite3.Error):
                raise self._map_sqlite_error("transaction", exc) from exc
            raise

    def _ensure_initialized(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()
        except SQLiteCorruptionError as exc:
            raise SQLiteInitializationError(
                f"failed to initialize lease authority: {exc}"
            ) from exc
        except (LeaseAuthorityError, OSError):
            raise
        except sqlite3.Error as exc:
            raise SQLiteInitializationError(
                f"failed to initialize lease authority: {exc}"
            ) from exc

    def _init_schema(self) -> None:
        with self._transaction(immediate=True) as cur:
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_counters (
                    scope TEXT NOT NULL,
                    key1 TEXT NOT NULL,
                    key2 TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL,
                    PRIMARY KEY (scope, key1, key2),
                    CHECK (generation > 0)
                );
                CREATE TABLE IF NOT EXISTS queue_items (
                    queue_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    not_before TEXT,
                    attempt_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (queue_id, item_id)
                );
                CREATE INDEX IF NOT EXISTS idx_queue_items_select
                    ON queue_items(queue_id, status, priority DESC, created_at, item_id);
                CREATE INDEX IF NOT EXISTS idx_queue_items_project
                    ON queue_items(project_id, status);
                CREATE TABLE IF NOT EXISTS queue_claims (
                    claim_id TEXT PRIMARY KEY,
                    queue_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    lease_token TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    claimed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    UNIQUE(queue_id, item_id),
                    CHECK (generation > 0)
                );
                CREATE TABLE IF NOT EXISTS project_leases (
                    project_id TEXT PRIMARY KEY,
                    queue_id TEXT NOT NULL DEFAULT '',
                    item_id TEXT NOT NULL DEFAULT '',
                    owner_run_id TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    leased_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    CHECK (generation > 0)
                );
                """
            )
            # Upgrade databases created by earlier incomplete M4 attempts.
            self._ensure_column(cur, "queue_claims", "project_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(cur, "project_leases", "queue_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(cur, "project_leases", "item_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(cur, "project_leases", "owner_pid", "INTEGER NOT NULL DEFAULT 0")
            cur.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('lease_authority_schema_version', ?)",
                ('2',),
            )
            cur.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('queue_authority_schema_version', ?)",
                (str(self._SCHEMA_VERSION),),
            )

    @staticmethod
    def _ensure_column(cur: sqlite3.Cursor, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _canonical_json(value: dict[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _next_generation(self, cur: sqlite3.Cursor, scope: str, key1: str, key2: str = "") -> int:
        row = cur.execute(
            "SELECT generation FROM generation_counters WHERE scope=? AND key1=? AND key2=?",
            (scope, key1, key2),
        ).fetchone()
        generation = (int(row["generation"]) + 1) if row else 1
        cur.execute(
            """INSERT INTO generation_counters(scope,key1,key2,generation)
               VALUES(?,?,?,?)
               ON CONFLICT(scope,key1,key2) DO UPDATE SET generation=excluded.generation""",
            (scope, key1, key2, generation),
        )
        return generation

    # ------------------------------------------------------------------
    # Queue item authority
    # ------------------------------------------------------------------
    def upsert_queue_item(self, item: dict[str, Any]) -> None:
        required = ("queue_id", "item_id", "project_id", "status")
        missing = [key for key in required if not item.get(key)]
        if missing:
            raise SQLiteConstraintError(f"queue item missing required field(s): {missing}")
        payload = dict(item)
        with self._transaction(immediate=True) as cur:
            cur.execute(
                """INSERT INTO queue_items(
                       queue_id,item_id,project_id,status,priority,not_before,
                       attempt_count,created_at,updated_at,payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(queue_id,item_id) DO UPDATE SET
                       project_id=excluded.project_id,
                       status=excluded.status,
                       priority=excluded.priority,
                       not_before=excluded.not_before,
                       attempt_count=excluded.attempt_count,
                       updated_at=excluded.updated_at,
                       payload_json=excluded.payload_json""",
                (
                    payload["queue_id"], payload["item_id"], payload["project_id"],
                    payload["status"], int(payload.get("priority", 50)),
                    payload.get("not_before"), int(payload.get("attempt_count", 0)),
                    payload.get("created_at", ""), payload.get("updated_at", ""),
                    self._canonical_json(payload),
                ),
            )

    def get_queue_item(self, queue_id: str, item_id: str) -> Optional[dict[str, Any]]:
        try:
            row = self.conn.execute(
                "SELECT payload_json FROM queue_items WHERE queue_id=? AND item_id=?",
                (queue_id, item_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise self._map_sqlite_error("get_queue_item", exc) from exc
        if row is None:
            return None
        try:
            return json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SQLiteCorruptionError(
                f"queue item {queue_id}/{item_id} has invalid payload_json"
            ) from exc

    def list_queue_items(self, queue_id: str) -> list[dict[str, Any]]:
        try:
            rows = self.conn.execute(
                "SELECT payload_json FROM queue_items WHERE queue_id=? ORDER BY created_at,item_id",
                (queue_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise self._map_sqlite_error("list_queue_items", exc) from exc
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                result.append(json.loads(row["payload_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise SQLiteCorruptionError(
                    f"queue {queue_id} contains invalid payload_json"
                ) from exc
        return result

    def update_queue_item(self, queue_id: str, item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Update an unclaimed item.

        Any active queue claim makes the item ownership-protected. Runtime
        writers must then use ``update_claimed_queue_item()`` with the exact
        claim tuple. This prevents cancel/retry/reconcile paths from mutating an
        item owned by another scheduler process.
        """
        with self._transaction(immediate=True) as cur:
            active = cur.execute(
                """SELECT claim_id,run_id FROM queue_claims
                   WHERE queue_id=? AND item_id=? AND expires_at>?""",
                (queue_id, item_id, time.time()),
            ).fetchone()
            if active is not None:
                raise StaleLeaseError(
                    f"queue item {queue_id}/{item_id} is actively owned by "
                    f"run {active['run_id']}; ownership-bound update required"
                )
            row = cur.execute(
                "SELECT payload_json FROM queue_items WHERE queue_id=? AND item_id=?",
                (queue_id, item_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"item {item_id!r} not found in queue {queue_id!r}")
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                raise SQLiteCorruptionError(
                    f"queue item {queue_id}/{item_id} has invalid payload_json"
                ) from exc
            payload.update(fields)
            cur.execute(
                """UPDATE queue_items SET project_id=?,status=?,priority=?,not_before=?,
                       attempt_count=?,updated_at=?,payload_json=?
                   WHERE queue_id=? AND item_id=?""",
                (
                    payload["project_id"], payload["status"], int(payload.get("priority", 50)),
                    payload.get("not_before"), int(payload.get("attempt_count", 0)),
                    payload.get("updated_at", ""), self._canonical_json(payload), queue_id, item_id,
                ),
            )
            return payload

    def delete_queue_item(self, queue_id: str, item_id: str) -> None:
        with self._transaction(immediate=True) as cur:
            active = cur.execute(
                "SELECT 1 FROM queue_claims WHERE queue_id=? AND item_id=? AND expires_at>?",
                (queue_id, item_id, time.time()),
            ).fetchone()
            if active:
                raise LeaseAuthorityError("cannot delete an actively claimed queue item")
            cur.execute("DELETE FROM queue_items WHERE queue_id=? AND item_id=?", (queue_id, item_id))

    # ------------------------------------------------------------------
    # One-time legacy migration
    # ------------------------------------------------------------------
    def migrate_from_json(self, json_path: Path) -> None:
        json_path = Path(json_path)
        marker = f"json_migration:{json_path.resolve()}"
        lock_path = self.db_path.with_suffix(self.db_path.suffix + ".migration.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(lock_path, "a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                with self._transaction(immediate=True) as cur:
                    row = cur.execute("SELECT value FROM metadata WHERE key=?", (marker,)).fetchone()
                    if row and row["value"] == "complete":
                        return
                    if not json_path.exists():
                        cur.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?, 'complete')", (marker,))
                        cur.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('json_migration_complete', ?)", (json_path.name,))
                        return
                    try:
                        data = json.loads(json_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise MigrationError(f"cannot migrate {json_path}: {exc}") from exc
                    now = time.time()
                    for item in data.get("items", []):
                        if not isinstance(item, dict):
                            raise MigrationError("legacy queue contains a non-object item")
                        required = ("queue_id", "item_id", "project_id")
                        if any(not item.get(k) for k in required):
                            raise MigrationError("legacy queue item is missing identity fields")
                        item.setdefault("status", "QUEUED")
                        item.setdefault("priority", 50)
                        item.setdefault("attempt_count", 0)
                        item.setdefault("created_at", "")
                        item.setdefault("updated_at", "")
                        cur.execute(
                            """INSERT OR IGNORE INTO queue_items(
                                   queue_id,item_id,project_id,status,priority,not_before,
                                   attempt_count,created_at,updated_at,payload_json)
                               VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (item["queue_id"], item["item_id"], item["project_id"], item["status"],
                             int(item.get("priority", 50)), item.get("not_before"),
                             int(item.get("attempt_count", 0)), item.get("created_at", ""),
                             item.get("updated_at", ""), self._canonical_json(item)),
                        )
                    for claim in data.get("queue_claims", []):
                        queue_id = claim["queue_id"]
                        item_id = claim["item_id"]
                        generation = self._next_generation(cur, "queue", queue_id, item_id)
                        token = secrets.token_hex(16)
                        claim_id = claim.get("claim_id") or f"migrated:{queue_id}:{item_id}:{generation}"
                        cur.execute(
                            """INSERT OR IGNORE INTO queue_claims(
                                   claim_id,queue_id,item_id,project_id,run_id,owner_pid,lease_token,
                                   generation,claimed_at,expires_at,heartbeat_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (claim_id, queue_id, item_id, claim.get("project_id", ""),
                             claim["run_id"], int(claim.get("owner_pid", 0)), token,
                             generation, float(claim.get("claimed_at", now)),
                             float(claim.get("expires_at", now + 3600)), now),
                        )
                    for lease in data.get("project_leases", []):
                        project_id = lease["project_id"]
                        generation = self._next_generation(cur, "project", project_id)
                        cur.execute(
                            """INSERT OR IGNORE INTO project_leases(
                                   project_id,queue_id,item_id,owner_run_id,owner_pid,lease_token,
                                   generation,leased_at,expires_at,heartbeat_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (project_id, lease.get("queue_id", ""), lease.get("item_id", ""),
                             lease["owner_run_id"], int(lease.get("owner_pid", 0)),
                             secrets.token_hex(16), generation, float(lease.get("leased_at", now)),
                             float(lease.get("expires_at", now + 3600)), now),
                        )
                    cur.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?, 'complete')", (marker,))
                    # Compatibility marker used by existing tests/evidence.
                    cur.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('json_migration_complete', ?)", (json_path.name,))
        except MigrationError:
            raise
        except OSError as exc:
            raise MigrationError(f"migration lock failure: {exc}") from exc

    # ------------------------------------------------------------------
    # Lease operations
    # ------------------------------------------------------------------
    def _claim_queue_in_tx(
        self, cur: sqlite3.Cursor, *, queue_id: str, item_id: str,
        project_id: str, run_id: str, owner_pid: int, ttl_seconds: float,
        now: float,
    ) -> QueueClaim:
        existing = cur.execute(
            "SELECT * FROM queue_claims WHERE queue_id=? AND item_id=?",
            (queue_id, item_id),
        ).fetchone()
        if existing and float(existing["expires_at"]) > now:
            raise SQLiteConstraintError(
                f"queue item {queue_id}/{item_id} already claimed by {existing['run_id']}"
            )
        if existing:
            cur.execute("DELETE FROM queue_claims WHERE claim_id=?", (existing["claim_id"],))
        generation = self._next_generation(cur, "queue", queue_id, item_id)
        token = secrets.token_hex(16)
        claim_id = f"{queue_id}:{item_id}:{generation}:{secrets.token_hex(8)}"
        expires_at = now + float(ttl_seconds)
        cur.execute(
            """INSERT INTO queue_claims(
                   claim_id,queue_id,item_id,project_id,run_id,owner_pid,lease_token,
                   generation,claimed_at,expires_at,heartbeat_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (claim_id, queue_id, item_id, project_id, run_id, int(owner_pid), token,
             generation, now, expires_at, now),
        )
        return QueueClaim(claim_id, queue_id, item_id, run_id, int(owner_pid), token,
                          generation, now, expires_at, now, project_id)

    def _claim_project_in_tx(
        self, cur: sqlite3.Cursor, *, project_id: str, queue_id: str,
        item_id: str, run_id: str, owner_pid: int, ttl_seconds: float,
        now: float,
    ) -> ProjectLease:
        existing = cur.execute(
            "SELECT * FROM project_leases WHERE project_id=?", (project_id,)
        ).fetchone()
        if existing and float(existing["expires_at"]) > now:
            raise SQLiteConstraintError(
                f"project {project_id} already leased by {existing['owner_run_id']}"
            )
        if existing:
            cur.execute("DELETE FROM project_leases WHERE project_id=?", (project_id,))
        generation = self._next_generation(cur, "project", project_id)
        token = secrets.token_hex(16)
        expires_at = now + float(ttl_seconds)
        cur.execute(
            """INSERT INTO project_leases(
                   project_id,queue_id,item_id,owner_run_id,owner_pid,lease_token,
                   generation,leased_at,expires_at,heartbeat_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (project_id, queue_id, item_id, run_id, int(owner_pid), token,
             generation, now, expires_at, now),
        )
        return ProjectLease(project_id, run_id, token, generation, now,
                            expires_at, now, queue_id, item_id, int(owner_pid))

    def claim_queue_item(
        self, queue_id: str, item_id: str, run_id: str, owner_pid: int,
        ttl_seconds: float, project_id: str = "",
    ) -> QueueClaim:
        now = time.time()
        with self._transaction(immediate=True) as cur:
            return self._claim_queue_in_tx(
                cur, queue_id=queue_id, item_id=item_id, project_id=project_id,
                run_id=run_id, owner_pid=owner_pid, ttl_seconds=ttl_seconds, now=now,
            )

    def claim_project_lease(
        self, project_id: str, run_id: str, ttl_seconds: float,
        queue_id: str = "", item_id: str = "", owner_pid: int = 0,
    ) -> ProjectLease:
        now = time.time()
        with self._transaction(immediate=True) as cur:
            return self._claim_project_in_tx(
                cur, project_id=project_id, queue_id=queue_id, item_id=item_id,
                run_id=run_id, owner_pid=owner_pid, ttl_seconds=ttl_seconds, now=now,
            )

    def claim_item_and_project(
        self, *, queue_id: str, item_id: str, project_id: str, run_id: str,
        owner_pid: int, ttl_seconds: float,
    ) -> CombinedLease:
        """Atomically claim a queue item and its project in one transaction."""
        now = time.time()
        with self._transaction(immediate=True) as cur:
            item = cur.execute(
                "SELECT status FROM queue_items WHERE queue_id=? AND item_id=?",
                (queue_id, item_id),
            ).fetchone()
            if item is None:
                raise SQLiteConstraintError(f"unknown queue item {queue_id}/{item_id}")
            if item["status"] != "QUEUED":
                raise SQLiteConstraintError(
                    f"queue item {queue_id}/{item_id} is {item['status']}, not QUEUED"
                )
            project = self._claim_project_in_tx(
                cur, project_id=project_id, queue_id=queue_id, item_id=item_id,
                run_id=run_id, owner_pid=owner_pid, ttl_seconds=ttl_seconds, now=now,
            )
            try:
                claim = self._claim_queue_in_tx(
                    cur, queue_id=queue_id, item_id=item_id, project_id=project_id,
                    run_id=run_id, owner_pid=owner_pid, ttl_seconds=ttl_seconds, now=now,
                )
            except Exception:
                # The enclosing transaction rolls back the project insert too.
                raise
            payload_row = cur.execute(
                "SELECT payload_json FROM queue_items WHERE queue_id=? AND item_id=?",
                (queue_id, item_id),
            ).fetchone()
            payload = json.loads(payload_row["payload_json"])
            payload["status"] = "RUNNING"
            payload["started_at"] = payload.get("started_at") or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
            )
            payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            cur.execute(
                "UPDATE queue_items SET status='RUNNING',updated_at=?,payload_json=? WHERE queue_id=? AND item_id=?",
                (payload["updated_at"], self._canonical_json(payload), queue_id, item_id),
            )
            return CombinedLease(claim, project)

    def _load_claim(self, cur: sqlite3.Cursor, claim_id: str) -> sqlite3.Row:
        row = cur.execute("SELECT * FROM queue_claims WHERE claim_id=?", (claim_id,)).fetchone()
        if row is None:
            raise StaleLeaseError(f"claim {claim_id} no longer exists")
        return row

    def get_claim_for_item(self, queue_id: str, item_id: str) -> Optional[QueueClaim]:
        """Return the current claim for an item, including an expired claim.

        Recovery needs the exact persisted ownership tuple. Absence means the
        RUNNING row is orphaned/corrupt and must not be treated as actively
        owned.
        """
        try:
            row = self.conn.execute(
                "SELECT * FROM queue_claims WHERE queue_id=? AND item_id=?",
                (queue_id, item_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise self._map_sqlite_error("get_claim_for_item", exc) from exc
        if row is None:
            return None
        return QueueClaim(
            row["claim_id"], row["queue_id"], row["item_id"], row["run_id"],
            int(row["owner_pid"]), row["lease_token"], int(row["generation"]),
            float(row["claimed_at"]), float(row["expires_at"]),
            float(row["heartbeat_at"]), row["project_id"],
        )

    @staticmethod
    def _same_token(actual: str, supplied: str) -> bool:
        return hmac.compare_digest(str(actual), str(supplied))

    def _require_claim_owner(
        self, row: sqlite3.Row, *, lease_token: str, generation: int,
        run_id: Optional[str] = None, owner_pid: Optional[int] = None,
        require_unexpired: bool = True,
    ) -> None:
        if not self._same_token(row["lease_token"], lease_token):
            raise StaleLeaseError("stale owner: lease token mismatch")
        if int(row["generation"]) != int(generation):
            raise StaleLeaseError("stale owner: lease generation mismatch")
        if run_id is not None and row["run_id"] != run_id:
            raise StaleLeaseError("lease run_id mismatch")
        if owner_pid is not None and int(row["owner_pid"]) != int(owner_pid):
            raise StaleLeaseError("lease owner_pid mismatch")
        if require_unexpired and float(row["expires_at"]) <= time.time():
            raise StaleLeaseError("lease expired")

    def renew_lease(
        self, claim_id: str, lease_token: str, generation: int,
        ttl_seconds: float, run_id: Optional[str] = None,
        owner_pid: Optional[int] = None,
    ) -> QueueClaim:
        now = time.time()
        expires_at = now + float(ttl_seconds)
        with self._transaction(immediate=True) as cur:
            row = self._load_claim(cur, claim_id)
            self._require_claim_owner(
                row, lease_token=lease_token, generation=generation,
                run_id=run_id, owner_pid=owner_pid,
            )
            cur.execute(
                "UPDATE queue_claims SET expires_at=?,heartbeat_at=? WHERE claim_id=?",
                (expires_at, now, claim_id),
            )
            if row["project_id"]:
                project = cur.execute(
                    "SELECT * FROM project_leases WHERE project_id=?", (row["project_id"],)
                ).fetchone()
                if project is None or project["owner_run_id"] != row["run_id"]:
                    raise StaleLeaseError("paired project lease missing or owned by another run")
                cur.execute(
                    "UPDATE project_leases SET expires_at=?,heartbeat_at=? WHERE project_id=?",
                    (expires_at, now, row["project_id"]),
                )
            return QueueClaim(
                row["claim_id"], row["queue_id"], row["item_id"], row["run_id"],
                int(row["owner_pid"]), row["lease_token"], int(row["generation"]),
                float(row["claimed_at"]), expires_at, now, row["project_id"],
            )

    def heartbeat(
        self, claim_id: str, lease_token: str, generation: int,
        ttl_seconds: float = 5.0, run_id: Optional[str] = None,
        owner_pid: Optional[int] = None,
    ) -> bool:
        try:
            self.renew_lease(
                claim_id, lease_token, generation, ttl_seconds,
                run_id=run_id, owner_pid=owner_pid,
            )
            return True
        except StaleLeaseError:
            return False

    def check_lease_valid(
        self, claim_id: str, lease_token: str, generation: int,
        run_id: Optional[str] = None, owner_pid: Optional[int] = None,
    ) -> bool:
        try:
            row = self.conn.execute("SELECT * FROM queue_claims WHERE claim_id=?", (claim_id,)).fetchone()
        except sqlite3.Error as exc:
            raise self._map_sqlite_error("check_lease_valid", exc) from exc
        if row is None:
            return False
        try:
            self._require_claim_owner(
                row, lease_token=lease_token, generation=generation,
                run_id=run_id, owner_pid=owner_pid,
            )
            if row["project_id"]:
                project = self.conn.execute(
                    "SELECT * FROM project_leases WHERE project_id=?", (row["project_id"],)
                ).fetchone()
                if project is None or project["owner_run_id"] != row["run_id"]:
                    return False
                if float(project["expires_at"]) <= time.time():
                    return False
            return True
        except StaleLeaseError:
            return False

    def check_project_lease_valid(
        self, project_id: str, lease_token: str, generation: int,
        run_id: Optional[str] = None, owner_pid: Optional[int] = None,
    ) -> bool:
        row = self.conn.execute(
            "SELECT * FROM project_leases WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None or float(row["expires_at"]) <= time.time():
            return False
        if not self._same_token(row["lease_token"], lease_token):
            return False
        if int(row["generation"]) != int(generation):
            return False
        if run_id is not None and row["owner_run_id"] != run_id:
            return False
        if owner_pid is not None and int(row["owner_pid"]) != int(owner_pid):
            return False
        return True

    def update_claimed_queue_item(
        self, *, claim_id: str, lease_token: str, generation: int,
        run_id: str, owner_pid: int, fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a queue item only while the exact claim is still owned."""
        with self._transaction(immediate=True) as cur:
            row = self._load_claim(cur, claim_id)
            self._require_claim_owner(
                row, lease_token=lease_token, generation=generation,
                run_id=run_id, owner_pid=owner_pid,
            )
            project = cur.execute(
                "SELECT * FROM project_leases WHERE project_id=?", (row["project_id"],)
            ).fetchone()
            if project is None or project["owner_run_id"] != run_id:
                raise StaleLeaseError("paired project lease missing or stale")
            if int(project["owner_pid"]) != int(owner_pid) or float(project["expires_at"]) <= time.time():
                raise StaleLeaseError("paired project lease lost")
            item_row = cur.execute(
                "SELECT payload_json FROM queue_items WHERE queue_id=? AND item_id=?",
                (row["queue_id"], row["item_id"]),
            ).fetchone()
            if item_row is None:
                raise SQLiteCorruptionError("claimed queue item row is missing")
            payload = json.loads(item_row["payload_json"])
            payload.update(fields)
            payload["updated_at"] = payload.get("updated_at") or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            cur.execute(
                """UPDATE queue_items SET project_id=?,status=?,priority=?,not_before=?,
                       attempt_count=?,updated_at=?,payload_json=?
                   WHERE queue_id=? AND item_id=?""",
                (payload["project_id"], payload["status"], int(payload.get("priority", 50)),
                 payload.get("not_before"), int(payload.get("attempt_count", 0)),
                 payload["updated_at"], self._canonical_json(payload),
                 row["queue_id"], row["item_id"]),
            )
            return payload

    def complete_item_and_release(
        self, *, claim_id: str, lease_token: str, generation: int,
        project_lease_token: str, project_generation: int,
        run_id: str, owner_pid: int, final_status: str,
        item_fields: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Verify both ownership records, update item, then release atomically."""
        with self._transaction(immediate=True) as cur:
            row = self._load_claim(cur, claim_id)
            self._require_claim_owner(
                row, lease_token=lease_token, generation=generation,
                run_id=run_id, owner_pid=owner_pid,
            )
            project = cur.execute(
                "SELECT * FROM project_leases WHERE project_id=?", (row["project_id"],)
            ).fetchone()
            if project is None:
                raise StaleLeaseError("paired project lease missing")
            if not self._same_token(project["lease_token"], project_lease_token):
                raise StaleLeaseError("project lease token mismatch")
            if int(project["generation"]) != int(project_generation):
                raise StaleLeaseError("project lease generation mismatch")
            if project["owner_run_id"] != run_id or int(project["owner_pid"]) != int(owner_pid):
                raise StaleLeaseError("project lease owner mismatch")
            if float(project["expires_at"]) <= time.time():
                raise StaleLeaseError("project lease expired")
            item_row = cur.execute(
                "SELECT payload_json FROM queue_items WHERE queue_id=? AND item_id=?",
                (row["queue_id"], row["item_id"]),
            ).fetchone()
            if item_row is None:
                raise SQLiteCorruptionError("claimed queue item row is missing")
            payload = json.loads(item_row["payload_json"])
            payload.update(item_fields or {})
            payload["status"] = final_status
            payload["updated_at"] = payload.get("updated_at") or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            cur.execute(
                "UPDATE queue_items SET status=?,updated_at=?,payload_json=? WHERE queue_id=? AND item_id=?",
                (final_status, payload["updated_at"], self._canonical_json(payload),
                 row["queue_id"], row["item_id"]),
            )
            cur.execute("DELETE FROM queue_claims WHERE claim_id=?", (claim_id,))
            cur.execute("DELETE FROM project_leases WHERE project_id=?", (row["project_id"],))
            return payload

    def ack_lease(
        self, claim_id: str, lease_token: str, generation: int,
        final_status: str,
    ) -> bool:
        # Compatibility API for direct claims without a paired project lease.
        with self._transaction(immediate=True) as cur:
            row = cur.execute("SELECT * FROM queue_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if row is None:
                return False
            try:
                self._require_claim_owner(row, lease_token=lease_token, generation=generation)
            except StaleLeaseError:
                return False
            cur.execute("DELETE FROM queue_claims WHERE claim_id=?", (claim_id,))
            return True

    def nack_lease(self, claim_id: str, lease_token: str, generation: int) -> bool:
        return self.ack_lease(claim_id, lease_token, generation, "FAILED")

    def release_project_lease(
        self, project_id: str, lease_token: str, generation: int,
        run_id: Optional[str] = None, owner_pid: Optional[int] = None,
    ) -> bool:
        with self._transaction(immediate=True) as cur:
            row = cur.execute("SELECT * FROM project_leases WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                return False
            if not self._same_token(row["lease_token"], lease_token):
                return False
            if int(row["generation"]) != int(generation):
                return False
            if run_id is not None and row["owner_run_id"] != run_id:
                return False
            if owner_pid is not None and int(row["owner_pid"]) != int(owner_pid):
                return False
            cur.execute("DELETE FROM project_leases WHERE project_id=?", (project_id,))
            return True

    def heartbeat_project(
        self, project_id: str, lease_token: str, generation: int,
        ttl_seconds: float = 5.0,
    ) -> bool:
        now = time.time()
        with self._transaction(immediate=True) as cur:
            row = cur.execute("SELECT * FROM project_leases WHERE project_id=?", (project_id,)).fetchone()
            if row is None or float(row["expires_at"]) <= now:
                return False
            if not self._same_token(row["lease_token"], lease_token):
                return False
            if int(row["generation"]) != int(generation):
                return False
            cur.execute(
                "UPDATE project_leases SET heartbeat_at=?,expires_at=? WHERE project_id=?",
                (now, now + float(ttl_seconds), project_id),
            )
            return True

    def advance_generation(
        self, claim_id: str, old_lease_token: str, old_generation: int,
        new_run_id: str, new_owner_pid: int, ttl_seconds: float,
    ) -> QueueClaim:
        with self._transaction(immediate=True) as cur:
            row = self._load_claim(cur, claim_id)
            if float(row["expires_at"]) > time.time():
                raise StaleLeaseError("cannot advance a non-expired lease")
            if not self._same_token(row["lease_token"], old_lease_token) or int(row["generation"]) != int(old_generation):
                raise StaleLeaseError("old lease identity mismatch")
            cur.execute("DELETE FROM queue_claims WHERE claim_id=?", (claim_id,))
            return self._claim_queue_in_tx(
                cur, queue_id=row["queue_id"], item_id=row["item_id"],
                project_id=row["project_id"], run_id=new_run_id,
                owner_pid=new_owner_pid, ttl_seconds=ttl_seconds, now=time.time(),
            )

    def reconcile_expired_claim(
        self, *, queue_id: str, item_id: str, claim_id: str,
        lease_token: str, generation: int, run_id: str, owner_pid: int,
        final_status: str, item_fields: Optional[dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Recover exactly one expired claim and its paired project atomically.

        The persisted queue claim is the recovery authority. Active claims are
        never mutated, and a mismatched/missing paired project lease is treated
        as corruption rather than silently released.
        """
        current_time = float(now if now is not None else time.time())
        with self._transaction(immediate=True) as cur:
            row = self._load_claim(cur, claim_id)
            if row["queue_id"] != queue_id or row["item_id"] != item_id:
                raise StaleLeaseError("recovery claim does not belong to requested item")
            self._require_claim_owner(
                row, lease_token=lease_token, generation=generation,
                run_id=run_id, owner_pid=owner_pid, require_unexpired=False,
            )
            if float(row["expires_at"]) > current_time:
                raise StaleLeaseError("cannot reconcile an active lease")
            project = cur.execute(
                "SELECT * FROM project_leases WHERE project_id=?",
                (row["project_id"],),
            ).fetchone()
            if project is None:
                raise SQLiteCorruptionError("expired claim is missing its paired project lease")
            if (
                project["queue_id"] != queue_id
                or project["item_id"] != item_id
                or project["owner_run_id"] != run_id
                or int(project["owner_pid"]) != int(owner_pid)
            ):
                raise SQLiteCorruptionError("expired claim/project ownership tuple mismatch")
            if float(project["expires_at"]) > current_time:
                raise StaleLeaseError("paired project lease is still active")

            item_row = cur.execute(
                "SELECT payload_json FROM queue_items WHERE queue_id=? AND item_id=?",
                (queue_id, item_id),
            ).fetchone()
            if item_row is None:
                raise SQLiteCorruptionError("expired claim points to a missing queue item")
            try:
                payload = json.loads(item_row["payload_json"])
            except json.JSONDecodeError as exc:
                raise SQLiteCorruptionError("expired claim queue payload is corrupt") from exc
            payload.update(item_fields or {})
            payload["status"] = final_status
            payload["updated_at"] = payload.get("updated_at") or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(current_time)
            )
            cur.execute(
                """UPDATE queue_items SET status=?,updated_at=?,payload_json=?
                   WHERE queue_id=? AND item_id=?""",
                (
                    final_status, payload["updated_at"], self._canonical_json(payload),
                    queue_id, item_id,
                ),
            )
            cur.execute(
                "DELETE FROM queue_claims WHERE claim_id=? AND generation=?",
                (claim_id, int(generation)),
            )
            cur.execute(
                """DELETE FROM project_leases
                   WHERE project_id=? AND queue_id=? AND item_id=?
                     AND owner_run_id=? AND owner_pid=?""",
                (row["project_id"], queue_id, item_id, run_id, int(owner_pid)),
            )
            return payload

    def recover_expired(self, *, now: Optional[float] = None, status: str = "QUEUED") -> list[dict[str, Any]]:
        """Transactionally recover expired claims and release paired projects."""
        now = float(now if now is not None else time.time())
        recovered: list[dict[str, Any]] = []
        with self._transaction(immediate=True) as cur:
            rows = cur.execute("SELECT * FROM queue_claims WHERE expires_at<=? ORDER BY claim_id", (now,)).fetchall()
            for row in rows:
                item_row = cur.execute(
                    "SELECT payload_json FROM queue_items WHERE queue_id=? AND item_id=?",
                    (row["queue_id"], row["item_id"]),
                ).fetchone()
                if item_row:
                    payload = json.loads(item_row["payload_json"])
                    payload["status"] = status
                    payload["last_error"] = "STALE_LEASE_RECOVERED"
                    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                    cur.execute(
                        "UPDATE queue_items SET status=?,updated_at=?,payload_json=? WHERE queue_id=? AND item_id=?",
                        (status, payload["updated_at"], self._canonical_json(payload),
                         row["queue_id"], row["item_id"]),
                    )
                cur.execute("DELETE FROM queue_claims WHERE claim_id=?", (row["claim_id"],))
                if row["project_id"]:
                    cur.execute(
                        "DELETE FROM project_leases WHERE project_id=? AND owner_run_id=?",
                        (row["project_id"], row["run_id"]),
                    )
                recovered.append(dict(row))
        return recovered

    def get_active_claims(self, queue_id: Optional[str] = None) -> list[QueueClaim]:
        sql = "SELECT * FROM queue_claims WHERE expires_at>?"
        params: list[Any] = [time.time()]
        if queue_id is not None:
            sql += " AND queue_id=?"
            params.append(queue_id)
        sql += " ORDER BY claim_id"
        rows = self.conn.execute(sql, params).fetchall()
        return [
            QueueClaim(
                row["claim_id"], row["queue_id"], row["item_id"], row["run_id"],
                int(row["owner_pid"]), row["lease_token"], int(row["generation"]),
                float(row["claimed_at"]), float(row["expires_at"]),
                float(row["heartbeat_at"]), row["project_id"],
            ) for row in rows
        ]

    def get_active_project_leases(self) -> list[ProjectLease]:
        rows = self.conn.execute(
            "SELECT * FROM project_leases WHERE expires_at>? ORDER BY project_id",
            (time.time(),),
        ).fetchall()
        return [
            ProjectLease(
                row["project_id"], row["owner_run_id"], row["lease_token"],
                int(row["generation"]), float(row["leased_at"]),
                float(row["expires_at"]), float(row["heartbeat_at"]),
                row["queue_id"], row["item_id"], int(row["owner_pid"]),
            ) for row in rows
        ]

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            delattr(self._local, "conn")
