#!/usr/bin/env python3
"""Diagnostic variant of `c8b_synthetic_normal_mission.py`: isolates whether
the REAL CodexCLIReviewer dispatch (through the full orchestrator pipeline)
works independently of GLMBuilder's real dispatch, by pairing a local
deterministic builder (performing the EXACT same file edit GLMBuilder's
mission would have produced) with the REAL CodexCLIReviewer.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.joao_orchestrator.bubble.orchestrator import run_c8b_mission  # noqa: E402
from src.joao_orchestrator.bubble.runtime import CodexCLIReviewer, RunRuntime, SandboxBuilder  # noqa: E402


def _repo_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True,
                          text=True, check=True).stdout.strip()


def build_workspace(root: Path) -> Path:
    workspace = root / "synthetic-mission-workspace"
    workspace.mkdir()
    run = lambda argv: subprocess.run(argv, cwd=str(workspace), check=True, capture_output=True, text=True)
    run(["git", "init", "-q"])
    run(["git", "config", "user.email", "synthetic-mission@joao.invalid"])
    run(["git", "config", "user.name", "JOAO C8-B synthetic mission"])
    (workspace / "module.py").write_text("VALUE = 1\n")
    (workspace / "test_module.py").write_text("from module import VALUE\nassert VALUE == 2\n")
    (workspace / ".joao-profile.json").write_text(json.dumps({
        "project_id": "c8b-synthetic-normal", "display_name": "C8-B synthetic normal mission",
        "repository_root": str(workspace), "allowed_write_paths": ["module.py"], "forbidden_paths": []}))
    run(["git", "add", "."])
    run(["git", "commit", "-qm", "synthetic mission baseline"])
    return workspace


def deterministic_build(_mission, workspace, _correction):
    (workspace / "module.py").write_text("VALUE = 2\n")
    return {"ok": True, "provider": "sandbox", "model": "deterministic-fixture"}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="joao-c8b-synthetic-codexonly-"))
    workspace = build_workspace(tmp)
    state_root = tmp / "state"

    reviewer = CodexCLIReviewer()
    if not reviewer.available():
        print(json.dumps({"ok": False, "reason": "CodexCLIReviewer.available() is False"}))
        return 1

    runtime = RunRuntime(state_root, builder=SandboxBuilder(deterministic_build), reviewer=reviewer)

    head = _repo_head()
    authority_hash = hashlib.sha256(b"c8b-synthetic-codex-only-diagnostic").hexdigest()

    mission = (
        "PROPOSED PLAN (not yet executed — you are reviewing whether this plan is "
        "reasonable and safe to attempt, not whether it has already been done): "
        "the builder will change the VALUE constant in module.py from 1 to 2, and "
        "touch no other file, so that test_module.py's test_value() passes. This is "
        "a bounded, single-line, low-risk change with an existing test asserting the "
        "desired end state. Approve this plan (ACCEPT) if the proposed approach is "
        "sound; do not evaluate the current unbuilt worktree as if the change should "
        "already be present in it."
    )

    result = run_c8b_mission(
        runtime=runtime, project_id="c8b-synthetic-normal", workspace=workspace, mission=mission,
        targeted_tests=[], full_tests=[[sys.executable, "test_module.py"]],
        risk_tier="normal", canary_required=False,
        spec_sha=head, roadmap_sha=head, authority_instruction_hash=authority_hash,
        forbidden_paths=[], criterion_bindings={},
    )

    folder = state_root / "runs" / result["run_id"]
    run_json = json.loads((folder / "run.json").read_text()) if (folder / "run.json").exists() else {}
    review_evidence = json.loads((folder / "review-evidence.json").read_text()) if (folder / "review-evidence.json").exists() else {}

    candidate = run_json.get("candidate") or {}
    freeze_tree = candidate.get("candidate_tree")
    finish_tree = result.get("candidate_tree")
    review_proof_tree = (review_evidence.get("proof") or {}).get("candidate_tree")

    proof = {
        "REAL_CODEX_REVIEWER_DISPATCH": review_evidence.get("stage") == "final" and "returncode" in review_evidence,
        "CODEX_RETURNCODE": review_evidence.get("returncode"),
        "CODEX_VERDICT": review_evidence.get("verdict"),
        "SAME_CANDIDATE_TREE_FROM_FREEZE_TO_FINISH": bool(freeze_tree) and freeze_tree == finish_tree == review_proof_tree,
        "BOSS_APPROVAL_FABRICATED": (folder / "approval-record.json").exists(),
        "PROMOTION_EXECUTED": False,
        "run_id": result["run_id"],
        "eligibility_ok": result["ok"],
        "eligibility_reason_code": result["reason_code"],
        "candidate_tree": finish_tree,
        "candidate_commit": candidate.get("candidate_commit"),
        "run_status": run_json.get("status"),
        "state_root": str(state_root),
    }
    print(json.dumps(proof, indent=2, sort_keys=True))
    (tmp / "synthetic-mission-proof.json").write_text(json.dumps(proof, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
