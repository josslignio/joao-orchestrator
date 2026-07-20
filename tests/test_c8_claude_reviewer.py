"""C8-B: `ClaudeCLIReviewer` — mirrors `CodexCLIReviewer`/`GLMReviewer`'s
candidate-binding discipline, dispatch target substituted
(`JOAO_WORKER_INTEGRATION_SPEC.md` §2.1, `provider_family="anthropic"`).
No live network dispatch here — a fake `ExecutionBackend` stands in for
`claude`; the required REAL synthetic mission exercises the live binary
separately (`scripts/c8b_mission_a_glm_to_claude.py`).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.joao_orchestrator.bubble.candidate import freeze_candidate
from src.joao_orchestrator.bubble.runtime import ClaudeCLIReviewer, _extract_claude_final_answer

TREE_PLACEHOLDER = "a" * 40


def _result_envelope(text: str) -> str:
    return json.dumps({"type": "result", "subtype": "success", "result": text, "is_error": False})


def test_extract_claude_final_answer_unwraps_result_envelope():
    verdict = json.dumps({"candidate_tree": "x", "verdict": "ACCEPT", "findings": [],
                          "reviewer": {"provider": "claude-cli", "model": "sonnet"}})
    envelope = _result_envelope(verdict)
    assert _extract_claude_final_answer(envelope) == verdict


def test_extract_claude_final_answer_returns_raw_when_not_an_envelope():
    garbage = "not json and not an envelope either"
    assert _extract_claude_final_answer(garbage) == garbage


def test_extract_claude_final_answer_returns_raw_when_result_field_missing():
    # A well-formed JSON object that is NOT the result envelope (e.g. a
    # different `--output-format json` variant, or the bare verdict itself
    # printed without --output-format at all) must never be misread as an
    # envelope with a fabricated `result` field.
    bare_verdict = json.dumps({"candidate_tree": "x", "verdict": "ACCEPT", "findings": [],
                               "reviewer": {"provider": "claude-cli", "model": "sonnet"}})
    assert _extract_claude_final_answer(bare_verdict) == bare_verdict


class _FakeBackend:
    """Stands in for ExecutionBackend — returns a scripted `--output-format
    json` envelope directly as `stdout`, instead of dispatching a real
    subprocess."""
    def __init__(self, answer_json: dict | str, pid: int = 4242, returncode: int = 0):
        self.answer_json = answer_json
        self.pid = pid
        self.returncode = returncode

    def execute(self, argv, *, cwd, timeout, network=False, environment_allowlist=None,
               protected=False, preserve_host_environment=False, extra_read_paths=None,
               extra_write_paths=None, auth_stage=None):
        answer = self.answer_json if isinstance(self.answer_json, str) else json.dumps(self.answer_json)
        return {"pid": self.pid, "returncode": self.returncode,
                "stdout": _result_envelope(answer), "stderr": ""}


class _UnavailableBackend:
    def execute(self, *args, **kwargs):
        return {"pid": None, "returncode": -1, "stdout": "", "stderr": "claude not found"}


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
                            "reviewer": {"provider": "claude-cli", "model": "sonnet"}})
    reviewer = ClaudeCLIReviewer(backend=backend)
    run = {"mission": "fix it", "candidate": candidate, "candidate_tree": tree}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is True
    assert result["proof"]["candidate_tree"] == tree
    # RI-4: reviewer/model identity in the proof is always controller-computed.
    assert result["proof"]["reviewer"] == {"provider": "claude-cli", "model": reviewer.model}


def test_review_stage_missing_candidate_blocks(tmp_path):
    reviewer = ClaudeCLIReviewer(backend=_FakeBackend({}))
    result = reviewer.review_stage({"mission": "x", "candidate": None}, tmp_path, "final")
    assert result["ok"] is False and result["decision"] == "block"


def test_review_stage_unavailable_dispatch_blocks(tmp_path):
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    reviewer = ClaudeCLIReviewer(backend=_UnavailableBackend())
    run = {"mission": "x", "candidate": candidate, "candidate_tree": candidate["candidate_tree"]}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False
    assert "unavailable" in result["reason"]


def test_review_stage_tampered_before_review_blocks(tmp_path):
    import stat
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    # Mutate the frozen read-only candidate copy directly on disk BEFORE the
    # reviewer ever dispatches — the pre-recompute must catch this even
    # though `candidate["candidate_tree"]` (the frozen value) is untouched.
    readonly = Path(candidate["readonly_copy"])
    target = readonly / "a.py"
    target.chmod(target.stat().st_mode | stat.S_IWUSR)
    target.write_text("X = 999\n")
    reviewer = ClaudeCLIReviewer(backend=_FakeBackend({}))
    run = {"mission": "x", "candidate": candidate, "candidate_tree": candidate["candidate_tree"]}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False
    assert "tampered before" in result["reason"]


def test_review_stage_wrong_verdict_tree_blocks(tmp_path):
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    tree = candidate["candidate_tree"]
    backend = _FakeBackend({"candidate_tree": "b" * 40, "verdict": "ACCEPT", "findings": [],
                            "reviewer": {"provider": "claude-cli", "model": "sonnet"}})
    reviewer = ClaudeCLIReviewer(backend=backend)
    run = {"mission": "x", "candidate": candidate, "candidate_tree": tree}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False


def test_review_stage_malformed_answer_blocks(tmp_path):
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    reviewer = ClaudeCLIReviewer(backend=_FakeBackend("prose, not json"))
    run = {"mission": "x", "candidate": candidate, "candidate_tree": candidate["candidate_tree"]}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False and result["schema_valid"] is False


def test_dynamic_invocation_proof_claude_reviewer_review_stage_is_invoked(tmp_path, monkeypatch):
    """G-AUTH-IO dynamic proof: ClaudeCLIReviewer.review_stage is genuinely
    called by the orchestrator (as the critical-tier second reviewer), not
    merely referenced by test code."""
    from src.joao_orchestrator.bubble.orchestrator import run_c8b_mission
    from src.joao_orchestrator.bubble.runtime import CodexCLIReviewer, RunRuntime, SandboxBuilder

    calls = []
    real_review_stage = ClaudeCLIReviewer.review_stage

    def spy(self, run, run_dir, stage, active_rules=""):
        calls.append(stage)
        return real_review_stage(self, run, run_dir, stage, active_rules=active_rules)

    monkeypatch.setattr(ClaudeCLIReviewer, "review_stage", spy)

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

    class _FakeClaudeBackend:
        def execute(self, argv, *, cwd, timeout, network=False, environment_allowlist=None,
                   protected=False, preserve_host_environment=False, extra_read_paths=None,
                   extra_write_paths=None, auth_stage=None):
            answer = json.dumps({"candidate_tree": None, "verdict": "ACCEPT", "findings": [],
                                 "reviewer": {"provider": "claude-cli", "model": ClaudeCLIReviewer.model}})
            return {"pid": 1, "returncode": 0, "stdout": _result_envelope(answer), "stderr": ""}

    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(build), reviewer=_AcceptedCodex())
    claude_reviewer = ClaudeCLIReviewer(backend=_FakeClaudeBackend())
    run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                    targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                    risk_tier="critical", canary_required=False, second_reviewer=claude_reviewer,
                    spec_sha="s" * 40, roadmap_sha="r" * 40, authority_instruction_hash="h" * 64,
                    forbidden_paths=[], criterion_bindings={})
    assert "final" in calls


# ---------------------------------------------------------------------------
# ADD-5 (Boss addendum, 2026-07-20): the reviewer-only host-passthrough trust
# exception — dynamic proof of each of the ten compensating factors that make
# it acceptable, exercised against real dispatch code with a fake backend.
# ---------------------------------------------------------------------------
class _RecordingBackend:
    """Captures the exact kwargs ClaudeCLIReviewer.review_stage passes to
    ExecutionBackend.execute(), so the ADD-5 dispatch-shape claims are
    verified against the real call, not merely asserted in a docstring."""
    def __init__(self, answer_json: dict | str):
        self.answer_json = answer_json
        self.calls = []

    def execute(self, argv, *, cwd, timeout, network=False, environment_allowlist=None,
               protected=False, preserve_host_environment=False, extra_read_paths=None,
               extra_write_paths=None, auth_stage=None):
        self.calls.append({"argv": argv, "preserve_host_environment": preserve_host_environment,
                           "network": network, "protected": protected})
        answer = self.answer_json if isinstance(self.answer_json, str) else json.dumps(self.answer_json)
        return {"pid": 1, "returncode": 0, "stdout": _result_envelope(answer), "stderr": ""}


def test_add5_reviewer_dispatch_uses_preserve_host_environment_and_plan_mode(tmp_path):
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    tree = candidate["candidate_tree"]
    backend = _RecordingBackend({"candidate_tree": tree, "verdict": "ACCEPT", "findings": [],
                                 "reviewer": {"provider": "claude-cli", "model": "m"}})
    reviewer = ClaudeCLIReviewer(backend=backend)
    run = {"mission": "fix it", "candidate": candidate, "candidate_tree": tree}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is True
    assert len(backend.calls) == 1
    call = backend.calls[0]
    # (1) reviewer role only — never protected=True (a builder-only concept).
    assert call["protected"] is False
    # (2) preserve_host_environment=True IS granted for this reviewer dispatch.
    assert call["preserve_host_environment"] is True
    # (3) Claude's own strongest read-only mode is what's actually requested.
    argv = call["argv"]
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "plan"
    # never --bare here (that would defeat the whole point of the real session).
    assert "--bare" not in argv


def test_add5_candidate_tree_recomputed_after_review_mutation_blocks(tmp_path):
    """(4) tree recomputed AFTER dispatch; (5) any mutation => BLOCK — here
    the fake dispatch itself mutates the candidate as a side effect,
    simulating a reviewer process (or anything else) tampering with it
    during/after inspection."""
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    tree = candidate["candidate_tree"]

    class _MutatingBackend:
        def execute(self, argv, *, cwd, timeout, network=False, environment_allowlist=None,
                   protected=False, preserve_host_environment=False, extra_read_paths=None,
                   extra_write_paths=None, auth_stage=None):
            import stat
            target = Path(cwd) / "a.py"
            target.chmod(target.stat().st_mode | stat.S_IWUSR)
            target.write_text("X = 999  # mutated during review\n")
            answer = json.dumps({"candidate_tree": tree, "verdict": "ACCEPT", "findings": [],
                                 "reviewer": {"provider": "claude-cli", "model": "m"}})
            return {"pid": 1, "returncode": 0, "stdout": _result_envelope(answer), "stderr": ""}

    reviewer = ClaudeCLIReviewer(backend=_MutatingBackend())
    run = {"mission": "x", "candidate": candidate, "candidate_tree": tree}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False
    assert "tampered during or after" in result["reason"]


def test_add5_timeout_or_non_zero_exit_blocks(tmp_path):
    """(6) timeout/process failure never masked by an otherwise well-formed
    ACCEPT payload — a non-zero returncode alone must BLOCK."""
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    tree = candidate["candidate_tree"]
    answer = json.dumps({"candidate_tree": tree, "verdict": "ACCEPT", "findings": [],
                         "reviewer": {"provider": "claude-cli", "model": "m"}})

    class _NonZeroExitBackend:
        def execute(self, argv, *, cwd, timeout, network=False, environment_allowlist=None,
                   protected=False, preserve_host_environment=False, extra_read_paths=None,
                   extra_write_paths=None, auth_stage=None):
            return {"pid": 1, "returncode": 124, "stdout": _result_envelope(answer), "stderr": "timed out"}

    reviewer = ClaudeCLIReviewer(backend=_NonZeroExitBackend())
    run = {"mission": "x", "candidate": candidate, "candidate_tree": tree}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False


def test_add5_missing_verdict_key_blocks(tmp_path):
    """(7) a well-formed JSON object with no `verdict` key is a malformed
    contract response, never a fabricated pass."""
    workspace, run_dir, candidate = _git_candidate(tmp_path)
    tree = candidate["candidate_tree"]
    answer = json.dumps({"candidate_tree": tree, "findings": [],
                         "reviewer": {"provider": "claude-cli", "model": "m"}})
    reviewer = ClaudeCLIReviewer(backend=_RecordingBackend(answer))
    run = {"mission": "x", "candidate": candidate, "candidate_tree": tree}
    result = reviewer.review_stage(run, run_dir, "final")
    assert result["ok"] is False and result["schema_valid"] is False


def test_add5_controller_owned_identity_remains_anthropic():
    assert ClaudeCLIReviewer.provider_family == "anthropic"


def test_add5_reviewer_cannot_write_approval_or_promotion():
    """(10) a reviewer can never write Boss approval or promotion evidence —
    it has no such capability at all (only `RunRuntime.approve()`/`promote()`,
    called by the controller, ever do)."""
    assert not hasattr(ClaudeCLIReviewer, "approve")
    assert not hasattr(ClaudeCLIReviewer, "promote")
