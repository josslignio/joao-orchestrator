import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from joao_orchestrator.bubble.api import LocalAPIServer
from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, RuntimeStateError, SandboxBuilder


class AcceptedReviewer:
    provider = "codex"; model = "fixture-independent"
    def review(self, run, _):
        return {"ok": True, "decision": "pass", "proof": {"verdict": "ACCEPT", "reviewed_diff_sha256": run["final_diff_sha256"]}}


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
    return RunRuntime(tmp_path / "state", builder=SandboxBuilder(builder), reviewer=reviewer, profiles=LocalProfileAdapter())


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
    value = runtime(tmp_path, lambda *_: {"ok": True})
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
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        run_id = json.loads(response.read())["run_id"]
    for _ in range(30):
        state = api.runtime.get(run_id)
        if state["status"] == "needs_approval":
            break
        time.sleep(.1)
    assert api.runtime.get(run_id)["status"] == "needs_approval"
    api.close()
