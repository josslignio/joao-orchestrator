"""C8-A / G-FROZEN-FINISH-LINE: `RunRuntime.start()` is the REAL entrypoint
that writes the immutable `frozen_mission.json` artifact
(`JOAO_C8_GATE_CONTRACTS.md`); `bubble/gates.py::gate_frozen_finish_line` is
the pure function that compares a run's changed paths/corrections against it.
This file proves both ends: the real write (via `start()`, not just
`build_frozen_mission()` called directly) and the real artifact being fed
into the gate (not just a hand-built dict — that unit coverage for the gate's
reason codes lives in `tests/test_c8_gates.py`).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.joao_orchestrator.bubble import gates
from src.joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder


def _git_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    for argv in (["git", "init", "-q"],
                ["git", "config", "user.email", "fixture@example.invalid"],
                ["git", "config", "user.name", "fixture"]):
        subprocess.run(argv, cwd=workspace, check=True)
    (workspace / "module.py").write_text("VALUE = 1\n")
    (workspace / ".joao-profile.json").write_text(json.dumps({
        "project_id": "fixture", "display_name": "fixture", "repository_root": str(workspace),
        "allowed_write_paths": ["module.py"], "forbidden_paths": [],
    }))
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
    return workspace


def _runtime(tmp_path: Path) -> RunRuntime:
    return RunRuntime(tmp_path / "state", builder=SandboxBuilder(lambda *a: {"ok": True}),
                      profiles=LocalProfileAdapter())


def test_start_writes_frozen_mission_json_with_the_given_fields(tmp_path):
    workspace = _git_workspace(tmp_path)
    value = _runtime(tmp_path)
    bindings = {
        "AC-C8-001": {
            "allowed_paths": ["src/joao_orchestrator/bubble/gates.py"],
            "required_tests": ["tests/test_c8_gates.py::test_g_dbl_audit_green_normal_one_distinct_reviewer_passes"],
            "allowed_actions": ["modify", "create"],
        }
    }
    run_id = value.start(project_id="fixture", workspace=workspace, mission="fix",
                         targeted_tests=[[sys.executable, "-c", "pass"]],
                         full_tests=[[sys.executable, "-c", "pass"]],
                         risk_tier="critical", canary_required=True, spec_sha="s" * 40,
                         roadmap_sha="r" * 40, authority_instruction_hash="h" * 64,
                         forbidden_paths=["memory/lessons.jsonl"], criterion_bindings=bindings)

    frozen_path = tmp_path / "state" / "runs" / run_id / "frozen_mission.json"
    assert frozen_path.is_file()
    frozen = json.loads(frozen_path.read_text())
    assert frozen == gates.build_frozen_mission(
        spec_sha="s" * 40, roadmap_sha="r" * 40, authority_instruction_hash="h" * 64,
        risk_tier="critical", canary_required=True, forbidden_paths=["memory/lessons.jsonl"],
        criterion_bindings=bindings)


def test_start_without_c8_kwargs_still_writes_a_frozen_mission_with_null_risk_tier(tmp_path):
    # Additive-only: existing callers that never pass the new C8-A kwargs get
    # an unconditional frozen_mission.json with risk_tier=None — start()
    # itself is unchanged for them (no new BLOCK), matching D1's contract
    # that the RISK-TIER-ABSENT FAIL-CLOSED behavior lives in the gate, not
    # forced retroactively onto every pre-C8-A caller of start().
    workspace = _git_workspace(tmp_path)
    value = _runtime(tmp_path)
    run_id = value.start(project_id="fixture", workspace=workspace, mission="fix",
                         targeted_tests=[[sys.executable, "-c", "pass"]],
                         full_tests=[[sys.executable, "-c", "pass"]])
    frozen = json.loads((tmp_path / "state" / "runs" / run_id / "frozen_mission.json").read_text())
    assert frozen["risk_tier"] is None
    assert frozen["canary_required"] is False
    assert frozen["forbidden_paths"] == []
    assert frozen["criterion_bindings"] == {}

    # And the gate itself, evaluated against this exact real artifact,
    # fail-closes on the missing risk_tier — D1 enforced end-to-end.
    result = gates.gate_frozen_finish_line(frozen_mission=frozen, changed_paths=[])
    assert result["ok"] is False and result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"


def test_frozen_mission_json_is_never_rewritten_by_run_once(tmp_path):
    workspace = _git_workspace(tmp_path)
    value = _runtime(tmp_path)
    run_id = value.start(project_id="fixture", workspace=workspace, mission="fix",
                         targeted_tests=[[sys.executable, "-c", "pass"]],
                         full_tests=[[sys.executable, "-c", "pass"]], risk_tier="normal")
    frozen_path = tmp_path / "state" / "runs" / run_id / "frozen_mission.json"
    before = frozen_path.read_text()
    before_mtime = frozen_path.stat().st_mtime_ns
    value.run_once(run_id)
    assert frozen_path.read_text() == before
    assert frozen_path.stat().st_mtime_ns == before_mtime


def test_real_frozen_mission_artifact_gates_a_compliant_changeset(tmp_path):
    workspace = _git_workspace(tmp_path)
    value = _runtime(tmp_path)
    bindings = {
        "AC-C8-GATES": {
            "allowed_paths": ["src/joao_orchestrator/bubble/gates.py", "tests/test_c8_gates.py"],
            "required_tests": ["tests/test_c8_gates.py::test_g_hermetic_green_no_touches_passes"],
            "allowed_actions": ["modify", "create"],
        }
    }
    run_id = value.start(project_id="fixture", workspace=workspace, mission="fix",
                         targeted_tests=[[sys.executable, "-c", "pass"]],
                         full_tests=[[sys.executable, "-c", "pass"]],
                         risk_tier="normal", spec_sha="s" * 40, roadmap_sha="r" * 40,
                         authority_instruction_hash="h" * 64,
                         forbidden_paths=["memory/lessons.jsonl"],
                         criterion_bindings=bindings)
    frozen = json.loads((tmp_path / "state" / "runs" / run_id / "frozen_mission.json").read_text())

    compliant = gates.gate_frozen_finish_line(
        frozen_mission=frozen,
        changed_paths=[{"path": "src/joao_orchestrator/bubble/gates.py", "action": "modify"}],
        required_test_results={
            "tests/test_c8_gates.py::test_g_hermetic_green_no_touches_passes":
                {"passed": True, "candidate_tree": "t" * 40},
        },
        candidate_tree="t" * 40)
    assert compliant["ok"] is True and compliant["reason_code"] == "G_FROZEN_FINISH_LINE_OK"

    scope_creep = gates.gate_frozen_finish_line(
        frozen_mission=frozen,
        changed_paths=[{"path": "memory/lessons.jsonl", "action": "modify"}],
        candidate_tree="t" * 40)
    assert scope_creep["ok"] is False and scope_creep["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
