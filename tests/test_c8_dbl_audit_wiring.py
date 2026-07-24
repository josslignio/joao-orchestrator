"""C8-B: G-DBL-AUDIT wired against the REAL adapter identities
(`GLMBuilder`, `CodexCLIReviewer`, `GLMReviewer`, `GPTFormalEvidenceReviewer`)
rather than hand-built provider/provider_family strings — proves the actual
class attributes feed the gate correctly, per `JOAO_WORKER_INTEGRATION_SPEC.md`
§5's authoritative identity table:

    GLM / Z.AI          -> provider_family = zai
    Codex               -> provider_family = openai
    GPT (contre-audit)  -> provider_family = openai   # SAME family as Codex
    Claude              -> provider_family = anthropic
"""
from __future__ import annotations

from pathlib import Path

from src.joao_orchestrator.bubble import gates
from src.joao_orchestrator.bubble.orchestrator import run_c8b_mission
from src.joao_orchestrator.bubble.runtime import (
    CodexCLIReviewer, GLMBuilder, GLMReviewer, GPTFormalEvidenceReviewer, RunRuntime, SandboxBuilder,
)

TREE = "a" * 40


def _verdict(cls, ok=True, decision="pass", tree=TREE):
    return {"provider": cls.provider, "provider_family": cls.provider_family, "model": cls.model,
            "ok": ok, "decision": decision, "candidate_tree": tree}


def test_glm_builder_codex_reviewer_normal_tier_passes():
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict(CodexCLIReviewer)], candidate_tree=TREE)
    assert result["ok"] is True and result["reason_code"] == "G_DBL_AUDIT_OK"


def test_glm_builder_glm_reviewer_self_review_blocks():
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict(GLMReviewer)], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_BUILDER_SELF_REVIEW"


def test_glm_critical_codex_plus_gpt_same_family_blocks():
    # Codex and GPT-formal are BOTH provider_family="openai" — this must
    # never be accepted as two distinct families for a critical GLM build.
    result = gates.gate_dbl_audit(
        risk_tier="critical", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict(CodexCLIReviewer), _verdict(GPTFormalEvidenceReviewer)],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_SAME_FAMILY"
    assert set(result["families"]) == {"openai"}


def test_codex_provider_family_equals_gpt_formal_provider_family():
    # The exact identity claim this whole wiring depends on.
    assert CodexCLIReviewer.provider_family == GPTFormalEvidenceReviewer.provider_family == "openai"
    assert GLMBuilder.provider_family == GLMReviewer.provider_family == "zai"


def _sandbox(tmp_path: Path):
    import json
    import subprocess
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
    """Minimal ReviewerAdapter stand-in carrying real identity attributes
    from a real class, but a scripted verdict — used to prove the
    ORCHESTRATOR wires gate_dbl_audit correctly end-to-end without a live
    network dispatch."""
    def __init__(self, identity_cls, ok=True, decision="pass"):
        self.provider = identity_cls.provider
        self.model = identity_cls.model
        self.provider_family = identity_cls.provider_family
        self.ok = ok
        self.decision = decision

    def review_stage(self, run, run_dir, stage, active_rules=""):
        return {"ok": self.ok, "decision": self.decision,
                "proof": {"candidate_tree": run.get("candidate_tree"), "verdict": "ACCEPT" if self.ok else "BLOCK",
                         "findings": [], "reviewer": {"provider": self.provider, "model": self.model}}}

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


def test_dynamic_invocation_proof_gate_dbl_audit_is_actually_called_by_orchestrator(tmp_path, monkeypatch, allow_test_write_tier):
    """G-AUTH-IO dynamic proof: gate_dbl_audit is not just referenced by
    orchestrator.py — it is genuinely CALLED, with the real builder/reviewer
    identities, during a real run_c8b_mission() invocation."""
    calls = []
    real_gate_dbl_audit = gates.gate_dbl_audit

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real_gate_dbl_audit(*args, **kwargs)

    import src.joao_orchestrator.bubble.orchestrator as orchestrator_mod
    monkeypatch.setattr(orchestrator_mod.gates_mod, "gate_dbl_audit", spy)

    work = _sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(build),
                         reviewer=_FakeReviewer(CodexCLIReviewer))
    result = run_c8b_mission(
        runtime=runtime, project_id="fixture", workspace=work, mission="fix",
        targeted_tests=[], full_tests=[["python3", "test_module.py"]],
        risk_tier="normal", canary_required=False, spec_sha="s" * 40, roadmap_sha="r" * 40,
        authority_instruction_hash="h" * 64, forbidden_paths=[], criterion_bindings={})

    assert len(calls) == 1, "gate_dbl_audit must be called exactly once by the orchestrator"
    assert calls[0]["builder_provider"] == "sandbox"
    assert result["ok"] is True
