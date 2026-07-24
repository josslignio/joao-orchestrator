"""B-28 Phase 5 — GATE 5: Phase 0 kickoff is a product feature with a hard launch gate."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from joao_orchestrator.bubble.kickoff import QUESTIONS, Kickoff, KickoffError, spec_is_signed
from joao_orchestrator.bubble.runtime import (LocalProfileAdapter, RunRuntime,
                                              RuntimeStateError, SandboxBuilder)


class AcceptedStageReviewer:
    provider = "codex"; model = "fixture"
    def review_stage(self, run, run_dir, stage, active_rules=""):
        return {"ok": True, "decision": "pass", "stage": stage,
                "proof": {"verdict": "ACCEPT", "candidate_tree": run.get("candidate_tree"),
                         "findings": [], "reviewer": {"provider": self.provider, "model": self.model}}}
    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


def _answer_all(k: Kickoff, project: str):
    # one question at a time, in order — the interview is progressive
    asked = []
    while True:
        q = k.current_question(project)
        if q is None:
            break
        asked.append(q.key)
        k.answer(project, f"réponse pour {q.key}")
    return asked


def test_interview_is_progressive_and_covers_blocks_a_to_g(tmp_path):
    k = Kickoff(tmp_path / "projects")
    k.start("toy")
    # first question is A1, and only one is served at a time
    assert k.current_question("toy").key == "A1"
    asked = _answer_all(k, "toy")
    assert asked == [q.key for q in QUESTIONS]
    assert {q.block for q in QUESTIONS} == set("ABCDEFG")
    assert k.interview_complete("toy")


def test_completing_interview_generates_three_unsigned_artifacts(tmp_path):
    k = Kickoff(tmp_path / "projects")
    k.start("toy"); _answer_all(k, "toy")
    folder = tmp_path / "projects" / "toy"
    for name in ("PROJECT_SPEC.md", "ROADMAP.md", "PROJECT_MEMORY.md"):
        assert (folder / name).is_file(), name
    spec = (folder / "PROJECT_SPEC.md").read_text()
    assert "SIGNÉ : ❌ EN ATTENTE" in spec
    assert not spec_is_signed(tmp_path / "projects", "toy")


def test_premortem_block_is_generated_from_the_selector(tmp_path):
    k = Kickoff(tmp_path / "projects")
    k.start("toy")
    # answer with visual/docx wording so the authority lessons surface
    for q in QUESTIONS:
        k.answer("toy", "génère un CV docx rendu depuis le master visuel approuvé" if q.key == "A1" else f"r-{q.key}")
    spec = (tmp_path / "projects" / "toy" / "PROJECT_SPEC.md").read_text()
    assert "## 8. PRE-MORTEM" in spec
    assert "D-042" in spec or "D-034" in spec or "LOI-1" in spec


def test_sign_requires_explicit_action_and_is_never_automatic(tmp_path):
    k = Kickoff(tmp_path / "projects")
    k.start("toy"); _answer_all(k, "toy")
    assert not k.is_signed("toy")  # completing the interview does NOT sign
    k.sign("toy", date_str="2026-07-18")
    assert k.is_signed("toy")
    spec = (tmp_path / "projects" / "toy" / "PROJECT_SPEC.md").read_text()
    assert "SIGNÉ : ✅ GO Boss — 2026-07-18" in spec


def test_sign_before_interview_complete_is_refused(tmp_path):
    k = Kickoff(tmp_path / "projects")
    k.start("toy")
    with pytest.raises(KickoffError):
        k.sign("toy", date_str="2026-07-18")


# ── the hard launch gate ──
def _sandbox(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"; ws.mkdir()
    for argv in (["git", "init", "-q"], ["git", "config", "user.email", "f@x.invalid"],
                 ["git", "config", "user.name", "f"]):
        subprocess.run(argv, cwd=ws, check=True)
    (ws / "module.py").write_text("VALUE = 1\n")
    (ws / "test_module.py").write_text("from module import VALUE\nassert VALUE == 2\n")
    (ws / ".joao-profile.json").write_text(json.dumps({
        "project_id": "toy", "display_name": "toy", "repository_root": str(ws),
        "allowed_write_paths": ["module.py"], "forbidden_paths": []}))
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=ws, check=True)
    return ws


def _runtime(tmp_path, build):
    return RunRuntime(tmp_path / "state", builder=SandboxBuilder(build),
                      reviewer=AcceptedStageReviewer(), profiles=LocalProfileAdapter(),
                      enforce_phase0=True, projects_root=tmp_path / "projects")


def test_launch_refuses_mission_without_signed_spec(tmp_path):
    ws = _sandbox(tmp_path)
    rt = _runtime(tmp_path, lambda *a: {"ok": True})
    with pytest.raises(RuntimeStateError, match="Phase 0 non faite"):
        rt.start(project_id="toy", workspace=ws, mission="do it",
                 targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])


def test_e2e_kickoff_then_sign_then_mission_accepted(tmp_path, allow_test_write_tier):
    # GATE 5 (complete): kickoff toy → signed spec → mission accepted end-to-end.
    ws = _sandbox(tmp_path)
    k = Kickoff(tmp_path / "projects")
    k.start("toy"); _answer_all(k, "toy"); k.sign("toy", date_str="2026-07-18")

    def build(_mission, workspace, _c):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    rt = _runtime(tmp_path, build)
    run_id = rt.start(project_id="toy", workspace=ws, mission="fix the value",
                      targeted_tests=[[sys.executable, "test_module.py"]],
                      full_tests=[[sys.executable, "test_module.py"]])
    assert rt.run_once(run_id)["status"] == "needs_approval"
    assert rt.approve(run_id)["status"] == "accepted"


def test_api_kickoff_flow_drives_questions_and_go_boss_signature(tmp_path):
    import urllib.request

    from joao_orchestrator.bubble.api import LocalAPIServer
    rt = RunRuntime(tmp_path / "state", builder=SandboxBuilder(lambda *a: {"ok": True}),
                    reviewer=AcceptedStageReviewer(), profiles=LocalProfileAdapter(),
                    enforce_phase0=True, projects_root=tmp_path / "projects")
    api = LocalAPIServer(rt)
    api.serve_in_thread()

    def call(path, method="POST", body=None):
        req = urllib.request.Request(api.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-JOAO-Token": api.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    try:
        state = call("kickoff/toy/start")
        assert state["question"]["key"] == "A1" and not state["signed"]
        while not state["complete"]:
            state = call("kickoff/toy/answer", body={"text": "réponse " + state["question"]["key"]})
        assert not state["signed"], "interview completion must not auto-sign"
        state = call("kickoff/toy/sign")           # the explicit GO Boss action
        assert state["signed"]
        assert spec_is_signed(tmp_path / "projects", "toy")
    finally:
        api.close()
