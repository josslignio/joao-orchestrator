"""Project memory layer (V1.4.1 — JOSS-451).

Provider-independent local memory stored under ``<runtime-root>/memory/``::

    <state_root>/memory/
        global/
            records.jsonl      append-only audit log (authoritative history)
            snapshot.json      materialized current records (atomic rewrite)
        projects/
            <project_id>/
                records.jsonl
                snapshot.json

Design invariants (see repository AGENTS/invariants):

* JSONL is the append-only, authoritative audit log. It is NEVER rewritten or
  destructively migrated. Every ``put``/``supersede`` appends exactly one line.
* ``snapshot.json`` is a derived, materialized view of the current record set,
  rewritten atomically (write-tmp + fsync + os.replace) on every mutation. It
  is the read path for active-record reads.
* SQLite is intentionally NOT used here — JSON/JSONL artifacts remain
  authoritative. (SQLite may only index; this layer needs no index.)
* Runtime state lives outside managed repositories. The runtime root is
  validated on construction (no ancestor may contain ``.git``).
* Records are content-addressed: ``record_id`` and ``sha256`` are derived
  deterministically from the semantic payload (project_id, record_type, title,
  content, tags, source_label). Timestamps, the ``active`` flag, ``supersedes``,
  and the hash itself are excluded so the address is reproducible.
* Supersession appends a new active record and marks the prior record inactive
  in the snapshot. The old audit line is untouched — no destructive migration.
* Tampered or malformed records fail closed (``ProvenanceError``).

Standard library only. No network, no subprocess, no ``shell=True``, no package
installation, no credentials persistence.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..domain.identifiers import validate_identifier
from .atomic import append_line, atomic_write_json

MEMORY_SCHEMA_VERSION = 1
RECORDS_LOG = "records.jsonl"
SNAPSHOT_FILE = "snapshot.json"
RECORD_ID_PREFIX = "mem-"


class MemoryError(ValueError):
    """Base class for memory-layer usage errors."""


class ProvenanceError(MemoryError):
    """Raised when a record fails provenance validation (tampered/invalid)."""


# --------------------------------------------------------------------------- #
# Runtime-root validation + path helpers
# --------------------------------------------------------------------------- #

def _assert_outside_managed_repo(path: Path) -> None:
    """Reject any runtime path that is inside a managed Git repository.

    Mirrors the invariant enforced by TaskStore: orchestrator state must live
    outside the repositories it manages. ``.git`` presence is detected without
    invoking Git.
    """
    resolved = Path(path).expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            raise ValueError(
                f"memory state must be stored outside managed repositories: "
                f"{resolved} is inside {candidate}"
            )


def memory_root(state_root: Path) -> Path:
    return Path(state_root).expanduser().resolve() / "memory"


def global_memory_dir(state_root: Path) -> Path:
    return memory_root(state_root) / "global"


def project_memory_dir(state_root: Path, project_id: str) -> Path:
    validate_identifier(project_id, "project_id")
    return memory_root(state_root) / "projects" / project_id


# --------------------------------------------------------------------------- #
# Deterministic hashing + record construction
# --------------------------------------------------------------------------- #

def _canonical_payload(project_id: str, record_type: str, title: str,
                       content: str, tags: Iterable[str],
                       source_label: str) -> bytes:
    """Deterministic JSON-UTF8 encoding of a record's semantic identity.

    Timestamps, the ``active`` flag, ``supersedes``, ``record_id`` and
    ``sha256`` are deliberately excluded so the address is reproducible across
    machines and runs. ``ensure_ascii=False`` keeps content legible while
    ``sort_keys`` + compact separators make the encoding canonical.
    """
    payload = {
        "project_id": project_id or "",
        "record_type": record_type,
        "title": title,
        "content": content,
        "tags": sorted(set(str(t) for t in tags)),
        "source_label": source_label,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")


def compute_record_hash(project_id: str, record_type: str, title: str,
                        content: str, tags: Iterable[str],
                        source_label: str) -> str:
    """SHA-256 hex digest over the canonical semantic payload."""
    return hashlib.sha256(_canonical_payload(
        project_id, record_type, title, content, tags, source_label)).hexdigest()


def derive_record_id(project_id: str, record_type: str, title: str,
                     content: str, tags: Iterable[str],
                     source_label: str) -> str:
    """Deterministic, content-addressed record id (``mem-`` + 16 hex chars)."""
    digest = compute_record_hash(
        project_id, record_type, title, content, tags, source_label)
    return RECORD_ID_PREFIX + digest[:16]


def _build_record(project_id: str, record_type: str, title: str,
                  content: str, tags: Iterable[str], source_label: str,
                  supersedes: Optional[str] = None, active: bool = True,
                  created_at: Optional[str] = None,
                  updated_at: Optional[str] = None) -> dict:
    tags_list = sorted(set(str(t) for t in tags))
    digest = compute_record_hash(
        project_id, record_type, title, content, tags_list, source_label)
    rid = RECORD_ID_PREFIX + digest[:16]
    ts = created_at or _now()
    return {
        "record_id": rid,
        "project_id": project_id or "",
        "record_type": record_type,
        "title": title,
        "content": content,
        "tags": tags_list,
        "source_label": source_label,
        "created_at": ts,
        "updated_at": updated_at or ts,
        "supersedes": supersedes,
        "active": bool(active),
        "sha256": digest,
        "schema_version": MEMORY_SCHEMA_VERSION,
    }


def _now() -> str:
    # Local import keeps the module dependency surface minimal and avoids a
    # circular import with domain.events at module load on stripped checkouts.
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def verify_record(record: dict) -> None:
    """Fail closed if the record's id/hash do not match its semantic payload."""
    if not isinstance(record, dict):
        raise ProvenanceError("record is not a JSON object")
    required = ("record_id", "project_id", "record_type", "title", "content",
                "tags", "source_label", "created_at", "updated_at",
                "supersedes", "active", "sha256", "schema_version")
    missing = [k for k in required if k not in record]
    if missing:
        raise ProvenanceError(f"record missing required fields: {missing}")
    pid = record["project_id"]
    rtype = record["record_type"]
    title = record["title"]
    content = record["content"]
    tags = record.get("tags") or []
    source = record["source_label"]
    expected_id = derive_record_id(pid, rtype, title, content, tags, source)
    if record["record_id"] != expected_id:
        raise ProvenanceError(
            f"record_id mismatch for stored={record['record_id']!r}: "
            f"expected={expected_id!r}")
    expected_hash = compute_record_hash(pid, rtype, title, content, tags, source)
    if record["sha256"] != expected_hash:
        raise ProvenanceError(
            f"sha256 mismatch for {record['record_id']}: "
            f"stored={record['sha256']} expected={expected_hash}")


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

