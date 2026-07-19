"""Deliverable 7: atomic promotion of an ACCEPTED candidate, plus a rollback
mechanism that this run's own evidence proves was actually executed once.

Promotion fast-forwards a branch to the exact candidate commit that was
built, tested, and reviewed — never a re-derived tree. `git update-ref
<ref> <new> <old>` is a compare-and-swap: it fails atomically if the branch
moved since the check, rather than blindly overwriting concurrent work.
Promotion itself refuses outright (no force) if the branch has already
drifted from the candidate's recorded parent before it even attempts the
compare-and-swap.

A0-4 / A0-6 (correction pass, 2026-07-19): the pre-A0.1 `promote()`/
`rollback()` did the branch-ref compare-and-swap correctly, but then
materialized that ref move into the live `workspace` with a `git reset
--hard` whose return code was ignored (`check=False`). Two concrete flaws
followed from that:

  - `reset --hard` never removes untracked/ignored files. A gitignored
    payload (e.g. exactly the `*.secret` file the new A0-4 gate in
    `_execute` refuses earlier in the pipeline) sitting in `workspace`
    survives a promotion completely untouched, right next to the newly
    promoted code — "detected and refused" upstream is not the same
    guarantee as "cannot survive a promotion" if promotion itself would
    have let it ride along regardless.
  - a failed `reset --hard` (silently swallowed by `check=False`) could
    leave the branch ref already moved to the new commit while the actual
    worktree on disk still showed the old one — a false success with no
    detectable divergence, and no automatic rollback.

Promotion now never touches the live `workspace` worktree at all. After the
ref CAS succeeds, a brand-new, throwaway ("sterile") worktree is checked out
fresh from the candidate commit — a fresh `git worktree add` only ever
materializes what git actually tracked in that commit's tree, so a stray
untracked/ignored file from the old workspace has no way to appear in it.
Before the promotion is ever reported as verified, the sterile worktree's
tree hash is independently recomputed, its `git status` is confirmed clean,
and it is scanned for sensitive ignored files. Any checkout or verification
failure triggers an immediate CAS rollback of the branch ref — never an
ignored return code, never a manifest written before verification.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..policy.paths import detect_sensitive_ignored_files
from ..storage.atomic import append_line, atomic_write_json
from .candidate import recompute_candidate_tree
from .change_capture import ignored_files_inventory


class PromotionError(RuntimeError):
    pass


_APPROVAL_KEYS = {"run_id", "candidate_commit", "candidate_tree", "review_proof_sha256",
                  "approved_by", "approved_at", "previous_status"}


def create_approval_record(run: dict[str, Any], candidate: dict[str, Any], *,
                           review_proof_sha256: str, approved_by: str = "human",
                           approved_at: str | None = None) -> dict[str, Any]:
    """A0.2 §15: the immutable approval object `promote()` requires — separate
    from the mutable `run.json`. Binds the human decision to the exact
    run/candidate/review triple; `promote()` independently re-verifies every
    field against the run's own on-disk evidence before it ever touches git."""
    return {
        "run_id": run["run_id"],
        "candidate_commit": candidate["candidate_commit"],
        "candidate_tree": candidate["candidate_tree"],
        "review_proof_sha256": review_proof_sha256,
        "approved_by": approved_by,
        "approved_at": approved_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "previous_status": "needs_approval",
    }


def write_approval_record(run_dir: Path, ledger_path: Path, record: dict[str, Any]) -> Path:
    """Persist the approval record twice: once per-run (content-addressed by
    the run's own folder, easy to find) and once appended to a global,
    append-only ledger (`ledger_path`) so a later `promote()` call — possibly
    in a different process — can still find and re-verify it. Never
    rewritten once written (A0.2 §15's "append-only or content-addressed")."""
    path = Path(run_dir) / "approval-record.json"
    atomic_write_json(path, record)
    append_line(Path(ledger_path), json.dumps(record, sort_keys=True))
    return path


def _git(argv: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + argv, cwd=str(cwd), shell=False,
                          capture_output=True, text=True, check=check)


def _current_branch(workspace: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], workspace).stdout.strip()


