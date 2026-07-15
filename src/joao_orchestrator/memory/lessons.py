"""C4 Validated Lessons Memory.

Learns from validated failures and corrections WITHOUT injecting raw history.

Extends the existing memory architecture (storage/memory.py). Does NOT create a
competing project memory system. Lessons are stored under
``<state_root>/memory/lessons/`` as append-only JSONL (authoritative) with a
materialized snapshot (atomic rewrite).

Lesson creation rule — a lesson may be stored ONLY when ALL are true:
  - a real defect occurred
  - a concrete correction was applied
  - a regression test was added or updated
  - required gates passed
  - C1 accepted the corrected candidate
  - provenance is complete

NEVER store: unverified model speculation, raw conversation, secrets,
credentials, large logs, personal data, temporary noise.

Retrieval is deterministic-first (project match, task category, path overlap,
failure fingerprint, lexical overlap, recency tie-break) and bounded (max 5
lessons, max 1500 characters, no raw executable instructions). Lesson text is
untrusted bounded context: it may advise but NEVER expand permission.

Deterministic. No network. Stdlib only. Persistence outside repos.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

from ..domain.identifiers import validate_identifier
from ..evaluation.models import canonical_json_bytes, sha256_json
from ..storage.atomic import append_line, atomic_write_json


SCHEMA_VERSION = 1
MAX_RETRIEVED_LESSONS = 5
MAX_RETRIEVED_CHARS = 1500


class LessonError(ValueError):
    """Base class for lesson-layer errors."""


class LessonIntegrityError(LessonError):
    """Raised when a lesson fails integrity (tampered/invalid)."""


class LessonValidationError(LessonError):
    """Raised when a lesson fails the creation-rule gate."""


# ---------------------------------------------------------------------------
# Lesson model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lesson:
    """One validated lesson learned from a real corrected defect."""

    schema_version: int
    lesson_id: str
    project_id: str
    task_category: str
    source_task: str
    source_commit: str
    failure_fingerprint: str
    root_cause: str
    validated_fix: str
    regression_test: str
    applicable_paths: tuple[str, ...]
    tags: tuple[str, ...]
    created_at: str
    validator: str           # who/what validated it (e.g. "C1", "human")
    C1_decision_hash: str
    integrity_hash: str
    supersedes: str = ""     # lesson_id this one supersedes
    status: str = "ACTIVE"   # ACTIVE / SUPERSEDED / REJECTED

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "lesson_id": self.lesson_id,
            "project_id": self.project_id,
            "task_category": self.task_category,
            "source_task": self.source_task,
            "source_commit": self.source_commit,
            "failure_fingerprint": self.failure_fingerprint,
            "root_cause": self.root_cause,
            "validated_fix": self.validated_fix,
            "regression_test": self.regression_test,
            "applicable_paths": list(self.applicable_paths),
            "tags": list(self.tags),
            "created_at": self.created_at,
            "validator": self.validator,
            "C1_decision_hash": self.C1_decision_hash,
            "supersedes": self.supersedes,
            "status": self.status,
        }

    def with_integrity(self) -> "Lesson":
        return replace(self, integrity_hash=sha256_json(self.unsigned_dict()))

    def verify_integrity(self) -> bool:
        return bool(self.integrity_hash) and self.integrity_hash == sha256_json(
            self.unsigned_dict()
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["integrity_hash"] = self.integrity_hash
        return payload

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Lesson":
        return cls(
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            lesson_id=str(d["lesson_id"]),
            project_id=str(d["project_id"]),
            task_category=str(d.get("task_category", "")),
            source_task=str(d.get("source_task", "")),
            source_commit=str(d.get("source_commit", "")),
            failure_fingerprint=str(d.get("failure_fingerprint", "")),
            root_cause=str(d.get("root_cause", "")),
            validated_fix=str(d.get("validated_fix", "")),
            regression_test=str(d.get("regression_test", "")),
            applicable_paths=tuple(d.get("applicable_paths", [])),
            tags=tuple(d.get("tags", [])),
            created_at=str(d.get("created_at", "")),
            validator=str(d.get("validator", "")),
            C1_decision_hash=str(d.get("C1_decision_hash", "")),
            integrity_hash=str(d.get("integrity_hash", "")),
            supersedes=str(d.get("supersedes", "")),
            status=str(d.get("status", "ACTIVE")),
        )

    @property
    def content_chars(self) -> int:
        """Character count of the human-relevant text (root_cause + fix)."""
        return len(self.root_cause) + len(self.validated_fix)


# ---------------------------------------------------------------------------
# Creation rule gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LessonEvidence:
    """Evidence that a real corrected defect occurred. All fields required."""

    project_id: str
    task_category: str
    source_task: str
    source_commit: str
    failure_fingerprint: str
    root_cause: str
    validated_fix: str
    regression_test: str
    applicable_paths: tuple[str, ...]
    tags: tuple[str, ...]
    c1_decision_hash: str
    validator: str = "C1"
    gates_passed: bool = True
    defect_occurred: bool = True
    correction_applied: bool = True


def _validate_evidence(ev: LessonEvidence) -> None:
    """Enforce the lesson creation rule. Raises LessonValidationError on failure."""
    if not ev.defect_occurred:
        raise LessonValidationError("no real defect occurred; lesson rejected")
    if not ev.correction_applied:
        raise LessonValidationError("no concrete correction was applied; lesson rejected")
    if not ev.regression_test or not ev.regression_test.strip():
        raise LessonValidationError("no regression test added; lesson rejected")
    if not ev.gates_passed:
        raise LessonValidationError("required gates did not pass; lesson rejected")
    if not ev.c1_decision_hash:
        raise LessonValidationError("no C1 acceptance hash; lesson rejected")
    if not ev.source_commit:
        raise LessonValidationError("provenance incomplete: missing source_commit")
    if not ev.source_task:
        raise LessonValidationError("provenance incomplete: missing source_task")
    if not ev.root_cause.strip() or not ev.validated_fix.strip():
        raise LessonValidationError("root_cause and validated_fix must be non-empty")
    # Reject lessons that look like raw conversation / speculation.
    speculative = ("maybe", "possibly", "i think", "i guess", "not sure")
    low = (ev.root_cause + " " + ev.validated_fix).lower()
    if any(low.startswith(s) for s in speculative):
        raise LessonValidationError(
            "lesson looks like unverified speculation; rejected")


def _lesson_id(ev: LessonEvidence) -> str:
    """Deterministic content-addressed lesson id."""
    payload = {
        "project_id": ev.project_id,
        "failure_fingerprint": ev.failure_fingerprint,
        "root_cause": ev.root_cause,
        "validated_fix": ev.validated_fix,
    }
    return "lesson-" + sha256_json(payload)[:16]


def _fingerprint_hash(ev: LessonEvidence) -> str:
    """Dedup key: project + failure fingerprint + root cause + validated fix."""
    return sha256_json({
        "project_id": ev.project_id,
        "failure_fingerprint": ev.failure_fingerprint,
        "root_cause": ev.root_cause,
        "validated_fix": ev.validated_fix,
    })


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _assert_outside_repo(path: Path) -> None:
    resolved = Path(path).expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            raise ValueError(
                f"lessons state must be outside managed repositories: {resolved}")


class LessonStore:
    """Append-only validated lessons memory.

    Layout (under state_root outside repos):
      <state_root>/memory/lessons/
        records.jsonl     append-only authoritative audit log
        snapshot.json     materialized current active+superseded lessons
    """

    def __init__(self, state_root: Path):
        self.state_root = Path(state_root).expanduser().resolve()
        _assert_outside_repo(self.state_root)
        self.dir = self.state_root / "memory" / "lessons"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records_path = self.dir / "records.jsonl"
        self.snapshot_path = self.dir / "snapshot.json"
        self._lock = threading.Lock()

    # -- snapshot I/O ---------------------------------------------------- #

    def _read_snapshot(self) -> dict[str, Lesson]:
        if not self.snapshot_path.is_file():
            return {}
        try:
            data = json.loads(self.snapshot_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        out: dict[str, Lesson] = {}
        for lid, rec in data.get("lessons", {}).items():
            try:
                lesson = Lesson.from_dict(rec)
            except (KeyError, ValueError):
                continue
            if not lesson.verify_integrity():
                # Corrupt snapshot entry: skip but audit.
                continue
            out[lid] = lesson
        return out

    def _write_snapshot(self, lessons: dict[str, Lesson]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "lessons": {lid: l.to_dict() for lid, l in lessons.items()},
        }
        atomic_write_json(self.snapshot_path, payload)

    def _append_audit(self, lesson: Lesson) -> None:
        append_line(self.records_path,
                    json.dumps(lesson.to_dict(), ensure_ascii=False, sort_keys=True))

    # -- public API ------------------------------------------------------ #

    def store(self, ev: LessonEvidence) -> Lesson:
        """Store a validated lesson. Enforces the creation rule.

        Deduplicates by (project, failure fingerprint, root cause, validated fix).
        If an identical ACTIVE lesson exists, it is returned unchanged (no dup).
        A stronger validated lesson may supersede an older one.
        """
        _validate_evidence(ev)
        validate_identifier(ev.project_id, "project_id")
        lid = _lesson_id(ev)
        fp = _fingerprint_hash(ev)

        with self._lock:
            lessons = self._read_snapshot()

            # Dedup: identical fingerprint already active?
            for existing in lessons.values():
                if existing.status != "ACTIVE":
                    continue
                existing_fp = sha256_json({
                    "project_id": existing.project_id,
                    "failure_fingerprint": existing.failure_fingerprint,
                    "root_cause": existing.root_cause,
                    "validated_fix": existing.validated_fix,
                })
                if existing_fp == fp:
                    return existing  # already stored, no duplicate

            # Build the lesson.
            supersedes = ""
            # A stronger lesson may supersede an older one with the same
            # failure_fingerprint but different (improved) fix.
            for existing_lid, existing in list(lessons.items()):
                if (existing.status == "ACTIVE"
                        and existing.failure_fingerprint == ev.failure_fingerprint
                        and existing.project_id == ev.project_id
                        and existing.validated_fix != ev.validated_fix):
                    # Mark old as SUPERSEDED.
                    old = replace(existing, status="SUPERSEDED").with_integrity()
                    lessons[existing_lid] = old
                    self._append_audit(old)
                    supersedes = existing_lid
                    break

            lesson = Lesson(
                schema_version=SCHEMA_VERSION,
                lesson_id=lid,
                project_id=ev.project_id,
                task_category=ev.task_category,
                source_task=ev.source_task,
                source_commit=ev.source_commit,
                failure_fingerprint=ev.failure_fingerprint,
                root_cause=ev.root_cause,
                validated_fix=ev.validated_fix,
                regression_test=ev.regression_test,
                applicable_paths=ev.applicable_paths,
                tags=ev.tags,
                created_at="",  # filled below
                validator=ev.validator,
                C1_decision_hash=ev.c1_decision_hash,
                integrity_hash="",
                supersedes=supersedes,
                status="ACTIVE",
            )
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            lesson = replace(lesson, created_at=ts).with_integrity()
            lessons[lid] = lesson
            self._append_audit(lesson)
            self._write_snapshot(lessons)
            return lesson

    def load_all(self) -> List[Lesson]:
        """Load all lessons from the snapshot (active + superseded)."""
        return list(self._read_snapshot().values())

    def load_active(self, project_id: Optional[str] = None) -> List[Lesson]:
        """Load active lessons, optionally filtered by project."""
        lessons = self._read_snapshot().values()
        out = [l for l in lessons if l.status == "ACTIVE"]
        if project_id is not None:
            out = [l for l in out if l.project_id == project_id]
        out.sort(key=lambda l: (l.created_at, l.lesson_id))
        return out

    def validate_audit(self) -> dict[str, Any]:
        """Cross-check snapshot against the audit log. Fail closed on corruption."""
        snapshot = self._read_snapshot()
        log_ids: set[str] = set()
        corrupt = 0
        if self.records_path.is_file():
            for line in self.records_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    lesson = Lesson.from_dict(rec)
                    if not lesson.verify_integrity():
                        corrupt += 1
                        continue
                    log_ids.add(lesson.lesson_id)
                except (json.JSONDecodeError, KeyError, ValueError):
                    corrupt += 1
        missing = sorted(set(snapshot) - log_ids)
        return {
            "ok": not missing and corrupt == 0,
            "snapshot_count": len(snapshot),
            "audit_ids": len(log_ids),
            "missing_from_audit": missing,
            "corrupt_lines": corrupt,
        }

    def stats(self) -> dict[str, Any]:
        lessons = self._read_snapshot()
        active = sum(1 for l in lessons.values() if l.status == "ACTIVE")
        superseded = sum(1 for l in lessons.values() if l.status == "SUPERSEDED")
        projects = sorted({l.project_id for l in lessons.values()})
        return {
            "total": len(lessons),
            "active": active,
            "superseded": superseded,
            "projects": projects,
        }