class MemoryStore:
    """Append-only, content-addressed project memory.

    ``project_id`` of ``None`` or ``""`` addresses the GLOBAL memory scope;
    any other value addresses a per-project directory after identifier
    validation. All writes go through a process-local lock; the on-disk
    artifacts are themselves atomic (snapshot) and append-only (audit log).
    """

    def __init__(self, state_root: Path):
        self.state_root = Path(state_root).expanduser().resolve()
        _assert_outside_managed_repo(self.state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- path helpers ---------------------------------------------------- #
    def _dir_for(self, project_id: Optional[str]) -> Path:
        if not project_id:
            return global_memory_dir(self.state_root)
        return project_memory_dir(self.state_root, project_id)

    def _log_path(self, project_id: Optional[str]) -> Path:
        return self._dir_for(project_id) / RECORDS_LOG

    def _snapshot_path(self, project_id: Optional[str]) -> Path:
        return self._dir_for(project_id) / SNAPSHOT_FILE

    # -- snapshot I/O ---------------------------------------------------- #
    def _read_snapshot(self, project_id: Optional[str]) -> Dict[str, dict]:
        path = self._snapshot_path(project_id)
        if not path.is_file():
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        records = data.get("records", {}) if isinstance(data, dict) else {}
        out: Dict[str, dict] = {}
        for rid, rec in records.items():
            if not isinstance(rec, dict):
                raise ProvenanceError(f"snapshot entry is not an object: {rid}")
            if rid != rec.get("record_id"):
                raise ProvenanceError(
                    f"snapshot key does not match record_id: {rid}")
            verify_record(rec)  # fail closed on tampered records
            out[rid] = rec
        return out

    def _write_snapshot(self, project_id: Optional[str],
                        records: Dict[str, dict]) -> None:
        payload = {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "project_id": project_id or "",
            "records": records,
        }
        atomic_write_json(self._snapshot_path(project_id), payload)

    def _append_audit(self, project_id: Optional[str], record: dict) -> None:
        append_line(self._log_path(project_id),
                    json.dumps(record, sort_keys=True, ensure_ascii=False))

    # -- public API ------------------------------------------------------ #
    def put(self, project_id: Optional[str], record_type: str, title: str,
            content: str, tags: Iterable[str] = (), source_label: str = "",
            supersedes: Optional[str] = None) -> dict:
        """Append a record and refresh the materialized snapshot.

        When ``supersedes`` names an existing record, that record is marked
        inactive in the snapshot (its audit line is never edited) and the new
        record becomes active, preserving lineage via ``created_at``.
        """
        if not isinstance(record_type, str) or not record_type:
            raise MemoryError("record_type is required and must be a string")
        if not isinstance(title, str) or not isinstance(content, str):
            raise MemoryError("title and content must be strings")
        if not isinstance(source_label, str):
            raise MemoryError("source_label must be a string")
        pid = project_id or ""
        with self._lock:
            records = self._read_snapshot(pid)
            if supersedes is not None:
                if supersedes not in records:
                    raise MemoryError(
                        f"cannot supersede unknown record: {supersedes}")
                prior = dict(records[supersedes])
            else:
                prior = None
            record = _build_record(
                pid, record_type, title, content, tags, source_label,
                supersedes=supersedes)
            if prior is not None:
                if record["record_id"] == supersedes:
                    raise MemoryError(
                        "supersede payload is identical to the target record")
                # Carry the lineage timestamp; hash excludes timestamps so
                # the new id still reflects the new content.
                record["created_at"] = prior["created_at"]
                prior["active"] = False
                prior["updated_at"] = record["updated_at"]
                records[supersedes] = prior
            records[record["record_id"]] = record
            # Append-only audit log: one authoritative line per mutation.
            self._append_audit(pid, record)
            self._write_snapshot(pid, records)
            return record

    def supersede(self, project_id: Optional[str], old_record_id: str, *,
                  record_type: str, title: str, content: str,
                  tags: Iterable[str] = (), source_label: str = "") -> dict:
        return self.put(project_id, record_type, title, content, tags,
                        source_label, supersedes=old_record_id)

    def list_active(self, project_id: Optional[str]) -> List[dict]:
        """Return active records for a scope, ordered deterministically."""
        records = self._read_snapshot(project_id or "")
        out = [r for r in records.values() if r.get("active")]
        out.sort(key=lambda r: (r.get("created_at", ""), r["record_id"]))
        return out

    def get(self, project_id: Optional[str],
            record_id: str) -> Optional[dict]:
        return self._read_snapshot(project_id or "").get(record_id)

    def validate(self, project_id: Optional[str] = None) -> dict:
        """Cross-check the snapshot against the append-only audit log.

        Every snapshot record must pass provenance (id + sha256) AND every
        record id present in the snapshot must appear at least once in the
        audit log. Raises ``ProvenanceError`` on any inconsistency.
        """
        pid = project_id or ""
        records = self._read_snapshot(pid)  # verifies each record in place
        log_ids = set()
        log_path = self._log_path(pid)
        if log_path.is_file():
            with open(log_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ProvenanceError(
                            f"corrupt audit log line: {exc}") from exc
                    verify_record(rec)
                    log_ids.add(rec["record_id"])
        missing = sorted(set(records) - log_ids)
        if missing:
            raise ProvenanceError(
                f"snapshot records absent from audit log: {missing}")
        active = sum(1 for r in records.values() if r.get("active"))
        return {
            "ok": True,
            "project_id": pid,
            "dir": str(self._dir_for(pid)),
            "records": len(records),
            "active": active,
            "audit_record_ids": len(log_ids),
        }
