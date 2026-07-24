"""Centralized, deterministic resolution of the project-profile and
project-authority (Phase-0 spec) roots (Boss directive, 2026-07-21).

Before this module existed, `mission_intent.py` guessed a profiles root as
"whatever directory sits next to `projects_root`" — an ad-hoc assumption that
happened to work only when a caller passed matching sibling directories
(exactly what the test suite does, never what the live CLI/API path
produces). This module replaces that guess with one explicit, auditable
resolution order, and never searches the user's HOME directory at large.

Resolution order (identical shape for the profiles root and the projects
root) — first candidate that actually exists on disk wins:
  1. an explicitly configured root (constructor argument, or the
     `JOAO_PROFILES_ROOT` / `JOAO_PROJECTS_ROOT` environment variable)
  2. `<joao repo>/project_profiles` or `<joao repo>/projects` — this repo's
     own committed data
  3. `<state_root>/project_profiles` or `<state_root>/projects`, but ONLY
     when explicitly declared via a symlink at that path, or a manifest
     file `<state_root>/project_registry.manifest.json` naming it — never a
     silent default onto state-root data that happens to exist
If none of these resolve to a real directory, resolution fails closed
(`exists=False`) rather than falling back to scanning HOME.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_PROFILES_ENV_VAR = "JOAO_PROFILES_ROOT"
_PROJECTS_ENV_VAR = "JOAO_PROJECTS_ROOT"
_MANIFEST_FILENAME = "project_registry.manifest.json"


@dataclass(frozen=True)
class ResolvedRoot:
    path: Path
    source: str  # "explicit_arg" | "explicit_env" | "repo" | "state_manifest" | "unresolved"
    exists: bool

    def as_diagnostics(self) -> dict[str, str | bool | None]:
        return {"path": str(self.path) if self.path else None, "source": self.source, "exists": self.exists}


def _state_manifest_root(state_root: Path | None, key: str) -> Path | None:
    """Rule 3: a state-root profile/projects dir counts ONLY when explicitly
    declared — either a symlink at `<state_root>/<key>` (resolved target), or
    a manifest file naming it. Never a blind `<state_root>/<key>` default."""
    if state_root is None:
        return None
    state_root = Path(state_root)
    candidate = state_root / key
    if candidate.is_symlink():
        resolved = candidate.resolve()
        return resolved if resolved.is_dir() else None
    manifest_path = state_root / _MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            declared = json.loads(manifest_path.read_text()).get(key)
        except (OSError, json.JSONDecodeError):
            declared = None
        if declared:
            declared_path = Path(declared).expanduser()
            if declared_path.is_dir():
                return declared_path
    return None


def _resolve(*, explicit: Path | None, env_var: str, repo_default: Path, state_root: Path | None,
             manifest_key: str) -> ResolvedRoot:
    if explicit is not None:
        path = Path(explicit).expanduser()
        return ResolvedRoot(path=path, source="explicit_arg", exists=path.is_dir())

    env_value = os.environ.get(env_var)
    if env_value:
        path = Path(env_value).expanduser()
        return ResolvedRoot(path=path, source="explicit_env", exists=path.is_dir())

    if repo_default.is_dir():
        return ResolvedRoot(path=repo_default, source="repo", exists=True)

    manifest_root = _state_manifest_root(state_root, manifest_key)
    if manifest_root is not None:
        return ResolvedRoot(path=manifest_root, source="state_manifest", exists=True)

    return ResolvedRoot(path=repo_default, source="unresolved", exists=False)


class ProjectRegistry:
    """One centralized resolver for project-profile and project-authority
    (Phase-0 PROJECT_SPEC.md) roots. Construct once per `RunRuntime`/CLI
    invocation; `resolve_profiles_root()`/`resolve_projects_root()` are pure
    and idempotent, safe to call repeatedly for diagnostics."""

    def __init__(self, *, state_root: Path | None = None, profiles_root: Path | None = None,
                 projects_root: Path | None = None):
        self.state_root = Path(state_root).expanduser() if state_root else None
        self._explicit_profiles_root = Path(profiles_root).expanduser() if profiles_root else None
        self._explicit_projects_root = Path(projects_root).expanduser() if projects_root else None

    def resolve_profiles_root(self) -> ResolvedRoot:
        return _resolve(explicit=self._explicit_profiles_root, env_var=_PROFILES_ENV_VAR,
                         repo_default=REPO_ROOT / "project_profiles", state_root=self.state_root,
                         manifest_key="project_profiles")

    def resolve_projects_root(self) -> ResolvedRoot:
        return _resolve(explicit=self._explicit_projects_root, env_var=_PROJECTS_ENV_VAR,
                         repo_default=REPO_ROOT / "projects", state_root=self.state_root,
                         manifest_key="projects")

    def project_repository_path(self, project_id: str) -> Path | None:
        """Rule 4: a project's real (non-JOAO-managed) repository path, when
        the project's own profile.json explicitly declares one via
        `"repository_path"` — never inferred, never searched for."""
        profiles_root = self.resolve_profiles_root()
        if not profiles_root.exists:
            return None
        profile_path = profiles_root.path / project_id / "profile.json"
        if not profile_path.is_file():
            return None
        try:
            data = json.loads(profile_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        declared = data.get("repository_path")
        if not declared:
            return None
        return Path(declared).expanduser()

    def diagnostics(self) -> dict[str, dict[str, str | bool | None]]:
        return {"profiles_root": self.resolve_profiles_root().as_diagnostics(),
                "projects_root": self.resolve_projects_root().as_diagnostics()}
