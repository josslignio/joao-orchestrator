"""Deliverable 7: atomic promotion of an ACCEPTED candidate, plus a rollback
mechanism that this run's own evidence proves was actually executed once.

Promotion fast-forwards a branch to the exact candidate commit that was
built, tested, and reviewed — never a re-derived tree. `git update-ref
<ref> <new> <old>` is a compare-and-swap: it fails atomically if the branch
moved since the check, rather than blindly overwriting concurrent work.
Promotion itself refuses outright (no force) if the branch has already
drifted from the candidate's recorded parent before it even attempts the
compare-and-swap.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ..storage.atomic import atomic_write_json


class PromotionError(RuntimeError):
    pass


def _git(argv: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + argv, cwd=str(cwd), shell=False,
                          capture_output=True, text=True, check=check)


def _current_branch(workspace: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], workspace).stdout.strip()


def promote(workspace: Path, run_dir: Path, candidate: dict[str, Any], run_id: str,
           branch: str | None = None) -> dict[str, Any]:
    """Atomically fast-forward `branch` (default: current branch) to the
    frozen candidate commit; tag it; write a manifest carrying the exact
    rollback command as evidence."""
    workspace = Path(workspace)
    branch = branch or _current_branch(workspace)
    current_tip = _git(["rev-parse", branch], workspace).stdout.strip()
    if current_tip != candidate["parent_commit"]:
        raise PromotionError(
            f"branch {branch!r} has moved since the candidate was frozen "
            f"(tip is {current_tip}, candidate's parent is {candidate['parent_commit']}) "
            "— refusing to force-promote over concurrent drift"
        )
    tag = f"joao-promoted-{run_id}"
    _git(["tag", "-a", tag, "-m", f"JOAO promotion of {run_id} ({candidate['candidate_tree']})",
         candidate["candidate_commit"]], workspace)
    try:
        _git(["update-ref", f"refs/heads/{branch}", candidate["candidate_commit"], current_tip], workspace)
    except subprocess.CalledProcessError as exc:
        _git(["tag", "-d", tag], workspace, check=False)
        raise PromotionError(f"branch {branch!r} moved concurrently during promotion — compare-and-swap refused: {exc.stderr}") from exc
    if _current_branch(workspace) == branch:
        _git(["reset", "--hard", candidate["candidate_commit"]], workspace, check=False)
    manifest = {
        "run_id": run_id, "branch": branch, "tag": tag,
        "promoted_commit": candidate["candidate_commit"], "candidate_tree": candidate["candidate_tree"],
        "previous_tip": current_tip,
        "rollback_command": ["git", "-C", str(workspace), "update-ref",
                             f"refs/heads/{branch}", current_tip, candidate["candidate_commit"]],
    }
    atomic_write_json(run_dir / "promotion-manifest.json", manifest)
    return manifest


def rollback(workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Execute the rollback recorded in a promotion manifest: compare-and-swap
    `branch` back to `previous_tip`, refusing if the branch is no longer
    exactly at the promoted commit (no blind overwrite of later work)."""
    workspace = Path(workspace)
    branch, previous_tip, promoted = manifest["branch"], manifest["previous_tip"], manifest["promoted_commit"]
    try:
        _git(["update-ref", f"refs/heads/{branch}", previous_tip, promoted], workspace)
    except subprocess.CalledProcessError as exc:
        raise PromotionError(
            f"branch {branch!r} is not at the promoted commit {promoted} anymore — "
            f"refusing a blind rollback: {exc.stderr}"
        ) from exc
    if _current_branch(workspace) == branch:
        _git(["reset", "--hard", previous_tip], workspace, check=False)
    return {"rolled_back": True, "branch": branch, "restored_to": previous_tip, "from_commit": promoted}
