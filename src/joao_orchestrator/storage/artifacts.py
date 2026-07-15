"""Safe artifact path helpers.

Task artifacts live under:
    <state_root>/tasks/<project_id>/<task_id>/<artifact>

Every dynamic component is validated as a single path component before it is
joined to the state root.
"""

from __future__ import annotations

from pathlib import Path

from ..domain.identifiers import validate_artifact_name, validate_identifier


def project_tasks_root(state_root: Path, project_id: str) -> Path:
    project_id = validate_identifier(project_id, "project_id")
    return Path(state_root).resolve() / "tasks" / project_id


def task_dir(state_root: Path, project_id: str, task_id: str) -> Path:
    task_id = validate_identifier(task_id, "task_id")
    return project_tasks_root(state_root, project_id) / task_id


def artifact_path(state_root: Path, project_id: str, task_id: str, name: str) -> Path:
    name = validate_artifact_name(name)
    return task_dir(state_root, project_id, task_id) / name


def list_task_dirs(state_root: Path, project_id: str):
    root = project_tasks_root(state_root, project_id)
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir())
