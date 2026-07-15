"""Project domain helpers: profile loading and validation.

Profiles live at <repo>/.agent/project.toml. For projects that cannot store
that file, an external profile path may be registered in the global registry.

TOML is parsed with the stdlib tomllib (Python 3.11+).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Optional

from .identifiers import validate_identifier
from .models import ProjectProfile


class ProfileError(ValueError):
    """Raised when a project profile is missing required fields or invalid."""


def load_profile(profile_path: Path) -> ProjectProfile:
    """Load and validate a project profile from a TOML file."""
    profile_path = Path(profile_path)
    if not profile_path.is_file():
        raise ProfileError(f"profile not found: {profile_path}")
    with open(profile_path, "rb") as fh:
        data = tomllib.load(fh)
    return profile_from_dict(data)


def profile_from_dict(data: dict) -> ProjectProfile:
    """Build a ProjectProfile from a parsed TOML dict."""
    proj = data.get("project", {})
    project_id = proj.get("project_id")
    if not project_id:
        raise ProfileError("project.project_id is required")
    try:
        project_id = validate_identifier(str(project_id), "project_id")
    except ValueError as exc:
        raise ProfileError(str(exc)) from exc

    repository_root = proj.get("repository_root", ".")
    if not isinstance(repository_root, str) or not repository_root.strip():
        raise ProfileError("project.repository_root is required")

    timeout = int(proj.get("command_timeout_seconds", 60))
    max_output = int(proj.get("max_output_bytes", 65536))
    concurrency = int(proj.get("concurrency_limit", 1))
    if timeout <= 0:
        raise ProfileError("command_timeout_seconds must be positive")
    if max_output <= 0:
        raise ProfileError("max_output_bytes must be positive")
    if concurrency <= 0:
        raise ProfileError("concurrency_limit must be positive")

    return ProjectProfile(
        project_id=project_id,
        display_name=str(proj.get("display_name", project_id)),
        repository_root=repository_root,
        default_branch=str(proj.get("default_branch", "main")),
        runtime=str(proj.get("runtime", "python")),
        python_strategy=str(proj.get("python_strategy", ".venv/bin/python")),
        allowed_write_paths=[str(v) for v in proj.get("allowed_write_paths", [])],
        forbidden_paths=[str(v) for v in proj.get("forbidden_paths", [])],
        generated_paths=[str(v) for v in proj.get("generated_paths", [])],
        validation_profile=str(proj.get("validation_profile", "default")),
        workspace_strategy=str(proj.get("workspace_strategy", "none")),
        command_timeout_seconds=timeout,
        max_output_bytes=max_output,
        concurrency_limit=concurrency,
        environment_allowlist=[str(v) for v in proj.get("environment_allowlist", [])],
        approval_required=bool(proj.get("approval_required", True)),
        schema_version=int(proj.get("schema_version", 1)),
    )


def find_profile(repo_root: Path) -> Optional[Path]:
    """Return the path to <repo>/.agent/project.toml if present."""
    candidate = Path(repo_root) / ".agent" / "project.toml"
    return candidate if candidate.is_file() else None


def detect_runtime(repo_root: Path) -> str:
    """Best-effort runtime detection for project init. Never runs project code."""
    repo_root = Path(repo_root)
    if (repo_root / "pyproject.toml").is_file() or (repo_root / "setup.py").is_file():
        return "python"
    if (repo_root / "package.json").is_file():
        return "node"
    if (repo_root / ".git").is_dir():
        return "generic_git"
    return "generic_git"
