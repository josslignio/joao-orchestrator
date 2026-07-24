"""C8-B: `bubble/orchestrator.run_c8b_mission` — sequences the existing
RunRuntime, wires a second reviewer for critical tier, produces the
mechanical `c8b-eligibility.json` artefact, and NEVER calls approve()/
promote() (`JOAO_C8_GATES_ROADMAP.md` LOT C8-B item 3).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.joao_orchestrator.bubble.orchestrator import run_c8b_mission
from src.joao_orchestrator.bubble.runtime import CodexCLIReviewer, GLMBuilder, GLMReviewer, RunRuntime, SandboxBuilder

TREE = "a" * 40
_AUTHORITY = dict(spec_sha="s" * 40, roadmap_sha="r" * 40, authority_instruction_hash="h" * 64,
                  forbidden_paths=[], criterion_bindings={})


def _sandbox(tmp_path: Path) -> Path:
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    for argv in (["git", "init", "-q"], ["git", "config", "user.email", "fixture@example.invalid"],
                ["git", "config", "user.name", "fixture"]):
        subprocess.run(argv, cwd=workspace, check=True)
    (workspace / "module.py").write_text("VALUE = 1\n")
    (workspace / "test_module.py").write_text("from module import VALUE\nassert VALUE == 2\n")
    (workspace / ".joao-profile.json").write_text(json.dumps({
        "project_id": "fixture", "display_name": "fixture", "repository_root": str(workspace),
        "allowed_write_paths": ["module.py"], "forbidden_paths": []}))
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
    return workspace


class _FakeReviewer:
    def __init__(self, identity_cls, ok=True, decision="pass", tree_override=None, raises=False):
        self.provider = identity_cls.provider
        self.model = identity_cls.model
        self.provider_family = identity_cls.provider_family
        self.ok = ok
        self.decision = decision
        self.tree_override = tree_override
        self.raises = raises
        self.calls = 0

    def review_stage(self, run, run_dir, stage, active_rules=""):
        self.calls += 1
        if self.raises:
            raise RuntimeError("simulated reviewer crash")
        tree = self.tree_override if self.tree_override is not None else run.get("candidate_tree")
        return {"ok": self.ok, "decision": self.decision,
                "proof": {"candidate_tree": tree, "verdict": "ACCEPT" if self.ok else "BLOCK",
                         "findings": [], "reviewer": {"provider": self.provider, "model": self.model}}}

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


def _passing_build(_, workspace, __):
    (workspace / "module.py").write_text("VALUE = 2\n")
    return {"ok": True}


def _failing_build(_, workspace, __):
    (workspace / "secret.txt").write_text("no")
    return {"ok": True}


def test_missing_authority_blocks():
    runtime = RunRuntime(Path("/tmp/unused"), builder=SandboxBuilder(_passing_build))
    result = run_c8b_mission(runtime=runtime, project_id="p", workspace=Path("/tmp"), mission="m",
                             targeted_tests=[], full_tests=[["true"]], risk_tier="normal",
                             canary_required=False, spec_sha="", roadmap_sha="r" * 40,
                             authority_instruction_hash="h" * 64, forbidden_paths=[], criterion_bindings={})
    assert result["ok"] is False and result["reason_code"] == "C8B_MISSING_AUTHORITY"


def test_invalid_risk_tier_blocks():
    runtime = RunRuntime(Path("/tmp/unused"), builder=SandboxBuilder(_passing_build))
    result = run_c8b_mission(runtime=runtime, project_id="p", workspace=Path("/tmp"), mission="m",
                             targeted_tests=[], full_tests=[["true"]], risk_tier="urgent",
                             canary_required=False, **_AUTHORITY)
    assert result["ok"] is False and result["reason_code"] == "C8B_INVALID_RISK_TIER"


def test_critical_without_second_reviewer_blocks():
    runtime = RunRuntime(Path("/tmp/unused"), builder=SandboxBuilder(_passing_build))
    result = run_c8b_mission(runtime=runtime, project_id="p", workspace=Path("/tmp"), mission="m",
                             targeted_tests=[], full_tests=[["true"]], risk_tier="critical",
                             canary_required=False, **_AUTHORITY)
    assert result["ok"] is False and result["reason_code"] == "C8B_MISSING_SECOND_REVIEWER"


def test_normal_tier_glm_builder_shape_codex_reviewer_happy_path(tmp_path, allow_test_write_tier):
    # Uses SandboxBuilder to stand in for "a GLM-shaped builder" (same
    # provider_family wiring, no live network) — the REAL GLMBuilder is
    # exercised for real in the required synthetic mission script.
    work = _sandbox(tmp_path)
    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(_passing_build),
                         reviewer=_FakeReviewer(CodexCLIReviewer))
    result = run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                             targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                             risk_tier="normal", canary_required=False, **_AUTHORITY)
    assert result["ok"] is True
    assert result["reason_code"] == "G_DBL_AUDIT_OK"
    assert result["candidate_tree"]
    assert result["boss_approval_status"] == "not_requested"
    assert result["promotion_status"] == "not_attempted"

    folder = tmp_path / "state" / "runs" / result["run_id"]
    eligibility_on_disk = json.loads((folder / "c8b-eligibility.json").read_text())
    assert eligibility_on_disk == result

    run_on_disk = json.loads((folder / "run.json").read_text())
    assert run_on_disk["status"] == "needs_approval"
    # never fabricated approval/promotion
    assert not (folder / "approval-record.json").exists()


def test_real_reviewer_plan_stage_block_is_recorded_not_a_crash(tmp_path):
    """Regression: a PRIMARY reviewer implementing `review_stage` (like the
    real `CodexCLIReviewer`/`GLMReviewer`) can legitimately BLOCK the
    pre-build "plan" stage — `RunRuntime.run_once()` must record that as a
    clean `blocked` status, never raise `RuntimeStateError("invalid
    transition ready -> blocked")`. Found via the real synthetic NORMAL
    mission (`scripts/c8b_synthetic_normal_mission.py`), where Codex's real
    plan review returned a negative verdict."""
    work = _sandbox(tmp_path)
    blocking_plan_reviewer = _FakeReviewer(CodexCLIReviewer, ok=False, decision="block")
    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(_passing_build),
                         reviewer=blocking_plan_reviewer)
    result = run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                             targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                             risk_tier="normal", canary_required=False, **_AUTHORITY)
    assert result["ok"] is False
    assert result["reason_code"] == "C8B_PIPELINE_NOT_ELIGIBLE"
    assert result["status"] == "blocked"


def test_builder_failure_never_reaches_eligibility_pass(tmp_path):
    work = _sandbox(tmp_path)
    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(_failing_build),
                         reviewer=_FakeReviewer(CodexCLIReviewer))
    result = run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                             targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                             risk_tier="normal", canary_required=False, **_AUTHORITY)
    assert result["ok"] is False
    assert result["reason_code"] == "C8B_PIPELINE_NOT_ELIGIBLE"
    assert result["status"] == "blocked"


def test_critical_tier_second_reviewer_same_family_blocks(tmp_path, allow_test_write_tier):
    from src.joao_orchestrator.bubble.runtime import GPTFormalEvidenceReviewer
    work = _sandbox(tmp_path)
    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(_passing_build),
                         reviewer=_FakeReviewer(CodexCLIReviewer))
    second = _FakeReviewer(GPTFormalEvidenceReviewer)
    result = run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                             targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                             risk_tier="critical", canary_required=False, second_reviewer=second,
                             **_AUTHORITY)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_SAME_FAMILY"
    assert second.calls == 1


def test_second_reviewer_wrong_tree_blocks(tmp_path, allow_test_write_tier):
    work = _sandbox(tmp_path)
    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(_passing_build),
                         reviewer=_FakeReviewer(CodexCLIReviewer))
    second = _FakeReviewer(GLMReviewer, tree_override="f" * 40)  # wrong family too, but tree-mismatch fires first
    result = run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                             targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                             risk_tier="critical", canary_required=False, second_reviewer=second,
                             **_AUTHORITY)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_TREE_MISMATCH"


def test_second_reviewer_raising_is_caught_and_blocks(tmp_path):
    work = _sandbox(tmp_path)
    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(_passing_build),
                         reviewer=_FakeReviewer(CodexCLIReviewer))
    second = _FakeReviewer(GLMReviewer, raises=True)
    result = run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                             targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                             risk_tier="critical", canary_required=False, second_reviewer=second,
                             **_AUTHORITY)
    assert result["ok"] is False  # a raising reviewer never strands the mission or crashes the orchestrator


def test_candidate_mutation_during_second_review_blocks(tmp_path, allow_test_write_tier):
    """Adversarial proof: tamper the frozen candidate's readonly_copy between
    freeze and the SECOND reviewer's dispatch — the second reviewer's own
    pre-tree recompute (identical discipline to CodexCLIReviewer) must catch
    it, exactly like a tamper during the PRIMARY review already does."""
    work = _sandbox(tmp_path)

    class _TamperingReviewer(_FakeReviewer):
        def review_stage(self, run, run_dir, stage, active_rules=""):
            self.calls += 1
            candidate = run.get("candidate")
            if candidate and stage != "plan":
                # Mutate the read-only candidate copy directly on disk —
                # simulates an out-of-band tamper between freeze and review.
                readonly = Path(candidate["readonly_copy"])
                target = readonly / "module.py"
                import os
                import stat
                mode = target.stat().st_mode
                target.chmod(mode | stat.S_IWUSR)
                target.write_text("VALUE = 999\n")
            from src.joao_orchestrator.bubble.candidate import recompute_candidate_tree
            pre_tree = recompute_candidate_tree(Path(candidate["readonly_copy"]))
            if pre_tree != candidate["candidate_tree"]:
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": "candidate was tampered before/during this reviewer's own inspection",
                        "expected_candidate_tree": candidate["candidate_tree"],
                        "recomputed_tree_before_review": pre_tree}
            return super().review_stage(run, run_dir, stage, active_rules=active_rules)

    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(_passing_build),
                         reviewer=_FakeReviewer(CodexCLIReviewer))
    second = _TamperingReviewer(GLMReviewer)
    result = run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                             targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                             risk_tier="critical", canary_required=False, second_reviewer=second,
                             **_AUTHORITY)
    assert result["ok"] is False
    tampered_verdict = result["reviewer_verdicts"][1]
    assert tampered_verdict["ok"] is False


def test_dynamic_invocation_proof_orchestrator_run_c8b_mission_is_invoked(tmp_path, monkeypatch):
    """G-AUTH-IO dynamic proof: `run_c8b_mission` itself is genuinely called
    (not merely referenced) — spies on RunRuntime.run_once, which only ever
    executes as part of a real run_c8b_mission() call."""
    calls = []
    real_run_once = RunRuntime.run_once

    def spy(self, run_id):
        calls.append(run_id)
        return real_run_once(self, run_id)

    monkeypatch.setattr(RunRuntime, "run_once", spy)
    work = _sandbox(tmp_path)
    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(_passing_build),
                         reviewer=_FakeReviewer(CodexCLIReviewer))
    run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                    targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                    risk_tier="normal", canary_required=False, **_AUTHORITY)
    assert len(calls) == 1
