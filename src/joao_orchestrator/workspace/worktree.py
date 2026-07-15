"""Isolated Git worktree manager for task dispatch.

Creates a dedicated worktree per task under the orchestrator runtime state,
outside the managed repository.  The worktree is populated from an explicitly
approved source branch or commit and used as the cwd for provider execution.

Safety invariants:
- Never uses shell=True.
- Never runs reset --hard, clean -fd, force push, rebase, merge,
  branch deletion, package installation, or destructive Git commands.
- The worktree path is always under the orchestrator runtime state directory
  and never inside the managed repository.
- The source revision must resolve to a valid commit in the managed repo.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..domain.events import now_iso
from .git import _git_env


# Only these subcommands are ever issued for worktree management.
_SAFE_WORKTREE_SUBCOMMANDS = {
    "worktree", "rev-parse", "status", "diff", "log",
    "ls-files", "show", "merge-base",
}


@dataclass
class WorktreeMeta:
    """Persisted metadata about an isolated task worktree."""
    task_id: str
    project_id: str
    worktree_path: str
    source_revision: str          # branch name or commit SHA
    resolved_commit: str           # full SHA that was checked out
    source_branch: str
    created_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    ok: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _run_git(root: Path, argv: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a Git command with shell=False in the given root."""
    if not argv or argv[0] not in _SAFE_WORKTREE_SUBCOMMANDS:
        raise ValueError(f"unsafe worktree subcommand: {argv}")
    git_bin = shutil.which("git", path=_git_env()["PATH"]) or "git"
    return subprocess.run(
        [git_bin] + argv,
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(Path(root).resolve()),
        env=_git_env(),
    )


def resolve_revision(repo_root: Path, revision: str) -> str:
    """Resolve a branch name or commit shorthand to a full SHA."""
    proc = _run_git(repo_root, ["rev-parse", "--verify", revision])
    if proc.returncode != 0:
        raise ValueError(
            f"cannot resolve revision {revision!r}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def create_worktree(
    repo_root: Path,
    worktree_parent: Path,
    task_id: str,
    project_id: str,
    source_revision: str,
    source_branch: Optional[str] = None,
) -> WorktreeMeta:
    """Create an isolated Git worktree for a task.

    The worktree is created at ``<worktree_parent>/<project_id>/<task_id>/worktree/``,
    outside the managed repository.  It checks out an explicit revision (branch or
    commit) so the provider works on a known starting point.

    Returns a WorktreeMeta with the resolved commit and paths.
    """
    repo_root = Path(repo_root).resolve()
    worktree_parent = Path(worktree_parent).resolve()

    task_dir = worktree_parent / project_id / task_id
    wt_path = task_dir / "worktree"
    wt_path.mkdir(parents=True, exist_ok=True)

    # Ensure the worktree path is not inside the managed repository.
    try:
        wt_path.resolve().relative_to(repo_root)
        raise ValueError(
            "worktree path must be outside the managed repository"
        )
    except ValueError:
        pass  # Good: not inside repo.

    resolved = resolve_revision(repo_root, source_revision)
    if source_branch is None:
        branch = resolve_branch(repo_root)
    else:
        branch = source_branch

    started = now_iso()
    start = time.monotonic()

    try:
        proc = _run_git(
            repo_root,
            ["worktree", "add", str(wt_path), resolved],
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {proc.stderr.strip()}"
            )
    except Exception as exc:
        duration = time.monotonic() - start
        meta = WorktreeMeta(
            task_id=task_id, project_id=project_id,
            worktree_path=str(wt_path),
            source_revision=source_revision,
            resolved_commit=resolved,
            source_branch=branch,
            created_at=started, finished_at=now_iso(),
            duration_seconds=duration, ok=False, error=str(exc),
        )
        return meta

    duration = time.monotonic() - start
    return WorktreeMeta(
        task_id=task_id, project_id=project_id,
        worktree_path=str(wt_path),
        source_revision=source_revision,
        resolved_commit=resolved,
        source_branch=branch,
        created_at=started, finished_at=now_iso(),
        duration_seconds=duration, ok=True,
    )


def resolve_branch(repo_root: Path) -> str:
    """Return the current branch name of the repository."""
    proc = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if proc.returncode != 0:
        return "(unknown)"
    return proc.stdout.strip()


