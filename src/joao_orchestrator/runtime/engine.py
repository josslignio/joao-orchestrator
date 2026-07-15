"""Task engine: the single, canonical task store.

Migrated from V1.4.0 orchestrator/state.py. Key changes for V1.4.1:
- Tasks are project-scoped under <state_root>/tasks/<project_id>/<task_id>/
- Dual-write: JSON artifact (source of truth) + SQLite index (fast lookups)
- Uses the 17-state FSM (compat aliases fold old state names in)
- Atomic writes + append-only events (unchanged behaviour)

There is exactly ONE task engine. The legacy orchestrator/ package is a thin
re-export shim over this module (see orchestrator/state.py).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List, Optional

from ..domain.events import make_event, now_iso
from ..domain.models import TaskMeta, TaskState
from ..domain.tasks import new_task_id
from ..observability.events import read_events, write_event
from ..storage.atomic import atomic_write_json, atomic_write_text
from ..storage.artifacts import task_dir, artifact_path, list_task_dirs
from ..storage.sqlite_store import SqliteStore
from .transitions import ALLOWED_TRANSITIONS, TERMINAL_STATES, can_transition


class TransitionError(ValueError):
    """Raised when a state transition is not permitted."""


class TaskStore:
    """Filesystem + SQLite task store. Single engine for all projects."""

    def __init__(self, state_root: Path, sqlite: Optional[SqliteStore] = None):
        self.state_root = Path(state_root).expanduser().resolve()
        for candidate in (self.state_root, *self.state_root.parents):
            if (candidate / ".git").exists():
                raise ValueError(
                    f"task state cannot be stored inside a Git repository: "
                    f"{self.state_root}"
                )
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite or SqliteStore(self.state_root / "registry.db")
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Path helpers
    # ------------------------------------------------------------------ #
    def _tdir(self, project_id: str, task_id: str) -> Path:
        return task_dir(self.state_root, project_id, task_id)

    def _art(self, project_id: str, task_id: str, name: str) -> Path:
        return artifact_path(self.state_root, project_id, task_id, name)

    def task_directory(self, project_id: str, task_id: str) -> Path:
        """Return the validated task directory path."""
        return self._tdir(project_id, task_id)

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def create(self, project_id: str, title: str, request: str,
               branch: Optional[str] = None,
               done_criteria: Optional[str] = None,
               size_class: Optional[str] = None) -> TaskMeta:
        task_id = new_task_id()
        ts = now_iso()
        meta = TaskMeta(
            task_id=task_id, project_id=project_id, title=title, request=request,
            state=TaskState.DRAFT.value, created_at=ts, updated_at=ts,
            branch=branch, done_criteria=done_criteria, size_class=size_class,
        )
        with self._lock:
            tdir = self._tdir(project_id, task_id)
            tdir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._art(project_id, task_id, "request.json"),
                              {"task_id": task_id, "project_id": project_id,
                               "title": title, "request": request,
                               "branch": branch})
            atomic_write_json(self._art(project_id, task_id, "state.json"),
                              meta.to_dict())
            write_event(self._art(project_id, task_id, "events.jsonl"),
                        make_event(task_id, None, TaskState.DRAFT.value,
                                   reason="task created"))
            self.db.upsert_task(task_id, project_id, title,
                                TaskState.DRAFT.value, ts, ts, branch)
        return meta

    def load(self, project_id: str, task_id: str) -> TaskMeta:
        path = self._art(project_id, task_id, "state.json")
        if not path.is_file():
            raise KeyError(f"task not found: {project_id}/{task_id}")
        with open(path, "r", encoding="utf-8") as fh:
            return TaskMeta.from_dict(json.load(fh))

    def save_meta(self, meta: TaskMeta) -> None:
        meta.updated_at = now_iso()
        with self._lock:
            atomic_write_json(
                self._art(meta.project_id, meta.task_id, "state.json"),
                meta.to_dict())
            self.db.upsert_task(meta.task_id, meta.project_id, meta.title,
                                meta.state, meta.created_at, meta.updated_at,
                                meta.branch)

    def list_tasks(self, project_id: str) -> List[TaskMeta]:
        out = []
        for d in list_task_dirs(self.state_root, project_id):
            try:
                out.append(self.load(project_id, d.name))
            except (KeyError, OSError, ValueError):
                continue
        out.sort(key=lambda m: m.task_id, reverse=True)
        return out

    # ------------------------------------------------------------------ #
    # FSM
    # ------------------------------------------------------------------ #
    def can_transition(self, project_id: str, task_id: str, to_state: str) -> bool:
        meta = self.load(project_id, task_id)
        return can_transition(meta.state, to_state)

    def transition(self, project_id: str, task_id: str, to_state: str,
                   reason: str = "") -> TaskMeta:
        with self._lock:
            meta = self.load(project_id, task_id)
            allowed = ALLOWED_TRANSITIONS.get(meta.state, set())
            if meta.state in TERMINAL_STATES:
                raise TransitionError(
                    f"task {project_id}/{task_id} is terminal ({meta.state}); "
                    f"no transitions allowed")
            if to_state not in allowed:
                raise TransitionError(
                    f"invalid transition {meta.state} -> {to_state} for "
                    f"task {project_id}/{task_id}")
            from_state = meta.state
            meta.state = to_state
            meta.updated_at = now_iso()
            atomic_write_json(
                self._art(project_id, task_id, "state.json"), meta.to_dict())
            write_event(self._art(project_id, task_id, "events.jsonl"),
                        make_event(task_id, from_state, to_state, reason=reason))
            self.db.upsert_task(task_id, project_id, meta.title, to_state,
                                meta.created_at, meta.updated_at, meta.branch)
            return meta

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def read_events(self, project_id: str, task_id: str) -> list:
        return read_events(self._art(project_id, task_id, "events.jsonl"))

    # ------------------------------------------------------------------ #
    # Artifacts
    # ------------------------------------------------------------------ #
    def write_artifact_text(self, project_id: str, task_id: str,
                            name: str, text: str) -> Path:
        path = self._art(project_id, task_id, name)
        atomic_write_text(path, text)
        return path

    def write_artifact_json(self, project_id: str, task_id: str,
                            name: str, obj: dict) -> Path:
        path = self._art(project_id, task_id, name)
        atomic_write_json(path, obj)
        return path

    def artifact_path(self, project_id: str, task_id: str, name: str) -> Path:
        return self._art(project_id, task_id, name)
