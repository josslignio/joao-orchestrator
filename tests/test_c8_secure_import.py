"""C8-B: secure GPT formal-review evidence import — nonce, exact
candidate_tree, controller-owned inbox, single-use, replay rejection
(pre-C8-B correction #2, `JOAO_WORKER_INTEGRATION_SPEC.md` §5).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.joao_orchestrator.bubble import secure_import
from src.joao_orchestrator.bubble.runtime import GPTFormalEvidenceReviewer

TREE = "a" * 40
NOW = "2026-07-20T12:00:00Z"
LATER = "2099-07-20T12:05:00Z"
EXPIRED = "2026-07-20T11:00:00Z"


def _accept_payload(tree=TREE, provider="openai-gpt", model="gpt-5.6-thinking"):
    return json.dumps({"candidate_tree": tree, "verdict": "ACCEPT", "findings": [],
                       "reviewer": {"provider": provider, "model": model}})


def _envelope(nonce, payload):
    return json.dumps({"nonce": nonce, "verdict_payload": payload})


def _issue(inbox_dir, **overrides):
    kwargs = dict(run_id="run-1", mission_id="mission-1", candidate_tree=TREE,
                 expected_reviewer_provider="openai-gpt", issued_at=NOW, expires_at=LATER)
    kwargs.update(overrides)
    return secure_import.issue_challenge(inbox_dir, **kwargs)


def test_valid_single_use_import_accepts(tmp_path):
    inbox = tmp_path / "inbox"
    challenge = _issue(inbox)
    envelope = _envelope(challenge["nonce"], _accept_payload())
    result = secure_import.consume_import(inbox, envelope, run_id="run-1", mission_id="mission-1",
                                          candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert result["ok"] is True
    assert result["nonce_verified"] is True
    assert result["proof"]["reviewer"]["provider"] == "openai-gpt"


def test_replay_of_a_consumed_nonce_blocks(tmp_path):
    inbox = tmp_path / "inbox"
    challenge = _issue(inbox)
    envelope = _envelope(challenge["nonce"], _accept_payload())
    first = secure_import.consume_import(inbox, envelope, run_id="run-1", mission_id="mission-1",
                                         candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                         expected_model="gpt-5.6-thinking", now=NOW)
    assert first["ok"] is True
    replay = secure_import.consume_import(inbox, envelope, run_id="run-1", mission_id="mission-1",
                                          candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert replay["ok"] is False
    assert replay["reason_code"] == "SECURE_IMPORT_REPLAYED"


def test_missing_nonce_blocks(tmp_path):
    inbox = tmp_path / "inbox"
    _issue(inbox)
    envelope = json.dumps({"verdict_payload": _accept_payload()})
    result = secure_import.consume_import(inbox, envelope, run_id="run-1", mission_id="mission-1",
                                          candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert result["ok"] is False and result["reason_code"] == "SECURE_IMPORT_MISSING_NONCE"


def test_unknown_nonce_blocks(tmp_path):
    inbox = tmp_path / "inbox"
    _issue(inbox)
    envelope = _envelope("f" * 64, _accept_payload())
    result = secure_import.consume_import(inbox, envelope, run_id="run-1", mission_id="mission-1",
                                          candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert result["ok"] is False and result["reason_code"] == "SECURE_IMPORT_UNKNOWN_NONCE"


def test_path_traversal_nonce_blocks(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    envelope = _envelope("../../../etc/passwd", _accept_payload())
    result = secure_import.consume_import(inbox, envelope, run_id="run-1", mission_id="mission-1",
                                          candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert result["ok"] is False and result["reason_code"] == "SECURE_IMPORT_INVALID_NONCE"


def test_expired_challenge_blocks(tmp_path):
    inbox = tmp_path / "inbox"
    challenge = _issue(inbox, expires_at=EXPIRED)
    envelope = _envelope(challenge["nonce"], _accept_payload())
    result = secure_import.consume_import(inbox, envelope, run_id="run-1", mission_id="mission-1",
                                          candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert result["ok"] is False and result["reason_code"] == "SECURE_IMPORT_EXPIRED"


def test_wrong_run_blocks(tmp_path):
    inbox = tmp_path / "inbox"
    challenge = _issue(inbox)
    envelope = _envelope(challenge["nonce"], _accept_payload())
    result = secure_import.consume_import(inbox, envelope, run_id="run-DIFFERENT", mission_id="mission-1",
                                          candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert result["ok"] is False and result["reason_code"] == "SECURE_IMPORT_WRONG_RUN"


def test_wrong_tree_blocks(tmp_path):
    inbox = tmp_path / "inbox"
    challenge = _issue(inbox)
    envelope = _envelope(challenge["nonce"], _accept_payload())
    result = secure_import.consume_import(inbox, envelope, run_id="run-1", mission_id="mission-1",
                                          candidate_tree="b" * 40, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert result["ok"] is False and result["reason_code"] == "SECURE_IMPORT_WRONG_TREE"


def test_payload_bound_to_different_tree_than_challenge_blocks(tmp_path):
    # Envelope claims the RIGHT candidate_tree at the challenge level, but
    # the embedded verdict payload itself is bound to a different tree —
    # the underlying validate_reviewer_verdict catch must still fire.
    inbox = tmp_path / "inbox"
    challenge = _issue(inbox)
    envelope = _envelope(challenge["nonce"], _accept_payload(tree="c" * 40))
    result = secure_import.consume_import(inbox, envelope, run_id="run-1", mission_id="mission-1",
                                          candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert result["ok"] is False
    assert result["nonce_verified"] is True  # nonce itself was legitimate; the payload inside was not


def test_wrong_reviewer_blocks(tmp_path):
    inbox = tmp_path / "inbox"
    challenge = _issue(inbox, expected_reviewer_provider="codex-subscription")
    envelope = _envelope(challenge["nonce"], _accept_payload())
    result = secure_import.consume_import(inbox, envelope, run_id="run-1", mission_id="mission-1",
                                          candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert result["ok"] is False and result["reason_code"] == "SECURE_IMPORT_WRONG_REVIEWER"


def test_malformed_envelope_json_blocks(tmp_path):
    inbox = tmp_path / "inbox"
    _issue(inbox)
    result = secure_import.consume_import(inbox, "not json", run_id="run-1", mission_id="mission-1",
                                          candidate_tree=TREE, expected_reviewer_provider="openai-gpt",
                                          expected_model="gpt-5.6-thinking", now=NOW)
    assert result["ok"] is False and result["reason_code"] == "SECURE_IMPORT_MALFORMED_ENVELOPE"


def test_gpt_formal_evidence_reviewer_no_import_yet_blocks(tmp_path):
    reviewer = GPTFormalEvidenceReviewer(inbox_dir=tmp_path / "inbox")
    result = reviewer.review({"run_id": "run-1", "project_id": "mission-1", "candidate_tree": TREE}, tmp_path)
    assert result["ok"] is False
    assert result.get("required") is True


def test_gpt_formal_evidence_reviewer_valid_import_accepts(tmp_path):
    inbox = tmp_path / "inbox"
    # mission_id == run_id (Boss directive, 2026-07-21 fix): `.review()` used
    # to derive mission_id from project_id — a prior bug, diverging from the
    # worker-host builder side, which always used run_id.
    challenge = _issue(inbox, mission_id="run-1")
    (inbox / "gpt-review-import.json").write_text(_envelope(challenge["nonce"], _accept_payload()))
    reviewer = GPTFormalEvidenceReviewer(inbox_dir=inbox)
    result = reviewer.review({"run_id": "run-1", "project_id": "fixture", "candidate_tree": TREE}, tmp_path)
    assert result["ok"] is True


def test_dynamic_invocation_proof_gpt_formal_reviewer_review_is_invoked(tmp_path, monkeypatch):
    """G-AUTH-IO dynamic proof: GPTFormalEvidenceReviewer.review is genuinely
    called by the orchestrator when used as the critical-tier second
    reviewer, not merely referenced."""
    import subprocess

    from src.joao_orchestrator.bubble.orchestrator import run_c8b_mission
    from src.joao_orchestrator.bubble.runtime import CodexCLIReviewer, RunRuntime, SandboxBuilder

    calls = []
    real_review = GPTFormalEvidenceReviewer.review

    def spy(self, run, run_dir):
        calls.append(run.get("run_id"))
        return real_review(self, run, run_dir)

    monkeypatch.setattr(GPTFormalEvidenceReviewer, "review", spy)

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

    runtime = RunRuntime(tmp_path / "state", builder=SandboxBuilder(build), reviewer=_AcceptedCodex())
    gpt_reviewer = GPTFormalEvidenceReviewer(inbox_dir=tmp_path / "gpt-inbox")
    run_c8b_mission(runtime=runtime, project_id="fixture", workspace=work, mission="fix",
                    targeted_tests=[], full_tests=[["python3", "test_module.py"]],
                    risk_tier="critical", canary_required=False, second_reviewer=gpt_reviewer,
                    spec_sha="s" * 40, roadmap_sha="r" * 40, authority_instruction_hash="h" * 64,
                    forbidden_paths=[], criterion_bindings={})
    assert len(calls) == 1
