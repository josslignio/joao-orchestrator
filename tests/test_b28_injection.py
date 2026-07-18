"""B-28 Phase 3 — GATE 3: memory is injected into planner/builder/reviewer, no bypass.

E2E toy mission through the REAL production orchestrator (RunRuntime): the RÈGLES ACTIVES
block must appear in the builder prompt AND the reviewer prompt, the planner must record
its injected lesson ids, and every injection must be logged in the evidence.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder

HEADER = "🧠 RÈGLES ACTIVES"
FAILURE_HEADER = "🛑 MODES DE DÉFAILLANCE CONNUS"

CAPTURED: dict[str, str] = {}


class CapturingStageReviewer:
    provider = "codex"; model = "fixture-independent-stage"

    def review_stage(self, run, run_dir, stage: str, active_rules: str = ""):
        CAPTURED[stage] = active_rules
        return {"ok": True, "decision": "pass", "stage": stage,
                "proof": {"verdict": "ACCEPT", "candidate_tree": run.get("candidate_tree"),
                         "findings": [], "reviewer": {"provider": self.provider, "model": self.model}}}

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


def _sandbox(tmp_path: Path) -> Path:
    ws = tmp_path / "fixture"; ws.mkdir()
    for argv in (["git", "init", "-q"], ["git", "config", "user.email", "f@x.invalid"],
                 ["git", "config", "user.name", "f"]):
        subprocess.run(argv, cwd=ws, check=True)
    (ws / "module.py").write_text("VALUE = 1\n")
    (ws / "test_module.py").write_text("from module import VALUE\nassert VALUE == 2\n")
    (ws / ".joao-profile.json").write_text(json.dumps({
        "project_id": "job-cv-auto", "display_name": "fixture",
        "repository_root": str(ws), "allowed_write_paths": ["module.py"], "forbidden_paths": []}))
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=ws, check=True)
    return ws


def _drive(tmp_path):
    CAPTURED.clear()
    ws = _sandbox(tmp_path)
    seen_builder_mission = {}

    def build(mission, workspace, _correction):
        seen_builder_mission["mission"] = mission
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    rt = RunRuntime(tmp_path / "state", builder=SandboxBuilder(build),
                    reviewer=CapturingStageReviewer(), profiles=LocalProfileAdapter())
    run_id = rt.start(project_id="job-cv-auto", workspace=ws,
                      mission="regenerate the CV docx from the approved master, render the visuel",
                      targeted_tests=[[sys.executable, "test_module.py"]],
                      full_tests=[[sys.executable, "test_module.py"]])
    state = rt.run_once(run_id)
    return rt, run_id, state, seen_builder_mission


def test_block_reaches_builder_prompt(tmp_path):
    _, _, _, seen = _drive(tmp_path)
    assert HEADER in seen["mission"], "builder prompt must carry the RÈGLES ACTIVES block"
    assert "master" in seen["mission"].lower()  # the real mission is still there, after the block


def test_block_reaches_reviewer_prompt_with_failure_checklist(tmp_path):
    _drive(tmp_path)
    # the reviewer is invoked at plan, build and final stages — all must carry the block
    assert {"plan", "build", "final"} <= set(CAPTURED)
    for stage, rules in CAPTURED.items():
        assert HEADER in rules, f"reviewer {stage} prompt missing RÈGLES ACTIVES"
    assert FAILURE_HEADER in CAPTURED["final"], "reviewer must get the failure-modes checklist"


def test_planner_records_injected_ids(tmp_path):
    _, run_id, _, _ = _drive(tmp_path)
    plan = json.loads((tmp_path / "state" / "runs" / run_id / "plan.json").read_text())
    assert plan.get("injected_lesson_ids"), "planner must record its injected lesson ids"
    assert all(i.startswith("L-") for i in plan["injected_lesson_ids"])


def test_injection_is_logged_as_evidence_for_every_role(tmp_path):
    rt, run_id, _, _ = _drive(tmp_path)
    events = [e for e in rt.events(run_id) if e["kind"] == "memory_injected"]
    roles = {e["role"] for e in events}
    assert {"planner", "builder", "reviewer"} <= roles, roles
    assert all(e["injected_ids"] for e in events), "every injection must name the ids it used"
    folder = tmp_path / "state" / "runs" / run_id
    assert (folder / "active-rules-planner.md").is_file()
    assert (folder / "active-rules-builder.md").is_file()
    assert (folder / "active-rules-reviewer-final.md").is_file()


def test_run_reaches_needs_approval_with_injection_active(tmp_path):
    # injection must not break the happy path — the mission still completes.
    _, _, state, _ = _drive(tmp_path)
    assert state["status"] == "needs_approval"


def test_cv_mission_surfaces_authority_lessons_in_builder(tmp_path):
    # a docx/visual mission must inject the authority-chain lessons the ledger graved.
    _, _, _, seen = _drive(tmp_path)
    assert "D-042" in seen["mission"] or "D-034" in seen["mission"]


def test_retro_hook_fires_and_records_metric_state_local(tmp_path):
    # Phase 4 wiring: finishing a mission writes a retro template and records the metric,
    # into the runtime's own state_root — never the committed brain.
    rt, run_id, _, _ = _drive(tmp_path)
    rt.approve(run_id)
    folder = tmp_path / "state" / "runs" / run_id
    assert (folder / "retro-template.md").is_file()
    metrics = tmp_path / "state" / "memory" / "run_metrics.jsonl"
    assert metrics.is_file(), "run metric must be recorded under state_root/memory"
    rows = [json.loads(x) for x in metrics.read_text().splitlines() if x.strip()]
    assert rows[-1]["run_id"] == run_id and rows[-1]["perfect"] is True
    assert any(e["kind"] == "retro_recorded" for e in rt.events(run_id))
