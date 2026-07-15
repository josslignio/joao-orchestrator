"""Global multi-project registry.

Platform-appropriate directories (macOS defaults), all env-overridable:
  config:   ~/.config/joss-orchestrator/      (JOSS_ORCH_CONFIG_DIR)
  state:    ~/.local/share/joss-orchestrator/ (JOSS_ORCH_STATE_DIR)
  logs:     ~/.local/state/joss-orchestrator/ (JOSS_ORCH_LOGS_DIR)
  worktrees:~/.cache/joss-orchestrator/worktrees/ (JOSS_ORCH_WORKTREE_DIR)

The registry is backed by SQLite and stores project metadata plus an absolute
pointer to each project's profile. Relative repository_root values in a
profile are anchored to the repository passed to ``add``; they never depend on
the orchestrator process's current working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .domain.identifiers import validate_identifier
from .domain.models import ProjectProfile
from .domain.projects import ProfileError, detect_runtime, find_profile, load_profile
from .storage.sqlite_store import SqliteStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser().resolve()


def config_dir() -> Path:
    return _env_path("JOSS_ORCH_CONFIG_DIR", Path.home() / ".config" / "joss-orchestrator")


def state_dir() -> Path:
    return _env_path("JOSS_ORCH_STATE_DIR", Path.home() / ".local" / "share" / "joss-orchestrator")


def logs_dir() -> Path:
    return _env_path("JOSS_ORCH_LOGS_DIR", Path.home() / ".local" / "state" / "joss-orchestrator")


def worktree_dir() -> Path:
    return _env_path("JOSS_ORCH_WORKTREE_DIR", Path.home() / ".cache" / "joss-orchestrator" / "worktrees")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _assert_disjoint_state_and_repo(state_root: Path, repo_root: Path) -> None:
    """Reject state/repository overlap in either direction."""
    state_root = state_root.resolve()
    repo_root = repo_root.resolve()
    if _is_within(state_root, repo_root) or _is_within(repo_root, state_root):
        raise ValueError(
            f"orchestrator state must be outside managed repositories: "
            f"state={state_root}, repository={repo_root}"
        )


def _containing_git_repository(path: Path) -> Optional[Path]:
    """Return the nearest ancestor containing .git, without running Git."""
    current = Path(path).expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _resolve_profile_repo(profile: ProjectProfile, repo_root: Path) -> Path:
    configured = Path(profile.repository_root).expanduser()
    if configured.is_absolute():
        return configured.resolve()
    return (repo_root / configured).resolve()


@dataclass
class RegisteredProject:
    project_id: str
    display_name: str
    repository_root: str
    profile_path: Optional[str]
    registered_at: str


class ProjectRegistry:
    """Manages the global project registry."""

    def __init__(self, state_root: Optional[Path] = None):
        self.state_root = Path(state_root or state_dir()).expanduser().resolve()
        containing_repo = _containing_git_repository(self.state_root)
        if containing_repo is not None:
            raise ValueError(
                f"orchestrator state cannot be inside a Git repository: "
                f"state={self.state_root}, repository={containing_repo}"
            )
        self._db: Optional[SqliteStore] = None

    @property
    def db(self) -> SqliteStore:
        """Create the SQLite index lazily after path safety checks."""
        if self._db is None:
            self.state_root.mkdir(parents=True, exist_ok=True)
            self._db = SqliteStore(self.state_root / "registry.db")
        return self._db

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def add(self, repo_root: Path, profile_path: Optional[Path] = None,
            expected_project_id: Optional[str] = None) -> RegisteredProject:
        """Register a project after anchoring and validating its profile."""
        repo_root = Path(repo_root).expanduser().resolve()
        if not repo_root.is_dir():
            raise ValueError(f"repository root is not a directory: {repo_root}")
        _assert_disjoint_state_and_repo(self.state_root, repo_root)

        if profile_path is None:
            profile_path = find_profile(repo_root)
        if profile_path is None:
            raise ValueError(f"no .agent/project.toml found in {repo_root}; "
                             f"run 'project init' first")
        profile_path = Path(profile_path).expanduser().resolve()
        profile = load_profile(profile_path)

        if expected_project_id is not None:
            expected_project_id = validate_identifier(expected_project_id, "project_id")
            if profile.project_id != expected_project_id:
                raise ValueError(
                    f"profile project_id {profile.project_id!r} does not match "
                    f"requested {expected_project_id!r}"
                )

        resolved_profile_repo = _resolve_profile_repo(profile, repo_root)
        if resolved_profile_repo != repo_root:
            raise ValueError(
                f"profile repository_root resolves to {resolved_profile_repo}, "
                f"but registration target is {repo_root}"
            )

        if self.db.has_project(profile.project_id):
            raise ValueError(f"project already registered: {profile.project_id}")
        for existing in self.list():
            if Path(existing.repository_root).resolve() == repo_root:
                raise ValueError(
                    f"repository already registered as {existing.project_id}: {repo_root}"
                )

        ts = _now_iso()
        self.db.upsert_project(
            project_id=profile.project_id,
            display_name=profile.display_name,
            repository_root=str(repo_root),
            registered_at=ts,
            profile_path=str(profile_path),
        )
        return RegisteredProject(
            project_id=profile.project_id,
            display_name=profile.display_name,
            repository_root=str(repo_root),
            profile_path=str(profile_path),
            registered_at=ts,
        )

    def remove(self, project_id: str) -> int:
        project_id = validate_identifier(project_id, "project_id")
        return self.db.remove_project(project_id)

    def get(self, project_id: str) -> Optional[RegisteredProject]:
        project_id = validate_identifier(project_id, "project_id")
        row = self.db.get_project(project_id)
        if not row:
            return None
        return RegisteredProject(
            project_id=row["project_id"],
            display_name=row["display_name"],
            repository_root=row["repository_root"],
            profile_path=row.get("profile_path"),
            registered_at=row["registered_at"],
        )

    def list(self) -> List[RegisteredProject]:
        return [RegisteredProject(
            project_id=r["project_id"],
            display_name=r["display_name"],
            repository_root=r["repository_root"],
            profile_path=r.get("profile_path"),
            registered_at=r["registered_at"],
        ) for r in self.db.list_projects()]

    def load_profile(self, project_id: str) -> ProjectProfile:
        reg = self.get(project_id)
        if reg is None:
            raise KeyError(f"project not registered: {project_id}")
        repo_root = Path(reg.repository_root).resolve()
        _assert_disjoint_state_and_repo(self.state_root, repo_root)
        profile_path = Path(reg.profile_path).resolve() if reg.profile_path else find_profile(repo_root)
        if profile_path is None or not profile_path.is_file():
            raise ValueError(f"profile missing for registered project {project_id}")
        profile = load_profile(profile_path)
        if profile.project_id != reg.project_id:
            raise ValueError(
                f"profile project_id changed: registry={reg.project_id}, "
                f"profile={profile.project_id}"
            )
        if _resolve_profile_repo(profile, repo_root) != repo_root:
            raise ValueError("profile repository_root no longer matches registry")
        return replace(profile, repository_root=str(repo_root))

    # ------------------------------------------------------------------ #
    # project init (generate conservative starters)
    # ------------------------------------------------------------------ #
    @staticmethod
    def init_profile(repo_root: Path, project_id: str,
                     display_name: Optional[str] = None,
                     force: bool = False) -> Path:
        """Generate conservative project and validation profiles.

        The generated validation profile always contains at least one offline,
        read-only check so validation cannot succeed vacuously.
        """
        repo_root = Path(repo_root).expanduser().resolve()
        project_id = validate_identifier(project_id, "project_id")
        agent_dir = repo_root / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        profile_path = agent_dir / "project.toml"
        validation_path = agent_dir / "validation.toml"
        if not force:
            existing = [p for p in (profile_path, validation_path) if p.is_file()]
            if existing:
                raise FileExistsError(f"profile already exists: {existing[0]}")

        runtime = detect_runtime(repo_root)
        python_strategy = ".venv/bin/python" if runtime == "python" else ""
        display_name = display_name or project_id
        profile_path.write_text(_STARTER_PROFILE.format(
            project_id=project_id,
            display_name=str(display_name).replace('"', "'"),
            repository_root=str(repo_root).replace('"', '\\"'),
            default_branch="main",
            runtime=runtime,
            python_strategy=python_strategy,
        ), encoding="utf-8")
        validation_path.write_text(_STARTER_VALIDATION, encoding="utf-8")

        agents_md = agent_dir / "AGENTS.md"
        if not agents_md.is_file() or force:
            agents_md.write_text(
                f"# {display_name}\n\nManaged by joss-orchestrator. See "
                f"project.toml and validation.toml for configuration.\n",
                encoding="utf-8",
            )
        return profile_path

    # ------------------------------------------------------------------ #
    # project doctor
    # ------------------------------------------------------------------ #
    def doctor(self, project_id: str) -> dict:
        try:
            reg = self.get(project_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if reg is None:
            return {"ok": False, "error": f"not registered: {project_id}"}
        report = {
            "project_id": project_id,
            "ok": True,
            "notes": [],
            "repository_root": reg.repository_root,
            "profile_path": reg.profile_path,
            "registered_at": reg.registered_at,
        }
        repo = Path(reg.repository_root)
        if not repo.is_dir():
            report["ok"] = False
            report["notes"].append(f"repository root missing: {repo}")
        try:
            _assert_disjoint_state_and_repo(self.state_root, repo)
        except ValueError as exc:
            report["ok"] = False
            report["notes"].append(str(exc))
        profile_path = Path(reg.profile_path) if reg.profile_path else find_profile(repo)
        if profile_path is None or not profile_path.is_file():
            report["ok"] = False
            report["notes"].append("profile file missing")
        else:
            try:
                profile = load_profile(profile_path)
                if profile.project_id != project_id:
                    raise ProfileError("profile project_id does not match registry")
                if _resolve_profile_repo(profile, repo) != repo.resolve():
                    raise ProfileError("profile repository_root does not match registry")
                validation = repo / ".agent" / "validation.toml"
                if not validation.is_file():
                    report["ok"] = False
                    report["notes"].append("validation profile missing")
            except (ProfileError, ValueError) as exc:
                report["ok"] = False
                report["notes"].append(str(exc))
        return report


_STARTER_PROFILE = """\
# joss-orchestrator project profile. Edit conservatively.
[project]
project_id = "{project_id}"
display_name = "{display_name}"
repository_root = "{repository_root}"
default_branch = "{default_branch}"
runtime = "{runtime}"
python_strategy = "{python_strategy}"
workspace_strategy = "none"
validation_profile = "default"
command_timeout_seconds = 60
max_output_bytes = 65536
concurrency_limit = 1
approval_required = true

allowed_write_paths = [
    "scripts/",
    "src/",
    "tests/",
    "docs/",
    ".agent/AGENTS.md",
    "README.md",
]

forbidden_paths = [
    ".env",
    ".venv/",
    ".git/",
    ".agent/project.toml",
    ".agent/validation.toml",
]

generated_paths = []

environment_allowlist = ["PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ"]
"""

_STARTER_VALIDATION = """\
# Conservative, offline, read-only starter checks.
[[commands]]
executable = "$GIT"
args = ["status", "--short"]
profile = "default"
timeout = 20

[[commands]]
executable = "$GIT"
args = ["diff", "--no-ext-diff", "--no-textconv", "--check"]
profile = "default"
timeout = 20
"""
