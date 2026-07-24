"""C8-B: `GLMReviewer` — mirrors `CodexCLIReviewer`'s candidate-binding
discipline, dispatch target substituted (`JOAO_WORKER_INTEGRATION_SPEC.md`
§2.1). No live network dispatch here — a fake `ExecutionBackend` stands in
for `opencode`/`joao-glm`; the required REAL synthetic mission exercises the
live binary separately.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.joao_orchestrator.bubble.candidate import freeze_candidate
from src.joao_orchestrator.bubble.runtime import GLMReviewer, _extract_codex_final_answer, _extract_glm_final_answer

TREE_PLACEHOLDER = "a" * 40


def _ndjson_events(*texts: str) -> str:
    lines = [json.dumps({"type": "step_start"})]
    for text in texts:
        lines.append(json.dumps({"type": "text", "part": {"text": text}}))
    lines.append(json.dumps({"type": "step_finish"}))
    return "\n".join(lines) + "\n"


def test_extract_glm_final_answer_picks_the_last_text_event():
    ndjson = _ndjson_events("I'll investigate first.", '{"candidate_tree": "x", "verdict": "ACCEPT", "findings": [], "reviewer": {"provider": "zai-coding-plan", "model": "m"}}')
    answer = _extract_glm_final_answer(ndjson)
    assert json.loads(answer)["verdict"] == "ACCEPT"


def test_extract_glm_final_answer_empty_when_no_text_events():
    ndjson = "\n".join([json.dumps({"type": "tool_use"}), json.dumps({"type": "step_finish"})])
    assert _extract_glm_final_answer(ndjson) == ""


def test_extract_glm_final_answer_skips_unparseable_lines():
    ndjson = "not json\n" + json.dumps({"type": "text", "part": {"text": "real answer"}})
    assert _extract_glm_final_answer(ndjson) == "real answer"


def test_extract_codex_final_answer_prefers_direct_json_for_backward_compat():
    # Every existing A0/A0.1/A0.2 fake `codex` wrapper prints exactly one
    # bare JSON line — that shape must keep parsing unchanged.
    bare = json.dumps({"candidate_tree": "x", "verdict": "ACCEPT", "findings": [],
                       "reviewer": {"provider": "codex-subscription", "model": "m"}})
    assert _extract_codex_final_answer(bare) == bare.strip()


def test_extract_codex_final_answer_falls_back_to_ndjson_agent_message():
    # Real `codex exec --json` output: NDJSON events, verdict nested inside
    # the LAST `item.completed`/`agent_message` event's `text` field.
    verdict = json.dumps({"candidate_tree": "y", "verdict": "P1", "findings": ["repair X"],
                          "reviewer": {"provider": "codex-subscription", "model": "GPT-5"}})
    ndjson = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "item.started", "item": {"id": "i0", "type": "command_execution"}}),
        json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "command_execution",
                                                        "aggregated_output": "some tool output"}}),
        json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": verdict}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100}}),
    ])
    assert _extract_codex_final_answer(ndjson) == verdict


def test_extract_codex_final_answer_returns_input_unchanged_when_neither_shape_matches():
    garbage = "not json and not ndjson either"
    assert _extract_codex_final_answer(garbage) == garbage


class _FakeBackend:
    """Stands in for ExecutionBackend — writes a scripted NDJSON transcript
    to the `--output` path instead of dispatching a real subprocess."""
    def __init__(self, answer_json: dict | str, pid: int = 4242, returncode: int = 0):
        self.answer_json = answer_json
        self.pid = pid
        self.returncode = returncode

    def execute(self, argv, *, cwd, timeout, network=False, environment_allowlist=None,
               protected=False, preserve_host_environment=False, extra_read_paths=None,
               extra_write_paths=None, auth_stage=None):
        output_path = Path(argv[argv.index("--output") + 1])
        answer = self.answer_json if isinstance(self.answer_json, str) else json.dumps(self.answer_json)
        output_path.write_text(_ndjson_events("thinking...", answer))
        return {"pid": self.pid, "returncode": self.returncode, "stdout": "", "stderr": ""}


class _UnavailableBackend:
    def execute(self, *args, **kwargs):
        return {"pid": None, "returncode": -1, "stdout": "", "stderr": "opencode not found"}


def _git_candidate(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for argv in (["git", "init", "-q"], ["git", "config", "user.email", "f@example.invalid"],
                ["git", "config", "user.name", "f"]):
        subprocess.run(argv, cwd=workspace, check=True)
    (workspace / "a.py").write_text("X = 1\n")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
    (workspace / "a.py").write_text("X = 2\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    candidate = freeze_candidate(workspace, run_dir, "run-1", 1)
    return workspace, run_dir, candidate


def test_review_stage_final_accepts_with_matching_tree(tmp_path):
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    tree = candidate["candidate_tree"]
    backend = _FakeBackend({"candidate_tree": tree, "verdict": "ACCEPT", "findings": [],
                            "reviewer": {"provider": "zai-coding-plan", "model": "m"}})
    reviewer = GLMReviewer(backend=backend)
    run = {"mission": "fix it", "candidate": candidate, "candidate_tree": tree}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is True
    assert result["proof"]["candidate_tree"] == tree


def test_review_stage_missing_candidate_blocks(tmp_path):
    reviewer = GLMReviewer(backend=_FakeBackend({}))
    result = reviewer.review_stage({"mission": "x", "candidate": None}, tmp_path, "final")
    assert result["ok"] is False and result["decision"] == "block"


def test_review_stage_unavailable_dispatch_blocks(tmp_path):
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    reviewer = GLMReviewer(backend=_UnavailableBackend())
    run = {"mission": "x", "candidate": candidate, "candidate_tree": candidate["candidate_tree"]}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False
    assert "unavailable" in result["reason"]


def test_review_stage_tampered_before_review_blocks(tmp_path):
    import os
    import stat
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    # Mutate the frozen read-only candidate copy directly on disk BEFORE the
    # reviewer ever dispatches — the pre-recompute must catch this even
    # though `candidate["candidate_tree"]` (the frozen value) is untouched.
    readonly = Path(candidate["readonly_copy"])
    target = readonly / "a.py"
    target.chmod(target.stat().st_mode | stat.S_IWUSR)
    target.write_text("X = 999\n")
    reviewer = GLMReviewer(backend=_FakeBackend({}))
    run = {"mission": "x", "candidate": candidate, "candidate_tree": candidate["candidate_tree"]}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False
    assert "tampered before" in result["reason"]


def test_review_stage_wrong_verdict_tree_blocks(tmp_path):
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    tree = candidate["candidate_tree"]
    backend = _FakeBackend({"candidate_tree": "b" * 40, "verdict": "ACCEPT", "findings": [],
                            "reviewer": {"provider": "zai-coding-plan", "model": "m"}})
    reviewer = GLMReviewer(backend=backend)
    run = {"mission": "x", "candidate": candidate, "candidate_tree": tree}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False


def test_review_stage_malformed_answer_blocks(tmp_path):
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    reviewer = GLMReviewer(backend=_FakeBackend("prose, not json"))
    run = {"mission": "x", "candidate": candidate, "candidate_tree": candidate["candidate_tree"]}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False and result["schema_valid"] is False


def test_dynamic_invocation_proof_glm_reviewer_review_stage_is_invoked(tmp_path, monkeypatch, allow_test_write_tier):
    """G-AUTH-IO dynamic proof: GLMReviewer.review_stage is genuinely called
    by the orchestrator (as the critical-tier second reviewer), not merely
    referenced by test code."""
    from src.joao_orchestrator.bubble.orchestrator import run_c8b_mission
    from src.joao_orchestrator.bubble.runtime import CodexCLIReviewer, RunRuntime, SandboxBuilder

    calls = []
    real_review_stage = GLMReviewer.review_stage

    def spy(self, run, run_dir, stage, active_rules=""):
        calls.append(stage)
        return real_review_stage(self, run, run_dir, stage, active_rules=active_rules)

    monkeypatch.setattr(GLMReviewer, "review_stage", spy)

    work = tmp_path / "fixture"
    work.mkdir()
    for argv in (["git", "init", "-q"], ["git", "config", "user.email", "fixture@example.invalid"],
                ["git", "config", "user.name", "fixture"]):
        subprocess.run(argv, cwd=work, check=True)
    (work / "module.py").write_text("VALUE = 1\n")
    (work / "test_module.py").write_text("from module import VALUE\nassert VALUE == 2\n")
    (work / ".joao-profile.json").write_text(json.dumps({
        "project_id": "fixture", "display_name": "fixture", "repository_root": str(work),
        "allowed_write_paths": ["module.py"], "forbidden_paths": []}))
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=work, check=True)

    class _AcceptedCodex:
        provider = CodexCLIReviewer.provider
        model = CodexCLIReviewer.model
        provider_family = CodexCLIReviewer.provider_family

        def review_stage(self, run, run_dir, stage, active_rules=""):
            return {"ok": True, "decision": "pass",
                    "proof": {"candidate_tree": run.get("candidate_tree"), "verdict": "ACCEPT",
                             "findings": [], "reviewer": {"provider": self.provider, "model": self.model}}}

        def review(self, run, run_dir):
            return self.review_stage(run, run_dir, "final")

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    def glm_backend_factory():
        return None  # unused; GLMReviewer default backend, but dispatch is monkeypatched entirely below

    class _FakeGLMBackend:
        def execute(self, argv, *, cwd, timeout, network=False, environment_allowlist=None,
                   protected=False, preserve_host_environment=False, extra_read_paths=None,
                   extra_write_paths=None, auth_stage=None):
            output_path = Path(argv[argv.index("--output") + 1])
            output_path.write_text(_ndjson_events(json.dumps({
                "candidate_tree": None, "verdict": "ACCEPT", "findings": [],
                "reviewer": {"provider": "zai-coding-plan", "model": GLMReviewer.model}})))
            return {"pid": 1, "returncode": 0, "stdout": "", "stderr": ""}

    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(build), reviewer=_AcceptedCodex())
    glm_reviewer = GLMReviewer(backend=_FakeGLMBackend())
    run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                    targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                    risk_tier="critical", canary_required=False, second_reviewer=glm_reviewer,
                    spec_sha="s" * 40, roadmap_sha="r" * 40, authority_instruction_hash="h" * 64,
                    forbidden_paths=[], criterion_bindings={})
    assert "final" in calls
