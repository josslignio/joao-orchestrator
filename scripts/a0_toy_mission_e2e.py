#!/usr/bin/env python3
"""A0 / A0.1 EVIDENCE_REQUIRED: one full cycle — build -> candidate -> tests ->
review -> approval -> promotion -> rollback — on a toy mission, tracing the
SAME candidate_tree hash end to end. Prints a step-by-step trace and writes a
JSON evidence file. The rollback command is actually executed once, live, as
proof (not just written).

A0.1 correction (2026-07-19): the original `ToyReviewer` just echoed back
whatever `candidate_tree` the runtime handed it, without ever looking at a
single file — that is no longer sufficient evidence that a reviewer is
actually bound to what it claims to review (that is precisely what A0-1/A0-2
fix for the real `CodexCLIReviewer`). `InspectingReviewer` below replaces it:
it reads `greeting.py` from the exact path (`run["candidate"]["readonly_copy"]`)
it is handed for the "build"/"final" stages, fails if the mission's actual
acceptance criterion isn't met by that content, and independently
recomputes the candidate's tree hash from what is actually on disk via the
same primitive the controller itself uses (`recompute_candidate_tree`) —
failing if that disagrees with the hash it was told. Real Codex CLI was not
exercised in this run (quota constraint, same limitation documented in the
original A0 report); this is the documented substitute.

Usage: python3 scripts/a0_toy_mission_e2e.py [output_dir]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from joao_orchestrator.bubble.candidate import recompute_candidate_tree  # noqa: E402
from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder  # noqa: E402
from joao_orchestrator.bubble.promotion import promote, rollback  # noqa: E402


class InspectingReviewer:
    """A0.1: a fake reviewer that actually reads the candidate's on-disk
    files and independently re-derives the tree hash — it fails if the
    content doesn't satisfy the mission, or if the recomputed hash disagrees
    with the candidate_tree it was told to bind to."""
    provider = "toy-e2e-inspecting-reviewer"
    model = "reads-files-and-independently-recomputes-the-tree-hash"

    def review_stage(self, run, _run_dir, stage, active_rules=""):
        if stage == "plan":
            # No candidate exists yet at this stage — nothing to inspect or bind to.
            return {"ok": True, "decision": "pass", "stage": stage,
                    "proof": {"candidate_tree": None, "verdict": "ACCEPT", "findings": [],
                             "reviewer": {"provider": self.provider, "model": self.model}}}
        candidate = run.get("candidate")
        expected_tree = candidate.get("candidate_tree") if candidate else None
        review_root = Path(candidate["readonly_copy"]) if candidate else None
        if not review_root or not review_root.exists():
            return {"ok": False, "decision": "block", "stage": stage,
                    "reason": "no candidate readonly_copy to inspect"}
        greeting_path = review_root / "greeting.py"
        if not greeting_path.exists():
            return {"ok": False, "decision": "block", "stage": stage,
                    "reason": "greeting.py is missing from the candidate copy"}
        content = greeting_path.read_text()
        if "hello, world" not in content:
            return {"ok": False, "decision": "block", "stage": stage,
                    "reason": f"greeting.py does not satisfy the mission (actual content: {content!r})"}
        recomputed = recompute_candidate_tree(review_root)
        if recomputed != expected_tree:
            return {"ok": False, "decision": "block", "stage": stage,
                    "reason": f"independently recomputed tree {recomputed} != candidate_tree {expected_tree}"}
        return {
            "ok": True, "decision": "pass", "stage": stage,
            "proof": {
                "candidate_tree": expected_tree, "verdict": "ACCEPT",
                "findings": [f"read {greeting_path} directly and confirmed the mission's acceptance "
                            f"criterion is met; independently recomputed the tree hash and it matches"],
                "reviewer": {"provider": self.provider, "model": self.model},
            },
        }

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


def _git(argv, cwd, check=True):
    return subprocess.run(["git"] + argv, cwd=str(cwd), shell=False,
                          capture_output=True, text=True, check=check)


def build_toy_mission(workspace: Path) -> None:
    _git(["init", "-q", "-b", "main"], workspace)
    _git(["config", "user.email", "a0-e2e@example.invalid"], workspace)
    _git(["config", "user.name", "a0-e2e"], workspace)
    (workspace / "greeting.py").write_text("def greet():\n    return 'hello'\n")
    (workspace / "test_greeting.py").write_text(
        "from greeting import greet\n\n\ndef test_greet():\n    assert greet() == 'hello, world'\n"
    )
    (workspace / ".joao-profile.json").write_text(json.dumps({
        "project_id": "a0-toy", "display_name": "a0-toy", "repository_root": str(workspace),
        "allowed_write_paths": ["greeting.py"], "forbidden_paths": [],
    }))
    _git(["add", "."], workspace)
    _git(["commit", "-qm", "toy mission base"], workspace)


def toy_builder(_mission, workspace: Path, _correction: bool) -> dict:
    (workspace / "greeting.py").write_text("def greet():\n    return 'hello, world'\n")
    return {"ok": True}


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        REPO_ROOT / "evidence" / "A0_TOY_MISSION"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    trace: dict[str, object] = {}

    with tempfile.TemporaryDirectory(prefix="joao-a0-toy-") as tmp:
        state_root = Path(tmp) / "state"
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        build_toy_mission(workspace)
        base_commit = _git(["rev-parse", "HEAD"], workspace).stdout.strip()
        trace["base_commit"] = base_commit
        print(f"[1/7] toy mission repo created at base commit {base_commit[:12]}")

        rt = RunRuntime(state_root, builder=SandboxBuilder(toy_builder), reviewer=InspectingReviewer(),
                        profiles=LocalProfileAdapter())
        run_id = rt.start(project_id="a0-toy", workspace=workspace, mission="make greet() say hello, world",
                          targeted_tests=[[sys.executable, "test_greeting.py"]],
                          full_tests=[[sys.executable, "test_greeting.py"]])
        trace["run_id"] = run_id
        print(f"[2/7] run started: {run_id}")

        state = rt.run_once(run_id)
        trace["status_after_run_once"] = state["status"]
        assert state["status"] == "needs_approval", f"expected needs_approval, got {state['status']}"

        candidate = state["candidate"]
        candidate_tree = candidate["candidate_tree"]
        trace["candidate_tree"] = candidate_tree
        trace["candidate_commit"] = candidate["candidate_commit"]
        print(f"[3/7] build+candidate+tests+review complete — candidate_tree = {candidate_tree}")

        run_dir = state_root / "runs" / run_id
        build_review = json.loads((run_dir / "build-review-evidence.json").read_text())
        final_review = json.loads((run_dir / "final-review-evidence.json").read_text())
        trace["build_review_candidate_tree"] = build_review.get("proof", {}).get("candidate_tree")
        trace["final_review_candidate_tree"] = final_review.get("proof", {}).get("candidate_tree")
        same_hash_so_far = (
            trace["build_review_candidate_tree"] == candidate_tree
            and trace["final_review_candidate_tree"] == candidate_tree
        )
        trace["same_hash_through_review"] = same_hash_so_far
        print(f"[3/7] same candidate_tree bound by build review AND final review: {same_hash_so_far}")
        assert same_hash_so_far

        accepted = rt.approve(run_id)
        trace["status_after_approve"] = accepted["status"]
        assert accepted["status"] == "accepted"
        print("[4/7] approved (candidate_tree re-verified once more immediately before acceptance)")

        # A0-4/A0-6 (correction pass): promotion no longer touches the live
        # `workspace` worktree at all (a `reset --hard` there would never
        # have cleaned an untracked/ignored file anyway) — it materializes
        # and independently verifies the promotion in a brand-new sterile
        # worktree instead. The branch ref itself IS moved (verified via
        # `git rev-parse`, never by inspecting `workspace`'s files).
        manifest = promote(workspace, run_dir, candidate, run_id)
        trace["promotion_manifest"] = manifest
        assert manifest["verified"] is True
        promoted_tree = _git(["rev-parse", f"{manifest['promoted_commit']}^{{tree}}"], workspace).stdout.strip()
        trace["promoted_commit_tree"] = promoted_tree
        same_hash_through_promotion = promoted_tree == candidate_tree
        trace["same_hash_through_promotion"] = same_hash_through_promotion
        print(f"[5/7] promoted: branch main -> {manifest['promoted_commit'][:12]}, "
              f"tag {manifest['tag']}, tree matches candidate_tree: {same_hash_through_promotion}")
        assert same_hash_through_promotion

        branch_tip_after_promotion = _git(["rev-parse", "main"], workspace).stdout.strip()
        trace["branch_tip_after_promotion"] = branch_tip_after_promotion
        assert branch_tip_after_promotion == manifest["promoted_commit"]
        sterile_worktree = Path(manifest["promoted_worktree"])
        greeting_after_promotion = (sterile_worktree / "greeting.py").read_text()
        trace["greeting_after_promotion"] = greeting_after_promotion
        trace["promoted_worktree"] = str(sterile_worktree)
        print(f"[5/7] the NEW sterile worktree ({sterile_worktree}) reflects the promotion: "
              f"{greeting_after_promotion.strip()!r} — the live workspace itself is deliberately untouched")

        rollback_result = rollback(workspace, manifest)
        trace["rollback_result"] = rollback_result
        assert rollback_result["verified"] is True
        branch_tip_after_rollback = _git(["rev-parse", "main"], workspace).stdout.strip()
        trace["branch_tip_after_rollback"] = branch_tip_after_rollback
        rollback_restored_base = branch_tip_after_rollback == base_commit == manifest["previous_tip"]
        trace["rollback_restored_base"] = rollback_restored_base
        print(f"[6/7] rollback EXECUTED — branch main restored to base commit: {rollback_restored_base}")
        assert rollback_restored_base

        # The base commit's own tree (still checked out in `workspace`,
        # which promotion/rollback never touched) is the rollback's
        # observable proof here — no sterile worktree is created for a
        # rollback (there is nothing new to materialize).
        greeting_at_base = (workspace / "greeting.py").read_text()
        trace["greeting_at_base_workspace_untouched_throughout"] = greeting_at_base
        print(f"[7/7] the live workspace was never mutated by promote()/rollback(): {greeting_at_base.strip()!r}")

        trace["same_hash_end_to_end"] = (
            trace["candidate_tree"] == trace["build_review_candidate_tree"]
            == trace["final_review_candidate_tree"] == trace["promoted_commit_tree"]
        )
        print(f"\nSAME HASH TRACED END TO END (freeze -> build review -> final review -> "
              f"promoted commit tree): {trace['same_hash_end_to_end']}")
        print(f"  candidate_tree = {candidate_tree}")

    (out_dir / "trace.json").write_text(json.dumps(trace, indent=2, sort_keys=True, default=str))
    print(f"\nEvidence written to {out_dir / 'trace.json'}")
    return 0 if trace["same_hash_end_to_end"] and rollback_restored_base else 1


if __name__ == "__main__":
    raise SystemExit(main())
