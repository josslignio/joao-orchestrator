"""V2 canonical external project state (§8).

Lives at::

    ~/.local/share/joss-orchestrator/projects/<project_id>/state/

Required files::

    project_state.json          — single source of truth (mutable)
    project_state.previous.json — last valid backup (immutable between writes)
    roadmap.json                — ordered DAG of milestones/gates (§9)
    gate_ledger.jsonl           — append-only gate outcomes (§9)
    known_good.json             — known-good tools + commands (§12)
    active_run.json             — the current run manifest (§20)
    last_valid_artifact.json    — preserved-on-failure artifact pointer

Writes are atomic (tmp + fsync + ``os.replace``) via the existing
:mod:`joao_orchestrator.storage.atomic` layer — no new write path is created.
Every write:

1. validates the new payload against the V2 schema (fail-closed);
2. copies the *current valid* file to ``.previous.json``;
3. writes the new file;
4. records the transition in an append-only events log.

No credentials are ever stored (enforced by redaction at write time).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..storage.atomic import atomic_write_json, append_line, FileLock  # reuse
from . import V2_SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# m4 closure: the state root is resolved from the JOSS_STATE_ROOT env var at
# call time (not baked in at import), so tests/CI/operator overrides can point
# it anywhere without monkeypatching a frozen import-time default. Falls back
# to the conventional ~/.local/share location. ``HOME`` is likewise resolved
# lazily so a generic library never hardcodes a specific user's home.
DEFAULT_STATE_REL = Path(".local") / "share" / "joss-orchestrator"

_STATE_ROOT_ENV = "JOSS_STATE_ROOT"


def _resolve_joss_root() -> Path:
    """Resolve the JOSS state root, honoring JOSS_STATE_ROOT at call time."""
    env = os.environ.get(_STATE_ROOT_ENV)
    if env:
        return Path(env).expanduser()
    return Path.home() / DEFAULT_STATE_REL


# Module-level handles kept for backward-compat with existing callers/tests
# that set ``state.JOSS_ROOT`` / ``state.PROJECTS_ROOT`` directly. They are
# re-resolved from the env when accessed via the helper below; direct
# assignment still overrides (last-writer-wins within the process).
JOSS_ROOT = _resolve_joss_root()
PROJECTS_ROOT = JOSS_ROOT / "projects"


def project_state_dir(project_id: str) -> Path:
    """Return the canonical external state directory for a project."""
    if not project_id or "/" in project_id or ".." in project_id:
        raise ValueError(f"invalid project_id: {project_id!r}")
    d = PROJECTS_ROOT / project_id / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


STATE_FILENAME = "project_state.json"
PREVIOUS_FILENAME = "project_state.previous.json"
ROADMAP_FILENAME = "roadmap.json"
LEDGER_FILENAME = "gate_ledger.jsonl"
KNOWN_GOOD_FILENAME = "known_good.json"
ACTIVE_RUN_FILENAME = "active_run.json"
LAST_VALID_FILENAME = "last_valid_artifact.json"
EVENTS_FILENAME = "state_events.jsonl"

# ---------------------------------------------------------------------------
# Sensitive key substrings — never persisted in state, telemetry, or logs.
# ---------------------------------------------------------------------------
_SENSITIVE_SUBSTRINGS = (
    "token", "secret", "password", "passwd", "api_key", "apikey",
    "credential", "auth", "cookie", "session",
)

# m6 closure: value-based secret patterns. Even when a value sits under a
# benign key, a credential-shaped string must be scrubbed before persistence.
# These match common bearer/API-key/ghp shapes found in real leak incidents.
import re as _re
_SECRET_VALUE_PATTERNS = (
    _re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),       # OpenAI-style
    _re.compile(r"AKIA[0-9A-Z]{16}"),             # AWS access key id
    _re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),   # GitHub tokens
    _re.compile(r"glpat-[A-Za-z0-9_\-]{15,}"),    # GitLab PAT
    _re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{16,}", _re.IGNORECASE),
    _re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),  # Slack tokens
)
_SECRET_VALUE_REPLACEMENT = "***REDACTED***"


def _redact_value(value: str) -> str:
    """Scrub credential-shaped substrings from a string value (m6)."""
    redacted = value
    for pat in _SECRET_VALUE_PATTERNS:
        redacted = pat.sub(_SECRET_VALUE_REPLACEMENT, redacted)
    return redacted


def _redact(obj: Any) -> Any:
    """Recursively drop sensitive keys AND scrub credential-shaped values.

    Key-based: keys containing a sensitive substring are dropped entirely.
    Value-based (m6): credential-shaped substrings inside otherwise-benign
    string values are replaced with ``***REDACTED***`` so a secret that
    travelled under a benign key (e.g. embedded in ``current_objective``)
    cannot be persisted.
    """
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(s in kl for s in _SENSITIVE_SUBSTRINGS):
                continue
            out[str(k)] = _redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        return _redact_value(obj)
    return obj


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# The canonical V2 project_state.json shape (§8). Keys are validated; unknown
# keys are allowed but flagged so legacy state can migrate side-by-side.
REQUIRED_STATE_KEYS = (
    "schema_version", "project_id", "repository_path", "current_head",
    "working_tree_clean", "global_product_goal", "current_objective",
    "current_milestone_id", "current_gate_id", "current_branch",
    "open_pr", "last_merged_pr", "passed_gates", "blocked_gates",
    "contradictory_evidence", "known_good_commands", "known_good_tool_paths",
    "runtime_artifact_paths", "current_blockers", "next_single_action",
    "acceptance_criteria", "last_visible_artifact", "last_successful_run",
    "retry_count", "repair_loop_count", "confidence", "updated_at",
)


def empty_project_state(project_id: str, repository_path: str = "") -> dict[str, Any]:
    """Return a valid empty V2 project state for ``project_id``."""
    now = _utcnow()
    return {
        "schema_version": V2_SCHEMA_VERSION,
        "project_id": project_id,
        "repository_path": repository_path,
        "repository_remote": "",
        "current_head": "",
        "origin_head": "",
        "working_tree_clean": True,
        "global_product_goal": "",
        "current_objective": "",
        "current_milestone_id": "",
        "current_gate_id": "",
        "current_branch": "",
        "open_pr": {"number": 0, "state": "NONE", "url": ""},
        "last_merged_pr": {"number": 0, "merge_commit": ""},
        "passed_gates": [],
        "blocked_gates": [],
        "contradictory_evidence": [],
        "known_good_commands": [],
        "known_good_tool_paths": {},
        "runtime_artifact_paths": [],
        "current_blockers": [],
        "next_single_action": "",
        "acceptance_criteria": [],
        "last_visible_artifact": {},
        "last_successful_run": {},
        "retry_count": 0,
        "repair_loop_count": 0,
        "time_budget_seconds": 0,
        "model_budget": {},
        "confidence": "LOW",
        "updated_at": now,
    }


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class StateValidationError(ValueError):
    """Raised when a state payload fails V2 schema validation (fail-closed)."""


def validate_project_state(state: Mapping[str, Any]) -> None:
    """Validate ``state`` against the V2 schema; raise on any violation."""
    if not isinstance(state, Mapping):
        raise StateValidationError("project_state must be a JSON object")
    missing = [k for k in REQUIRED_STATE_KEYS if k not in state]
    if missing:
        raise StateValidationError(f"missing required keys: {missing}")
    if state["schema_version"] != V2_SCHEMA_VERSION:
        raise StateValidationError(
            f"schema_version must be {V2_SCHEMA_VERSION!r}, got {state['schema_version']!r}"
        )
    # next_single_action must be exactly one non-empty string (§8).
    nsa = state.get("next_single_action")
    if not isinstance(nsa, str) or not nsa.strip():
        raise StateValidationError(
            "next_single_action must be exactly one non-empty string"
        )
    # Lists must be lists.
    for k in ("passed_gates", "blocked_gates", "contradictory_evidence",
              "current_blockers", "acceptance_criteria"):
        if not isinstance(state[k], list):
            raise StateValidationError(f"{k} must be a list")
    # confidence bounded vocabulary.
    if state["confidence"] not in {"LOW", "MEDIUM", "HIGH"}:
        raise StateValidationError("confidence must be LOW|MEDIUM|HIGH")
    # No sensitive keys.
    for k in state:
        if any(s in str(k).lower() for s in _SENSITIVE_SUBSTRINGS):
            raise StateValidationError(f"refusing to persist sensitive key: {k!r}")


# ---------------------------------------------------------------------------
# Atomic state store
# ---------------------------------------------------------------------------

class ProjectStateStore:
    """Atomic, schema-validated, previous-backup-preserving project state.

    M3 hardening: writes are serialized across processes by a stdlib
    :class:`~joao_orchestrator.storage.atomic.FileLock` (macOS/POSIX ``fcntl``
    advisory lock) with a bounded acquire timeout; both the current and the
    previous (backup) files are written via unique-temp + fsync + ``os.replace``
    so a crash never leaves either file partial; the previous backup is
    validated before it is trusted; and :meth:`load_last_valid` provides a
    fail-closed fallback to the last valid state when the current file is
    corrupt (preserving the last-valid state instead of losing it).
    """

    DEFAULT_LOCK_TIMEOUT = 5.0

    def __init__(self, project_id: str, *, lock_timeout: float | None = None):
        self.project_id = project_id
        self.dir = project_state_dir(project_id)
        self.state_path = self.dir / STATE_FILENAME
        self.previous_path = self.dir / PREVIOUS_FILENAME
        self.events_path = self.dir / EVENTS_FILENAME
        self.lock_timeout = (self.DEFAULT_LOCK_TIMEOUT
                             if lock_timeout is None else float(lock_timeout))

    # -- locking ----------------------------------------------------------
    def _lock(self) -> FileLock:
        return FileLock(self.state_path, timeout=self.lock_timeout)

    # -- read -------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """Load and validate the current state.

        Fail-closed: a missing, unparseable, or schema-invalid file raises
        ``StateValidationError``. Use :meth:`load_or_init` for first-run /
        migration paths, or :meth:`load_last_valid` to fall back to the last
        valid backup when the current file is corrupt.
        """
        if not self.state_path.exists():
            raise StateValidationError(
                f"no project_state.json at {self.state_path}"
            )
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            raise StateValidationError(
                f"current project_state.json is corrupt: {e}") from e
        validate_project_state(data)
        return data

    def load_or_init(self, repository_path: str = "") -> dict[str, Any]:
        """Load current state, or initialize a valid empty state if absent.

        If the current file is corrupt (not merely absent), this falls back to
        the last valid backup (:meth:`load_last_valid`) before initializing, so
        a crash never silently resets state to empty.
        """
        try:
            return self.load()
        except StateValidationError:
            prior = self.load_last_valid()
            if prior is not None:
                # Re-persist the recovered last-valid state under the lock.
                self.write(prior, reason="recover_last_valid")
                return prior
            state = empty_project_state(self.project_id, repository_path)
            # An empty state has no next action yet — give it a bootstrap one
            # so the invariant (exactly one next action) holds immediately.
            state["next_single_action"] = (
                "bootstrap: run `joss_v2 preflight --project "
                f"{self.project_id}` to populate known-good state"
            )
            self.write(state, reason="bootstrap")
            return state

    def load_previous(self) -> dict[str, Any] | None:
        """Load and validate the last backup, or ``None`` if none/invalid.

        M3: the previous file is validated before being trusted; a corrupt or
        schema-invalid previous returns ``None`` rather than yielding bad data
        (fail-closed).
        """
        if not self.previous_path.exists():
            return None
        try:
            with open(self.previous_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None  # corrupt previous — do not trust
        try:
            validate_project_state(data)
        except StateValidationError:
            return None  # schema-invalid previous — do not trust
        return data

    def load_last_valid(self) -> dict[str, Any] | None:
        """Return the best available valid state, current-or-previous.

        Tries the current file first; if it is corrupt or schema-invalid,
        falls back to the validated previous backup. Returns ``None`` only if
        neither is usable. This is the fail-closed, preserve-last-valid path
        for crash recovery (M3).
        """
        try:
            return self.load()
        except StateValidationError:
            return self.load_previous()

    # -- write ------------------------------------------------------------
    def write(self, state: Mapping[str, Any], reason: str = "") -> None:
        """Validate, back-up current→previous, then atomically write ``state``.

        Serialized across processes by a bounded inter-process lock (M3). Both
        the backup (previous) and the new (current) files are written via
        unique-temp + fsync + ``os.replace``; the previous backup is only
        overwritten once the new current is in place, so the last valid state
        is always preserved on disk. No partial overwrite is ever performed.
        """
        validate_project_state(state)  # fail-closed before touching disk
        redacted = _redact(state)
        with self._lock():
            self._write_locked(redacted, reason=reason)

    def _write_locked(self, redacted: Mapping[str, Any],
                      *, reason: str = "") -> None:
        # 1. Promote the CURRENT valid file to PREVIOUS atomically. We write
        #    the backup to a unique temp then os.replace, so a crash mid-backup
        #    cannot leave a corrupt previous. Only a valid current is promoted.
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    cur = json.load(fh)
                validate_project_state(cur)
            except (StateValidationError, json.JSONDecodeError, OSError):
                cur = None  # current is corrupt/unusable — do not back it up
            if cur is not None:
                atomic_write_json(self.previous_path, cur)

        # 2. Atomic write of the new current state (unique temp + fsync +
        #    os.replace). A crash before os.replace leaves the OLD current
        #    intact and the new temp orphaned (cleaned up by the writer).
        atomic_write_json(self.state_path, redacted)

        # 3. Append-only transition event (events.jsonl) — single primitive.
        self._append_event({
            "event": "state_written",
            "reason": reason,
            "next_single_action": redacted.get("next_single_action"),
            "confidence": redacted.get("confidence"),
            "passed_gates_count": len(redacted.get("passed_gates", [])),
            "timestamp": _utcnow(),
        })

    # -- helpers ----------------------------------------------------------
    def _append_event(self, record: Mapping[str, Any]) -> None:
        # m2 closure: route through the single atomic append primitive rather
        # than re-implementing open/flush/fsync here.
        append_line(self.events_path, json.dumps(record, sort_keys=True))

    def update(self, **changes: Any) -> dict[str, Any]:
        """Load, apply ``changes``, validate, and write atomically.

        The whole read-modify-write is serialized under the inter-process lock
        so two concurrent ``update`` calls cannot lose each other's changes
        (lost-update prevention, M3).
        """
        with self._lock():
            try:
                state = self.load()
            except StateValidationError:
                state = self.load_previous()
                if state is None:
                    raise
            state.update(changes)
            state["updated_at"] = _utcnow()
            validate_project_state(state)
            self._write_locked(_redact(state),
                               reason=changes.get("current_objective") or "update")
            return state


# ---------------------------------------------------------------------------
# Migration from legacy state (side-by-side, non-destructive, §29)
# ---------------------------------------------------------------------------

@dataclass
class MigrationReport:
    """Result of a legacy→V2 migration. Legacy state is NEVER deleted."""
    migrated: bool
    project_id: str
    keys_carried_over: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    v2_state_path: str = ""
    legacy_state_path: str = ""


def migrate_legacy_state(project_id: str, legacy_path: Path) -> MigrationReport:
    """Carry a legacy project state forward into V2 *beside* it.

    The legacy file is left untouched. Only safe, known keys are carried over;
    everything else is logged in ``notes``. This is a read-then-write; it never
    mutates or deletes the legacy file (§29: preserve legacy, no deletion).
    """
    report = MigrationReport(migrated=False, project_id=project_id,
                             legacy_state_path=str(legacy_path))
    if not legacy_path.exists():
        report.notes.append("legacy state file not found; initialized empty")
        store = ProjectStateStore(project_id)
        store.load_or_init()
        report.v2_state_path = str(store.state_path)
        report.migrated = True
        return report

    with open(legacy_path, encoding="utf-8") as fh:
        legacy = json.load(fh)

    state = empty_project_state(project_id, legacy.get("repository_path", ""))
    carried: list[str] = []
    # Carry over only safe, schema-compatible keys.
    carry_keys = (
        "repository_remote", "current_head", "origin_head",
        "global_product_goal", "current_objective", "current_branch",
        "open_pr", "last_merged_pr", "passed_gates", "blocked_gates",
        "current_blockers", "acceptance_criteria", "last_visible_artifact",
        "last_successful_run", "confidence",
    )
    for k in carry_keys:
        if k in legacy and legacy[k] not in (None, "", [], {}):
            state[k] = legacy[k]
            carried.append(k)
    state["known_good_tool_paths"] = legacy.get("known_good_tool_paths", {})
    if state["known_good_tool_paths"]:
        carried.append("known_good_tool_paths")

    # Seed a single next action if none carried.
    if not state["next_single_action"]:
        state["next_single_action"] = (
            "post-migration: run `joss_v2 preflight --project "
            f"{project_id}` to verify known-good state"
        )

    store = ProjectStateStore(project_id)
    store.write(state, reason="migrate_legacy_state")
    report.migrated = True
    report.keys_carried_over = carried
    report.v2_state_path = str(store.state_path)
    report.notes.append(
        "legacy state preserved untouched; V2 state written beside it")
    return report


__all__ = [
    "V2_SCHEMA_VERSION", "JOSS_ROOT", "PROJECTS_ROOT", "project_state_dir",
    "REQUIRED_STATE_KEYS", "StateValidationError", "ProjectStateStore",
    "empty_project_state", "validate_project_state", "migrate_legacy_state",
    "MigrationReport",
]