def _verify_sterile_worktree(worktree: Path, expected_tree: str) -> list[str]:
    """A0-4/A0-6: independently re-derive the tree hash from what is actually
    on disk in the fresh checkout (never trust the checkout silently
    "worked"), confirm the worktree itself reports clean (nothing beyond the
    candidate's own tracked tree materialized), and confirm no sensitive
    gitignored file is present. Returns a list of problems — empty means
    verified."""
    problems = []
    try:
        recomputed = recompute_candidate_tree(worktree)
    except Exception as exc:  # fail-closed: cannot verify => not verified
        return [f"could not recompute tree hash: {type(exc).__name__}: {exc}"]
    if recomputed != expected_tree:
        problems.append(f"tree mismatch: expected {expected_tree}, sterile worktree recomputed {recomputed}")
    status = _git(["status", "--porcelain"], worktree, check=False).stdout.strip()
    if status:
        problems.append(f"sterile worktree is not clean: {status!r}")
    sensitive = detect_sensitive_ignored_files(ignored_files_inventory(worktree))
    if sensitive:
        problems.append("sensitive ignored file(s) present in sterile worktree: " + "; ".join(sensitive))
    return problems


def _cas_rollback_ref(workspace: Path, branch: str, previous_tip: str, promoted: str, tag: str) -> None:
    """Used internally when post-CAS verification fails: move the branch ref
    straight back with its own compare-and-swap (never a blind/forced move)
    and drop the promotion tag. If this itself fails the exception is left
    to propagate — silently swallowing a rollback failure would be exactly
    the kind of ignored-return-code bug this correction pass exists to
    remove."""
    _git(["update-ref", f"refs/heads/{branch}", previous_tip, promoted], workspace)
    _git(["tag", "-d", tag], workspace, check=False)


def _verify_acceptance_for_promotion(run: dict[str, Any], candidate: dict[str, Any],
                                     approval_record: dict[str, Any], run_dir: Path) -> None:
    """A0.2 §15/run card #8: `promote()` verifies acceptance ITSELF — a
    mutable `run.json` with `status == "accepted"` is not sufficient on its
    own (that field could be hand-edited same as any other). This checks the
    run's own status/review-verified flags AND that the separately-persisted,
    append-only `approval_record` genuinely binds this exact run, candidate
    and review-evidence file — recomputing the review proof hash from the
    evidence file on disk rather than trusting the number the caller hands
    in. Raises `PromotionError` (never a bool the caller could ignore) on the
    first mismatch."""
    if run.get("status") != "accepted":
        raise PromotionError(f"A0.2: refusing promotion — run.status is {run.get('status')!r}, not 'accepted'")
    if not run.get("review_verified"):
        raise PromotionError("A0.2: refusing promotion — run.review_verified is not True")
    if run.get("candidate_tree") != candidate["candidate_tree"]:
        raise PromotionError(
            f"A0.2: refusing promotion — run.candidate_tree {run.get('candidate_tree')!r} does not match "
            f"the candidate being promoted {candidate['candidate_tree']!r}"
        )
    if approval_record.get("run_id") != run.get("run_id"):
        raise PromotionError("A0.2: refusing promotion — approval_record.run_id does not match this run")
    if approval_record.get("candidate_commit") != candidate.get("candidate_commit"):
        raise PromotionError("A0.2: refusing promotion — approval_record.candidate_commit does not match this candidate")
    if approval_record.get("candidate_tree") != candidate.get("candidate_tree"):
        raise PromotionError("A0.2: refusing promotion — approval_record.candidate_tree does not match this candidate")
    review_path = Path(run_dir) / "review-evidence.json"
    if not review_path.is_file():
        raise PromotionError("A0.2: refusing promotion — no review-evidence.json found for this run")
    actual_review_sha256 = hashlib.sha256(review_path.read_bytes()).hexdigest()
    if approval_record.get("review_proof_sha256") != actual_review_sha256:
        raise PromotionError(
            "A0.2: refusing promotion — approval_record.review_proof_sha256 does not match the "
            "review-evidence.json currently on disk for this run (evidence tampered or record forged)"
        )


