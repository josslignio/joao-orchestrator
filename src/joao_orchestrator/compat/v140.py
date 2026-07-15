"""Generic V1.4.0 compatibility adapters.

The top-level ``orchestrator`` package contains re-exports only.  Any adapter
logic needed by old signatures lives here in the canonical package and loads
rules from the target repository's profile rather than hardcoding a project.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional

from ..cli import build_handoff_prompt
from ..domain.models import ProjectProfile, TaskMeta
from ..domain.projects import find_profile, load_profile
from ..policy.commands import CommandDefinition
from ..policy.paths import detect_forbidden_changes, is_forbidden
from ..validation.executor import RestrictedExecutor as CanonicalRestrictedExecutor
from ..validation.profiles import load_profile_commands


def _profile_for_repo(repo_root: Path) -> ProjectProfile:
    root = Path(repo_root).expanduser().resolve()
    profile_path = find_profile(root)
    if profile_path:
        return replace(load_profile(profile_path), repository_root=str(root))
    return ProjectProfile(
        project_id="legacy",
        display_name="legacy",
        repository_root=str(root),
        allowed_write_paths=[],
        forbidden_paths=[],
        environment_allowlist=["PATH", "HOME", "USER", "LANG"],
    )


def _resolve_python(root: Path) -> str:
    venv_python = root / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable or "python3"


def _load_json_allowlist(root: Path, allowlist_path: Path):
    """Convert a caller-supplied legacy JSON allowlist fail-closed."""
    if not allowlist_path.is_file():
        return []
    with open(allowlist_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    resolver = {"python": _resolve_python(root), "git": "git"}
    return [
        CommandDefinition.from_dict(entry, resolve=lambda role: resolver[role])
        for entry in data.get("commands", [])
    ]


class LegacyRestrictedExecutor(CanonicalRestrictedExecutor):
    """V1.4.0 constructor adapter: ``(root, allowlist_path, ...)``."""

    def __init__(self, root, allowlist_path, timeout_default=60,
                 max_output_bytes=65536, redaction_enabled=True):
        root = Path(root).expanduser().resolve()
        profile = _profile_for_repo(root)
        profile.command_timeout_seconds = int(timeout_default)
        profile.max_output_bytes = int(max_output_bytes)
        commands = _load_json_allowlist(root, Path(allowlist_path))
        super().__init__(
            profile,
            commands,
            timeout_default=timeout_default,
            max_output_bytes=max_output_bytes,
            redaction_enabled=redaction_enabled,
        )


def compat_is_forbidden(path_str: str, profile: Optional[ProjectProfile] = None) -> str:
    profile = profile or ProjectProfile(
        project_id="legacy", display_name="legacy", repository_root="."
    )
    return is_forbidden(path_str, profile)


def compat_detect_forbidden_changes(changed_paths, profile: Optional[ProjectProfile] = None):
    profile = profile or ProjectProfile(
        project_id="legacy", display_name="legacy", repository_root="."
    )
    return detect_forbidden_changes(changed_paths, profile)


def build_zcode_prompt(task: TaskMeta, repo_root: Path, branch: str,
                       git_status: str, extra_allowed=()) -> str:
    """V1.4.0 signature backed by the repository's current profile."""
    root = Path(repo_root).expanduser().resolve()
    profile = _profile_for_repo(root)
    validation_path = root / ".agent" / "validation.toml"
    commands = []
    if validation_path.is_file():
        commands = load_profile_commands(validation_path, profile)
    command_strings = [" ".join([c.executable] + c.args) for c in commands]
    return build_handoff_prompt(task, profile, branch, git_status, command_strings)