def remove_worktree(repo_root: Path, worktree_path: Path) -> bool:
    """Remove a worktree registration and directory.

    This uses ``git worktree remove`` which is a safe, non-destructive operation
    (it removes the worktree but never force-deletes branches).
    """
    worktree_path = Path(worktree_path).resolve()
    if not worktree_path.is_dir():
        return True
    proc = _run_git(
        repo_root,
        ["worktree", "remove", str(worktree_path), "--force"],
        timeout=30,
    )
    return proc.returncode == 0


@dataclass
class CaptureResult:
    """Captured state after provider execution in the worktree."""
    git_status: str = ""
    changed_files: List[str] = field(default_factory=list)
    patch: str = ""
    provider_ok: bool = False
    provider_result: Optional[Dict] = None
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def capture_worktree_state(
    worktree_path: Path,
    provider_ok: bool = False,
    provider_result: Optional[Dict] = None,
    started_at: str = "",
    duration_seconds: float = 0.0,
) -> CaptureResult:
    """Capture git status, changed files, and patch from a worktree.

    This is read-only inspection only.
    """
    wt = Path(worktree_path).resolve()
    if not wt.is_dir():
        return CaptureResult(
            provider_ok=provider_ok,
            provider_result=provider_result,
            started_at=started_at,
            finished_at=now_iso(),
            duration_seconds=duration_seconds,
        )

    # git status --short
    status_proc = _run_git(wt, ["status", "--short"], timeout=20)
    git_status = status_proc.stdout if status_proc.returncode == 0 else ""

    # git diff --name-only (staged + unstaged)
    diff_names_proc = _run_git(wt, ["diff", "--name-only"], timeout=20)
    staged_names_proc = _run_git(wt, ["diff", "--name-only", "--cached"], timeout=20)
    # Also capture untracked files.
    untracked_proc = _run_git(
        wt,
        ["ls-files", "--others", "--exclude-standard"],
        timeout=20,
    )
    all_changed = []
    if diff_names_proc.returncode == 0:
        all_changed.extend(diff_names_proc.stdout.strip().splitlines())
    if staged_names_proc.returncode == 0:
        all_changed.extend(staged_names_proc.stdout.strip().splitlines())
    if untracked_proc.returncode == 0:
        all_changed.extend(untracked_proc.stdout.strip().splitlines())
    changed_files = list(dict.fromkeys(p for p in all_changed if p))

    # git diff (binary-safe: --binary flag)
    patch_proc = _run_git(
        wt, ["diff", "--binary", "--no-ext-diff", "--no-textconv"], timeout=30,
    )
    patch = patch_proc.stdout if patch_proc.returncode == 0 else ""

    return CaptureResult(
        git_status=git_status,
        changed_files=changed_files,
        patch=patch,
        provider_ok=provider_ok,
        provider_result=provider_result,
        started_at=started_at,
        finished_at=now_iso(),
        duration_seconds=duration_seconds,
    )


def enforce_allowed_write_paths(
    changed_files: List[str],
    allowed_write_paths: List[str],
    forbidden_paths: List[str],
) -> List[str]:
    """Fail-closed check: every changed file must match an allowed path
    and must not match any forbidden path. Returns list of violations."""
    violations = []
    for f in changed_files:
        norm = f.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]

        # Check forbidden first.
        forbidden = False
        for rule in forbidden_paths:
            rule_norm = rule.replace("\\", "/")
            if rule_norm.startswith("*."):
                if norm.rsplit("/", 1)[-1].endswith(rule_norm[1:]):
                    forbidden = True
                    break
            if rule_norm.endswith("/"):
                if norm == rule_norm.rstrip("/") or norm.startswith(rule_norm):
                    forbidden = True
                    break
            if norm == rule_norm or norm.startswith(rule_norm + "/"):
                forbidden = True
                break
        if forbidden:
            violations.append(f"forbidden: {f}")
            continue

        # Check allowed.
        allowed = False
        for rule in allowed_write_paths:
            rule_norm = rule.replace("\\", "/")
            if rule_norm.endswith("/"):
                if norm == rule_norm.rstrip("/") or norm.startswith(rule_norm):
                    allowed = True
                    break
            if norm == rule_norm or norm.startswith(rule_norm + "/"):
                allowed = True
                break
        if not allowed:
            violations.append(f"not in allowed_write_paths: {f}")

    return violations