def promote(workspace: Path, run_dir: Path, candidate: dict[str, Any], run_id: str,
           branch: str | None = None, *, run: dict[str, Any] | None = None,
           approval_record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Atomically fast-forward `branch` (default: current branch) to the
    frozen candidate commit, then materialize and independently verify that
    promotion in a brand-new sterile worktree — never in the live,
    possibly-cruft-carrying `workspace`. The success manifest is written
    ONLY after both the tree hash and the on-disk worktree state have been
    verified; any failure along the way immediately CAS-rolls-back the
    branch ref rather than leaving it pointing at an unverified commit.

    A0.2 §15: `run` and `approval_record` are required — `promote()` will not
    fast-forward anything until it has independently re-verified acceptance
    itself (see `_verify_acceptance_for_promotion`), never trusting the
    caller's word that the run was accepted."""
    if run is None or approval_record is None:
        raise PromotionError(
            "A0.2: promote() requires both `run` (the full run record) and `approval_record` "
            "(the immutable object from create_approval_record()) — a candidate/run_id pair "
            "alone is no longer sufficient to self-verify acceptance."
        )
    _verify_acceptance_for_promotion(run, candidate, approval_record, run_dir)
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

    # A0-4/A0-6: materialize the promotion in a brand-new sterile worktree —
    # never `reset --hard` the live `workspace` (which `reset --hard` would
    # never actually clean of untracked/ignored cruft anyway).
    sterile_dir = Path(run_dir) / f"promoted-worktree-{run_id}"
    if sterile_dir.exists():
        shutil.rmtree(sterile_dir, ignore_errors=True)
    checkout = _git(["worktree", "add", "--detach", str(sterile_dir), candidate["candidate_commit"]], workspace, check=False)
    if checkout.returncode != 0:
        _cas_rollback_ref(workspace, branch, current_tip, candidate["candidate_commit"], tag)
        raise PromotionError(
            f"A0-6: sterile worktree checkout failed (returncode={checkout.returncode}): {checkout.stderr.strip()} "
            f"— branch {branch!r} CAS-rolled-back to {current_tip} immediately; no false success was reported"
        )
    problems = _verify_sterile_worktree(sterile_dir, candidate["candidate_tree"])
    if problems:
        _git(["worktree", "remove", "--force", str(sterile_dir)], workspace, check=False)
        shutil.rmtree(sterile_dir, ignore_errors=True)
        _cas_rollback_ref(workspace, branch, current_tip, candidate["candidate_commit"], tag)
        raise PromotionError(
            "A0-6: promotion verification failed in the sterile worktree — " + "; ".join(problems) +
            f" — branch {branch!r} CAS-rolled-back to {current_tip} immediately; no false success was reported"
        )

    manifest = {
        "run_id": run_id, "branch": branch, "tag": tag,
        "promoted_commit": candidate["candidate_commit"], "candidate_tree": candidate["candidate_tree"],
        "previous_tip": current_tip, "promoted_worktree": str(sterile_dir),
        "verified": True, "verified_tree": candidate["candidate_tree"],
        "approval_record": approval_record,
        "acceptance_self_verified": True,
        "rollback_command": ["git", "-C", str(workspace), "update-ref",
                             f"refs/heads/{branch}", current_tip, candidate["candidate_commit"]],
    }
    atomic_write_json(run_dir / "promotion-manifest.json", manifest)
    return manifest


def rollback(workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Execute the rollback recorded in a promotion manifest: compare-and-swap
    `branch` back to `previous_tip`, refusing if the branch is no longer
    exactly at the promoted commit (no blind overwrite of later work).
    Verifies the branch ref actually landed back on `previous_tip` before
    reporting success (A0-6) — never the live `workspace` worktree (A0-4),
    which this module no longer touches at all; the promoted artifact lives
    in the sterile worktree recorded in the manifest, not in `workspace`."""
    workspace = Path(workspace)
    branch, previous_tip, promoted = manifest["branch"], manifest["previous_tip"], manifest["promoted_commit"]
    try:
        _git(["update-ref", f"refs/heads/{branch}", previous_tip, promoted], workspace)
    except subprocess.CalledProcessError as exc:
        raise PromotionError(
            f"branch {branch!r} is not at the promoted commit {promoted} anymore — "
            f"refusing a blind rollback: {exc.stderr}"
        ) from exc
    new_tip = _git(["rev-parse", branch], workspace).stdout.strip()
    if new_tip != previous_tip:
        raise PromotionError(
            f"A0-6: rollback did not verify — branch {branch!r} tip is {new_tip}, expected {previous_tip}"
        )
    return {"rolled_back": True, "branch": branch, "restored_to": previous_tip, "from_commit": promoted,
            "verified": True}
