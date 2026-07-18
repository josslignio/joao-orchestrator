"""B-29.1 (suite) — the cost cascade wired to (faked) real workers via CascadeBuilder.

Hermetic: a REAL temp git worktree + REAL objective test running (sys.executable on a local
assert file); only the GLM/Claude subprocesses are faked, so the isolation / capture / judge /
cost / per-worker-injection / fail-closed machinery is all exercised for real.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder
from joao_orchestrator.domain.models import ProjectProfile
from joao_orchestrator.providers.cascade import COST_CLAUDE, COST_GLM, Tier
from joao_orchestrator.providers.cascade_runtime import CascadeBuilder, WorkspaceGit

RULES_BLOCK = "🧠 RÈGLES ACTIVES (mémoire JOÃO — les violer = échec)\n- ☐ [L-024] async self-review"


def _repo(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for argv in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.invalid"],
                 ["git", "config", "user.name", "t"]):
        subprocess.run(argv, cwd=ws, check=True)
    (ws / "module.py").write_text("VALUE = 1\n")
    (ws / "test_module.py").write_text("from module import VALUE\nassert VALUE == 2\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=ws, check=True)
    return ws


def _run_dir(tmp_path: Path, ws: Path, *, critical=False, correction=False) -> Path:
    """A minimal run.json sidecar the way RunRuntime writes it, so build() can read routing."""
    rd = tmp_path / "run"
    rd.mkdir(exist_ok=True)
    (rd / "run.json").write_text(json.dumps({
        "run_id": "run-test", "workspace": str(ws), "critical": critical, "recurrence": False,
        "tags": [], "targeted_tests": [], "full_tests": [[sys.executable, "test_module.py"]],
    }))
    return rd


def _glm_writing(value_by_angle: dict[str, int], default: int) -> callable:
    """Fake joao-glm: writes module.py = VALUE per angle, so objective tests decide the winner."""
    def _run(workspace, task_file, output, allowed, angle):
        value = value_by_angle.get(angle, default)
        (Path(workspace) / "module.py").write_text(f"VALUE = {value}\n")
        Path(output).write_text(json.dumps({"ok": True, "angle": angle, "tokens": 10}) + "\n")
        return {"ok": True, "returncode": 0, "output": str(output),
                "real_cost": 10.0, "duration_s": 0.01}
    return _run


def _glm_broken(_ws, _tf, output, _al, angle):
    (Path(_ws) / "module.py").write_text("VALUE = 0\n")  # never satisfies VALUE == 2
    Path(output).write_text(json.dumps({"ok": True, "angle": angle}) + "\n")
    return {"ok": True, "returncode": 0, "output": str(output), "real_cost": 3.0, "duration_s": 0.01}


# ─────────────────────────── tier routing ───────────────────────────
def test_deterministic_tier_is_free_and_wins(tmp_path):
    ws = _repo(tmp_path)
    rd = _run_dir(tmp_path, ws)

    def tool(_task, workspace, _allowed):
        (Path(workspace) / "module.py").write_text("VALUE = 2\n")
        from joao_orchestrator.providers.cascade import WorkerResult
        return WorkerResult(provider="deterministic", cost=0.0, ok=True)

    builder = CascadeBuilder(deterministic=tool, glm_runner=_glm_broken)
    out = builder.build(RULES_BLOCK + "\n\nfix", ws, rd, ["module.py"], correction=False)
    assert out["ok"] and out["tier"] == Tier.DETERMINISTIC
    assert out["cost"] == 0.0
    assert (ws / "module.py").read_text() == "VALUE = 2\n"


def test_glm_solo_when_not_critical(tmp_path):
    ws = _repo(tmp_path)
    rd = _run_dir(tmp_path, ws, critical=False)
    builder = CascadeBuilder(glm_runner=_glm_writing({}, default=2))
    out = builder.build(RULES_BLOCK + "\n\nfix", ws, rd, ["module.py"], correction=False)
    assert out["ok"] and out["tier"] == Tier.GLM_SOLO
    assert out["cost"] == COST_GLM
    assert (ws / "module.py").read_text() == "VALUE = 2\n"


def test_critical_forces_best_of_n_and_picks_the_passing_angle(tmp_path):
    ws = _repo(tmp_path)
    rd = _run_dir(tmp_path, ws, critical=True)
    # only the 'readability' angle produces a passing solution
    builder = CascadeBuilder(glm_runner=_glm_writing({"readability": 2}, default=0),
                             angles=("performance", "readability", "edge-cases"))
    out = builder.build(RULES_BLOCK + "\n\nfix", ws, rd, ["module.py"], correction=False)
    assert out["ok"] and out["tier"] == Tier.BEST_OF_N
    assert out["cost"] == 3 * COST_GLM
    assert out["winner_angle"] == "readability"
    assert (ws / "module.py").read_text() == "VALUE = 2\n"
    decision = json.loads((rd / "cascade-decision.json").read_text())
    assert len(decision["candidates"]) == 3               # 3 GLM outputs archived
    assert decision["judge_verdict"]["method"] == "objective_tests"  # tests decided, no LLM judge
    # every candidate's task file carries the injected RÈGLES block (not just the winner)
    inj = json.loads((rd / "cascade-injection.json").read_text())
    assert inj["block_present_in_mission"] is True
    for cand in inj["candidates"]:
        assert "RÈGLES ACTIVES" in Path(cand["task_file"]).read_text()


def test_escalates_to_claude_when_all_glm_fail(tmp_path):
    ws = _repo(tmp_path)
    rd = _run_dir(tmp_path, ws, critical=True)

    def claude_ok(workspace, task_file, output, allowed, angle):
        (Path(workspace) / "module.py").write_text("VALUE = 2\n")
        Path(output).write_text("{}\n")
        return {"ok": True, "returncode": 0, "output": str(output), "real_cost": 0.0, "duration_s": 0.02}

    builder = CascadeBuilder(glm_runner=_glm_broken, claude_runner=claude_ok)
    out = builder.build(RULES_BLOCK + "\n\nfix", ws, rd, ["module.py"], correction=False)
    assert out["ok"] and out["tier"] == Tier.CLAUDE
    assert out["cost"] == 3 * COST_GLM + COST_CLAUDE
    assert (ws / "module.py").read_text() == "VALUE = 2\n"


def test_fail_closed_blocks_and_leaves_worktree_clean(tmp_path):
    ws = _repo(tmp_path)
    rd = _run_dir(tmp_path, ws, critical=True)

    def claude_bad(workspace, task_file, output, allowed, angle):
        (Path(workspace) / "module.py").write_text("VALUE = 5\n")
        Path(output).write_text("{}\n")
        return {"ok": True, "returncode": 0, "output": str(output), "real_cost": 0.0, "duration_s": 0.02}

    builder = CascadeBuilder(glm_runner=_glm_broken, claude_runner=claude_bad)
    out = builder.build(RULES_BLOCK + "\n\nfix", ws, rd, ["module.py"], correction=False)
    assert out["ok"] is False and out["blocked"] is True
    assert out["tier"] == Tier.BLOCKED
    # worktree returned to its pre-build state — no failed candidate left applied
    assert (ws / "module.py").read_text() == "VALUE = 1\n"
    assert subprocess.run(["git", "status", "--porcelain"], cwd=ws, capture_output=True,
                          text=True).stdout.strip() == ""


def test_cost_ledger_reports_savings_vs_direct_claude(tmp_path):
    ws = _repo(tmp_path)
    rd = _run_dir(tmp_path, ws, critical=True)
    builder = CascadeBuilder(glm_runner=_glm_writing({"performance": 2}, default=0))
    builder.build(RULES_BLOCK + "\n\nfix", ws, rd, ["module.py"], correction=False)
    cost = json.loads((rd / "cascade-cost.json").read_text())
    assert cost["cascade_proxy_cost"] == 3 * COST_GLM
    assert cost["direct_claude_proxy"] == COST_CLAUDE
    assert cost["savings_vs_direct_claude"] == COST_CLAUDE - 3 * COST_GLM
    assert cost["real_cost_signal"] > 0  # tokens the (fake) glm reported, not fabricated


# ─────────────────────────── end-to-end through RunRuntime ───────────────────────────
class _AcceptReviewer:
    provider = "codex"
    model = "fixture-independent"

    def review(self, run, _):
        return {"ok": True, "decision": "pass",
                "proof": {"verdict": "ACCEPT", "reviewed_diff_sha256": run["final_diff_sha256"]}}


def test_cascade_builder_drives_a_full_runruntime_run(tmp_path):
    ws = _repo(tmp_path)
    profile = ProjectProfile(project_id="fixture", display_name="fixture",
                             repository_root=str(ws), allowed_write_paths=["module.py"],
                             forbidden_paths=[])
    builder = CascadeBuilder(glm_runner=_glm_writing({"performance": 2}, default=2))
    rt = RunRuntime(tmp_path / "state", builder=builder, reviewer=_AcceptReviewer(),
                    profiles=LocalProfileAdapter())
    run_id = rt.start(project_id="fixture", workspace=ws, mission="make VALUE 2",
                      targeted_tests=[[sys.executable, "test_module.py"]],
                      full_tests=[[sys.executable, "test_module.py"]],
                      profile=profile, critical=True)
    state = rt.run_once(run_id)
    assert state["status"] == "needs_approval"
    assert rt.approve(run_id)["status"] == "accepted"
    rd = tmp_path / "state" / "runs" / run_id
    decision = json.loads((rd / "cascade-decision.json").read_text())
    assert decision["tier"] == Tier.BEST_OF_N and len(decision["candidates"]) == 3
    builder_ev = json.loads((rd / "builder-evidence.json").read_text())
    assert builder_ev["model"] == "zai-coding-plan/glm-4.5-air"  # real model always shown


def test_runner_crash_leaves_worktree_clean_and_blocks(tmp_path):
    # P1-A: a runner that raises (e.g. missing executable) must fail-closed, not strand a dirty tree
    ws = _repo(tmp_path)
    rd = _run_dir(tmp_path, ws, critical=False)

    def exploding(_ws, _tf, _out, _al, _angle):
        (Path(_ws) / "module.py").write_text("VALUE = 777\n")  # partial write before the crash
        raise FileNotFoundError("joao-glm vanished")

    builder = CascadeBuilder(glm_runner=exploding, claude_runner=exploding)
    out = builder.build(RULES_BLOCK + "\n\nfix", ws, rd, ["module.py"], correction=False)
    assert out["ok"] is False and out["blocked"] is True
    assert (ws / "module.py").read_text() == "VALUE = 1\n"          # restored
    assert subprocess.run(["git", "status", "--porcelain"], cwd=ws, capture_output=True,
                          text=True).stdout.strip() == ""


def test_preexisting_untracked_wip_is_preserved(tmp_path):
    # P2-A: a user's uncommitted untracked file must survive the best-of-N git resets
    ws = _repo(tmp_path)
    (ws / "my_wip_notes.py").write_text("SECRET = 'do not lose me'\n")   # untracked, not committed
    rd = _run_dir(tmp_path, ws, critical=True)
    builder = CascadeBuilder(glm_runner=_glm_writing({"performance": 2}, default=0))
    out = builder.build(RULES_BLOCK + "\n\nfix", ws, rd, ["module.py"], correction=False)
    assert out["ok"] is True
    assert (ws / "my_wip_notes.py").read_text() == "SECRET = 'do not lose me'\n"  # NOT wiped


def test_bad_judge_returning_noncandidate_fails_closed(tmp_path):
    # P3-A: a custom judge returning a fabricated WorkerResult must block, not crash
    ws = _repo(tmp_path)
    rd = _run_dir(tmp_path, ws, critical=True)
    from joao_orchestrator.providers.cascade import WorkerResult as WR

    def rogue_judge(_task, _candidates):
        # a fabricated "verified" winner that is NOT one of the real candidates
        return WR(provider="ghost", ok=True, tests_passed=True, score=99.0), {"reason": "fabricated"}

    builder = CascadeBuilder(glm_runner=_glm_writing({}, default=2), judge=rogue_judge,
                             claude_runner=_glm_broken)  # keep hermetic if it ever escalated
    out = builder.build(RULES_BLOCK + "\n\nfix", ws, rd, ["module.py"], correction=False)
    assert out["ok"] is False and out["blocked"] is True
    assert (ws / "module.py").read_text() == "VALUE = 1\n"


def test_runruntime_blocks_when_builder_raises(tmp_path):
    # P1-A (second half): RunRuntime must fail-closed if the builder adapter raises
    ws = _repo(tmp_path)
    profile = ProjectProfile(project_id="fixture", display_name="fixture",
                             repository_root=str(ws), allowed_write_paths=["module.py"],
                             forbidden_paths=[])

    class Exploding:
        provider = "boom"; model = "boom"
        def build(self, *a, **k):
            raise RuntimeError("kaboom")

    rt = RunRuntime(tmp_path / "state", builder=Exploding(), reviewer=_AcceptReviewer(),
                    profiles=LocalProfileAdapter())
    run_id = rt.start(project_id="fixture", workspace=ws, mission="x",
                      targeted_tests=[[sys.executable, "test_module.py"]],
                      full_tests=[[sys.executable, "test_module.py"]], profile=profile)
    assert rt.run_once(run_id)["status"] == "blocked"
    assert any(e["kind"] == "builder_exception" for e in rt.events(run_id))


def test_workspacegit_isolation_roundtrip(tmp_path):
    ws = _repo(tmp_path)
    git = WorkspaceGit(ws)
    base = git.snapshot_base()
    (ws / "module.py").write_text("VALUE = 42\n")
    (ws / "new.py").write_text("x = 1\n")
    cap = git.capture_changes(tmp_path / "cap")
    assert set(cap["files"]) == {"module.py", "new.py"}
    git.reset_to_base(base)
    assert (ws / "module.py").read_text() == "VALUE = 1\n"
    assert not (ws / "new.py").exists()
    git.apply_capture(tmp_path / "cap", cap)
    assert (ws / "module.py").read_text() == "VALUE = 42\n"
    assert (ws / "new.py").read_text() == "x = 1\n"
