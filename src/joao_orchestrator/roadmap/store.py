"""C3 Roadmap Compiler — persistence (outside managed repos).

Layout (under a state_root outside repos):
  <state_root>/roadmaps/<project_id>/<roadmap_id>/
    roadmap.json            atomic snapshot of the roadmap
    validation.json         last validation result
    events.jsonl            append-only state-transition audit

JSON/JSONL is authoritative. SQLite is not used here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..domain.identifiers import validate_identifier
from ..storage.atomic import append_line, atomic_write_json
from .models import Roadmap, RoadmapState
from .validator import ValidationResult


def _roadmap_dir(state_root: Path, project_id: str, roadmap_id: str) -> Path:
    validate_identifier(project_id, "project_id")
    d = Path(state_root).resolve() / "roadmaps" / project_id / roadmap_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def persist_roadmap(
    state_root: Path,
    roadmap: Roadmap,
    validation: Optional[ValidationResult] = None,
) -> Path:
    """Persist a roadmap + optional validation result outside repos."""
    if not roadmap.verify_integrity():
        raise ValueError("roadmap fails integrity check; refusing to persist")
    d = _roadmap_dir(state_root, roadmap.project_id, roadmap.roadmap_id)
    path = d / "roadmap.json"
    atomic_write_json(path, roadmap.to_dict())
    if validation is not None:
        atomic_write_json(d / "validation.json", validation.to_dict())
    append_line(d / "events.jsonl", json.dumps({
        "event": "persisted",
        "state": roadmap.state,
        "roadmap_hash": roadmap.roadmap_hash,
        "integrity_ok": roadmap.verify_integrity(),
    }, ensure_ascii=False))
    return path


def load_roadmap(state_root: Path, project_id: str, roadmap_id: str) -> Optional[Roadmap]:
    """Load a roadmap from disk. Returns None if not found."""
    d = _roadmap_dir(state_root, project_id, roadmap_id)
    path = d / "roadmap.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    roadmap = Roadmap.from_dict(data)
    # Fail closed: a tampered roadmap (integrity mismatch) is rejected.
    if not roadmap.verify_integrity():
        raise ValueError(
            f"roadmap {roadmap_id} fails integrity check; refusing to load "
            f"(possible tampering)")
    return roadmap


def transition_state(
    state_root: Path,
    project_id: str,
    roadmap_id: str,
    new_state: str,
    detail: str = "",
) -> None:
    """Append a state-transition event to the roadmap's audit log."""
    d = _roadmap_dir(state_root, project_id, roadmap_id)
    append_line(d / "events.jsonl", json.dumps({
        "event": "state_transition",
        "new_state": new_state,
        "detail": detail,
    }, ensure_ascii=False))


def list_roadmaps(state_root: Path, project_id: str) -> list[dict[str, Any]]:
    """List roadmap summaries for a project."""
    base = Path(state_root).resolve() / "roadmaps" / project_id
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for rd in sorted(base.iterdir()):
        rp = rd / "roadmap.json"
        if not rp.is_file():
            continue
        try:
            data = json.loads(rp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "roadmap_id": data.get("roadmap_id", rd.name),
            "project_id": data.get("project_id", project_id),
            "objective": data.get("objective", ""),
            "state": data.get("state", "UNKNOWN"),
            "integrity_sha256": data.get("integrity_sha256", ""),
            "task_count": len(data.get("tasks", [])),
        })
    return out
