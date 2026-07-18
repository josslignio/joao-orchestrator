#!/usr/bin/env python3
"""A0 EVIDENCE_REQUIRED: one full cycle — build -> candidate -> tests -> review
-> approval -> promotion -> rollback — on a toy mission, tracing the SAME
candidate_tree hash end to end. Prints a step-by-step trace and writes a JSON
evidence file. The rollback command is actually executed once, live, as proof
(not just written).

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

from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder  # noqa: E402
from joao_orchestrator.bubble.promotion import promote, rollback  # noqa: E402


class ToyReviewer:
    provider = "toy-e2e-reviewer"
    model = "deterministic-fixture"

    def review_stage(self, run, _run_dir, stage, active_rules=""):
        # The "plan" stage runs before any candidate exists — nothing to bind to yet.
        candidate_tree = run.get("candidate_tree") if stage != "plan" else None
        return {
            "ok": True, "decision": "pass", "stage": stage,
            "proof": {
                "candidate_tree": candidate_tree, "verdict": "ACCEPT",
                "findings": [],
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

        rt = RunRuntime(state_root, builder=SandboxBuilder(toy_builder), reviewer=ToyReviewer(),
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

        manifest = promote(workspace, run_dir, candidate, run_id)
        trace["promotion_manifest"] = manifest
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
        greeting_after_promotion = (workspace / "greeting.py").read_text()
        trace["greeting_after_promotion"] = greeting_after_promotion
        print(f"[5/7] worktree reflects the promotion: {greeting_after_promotion.strip()!r}")

        rollback_result = rollback(workspace, manifest)
        trace["rollback_result"] = rollback_result
        branch_tip_after_rollback = _git(["rev-parse", "main"], workspace).stdout.strip()
        trace["branch_tip_after_rollback"] = branch_tip_after_rollback
        rollback_restored_base = branch_tip_after_rollback == base_commit == manifest["previous_tip"]
        trace["rollback_restored_base"] = rollback_restored_base
        print(f"[6/7] rollback EXECUTED — branch main restored to base commit: {rollback_restored_base}")
        assert rollback_restored_base

        greeting_after_rollback = (workspace / "greeting.py").read_text()
        trace["greeting_after_rollback"] = greeting_after_rollback
        print(f"[7/7] worktree reflects the rollback: {greeting_after_rollback.strip()!r}")

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
