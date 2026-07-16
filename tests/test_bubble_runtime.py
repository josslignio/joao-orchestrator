import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest

from joao_orchestrator.bubble.api import LocalAPIServer
from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, RuntimeStateError, SandboxBuilder
from joao_orchestrator.domain.models import ProjectProfile


class AcceptedReviewer:
    provider = "codex"; model = "fixture-independent"
    def review(self, run, _):
        return {"ok": True, "decision": "pass", "proof": {"verdict": "ACCEPT", "reviewed_diff_sha256": run["final_diff_sha256"]}}
    def review_stage(self, run, folder, stage):
        if stage == "final":
            return self.review(run, folder)
        return {"ok": True, "decision": "pass", "stage": stage}


class UnverifiedStageReviewer:
    provider = "codex"; model = "fixture-unverified"
    def review_stage(self, run, _, stage):
        return {"ok": True, "decision": "pass", "stage": stage}


def sandbox(tmp_path: Path):
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    for argv in (["git", "init", "-q"], ["git", "config", "user.email", "fixture@example.invalid"], ["git", "config", "user.name", "fixture"]):
        subprocess.run(argv, cwd=workspace, check=True)
    (workspace / "module.py").write_text("VALUE = 1\n")
    (workspace / "test_module.py").write_text("from module import VALUE\nassert VALUE == 2\n")
    (workspace / ".joao-profile.json").write_text(json.dumps({"project_id": "fixture", "display_name": "fixture", "repository_root": str(workspace), "allowed_write_paths": ["module.py"], "forbidden_paths": []}))
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
    return workspace


def runtime(tmp_path, builder, reviewer=None):
    adapter = SandboxBuilder(builder)
    return RunRuntime(tmp_path / "state", builder=adapter, builders={"glm": adapter, "codex": adapter}, reviewer=reviewer or AcceptedReviewer(), profiles=LocalProfileAdapter(), allow_test_adapters=True)


def test_nominal_run_evidence_and_approval(tmp_path):
    work = sandbox(tmp_path)
    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}
    value = runtime(tmp_path, build, AcceptedReviewer())
    run = value.start(project_id="fixture", workspace=work, mission="fix", targeted_tests=[[sys.executable, "test_module.py"]], full_tests=[[sys.executable, "test_module.py"]])
    assert value.run_once(run)["status"] == "needs_approval"
    assert value.approve(run)["status"] == "accepted"
    folder = tmp_path / "state" / "runs" / run
    assert (folder / "manifest.json").is_file()
    assert (folder / "final-diff.patch").is_file()
    assert len(value.events(run)) >= 7


