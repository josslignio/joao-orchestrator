"""Durable queue and bounded scheduler.

Migrated design for V1.4.1 Phase 4. Key properties:
- Queue state stored outside managed repositories (under state_root).
- Atomic JSON snapshots + append-only JSONL audit logs.
- Deterministic item selection (priority → created_at → lexical item_id).
- Project leases prevent concurrent execution on the same project.
- Bounded scheduler with explicit max-tasks and max-minutes limits.
- Crash recovery via reconciliation of RUNNING items against task evidence.
- Never automatically approves, commits, pushes, publishes, or merges.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..domain.events import now_iso
from ..domain.identifiers import validate_identifier
from ..storage.atomic import atomic_write_json, append_line


# ---------------------------------------------------------------------------
# Queue item statuses
# ---------------------------------------------------------------------------

class QueueItemStatus(str, Enum):
    QUEUED = "QUEUED"
    BLOCKED = "BLOCKED"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @classmethod
    def terminal(cls) -> set:
        return {cls.CANCELLED, cls.AWAITING_APPROVAL}


# ---------------------------------------------------------------------------
# Reason codes for selection/skip/stop decisions
# ---------------------------------------------------------------------------

class ReasonCode(str, Enum):
    READY = "READY"
    DEPENDENCY_PENDING = "DEPENDENCY_PENDING"
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
    DEPENDENCY_UNKNOWN = "DEPENDENCY_UNKNOWN"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    PROJECT_LOCKED = "PROJECT_LOCKED"
    SOURCE_REVISION_INVALID = "SOURCE_REVISION_INVALID"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_BUDGET_LOW = "PROVIDER_BUDGET_LOW"
    PROVIDER_OVERRIDE_REQUIRED = "PROVIDER_OVERRIDE_REQUIRED"
    TASK_RUN_FAILED = "TASK_RUN_FAILED"
    CONVERGENCE_FAILED = "CONVERGENCE_FAILED"
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"
    SCHEDULER_LIMIT_REACHED = "SCHEDULER_LIMIT_REACHED"
    SCHEDULER_IDLE = "SCHEDULER_IDLE"
    STALE_LEASE_RECOVERED = "STALE_LEASE_RECOVERED"
    AMBIGUOUS_RECOVERY_STATE = "AMBIGUOUS_RECOVERY_STATE"
    NOT_BEFORE_PENDING = "NOT_BEFORE_PENDING"
    INTERNAL_INVARIANT_FAILED = "INTERNAL_INVARIANT_FAILED"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class QueueItem:
    schema_version: int = 1
    queue_id: str = ""
    item_id: str = ""
    project_id: str = ""
    task_id: Optional[str] = None
    title: str = ""
    request_file: str = ""
    done_criteria: List[str] = field(default_factory=list)
    source_revision: str = ""
    category: str = "implementation"
    size: str = "small"
    priority: int = 50
    dependencies: List[str] = field(default_factory=list)
    implementation_engine: str = "auto"
    review_engine: str = "auto"
    fix_engine: str = "auto"
    max_fixes: int = 1
    status: str = QueueItemStatus.QUEUED.value
    attempt_count: int = 0
    max_attempts: int = 1
    not_before: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "schema_version": self.schema_version,
            "queue_id": self.queue_id,
            "item_id": self.item_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "title": self.title,
            "request_file": self.request_file,
            "done_criteria": self.done_criteria,
            "source_revision": self.source_revision,
            "category": self.category,
            "size": self.size,
            "priority": self.priority,
            "dependencies": self.dependencies,
            "implementation_engine": self.implementation_engine,
            "review_engine": self.review_engine,
            "fix_engine": self.fix_engine,
            "max_fixes": self.max_fixes,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "not_before": self.not_before,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_error": self.last_error,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "QueueItem":
        return cls(
            schema_version=d.get("schema_version", 1),
            queue_id=d.get("queue_id", ""),
            item_id=d.get("item_id", ""),
            project_id=d.get("project_id", ""),
            task_id=d.get("task_id"),
            title=d.get("title", ""),
            request_file=d.get("request_file", ""),
            done_criteria=d.get("done_criteria", []),
            source_revision=d.get("source_revision", ""),
            category=d.get("category", "implementation"),
            size=d.get("size", "small"),
            priority=d.get("priority", 50),
            dependencies=d.get("dependencies", []),
            implementation_engine=d.get("implementation_engine", "auto"),
            review_engine=d.get("review_engine", "auto"),
            fix_engine=d.get("fix_engine", "auto"),
            max_fixes=d.get("max_fixes", 1),
            status=d.get("status", QueueItemStatus.QUEUED.value),
            attempt_count=d.get("attempt_count", 0),
            max_attempts=d.get("max_attempts", 1),
            not_before=d.get("not_before"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            last_error=d.get("last_error"),
        )

    def is_terminal(self) -> bool:
        return self.status in QueueItemStatus.terminal()


@dataclass
class ProjectLease:
    project_id: str = ""
    queue_id: str = ""
    item_id: str = ""
    scheduler_run_id: str = ""
    owner_pid: int = 0
    hostname: str = ""
    acquired_at: str = ""
    heartbeat_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "queue_id": self.queue_id,
            "item_id": self.item_id,
            "scheduler_run_id": self.scheduler_run_id,
            "owner_pid": self.owner_pid,
            "hostname": self.hostname,
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectLease":
        return cls(
            project_id=d.get("project_id", ""),
            queue_id=d.get("queue_id", ""),
            item_id=d.get("item_id", ""),
            scheduler_run_id=d.get("scheduler_run_id", ""),
            owner_pid=d.get("owner_pid", 0),
            hostname=d.get("hostname", ""),
            acquired_at=d.get("acquired_at", ""),
            heartbeat_at=d.get("heartbeat_at", ""),
            expires_at=d.get("expires_at", ""),
        )

    def is_expired(self, now_fn=None) -> bool:
        now_fn = now_fn or now_iso
        now = now_fn()
        return self.expires_at and self.expires_at < now


@dataclass
class SchedulerRun:
    run_id: str = ""
    queue_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    tasks_completed: int = 0
    tasks_failed: int = 0
    stop_reason: str = ""
    max_tasks: int = 0
    max_minutes: int = 0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "queue_id": self.queue_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "stop_reason": self.stop_reason,
            "max_tasks": self.max_tasks,
            "max_minutes": self.max_minutes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SchedulerRun":
        return cls(
            run_id=d.get("run_id", ""),
            queue_id=d.get("queue_id", ""),
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
            tasks_completed=d.get("tasks_completed", 0),
            tasks_failed=d.get("tasks_failed", 0),
            stop_reason=d.get("stop_reason", ""),
            max_tasks=d.get("max_tasks", 0),
            max_minutes=d.get("max_minutes", 0),
        )


@dataclass
class SelectionResult:
    """Result of deterministic item selection."""
    selected_item: Optional[QueueItem] = None
    skipped_reasons: Dict[str, str] = field(default_factory=dict)
    no_runnable_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# QueueStore — durable queue persistence
# ---------------------------------------------------------------------------

class QueueStore:
    """Durable queue storage. All state outside managed repositories.

    JSON snapshot (queue.json) remains the canonical source of truth; JSONL
    (queue_events.jsonl) remains the append-only audit log; a SQLite index
    (queue_index.db) provides atomic claim/lease/ack/nack and fast recovery.
    """

    def __init__(self, state_root: Path, sqlite_index: Any = None):
        self.state_root = Path(state_root).expanduser().resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.queues_dir = self.state_root / "queues"
        self.queues_dir.mkdir(parents=True, exist_ok=True)
        self.leases_dir = self.state_root / "leases"
        self.leases_dir.mkdir(parents=True, exist_ok=True)
        self._lock = __import__("threading").Lock()
        # Optional SQLite index for atomic claim/lease/ack/nack. Lazily created
        # so legacy callers (no SQLite) keep working.
        if sqlite_index is not None:
            self.index = sqlite_index
        else:
            try:
                from ..storage.sqlite_store import SqliteQueueIndex
                self.index = SqliteQueueIndex(
                    self.state_root / "queue_index.db")
            except Exception:
                self.index = None

    # -- Path helpers ------------------------------------------------------ #

    def _queue_dir(self, queue_id: str) -> Path:
        validate_identifier(queue_id, "queue_id")
        d = self.queues_dir / queue_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _snapshot_path(self, queue_id: str) -> Path:
        return self._queue_dir(queue_id) / "queue.json"

    def _events_path(self, queue_id: str) -> Path:
        return self._queue_dir(queue_id) / "queue_events.jsonl"

    def _lease_path(self, project_id: str) -> Path:
        validate_identifier(project_id, "project_id")
        return self.leases_dir / f"{project_id}.json"

    def _scheduler_runs_path(self) -> Path:
        return self.state_root / "scheduler_runs.jsonl"

    def _scheduler_state_path(self) -> Path:
        return self.state_root / "scheduler_state.json"

    def _run_dir(self, queue_id: str, run_id: str) -> Path:
        d = self._queue_dir(queue_id) / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- Snapshot persistence ---------------------------------------------- #

    def _write_snapshot(self, queue_id: str, items: List[QueueItem]) -> None:
        atomic_write_json(
            self._snapshot_path(queue_id),
            {"schema_version": 1, "queue_id": queue_id,
             "items": [i.to_dict() for i in items]})

    def _append_event(self, queue_id: str, event: dict) -> None:
        event["ts"] = now_iso()
        append_line(self._events_path(queue_id), json.dumps(event, sort_keys=True))

    def _mirror_item(self, item: QueueItem) -> None:
        """Mirror an item into the SQLite index (best-effort)."""
        if self.index is None:
            return
        try:
            self.index.upsert_item(
                queue_id=item.queue_id, item_id=item.item_id,
                project_id=item.project_id, priority=item.priority,
                status=item.status, not_before=item.not_before,
                lease_owner=None, lease_expires=None,
                attempt_count=item.attempt_count,
                created_at=item.created_at or now_iso(),
                updated_at=item.updated_at or now_iso())
        except Exception:
            pass  # Index is best-effort; JSON remains canonical.

    # -- CRUD -------------------------------------------------------------- #

    def add_item(self, item: QueueItem) -> QueueItem:
        """Add an item to the queue. Validates uniqueness and dependencies."""
        validate_identifier(item.queue_id, "queue_id")
        validate_identifier(item.item_id, "item_id")
        validate_identifier(item.project_id, "project_id")
        with self._lock:
            existing = self.load_queue(item.queue_id)
            for ei in existing:
                if ei.item_id == item.item_id:
                    raise ValueError(
                        f"duplicate item_id {item.item_id!r} in queue "
                        f"{item.queue_id!r}")
            # Validate dependencies exist (no unknown, no cycle).
            self._validate_dependencies(item, existing)
            ts = now_iso()
            if not item.created_at:
                item.created_at = ts
            item.updated_at = item.updated_at or ts
            existing.append(item)
            self._write_snapshot(item.queue_id, existing)
            self._mirror_item(item)
            self._append_event(item.queue_id, {
                "event": "item_added",
                "item_id": item.item_id,
                "status": item.status,
            })
        return item

    def load_item(self, queue_id: str, item_id: str) -> QueueItem:
        for item in self.load_queue(queue_id):
            if item.item_id == item_id:
                return item
        raise KeyError(f"item {item_id!r} not found in queue {queue_id!r}")

    def load_queue(self, queue_id: str) -> List[QueueItem]:
        path = self._snapshot_path(queue_id)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return [QueueItem.from_dict(d) for d in data.get("items", [])]

    def update_item(self, queue_id: str, item_id: str,
                     **fields) -> QueueItem:
        with self._lock:
            items = self.load_queue(queue_id)
            found = None
            for i, item in enumerate(items):
                if item.item_id == item_id:
                    found = i
                    break
            if found is None:
                raise KeyError(
                    f"item {item_id!r} not found in queue {queue_id!r}")
            item = items[found]
            old_status = item.status
            for k, v in fields.items():
                setattr(item, k, v)
            item.updated_at = now_iso()
            items[found] = item
            self._write_snapshot(queue_id, items)
            self._mirror_item(item)
            if "status" in fields and fields["status"] != old_status:
                self._append_event(queue_id, {
                    "event": "status_changed",
                    "item_id": item_id,
                    "from": old_status,
                    "to": fields["status"],
                    "reason": fields.get("last_error", ""),
                })
        return item

    def cancel_item(self, queue_id: str, item_id: str,
                    reason: str = "") -> QueueItem:
        item = self.load_item(queue_id, item_id)
        if item.is_terminal():
            raise ValueError(
                f"item {item_id!r} is terminal ({item.status}); cannot cancel")
        return self.update_item(
            queue_id, item_id,
            status=QueueItemStatus.CANCELLED.value,
            last_error=reason or ReasonCode.CANCELLED_BY_USER.value,
            finished_at=now_iso())

    def retry_item(self, queue_id: str, item_id: str) -> QueueItem:
        """Retry a failed item. Preserves history in event log."""
        item = self.load_item(queue_id, item_id)
        if item.status != QueueItemStatus.FAILED.value:
            raise ValueError(
                f"item {item_id!r} is {item.status}; only FAILED items can be retried")
        if item.attempt_count >= item.max_attempts:
            raise ValueError(
                f"item {item_id!r} already at max attempts "
                f"({item.attempt_count}/{item.max_attempts})")
        ts = now_iso()
        return self.update_item(
            queue_id, item_id,
            status=QueueItemStatus.QUEUED.value,
            attempt_count=item.attempt_count + 1,
            started_at=None,
            finished_at=None,
            last_error=None,
        )

    # -- SQLite-index-backed atomic operations (Phase A) ------------------- #
    # These mirror the JSON state but use SQLite for atomicity under
    # contention. JSON + JSONL remain the canonical audit trail.

    def claim(self, queue_id: str, item_id: str, lease_owner: str,
              lease_expires: str, now: Optional[str] = None) -> bool:
        """Atomically claim an item. Returns True on success."""
        if self.index is None:
            # Fallback: JSON-only optimistic claim.
            return self._json_claim(queue_id, item_id, lease_owner,
                                    lease_expires)
        now = now or now_iso()
        # Mirror the lease into the index atomically; do this AFTER
        # update_item so the lease is not overwritten by _mirror_item.
        ok = self.index.claim(queue_id, item_id, lease_owner,
                              lease_expires, expected_status="QUEUED",
                              now=now)
        if not ok:
            return False
        # Update JSON (best-effort; index is the authority for the lease).
        self.update_item(queue_id, item_id,
                         status=QueueItemStatus.RUNNING.value,
                         started_at=now)
        # Re-apply the lease in the index (update_item overwrote it).
        self.index.update_status(queue_id, item_id, "RUNNING",
                                 updated_at=now,
                                 lease_owner=lease_owner,
                                 lease_expires=lease_expires)
        self._append_event(queue_id, {
            "event": "item_claimed",
            "item_id": item_id,
            "lease_owner": lease_owner,
            "lease_expires": lease_expires,
        })
        return True

    def ack(self, queue_id: str, item_id: str,
            final_status: str = QueueItemStatus.AWAITING_APPROVAL.value,
            now: Optional[str] = None) -> bool:
        """Acknowledge successful execution. Clears the lease."""
        now = now or now_iso()
        if self.index is not None:
            self.index.ack(queue_id, item_id, final_status=final_status,
                           updated_at=now)
        self.update_item(queue_id, item_id,
                         status=final_status, finished_at=now)
        self._append_event(queue_id, {
            "event": "item_acked",
            "item_id": item_id,
            "final_status": final_status,
        })
        return True

    def nack(self, queue_id: str, item_id: str,
             reason: str = "",
             now: Optional[str] = None) -> bool:
        """Negatively acknowledge: mark FAILED, clear lease."""
        now = now or now_iso()
        if self.index is not None:
            self.index.nack(queue_id, item_id, updated_at=now)
        self.update_item(queue_id, item_id,
                         status=QueueItemStatus.FAILED.value,
                         finished_at=now,
                         last_error=reason or "nack")
        self._append_event(queue_id, {
            "event": "item_nacked",
            "item_id": item_id,
            "reason": reason,
        })
        return True

    def _json_claim(self, queue_id: str, item_id: str,
                    lease_owner: str, lease_expires: str) -> bool:
        """Optimistic JSON-only claim fallback when SQLite is unavailable."""
        with self._lock:
            items = self.load_queue(queue_id)
            for i, item in enumerate(items):
                if item.item_id == item_id:
                    if item.status != QueueItemStatus.QUEUED.value:
                        return False
                    item.status = QueueItemStatus.RUNNING.value
                    item.started_at = now_iso()
                    items[i] = item
                    self._write_snapshot(queue_id, items)
                    self._append_event(queue_id, {
                        "event": "item_claimed",
                        "item_id": item_id,
                        "lease_owner": lease_owner,
                        "lease_expires": lease_expires,
                    })
                    return True
            return False

    # -- Startup recovery -------------------------------------------------- #

    def sync_index_from_json(self) -> int:
        """Rebuild the SQLite index from JSON snapshots. Returns item count.

        Called on startup to ensure the index matches canonical JSON state.
        """
        if self.index is None:
            return 0
        count = 0
        if not self.queues_dir.is_dir():
            return 0
        for qdir in self.queues_dir.iterdir():
            if not qdir.is_dir():
                continue
            queue_id = qdir.name
            items = self.load_queue(queue_id)
            for item in items:
                self._mirror_item(item)
                count += 1
        return count

    def recover_stale_leases(self, now: Optional[str] = None,
                             task_store: Any = None) -> List[dict]:
        """Recover items whose leases have expired.

        For each stale RUNNING item, inspect the task state if available:
        - AWAITING_APPROVAL → ack the item
        - FAILED → nack the item
        - any other state → nack (fail closed)

        Returns a list of recovery records.
        """
        now = now or now_iso()
        records: List[dict] = []
        if self.index is None:
            return records
        stale = self.index.stale_leases(now)
        for row in stale:
            queue_id = row["queue_id"]
            item_id = row["item_id"]
            project_id = row["project_id"]
            action = "FAILED"
            reason = "stale lease; ambiguous recovery"
            if task_store is not None and row.get("item_id"):
                # Look up the queue item to get the task_id.
                try:
                    item = self.load_item(queue_id, item_id)
                    if item.task_id:
                        meta = task_store.load(project_id, item.task_id)
                        if meta.state == "AWAITING_APPROVAL":
                            action = "AWAITING_APPROVAL"
                            reason = "stale lease; task reached approval"
                        elif meta.state == "FAILED":
                            action = "FAILED"
                            reason = "stale lease; task failed"
                except Exception:
                    pass
            if action == "AWAITING_APPROVAL":
                self.ack(queue_id, item_id, final_status=action, now=now)
            else:
                self.nack(queue_id, item_id, reason=reason, now=now)
            self._append_event(queue_id, {
                "event": "lease_recovered",
                "item_id": item_id,
                "project_id": project_id,
                "action": action,
                "reason": reason,
            })
            records.append({
                "queue_id": queue_id, "item_id": item_id,
                "project_id": project_id, "action": action,
                "reason": reason,
            })
        return records

    # -- Dependency validation --------------------------------------------- #

    def _validate_dependencies(self, item: QueueItem,
                              existing: List[QueueItem]) -> None:
        """Validate dependencies: no unknown IDs, no cycles, no failed deps."""
        existing_ids = {ei.item_id for ei in existing}
        for dep_id in item.dependencies:
            if dep_id not in existing_ids:
                raise ValueError(
                    f"unknown dependency {dep_id!r} for item {item.item_id!r}")

        # Check for cycles with the new item included.
        all_items = list(existing) + [item]
        cycle = _detect_dependency_cycle(all_items)
        if cycle:
            raise ValueError(
                f"dependency cycle detected: {' -> '.join(cycle)}")


def _detect_dependency_cycle(items: List[QueueItem]) -> Optional[List[str]]:
    """Detect dependency cycle using Kahn's algorithm. Returns cycle path or None."""
    item_map = {i.item_id: i for i in items}
    in_degree = {i.item_id: 0 for i in items}
    adj = {i.item_id: [] for i in items}
    for item in items:
        for dep in item.dependencies:
            if dep in in_degree:
                in_degree[item.item_id] += 1
                adj[dep].append(item.item_id)

    queue = [iid for iid, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited == len(items):
        return None  # No cycle.

    # Extract a cycle from remaining nodes.
    remaining = [iid for iid, deg in in_degree.items() if deg > 0]
    if not remaining:
        return None
    # Find path back to start.
    start = remaining[0]
    path = [start]
    seen = {start}
    current = start
    while True:
        deps_of = item_map.get(current)
        if deps_of:
            for dep in deps_of.dependencies:
                if dep in in_degree and in_degree[dep] > 0:
                    if dep in seen:
                        cycle_start = path.index(dep)
                        return path[cycle_start:] + [dep]
                    if dep in remaining:
                        path.append(dep)
                        seen.add(dep)
                        current = dep
                        break
            else:
                break
        else:
            break
    return remaining


# ---------------------------------------------------------------------------
# Scheduler engine
# ---------------------------------------------------------------------------

class SchedulerEngine:
    """Bounded scheduler that executes queue items using existing primitives."""

    LEASE_DURATION_SECONDS = 3600  # 1 hour default lease.

    def __init__(self, state_root: Path, now_fn=None, sleep_fn=None):
        self.state_root = Path(state_root).expanduser().resolve()
        self.store = QueueStore(self.state_root)
        self.now_fn = now_fn or now_iso
        self.sleep_fn = sleep_fn or _default_sleep

    # -- Lease management --------------------------------------------------- #

    def _acquire_lease(self, queue_id: str, item_id: str,
                       project_id: str,
                       run_id: str) -> ProjectLease:
        lease_path = self.store._lease_path(project_id)
        recovered = False
        with self.store._lock:
            if lease_path.is_file():
                existing = ProjectLease.from_dict(
                    json.loads(lease_path.read_text(encoding="utf-8")))
                if not existing.is_expired(self.now_fn):
                    raise ValueError(
                        f"project {project_id!r} is locked by "
                        f"queue={existing.queue_id} item={existing.item_id}")
                # Expired lease — recover it.
                recovered = True
                self.store._append_event(queue_id, {
                    "event": "lease_recovered",
                    "project_id": project_id,
                    "previous_item_id": existing.item_id,
                    "previous_run_id": existing.scheduler_run_id,
                    "reason": ReasonCode.STALE_LEASE_RECOVERED.value,
                })

            now = self.now_fn()
            expires = _add_seconds(now, self.LEASE_DURATION_SECONDS)
            lease = ProjectLease(
                project_id=project_id, queue_id=queue_id,
                item_id=item_id, scheduler_run_id=run_id,
                owner_pid=os.getpid(),
                hostname=_hostname(),
                acquired_at=now, heartbeat_at=now,
                expires_at=expires,
            )
            atomic_write_json(lease_path, lease.to_dict())
        return lease

    def _release_lease(self, project_id: str) -> None:
        lease_path = self.store._lease_path(project_id)
        with self.store._lock:
            if lease_path.is_file():
                lease_path.unlink()

    def _check_lease(self, project_id: str) -> Optional[ProjectLease]:
        lease_path = self.store._lease_path(project_id)
        if not lease_path.is_file():
            return None
        try:
            return ProjectLease.from_dict(
                json.loads(lease_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return None

    # -- Selection algorithm ----------------------------------------------- #

    def select_next(self, queue_id: str, budget_store: Any = None,
                    profile: Any = None,
                    repo_root: Optional[str] = None) -> SelectionResult:
        """Select the next runnable item deterministically."""
        items = self.store.load_queue(queue_id)
        queued = [i for i in items
                  if i.status == QueueItemStatus.QUEUED.value]
        if not queued:
            non_terminal = [i for i in items if not i.is_terminal()]
            if non_terminal:
                return SelectionResult(
                    no_runnable_reason=ReasonCode.SCHEDULER_IDLE.value)
            return SelectionResult(
                no_runnable_reason=ReasonCode.SCHEDULER_IDLE.value)

        skipped: Dict[str, str] = {}
        runnable: List[QueueItem] = []

        # Build dependency lookup.
        item_map = {i.item_id: i for i in items}

        for item in queued:
            if item.status == QueueItemStatus.CANCELLED.value:
                skipped[item.item_id] = ReasonCode.CANCELLED_BY_USER.value
                continue

            # Check not_before (deferred scheduling).
            if item.not_before and item.not_before > self.now_fn():
                skipped[item.item_id] = ReasonCode.NOT_BEFORE_PENDING.value
                continue

            # Check dependencies.
            dep_reason = _check_dependencies(item, item_map)
            if dep_reason:
                skipped[item.item_id] = dep_reason
                continue

            # Check project lease.
            lease = self._check_lease(item.project_id)
            if lease and not lease.is_expired(self.now_fn):
                skipped[item.item_id] = ReasonCode.PROJECT_LOCKED.value
                continue

            # Check source revision.
            if repo_root:
                try:
                    from ..workspace.worktree import resolve_revision
                    resolve_revision(Path(repo_root), item.source_revision)
                except Exception:
                    skipped[item.item_id] = \
                        ReasonCode.SOURCE_REVISION_INVALID.value
                    continue

            # Check provider route.
            if budget_store and profile:
                route_reason = _check_provider_route(
                    item, budget_store, profile)
                if route_reason:
                    skipped[item.item_id] = route_reason
                    continue

            runnable.append(item)

        if not runnable:
            return SelectionResult(
                skipped_reasons=skipped,
                no_runnable_reason=ReasonCode.SCHEDULER_IDLE.value)

        # Sort: highest priority first, then oldest created_at, then lexical.
        runnable.sort(key=lambda i: (
            -i.priority, i.created_at, i.item_id))

        selected = runnable[0]
        skipped[selected.item_id] = ReasonCode.READY.value
        return SelectionResult(
            selected_item=selected,
            skipped_reasons=skipped)

    # -- Item execution ---------------------------------------------------- #

    def execute_item(self, queue_id: str, item: QueueItem,
                     profile: Any, task_store: Any,
                     budget_store: Any, worktree_parent: Path,
                     reviewer_executable: Optional[str] = None,
                     fixer_executable: Optional[str] = None,
                     codex_available: bool = False,
                     codex_verified: bool = False,
                     opencode_available: bool = False,
                     opencode_verified: bool = False,
                     run_id: str = "",
                     ) -> Tuple[QueueItem, Optional[dict]]:
        """Execute a queue item. Returns (updated_item, execution_result).

        Delegates to existing task-run and convergence primitives.
        """
        run_id = run_id or uuid.uuid4().hex[:12]
        now = self.now_fn()

        # Mark RUNNING.
        item = self.store.update_item(
            queue_id, item.item_id,
            status=QueueItemStatus.RUNNING.value,
            started_at=now,
            last_error=None,
        )

        # Acquire project lease.
        try:
            lease = self._acquire_lease(
                queue_id, item.item_id, item.project_id, run_id)
        except ValueError as exc:
            item = self.store.update_item(
                queue_id, item.item_id,
                status=QueueItemStatus.BLOCKED.value,
                finished_at=self.now_fn(),
                last_error=str(exc),
            )
            return item, {"error": str(exc)}

        execution = {
            "queue_id": queue_id, "item_id": item.item_id,
            "run_id": run_id, "started_at": now,
        }

        try:
            # Create the canonical JOSS task.
            request_text = Path(item.request_file).read_text(
                encoding="utf-8")
            meta = task_store.create(
                item.project_id, item.title, request_text,
                done_criteria="\n".join(item.done_criteria),
                size_class=item.size.upper(),
            )

            # Link task_id to queue item.
            item = self.store.update_item(
                queue_id, item.item_id, task_id=meta.task_id)

            # Run worktree creation + provider dispatch + validation
            # via the existing task run pipeline.
            exec_result = _run_task_pipeline(
                meta=meta, profile=profile,
                task_store=task_store,
                budget_store=budget_store,
                worktree_parent=worktree_parent,
                source_revision=item.source_revision,
                engine=item.implementation_engine,
                item=item,
            )

            if not exec_result.get("ok"):
                # Task run failed.
                task_store.transition(
                    meta.project_id, meta.task_id,
                    "FAILED", reason=exec_result.get("error", "task run failed"))
                item = self.store.update_item(
                    queue_id, item.item_id,
                    status=QueueItemStatus.FAILED.value,
                    finished_at=self.now_fn(),
                    last_error=ReasonCode.TASK_RUN_FAILED.value,
                )
                execution["result"] = "FAILED"
                execution["error"] = exec_result.get("error", "")
                return item, execution

            # Reload meta — pipeline changed the task state in the store.
            meta = task_store.load(meta.project_id, meta.task_id)

            # Task reached AWAITING_APPROVAL. Run convergence.
            from .convergence import (
                ConvergenceConfig, run_convergence)
            wt_path = Path(
                task_store.artifact_path(
                    meta.project_id, meta.task_id, "worktree.json")
            ).parent  # worktree path from metadata
            # Load actual worktree path.
            wt_meta_path = task_store.artifact_path(
                meta.project_id, meta.task_id, "worktree.json")
            if wt_meta_path.is_file():
                wt_meta = json.loads(
                    wt_meta_path.read_text(encoding="utf-8"))
                wt_path = Path(wt_meta["worktree_path"])

            conv_config = ConvergenceConfig(
                max_fixes=item.max_fixes,
                review_engine=item.review_engine,
                fix_engine=item.fix_engine,
                review_executable=reviewer_executable,
                fix_executable=fixer_executable,
                now_fn=self.now_fn,
            )

            conv_result = run_convergence(
                meta, profile, task_store, budget_store,
                conv_config, wt_path,
                codex_available=codex_available,
                codex_verified=codex_verified,
                opencode_available=opencode_available,
                opencode_verified=opencode_verified,
            )

            execution["convergence"] = conv_result.to_dict()

            if conv_result.ok:
                item = self.store.update_item(
                    queue_id, item.item_id,
                    status=QueueItemStatus.AWAITING_APPROVAL.value,
                    finished_at=self.now_fn(),
                    last_error=ReasonCode.AWAITING_HUMAN_APPROVAL.value,
                )
                execution["result"] = "AWAITING_APPROVAL"
            else:
                item = self.store.update_item(
                    queue_id, item.item_id,
                    status=QueueItemStatus.FAILED.value,
                    finished_at=self.now_fn(),
                    last_error=(ReasonCode.CONVERGENCE_FAILED.value
                                + ": " + (conv_result.error or conv_result.reason_code)),
                )
                execution["result"] = "FAILED"

        except Exception as exc:
            execution["result"] = "FAILED"
            execution["error"] = str(exc)
            item = self.store.update_item(
                queue_id, item.item_id,
                status=QueueItemStatus.FAILED.value,
                finished_at=self.now_fn(),
                last_error=str(exc),
            )
        finally:
            self._release_lease(item.project_id)
            # Persist execution artifact.
            run_dir = self.store._run_dir(queue_id, run_id)
            atomic_write_json(
                run_dir / "execution.json", execution)

        return item, execution

    # -- Crash recovery ---------------------------------------------------- #

    def reconcile(self, queue_id: str,
                  task_store: Any) -> List[dict]:
        """Reconcile RUNNING items against task evidence after crash."""
        items = self.store.load_queue(queue_id)
        results = []
        for item in items:
            if item.status != QueueItemStatus.RUNNING.value:
                continue
            result = self._reconcile_one(queue_id, item, task_store)
            results.append(result)
        return results

    def _reconcile_one(self, queue_id: str, item: QueueItem,
                       task_store: Any) -> dict:
        """Reconcile a single RUNNING item."""
        if not item.task_id:
            # No task was created — ambiguous.
            self.store.update_item(
                queue_id, item.item_id,
                status=QueueItemStatus.FAILED.value,
                finished_at=self.now_fn(),
                last_error=ReasonCode.AMBIGUOUS_RECOVERY_STATE.value,
            )
            return {"item_id": item.item_id,
                    "action": "FAILED",
                    "reason": ReasonCode.AMBIGUOUS_RECOVERY_STATE.value}

        try:
            meta = task_store.load(item.project_id, item.task_id)
        except KeyError:
            self.store.update_item(
                queue_id, item.item_id,
                status=QueueItemStatus.FAILED.value,
                finished_at=self.now_fn(),
                last_error=ReasonCode.AMBIGUOUS_RECOVERY_STATE.value,
            )
            return {"item_id": item.item_id,
                    "action": "FAILED",
                    "reason": ReasonCode.AMBIGUOUS_RECOVERY_STATE.value}

        # Check lease state.
        lease = self._check_lease(item.project_id)

        if meta.state == "AWAITING_APPROVAL":
            self.store.update_item(
                queue_id, item.item_id,
                status=QueueItemStatus.AWAITING_APPROVAL.value,
                finished_at=self.now_fn(),
                last_error="reconciled: task reached AWAITING_APPROVAL",
            )
            if lease:
                self._release_lease(item.project_id)
            return {"item_id": item.item_id,
                    "action": "AWAITING_APPROVAL",
                    "reason": "task state reconciled"}

        if meta.state == "FAILED":
            self.store.update_item(
                queue_id, item.item_id,
                status=QueueItemStatus.FAILED.value,
                finished_at=self.now_fn(),
                last_error="reconciled: task reached FAILED",
            )
            if lease:
                self._release_lease(item.project_id)
            return {"item_id": item.item_id,
                    "action": "FAILED",
                    "reason": "task state reconciled"}

        # Task in intermediate state + expired lease.
        if lease and lease.is_expired(self.now_fn):
            self.store.update_item(
                queue_id, item.item_id,
                status=QueueItemStatus.FAILED.value,
                finished_at=self.now_fn(),
                last_error=ReasonCode.STALE_LEASE_RECOVERED.value,
            )
            self._release_lease(item.project_id)
            self.store._append_event(queue_id, {
                "event": "reconcile_stale",
                "item_id": item.item_id,
                "task_state": meta.state,
                "reason": ReasonCode.STALE_LEASE_RECOVERED.value,
            })
            return {"item_id": item.item_id,
                    "action": "FAILED",
                    "reason": ReasonCode.STALE_LEASE_RECOVERED.value}

        # Active lease + task in intermediate state — ambiguous.
        self.store.update_item(
            queue_id, item.item_id,
            status=QueueItemStatus.FAILED.value,
            finished_at=self.now_fn(),
            last_error=ReasonCode.AMBIGUOUS_RECOVERY_STATE.value,
        )
        return {"item_id": item.item_id,
                "action": "FAILED",
                "reason": ReasonCode.AMBIGUOUS_RECOVERY_STATE.value}

    # -- Scheduler runs ---------------------------------------------------- #

    def run_next(self, queue_id: str, profile: Any, task_store: Any,
                 budget_store: Any, worktree_parent: Path,
                 reviewer_executable: Optional[str] = None,
                 fixer_executable: Optional[str] = None,
                 codex_available: bool = False,
                 codex_verified: bool = False,
                 opencode_available: bool = False,
                 opencode_verified: bool = False,
                 run_id: str = "",
                 ) -> SchedulerRun:
        """Run a single item from the queue."""
        run_id = run_id or uuid.uuid4().hex[:12]
        now = self.now_fn()
        run = SchedulerRun(
            run_id=run_id, queue_id=queue_id,
            started_at=now, max_tasks=1, max_minutes=0)

        # Reconcile any stale RUNNING items first.
        self.reconcile(queue_id, task_store)

        # Select next item.
        sel = self.select_next(
            queue_id, budget_store=budget_store, profile=profile)
        if sel.no_runnable_reason:
            run.finished_at = self.now_fn()
            run.stop_reason = sel.no_runnable_reason
            self._persist_run(queue_id, run)
            return run

        item = sel.selected_item
        updated, execution = self.execute_item(
            queue_id, item, profile, task_store, budget_store,
            worktree_parent, reviewer_executable, fixer_executable,
            codex_available, codex_verified,
            opencode_available, opencode_verified,
            run_id=run_id,
        )

        if updated.status == QueueItemStatus.AWAITING_APPROVAL.value:
            run.tasks_completed = 1
        else:
            run.tasks_failed = 1

        run.finished_at = self.now_fn()
        run.stop_reason = execution.get("result", "UNKNOWN")
        self._persist_run(queue_id, run)
        return run

    def run_until_idle(self, queue_id: str, profile: Any,
                       task_store: Any, budget_store: Any,
                       worktree_parent: Path,
                       max_tasks: int = 10, max_minutes: int = 240,
                       poll_seconds: int = 60,
                       reviewer_executable: Optional[str] = None,
                       fixer_executable: Optional[str] = None,
                       codex_available: bool = False,
                       codex_verified: bool = False,
                       opencode_available: bool = False,
                       opencode_verified: bool = False,
                       ) -> SchedulerRun:
        """Run items until idle or bounds reached."""
        run_id = uuid.uuid4().hex[:12]
        now = self.now_fn()
        run = SchedulerRun(
            run_id=run_id, queue_id=queue_id,
            started_at=now,
            max_tasks=min(max_tasks, 100),  # Safe cap.
            max_minutes=min(max_minutes, 1440),  # Safe cap.
        )

        started_ts = _parse_iso(now)
        tasks_done = 0

        while tasks_done < run.max_tasks:
            # Check max_minutes.
            elapsed = (_parse_iso(self.now_fn()) - started_ts).total_seconds()
            if elapsed >= run.max_minutes * 60:
                run.stop_reason = ReasonCode.SCHEDULER_LIMIT_REACHED.value
                break

            sub = self.run_next(
                queue_id, profile, task_store, budget_store,
                worktree_parent, reviewer_executable, fixer_executable,
                codex_available, codex_verified,
                opencode_available, opencode_verified,
                run_id=run_id,
            )
            run.tasks_completed += sub.tasks_completed
            run.tasks_failed += sub.tasks_failed
            tasks_done += (sub.tasks_completed + sub.tasks_failed)

            if sub.stop_reason == ReasonCode.SCHEDULER_IDLE.value:
                run.stop_reason = ReasonCode.SCHEDULER_IDLE.value
                break
            if sub.stop_reason == ReasonCode.SCHEDULER_LIMIT_REACHED.value:
                run.stop_reason = ReasonCode.SCHEDULER_LIMIT_REACHED.value
                break

            # Poll interval.
            if tasks_done < run.max_tasks:
                self.sleep_fn(poll_seconds)

        if not run.stop_reason:
            run.stop_reason = ReasonCode.SCHEDULER_LIMIT_REACHED.value

        run.finished_at = self.now_fn()
        self._persist_run(queue_id, run)
        return run

    def _persist_run(self, queue_id: str, run: SchedulerRun) -> None:
        """Persist a scheduler run."""
        run_dir = self.store._run_dir(queue_id, run.run_id)
        atomic_write_json(run_dir / "scheduler_run.json", run.to_dict())
        append_line(
            self.store._scheduler_runs_path(),
            json.dumps(run.to_dict(), sort_keys=True))

    def get_status(self, queue_id: str,
                   budget_store: Any = None) -> dict:
        """Report scheduler status for a queue."""
        items = self.store.load_queue(queue_id)
        by_status = {}
        for item in items:
            by_status.setdefault(item.status, []).append(item.item_id)

        # Active leases.
        leases = []
        if self.store.leases_dir.is_dir():
            for lp in self.store.leases_dir.iterdir():
                if lp.suffix == ".json" and lp.is_file():
                    try:
                        lease = ProjectLease.from_dict(
                            json.loads(lp.read_text(encoding="utf-8")))
                        if not lease.is_expired(self.now_fn):
                            leases.append(lease.to_dict())
                    except (json.JSONDecodeError, OSError):
                        pass

        # Runnable items.
        sel = self.select_next(queue_id, budget_store=budget_store)
        runnable = (sel.selected_item.item_id
                    if sel.selected_item else None)
        blocked = sel.skipped_reasons

        # Budget status.
        budget_status = {}
        if budget_store:
            for provider in ("codex-subscription", "opencode-zai"):
                b = budget_store.load_budget(provider)
                if b:
                    budget_status[provider] = {
                        "remaining_percent": b.remaining_percent,
                        "status": b.status,
                        "updated_at": b.updated_at,
                    }

        return {
            "queue_id": queue_id,
            "total_items": len(items),
            "by_status": by_status,
            "runnable": runnable,
            "blocked": blocked,
            "active_leases": leases,
            "budget_status": budget_status,
        }


# ---------------------------------------------------------------------------
# Task pipeline helper — reuses existing primitives
# ---------------------------------------------------------------------------

def _run_task_pipeline(meta: Any, profile: Any, task_store: Any,
                       budget_store: Any, worktree_parent: Path,
                       source_revision: str, engine: str,
                       item: QueueItem = None) -> dict:
    """Run worktree creation + provider dispatch + validation.

    Returns {"ok": bool, "error": str, ...}.
    """
    from ..workspace.worktree import (
        create_worktree, capture_worktree_state,
        enforce_allowed_write_paths, resolve_revision)
    from ..validation.profiles import (
        load_profile_commands, commands_for_profile)
    from ..validation.executor import RestrictedExecutor

    repo_root = Path(profile.repository_root)

    try:
        # Resolve source revision.
        resolved_rev = resolve_revision(repo_root, source_revision)

        # Create isolated worktree.
        wt_meta = create_worktree(
            repo_root, worktree_parent,
            meta.task_id, meta.project_id,
            resolved_rev, meta.branch)
        wt_path = Path(wt_meta.worktree_path)

        # Store worktree metadata.
        wt_meta.created_at = now_iso()
        task_store.write_artifact_json(
            meta.project_id, meta.task_id,
            "worktree.json", wt_meta.to_dict())

        # Transition states through the proper FSM chain.
        try:
            task_store.transition(
                meta.project_id, meta.task_id,
                "PLANNED", reason="scheduler: auto-plan")
        except Exception:
            pass  # May already be past DRAFT
        try:
            task_store.transition(
                meta.project_id, meta.task_id,
                "WAITING_FOR_INPUT", reason="scheduler: auto-prepare")
        except Exception:
            pass  # May already be past PLANNED
        task_store.transition(
            meta.project_id, meta.task_id,
            "WORKSPACE_CREATING", reason="scheduler: creating worktree")
        task_store.transition(
            meta.project_id, meta.task_id,
            "WORKSPACE_READY", reason="scheduler: worktree created")
        task_store.transition(
            meta.project_id, meta.task_id,
            "DISPATCHED", reason=f"scheduler: dispatching engine={engine}")
        task_store.transition(
            meta.project_id, meta.task_id,
            "RUNNING", reason="scheduler: provider running")

        # Provider dispatch.
        # Fail-closed: non-fake engine without concrete adapter fails closed.
        if engine == "fake":
            from ..providers.fake_provider import FakeProvider
            from ..providers.base import ProviderRequest
            from ..policy.capabilities import CapabilitySet
            fake_files = {}
            if item and hasattr(item, "_fake_files"):
                fake_files = item._fake_files
            provider = FakeProvider(files_to_create=fake_files)
            req = ProviderRequest(
                role="coder",
                task_id=meta.task_id,
                project_id=meta.project_id,
                prompt=item.title if item else "",
                worktree_path=str(wt_path),
                capability_grant=CapabilitySet(frozenset({"workspace.write"})),
            )
            result = provider.invoke(req)
        else:
            # Manual or other engine — no concrete adapter available.
            # Fail-closed: never create a synthetic result with ok=True.
            return {"ok": False, "error": f"no concrete adapter for engine: {engine}"}

        if not result.ok:
            return {"ok": False, "error": getattr(result, "error", "provider dispatch failed")}

        # Capture worktree state.
        capture = capture_worktree_state(
            wt_path, provider_ok=result.ok, provider_result={"engine": engine})
        task_store.write_artifact_json(
            meta.project_id, meta.task_id,
            "changed_files.json", {"changed_files": capture.changed_files,
                                   "git_status": capture.git_status})
        if capture.patch:
            task_store.write_artifact_text(
                meta.project_id, meta.task_id,
                "patch.diff", capture.patch)

        task_store.transition(
            meta.project_id, meta.task_id,
            "CHANGES_READY", reason="scheduler: changes captured")

        # Enforce allowed paths.
        violations = enforce_allowed_write_paths(
            capture.changed_files,
            list(profile.allowed_write_paths),
            list(profile.forbidden_paths))
        if violations:
            return {"ok": False,
                    "error": f"path violations: {violations}"}

        # Deterministic validation.
        task_store.transition(
            meta.project_id, meta.task_id,
            "VALIDATING", reason="scheduler: validating")

        validation_path = repo_root / ".agent" / "validation.toml"
        commands = load_profile_commands(validation_path, profile)
        selected = commands_for_profile(commands, profile.validation_profile)
        executor = RestrictedExecutor(profile, selected)
        wt_cwd = str(wt_path.resolve())
        all_ok = True
        cmd_results = []
        for cmd_def in selected:
            argv = [cmd_def.executable] + list(cmd_def.args) + list(cmd_def.args_extra)
            r = executor.run(argv, timeout=cmd_def.timeout, cwd_override=wt_cwd)
            cmd_results.append(r)
            if r.returncode != 0:
                all_ok = False

        # Write validation.json artifact (required by convergence).
        # Record real return code and ok value for each command.
        from ..domain.models import ValidationRun
        vrun = ValidationRun(
            task_id=meta.task_id,
            ok=all_ok,
            commands=[
                {
                    "command": " ".join(c.args or []),
                    "ok": r.returncode == 0,
                    "returncode": r.returncode,
                }
                for c, r in zip(selected, cmd_results)
            ],
            violations=[str(r) for r in cmd_results if r.returncode != 0],
            started_at=now_iso(),
            finished_at=now_iso(),
        )
        task_store.write_artifact_json(
            meta.project_id, meta.task_id,
            "validation.json", vrun.to_dict())

        if not all_ok:
            task_store.transition(
                meta.project_id, meta.task_id,
                "FAILED", reason="scheduler: validation failed")
            return {"ok": False, "error": "validation failed"}

        task_store.transition(
            meta.project_id, meta.task_id,
            "VALIDATED", reason="scheduler: validated")

        # Write review_packet.md (required by convergence).
        # Use REVIEW_NOT_RUN until real independent evidence exists.
        changed_block = "\n".join(
            f"- `{p}`" for p in capture.changed_files
        ) if capture.changed_files else "_(no tracked changes)_"
        review_packet = (
            f"# Review Packet — {meta.task_id}\n\n"
            f"- **Task ID:** `{meta.task_id}`\n"
            f"- **Project:** `{meta.project_id}`\n"
            f"- **Generated:** {now_iso()}\n\n"
            f"## Changed Files\n{changed_block}\n\n"
            f"## Verdict\nREVIEW_NOT_RUN\n\n"
            "---\nEvidence only. No automatic commit, push, or merge.\n"
        )
        task_store.write_artifact_text(
            meta.project_id, meta.task_id,
            "review_packet.md", review_packet)

        task_store.transition(
            meta.project_id, meta.task_id,
            "AWAITING_APPROVAL",
            reason="scheduler: awaiting approval")

        return {"ok": True}

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _check_dependencies(item: QueueItem,
                        item_map: Dict[str, QueueItem]) -> Optional[str]:
    """Check if dependencies are satisfied. Returns reason or None."""
    for dep_id in item.dependencies:
        dep = item_map.get(dep_id)
        if dep is None:
            return ReasonCode.DEPENDENCY_UNKNOWN.value
        if dep.status == QueueItemStatus.FAILED.value:
            return ReasonCode.DEPENDENCY_FAILED.value
        if dep.status not in (QueueItemStatus.AWAITING_APPROVAL.value,
                              QueueItemStatus.CANCELLED.value):
            # Only AWAITING_APPROVAL (or CANCELLED for skip) counts.
            # Actually CANCELLED deps should block.
            pass
        if dep.status != QueueItemStatus.AWAITING_APPROVAL.value:
            return ReasonCode.DEPENDENCY_PENDING.value
    return None


def _check_provider_route(item: QueueItem, budget_store: Any,
                         profile: Any) -> Optional[str]:
    """Check if provider routing returns a usable provider."""
    # Fake and manual engines don't need real provider routing.
    if item.implementation_engine in ("fake", "manual"):
        return None
    try:
        from ..providers.router import RoutingRequest, route
        budget = budget_store.load_budget("codex-subscription")
        opencode_budget = budget_store.load_budget("opencode-zai")
        req = RoutingRequest(
            engine=item.implementation_engine,
            task_category=item.category,
            task_size=item.size,
            project_id=item.project_id,
        )
        result = route(req, budget, opencode_budget)
        if not result.selected_provider:
            if result.human_override_required:
                return ReasonCode.PROVIDER_OVERRIDE_REQUIRED.value
            return ReasonCode.PROVIDER_UNAVAILABLE.value
        if result.budget_status in ("exhausted", "unknown"):
            return ReasonCode.PROVIDER_BUDGET_LOW.value
    except Exception:
        return ReasonCode.PROVIDER_UNAVAILABLE.value
    return None


def _add_seconds(iso_ts: str, seconds: int) -> str:
    """Add seconds to an ISO timestamp."""
    try:
        dt = _parse_iso(iso_ts)
    except (ValueError, TypeError):
        dt = datetime.now(timezone.utc)
    dt = dt.replace(microsecond=0) + __import__("datetime").timedelta(seconds=seconds)
    return dt.isoformat().replace("+00:00", "Z")


def _parse_iso(iso_ts: str):
    """Parse an ISO timestamp to a datetime."""
    if not iso_ts:
        return datetime.now(timezone.utc)
    # Handle Z suffix.
    iso_ts = iso_ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _hostname() -> str:
    try:
        return __import__("socket").gethostname()
    except Exception:
        return "unknown"


def _default_sleep(seconds: int) -> None:
    """Default sleep implementation."""
    __import__("time").sleep(seconds)
