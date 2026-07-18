"""RI-3: the immutable build candidate.

After a build, the controller (never the builder) freezes the exact tree the
builder produced: everything is staged into a throwaway index, written as a
git tree object, wrapped in a commit that is never attached to any branch,
and pinned under `refs/joao/candidates/<run_id>-<attempt>` so it survives GC.
A read-only worktree checkout of that exact commit is what tests and the
reviewer see — never the live, still-mutable mission workspace.

`candidate_tree` (the git *tree* object hash, not the commit hash) is the
value RI-4 requires reviewer verdicts to bind to. It is recomputed fresh
(`git add -A` + `write-tree` inside the read-only copy) at approval time so a
post-test tamper of the frozen copy is detected even if the tamper bypassed
git entirely (a `chmod +w` + direct edit) — `git add -A` always re-stats and
re-hashes every file from what is actually on disk, never trusting a stale
index cache.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any


class CandidateError(RuntimeError):
    pass


def _git(argv: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + argv, cwd=str(cwd), shell=False,
                          capture_output=True, text=True, check=check)


def _chmod_read_only(root: Path) -> None:
    """Recursively strip write bits from every checked-out file (never `.git`,
    which the linked worktree's own bookkeeping legitimately needs to write)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    continue
                path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            except OSError:
                pass
        try:
            mode = Path(dirpath).stat().st_mode
            Path(dirpath).chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) | stat.S_IXUSR)
        except OSError:
            pass


def _chmod_writable(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        try:
            Path(dirpath).chmod(0o755)
        except OSError:
            pass
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if not path.is_symlink():
                    path.chmod(0o644)
            except OSError:
                pass


def freeze_candidate(workspace: Path, run_dir: Path, run_id: str, attempt: int) -> dict[str, Any]:
    """Stage the builder's changes into a frozen, un-branched commit; leave the
    live workspace's index exactly as it was; return the candidate's identity."""
    workspace = Path(workspace)
    before_head = _git(["rev-parse", "HEAD"], workspace).stdout.strip()
    _git(["add", "-A"], workspace)
    tree = _git(["write-tree"], workspace).stdout.strip()
    commit = _git(["commit-tree", tree, "-p", before_head, "-m",
                   f"JOAO candidate {run_id} attempt {attempt}"], workspace).stdout.strip()
    ref = f"refs/joao/candidates/{run_id}-{attempt:03d}"
    _git(["update-ref", ref, commit], workspace)
    # Restore the live workspace's index to its pre-freeze state — freezing
    # must never disturb an in-flight correction loop's working tree.
    _git(["reset", "--mixed", "-q", before_head], workspace, check=False)
    # `reset --mixed HEAD` would drop the builder's actual worktree edits back
    # to unstaged (correct — they must stay exactly as the builder left them),
    # but `reset` also moves HEAD if given a ref other than the current one;
    # `before_head` IS the current HEAD, so HEAD itself does not move.

    copy_dir = run_dir / f"candidate-readonly-{attempt:03d}"
    if copy_dir.exists():
        _chmod_writable(copy_dir)
        shutil.rmtree(copy_dir)
    result = _git(["worktree", "add", "--detach", str(copy_dir), commit], workspace, check=False)
    if result.returncode != 0:
        raise CandidateError(f"could not create read-only candidate copy: {result.stderr}")
    _chmod_read_only(copy_dir)

    return {
        "run_id": run_id, "attempt": attempt, "candidate_ref": ref,
        "candidate_commit": commit, "candidate_tree": tree,
        "readonly_copy": str(copy_dir), "parent_commit": before_head,
    }


def recompute_candidate_tree(readonly_copy: Path) -> str:
    """Independently re-derive the tree hash from what is actually on disk —
    never trust the value recorded at freeze time without re-checking it."""
    copy_dir = Path(readonly_copy)
    _chmod_writable(copy_dir)
    try:
        _git(["add", "-A"], copy_dir)
        tree = _git(["write-tree"], copy_dir).stdout.strip()
    finally:
        _chmod_read_only(copy_dir)
    return tree


def release_candidate(candidate: dict[str, Any], workspace: Path) -> None:
    """Remove a superseded candidate's read-only copy and worktree registration
    (the ref itself is kept — it is the permanent, GC-safe evidence trail)."""
    copy_dir = Path(candidate["readonly_copy"])
    if copy_dir.exists():
        _chmod_writable(copy_dir)
        _git(["worktree", "remove", "--force", str(copy_dir)], Path(workspace), check=False)
        if copy_dir.exists():
            shutil.rmtree(copy_dir, ignore_errors=True)