def test_out_of_scope_or_failed_test_blocks(tmp_path):
    work = sandbox(tmp_path)
    def build(_, workspace, __):
        (workspace / "secret.txt").write_text("no")
        return {"ok": True}
    value = runtime(tmp_path, build)
    run = value.start(project_id="fixture", workspace=work, mission="bad", targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    assert value.run_once(run)["status"] == "blocked"
    evidence = json.loads((tmp_path / "state" / "runs" / run / "changed-paths.json").read_text())
    assert evidence["violations"]


def test_dirty_secret_and_unreviewed_approval_are_refused(tmp_path):
    work = sandbox(tmp_path)
    (work / ".env").write_text("secret=not-read\n")
    value = runtime(tmp_path, lambda *_: {"ok": True}, UnverifiedStageReviewer())
    with pytest.raises(RuntimeStateError, match="out-of-scope drift"):
        value.start(project_id="fixture", workspace=work, mission="unsafe", targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    (work / ".env").unlink()
    run = value.start(project_id="fixture", workspace=work, mission="review", targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value.run_once(run)
    assert state["status"] == "needs_approval"
    with pytest.raises(RuntimeStateError, match="independent review proof"):
        value.approve(run)


def test_pause_resume_and_local_api(tmp_path):
    work = sandbox(tmp_path)
    value = runtime(tmp_path, lambda *_: {"ok": True})
    run = value.start(project_id="fixture", workspace=work, mission="pause", targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    assert value.pause(run)["status"] == "paused"
    assert value.resume(run)["status"] == "ready"
    api = LocalAPIServer(value)
    thread = api.serve_in_thread()
    assert thread.is_alive()
    assert api.url.startswith("http://127.0.0.1:")
    api.close()


def test_console_accepts_a_prompt_and_starts_a_run(tmp_path):
    work = sandbox(tmp_path)
    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}
    api = LocalAPIServer(runtime(tmp_path, build, AcceptedReviewer()))
    api.serve_in_thread()
    payload = json.dumps({
        "project_id": "fixture", "workspace": str(work), "mission": "Fix value",
        "allowed_paths": ["module.py"], "full_test_command": f"{sys.executable} test_module.py",
        "targeted_test_command": "",
    }).encode()
    request = urllib.request.Request(api.url + "missions", data=payload, method="POST",
                                     headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
    with urllib.request.urlopen(request, timeout=10) as response:
        run_id = json.loads(response.read())["run_id"]
    for _ in range(30):
        state = api.runtime.get(run_id)
        if state["status"] == "needs_approval":
            break
        time.sleep(.1)
    assert api.runtime.get(run_id)["status"] == "needs_approval"
    api.close()


def test_console_rejects_missing_token(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, lambda *_: {"ok": True}))
    api.serve_in_thread()
    request = urllib.request.Request(api.url + "missions", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 401
    api.close()


def test_quick_console_selects_one_builder_and_codex_review(tmp_path):
    def build(_, workspace, __):
        (workspace / "src" / "task.py").write_text("def identity(value):\n    return value\n")
        return {"ok": True}
    api = LocalAPIServer(runtime(tmp_path, build, AcceptedReviewer()))
    launched = api.quick_launch({"mission": "Keep identity", "builder_name": "codex", "review_mode": "codex"})
    run = api.runtime.get(launched["run_id"])
    assert run["builder_name"] == "codex"
    assert run["reviewer_names"] == ["codex"]
    api.workers[launched["run_id"]].join(timeout=10)
    assert api.runtime.get(launched["run_id"])["status"] == "needs_approval"
    api.close()


def test_quick_console_refuses_unconfigured_claude(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, lambda *_: {"ok": True}, AcceptedReviewer()))
    with pytest.raises(ValueError, match="Selected Claude reviewer is unavailable"):
        api.quick_launch({"mission": "Test", "builder_name": "glm", "review_mode": "claude"})
    api.close()


def test_codex_builder_is_refused_outside_an_isolated_workspace(tmp_path):
    work = sandbox(tmp_path)
    value = runtime(tmp_path, lambda *_: {"ok": True}, AcceptedReviewer())
    run = value.start(project_id="fixture", workspace=work, mission="Do not touch this worktree",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]],
                      builder_name="codex", reviewer_names=["codex"])
    assert value.run_once(run)["status"] == "blocked"
    assert value.get(run)["current_step"] == "Codex builder requires an isolated workspace"


def test_capabilities_do_not_claim_unconfigured_providers(tmp_path):
    api = LocalAPIServer(RunRuntime(tmp_path / "state", builder=SandboxBuilder(lambda *_: {"ok": True}), reviewer=AcceptedReviewer()))
    capabilities = api.capabilities()
    assert not capabilities["glm"]["available"]
    assert not capabilities["codex"]["available"]
    assert not capabilities["claude"]["available"]
    api.close()


def test_stop_is_queued_without_waiting_for_the_active_builder(tmp_path):
    work = sandbox(tmp_path)
    started = __import__("threading").Event()
    release = __import__("threading").Event()
    def build(_, workspace, __):
        started.set()
        assert release.wait(5)
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}
    value = runtime(tmp_path, build, AcceptedReviewer())
    run = value.start(project_id="fixture", workspace=work, mission="Fix value", targeted_tests=[], full_tests=[[sys.executable, "test_module.py"]])
    worker = __import__("threading").Thread(target=value.run_once, args=(run,))
    worker.start()
    assert started.wait(5)
    queued = value.stop(run)
    assert queued["control_request"] == "stop"
    release.set()
    worker.join(timeout=5)
    assert value.get(run)["status"] == "stopped"


def test_resume_after_safe_pause_reuses_the_existing_builder_diff(tmp_path):
    work = sandbox(tmp_path)
    started = __import__("threading").Event()
    release = __import__("threading").Event()
    calls = []
    def build(_, workspace, __):
        calls.append("build")
        started.set()
        assert release.wait(5)
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}
    value = runtime(tmp_path, build, AcceptedReviewer())
    run = value.start(project_id="fixture", workspace=work, mission="Fix value", targeted_tests=[], full_tests=[[sys.executable, "test_module.py"]])
    worker = __import__("threading").Thread(target=value.run_once, args=(run,))
    worker.start()
    assert started.wait(5)
    assert value.pause(run)["control_request"] == "pause"
    release.set()
    worker.join(timeout=5)
    assert value.get(run)["status"] == "paused"
    assert value.resume(run)["status"] == "building"
    assert value.run_once(run)["status"] == "needs_approval"
    assert calls == ["build"]


def test_required_stage_review_fails_closed_without_stage_adapter(tmp_path):
    work = sandbox(tmp_path)
    legacy = type("LegacyReviewer", (), {"provider": "legacy", "model": "legacy", "review": lambda *_: {"ok": True, "decision": "pass"}})()
    value = runtime(tmp_path, lambda *_: {"ok": True}, legacy)
    run = value.start(project_id="fixture", workspace=work, mission="No legacy shortcut", targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    assert value.run_once(run)["status"] == "blocked"


def test_builder_exception_fails_closed_instead_of_sticking_in_building(tmp_path):
    work = sandbox(tmp_path)
    value = runtime(tmp_path, lambda *_: (_ for _ in ()).throw(OSError("missing builder")), AcceptedReviewer())
    run = value.start(project_id="fixture", workspace=work, mission="Fail closed", targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    assert value.run_once(run)["status"] == "blocked"


def test_failed_run_remains_failed_when_repair_budget_is_exhausted(tmp_path):
    work = sandbox(tmp_path)
    value = runtime(tmp_path, lambda *_: {"ok": True}, AcceptedReviewer())
    run = value.start(project_id="fixture", workspace=work, mission="Fail test", targeted_tests=[], full_tests=[[sys.executable, "-c", "import sys; sys.exit(1)"]])
    assert value.run_once(run)["status"] == "failed"
    assert value.retry(run)["status"] == "failed"
    assert value.retry(run)["status"] == "failed"


def test_reviewer_exception_fails_closed(tmp_path):
    work = sandbox(tmp_path)
    broken = type("BrokenReviewer", (), {"provider": "broken", "model": "broken", "review_stage": lambda *_: (_ for _ in ()).throw(RuntimeError("reviewer crash"))})()
    value = runtime(tmp_path, lambda *_: {"ok": True}, broken)
    run = value.start(project_id="fixture", workspace=work, mission="Fail closed review", targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    assert value.run_once(run)["status"] == "blocked"


def test_console_refuses_duplicate_dispatch_for_same_worktree(tmp_path):
    work = sandbox(tmp_path)
    started = __import__("threading").Event()
    release = __import__("threading").Event()
    def build(_, workspace, __):
        started.set()
        assert release.wait(5)
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}
    api = LocalAPIServer(runtime(tmp_path, build, AcceptedReviewer()))
    api.serve_in_thread()
    run = api.runtime.start(
        project_id="fixture", workspace=work, mission="Fix value",
        targeted_tests=[], full_tests=[[sys.executable, "test_module.py"]],
        profile=ProjectProfile(project_id="fixture", display_name="fixture",
                               repository_root=str(work), allowed_write_paths=["module.py"],
                               forbidden_paths=[]),
    )
    api.drive(run)
    assert started.wait(5)
    with pytest.raises(RuntimeStateError, match="another JOAO builder"):
        api.drive(run)
    release.set()
    api.workers[run].join(timeout=5)
    api.close()
