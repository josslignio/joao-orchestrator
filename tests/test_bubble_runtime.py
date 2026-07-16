import json
import subprocess
import sys
from pathlib import Path

import pytest

from joao_orchestrator.bubble.api import LocalAPIServer
from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder


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


def runtime(tmp_path, builder):
    return RunRuntime(tmp_path / "state", builder=SandboxBuilder(builder), profiles=LocalProfileAdapter())


def test_nominal_run_evidence_and_approval(tmp_path):
    work = sandbox(tmp_path)
    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}
    value = runtime(tmp_path, build)
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
    run = value.start(project_id="fixture", workspace=work, mission="bad", targeted_tests=[], full_tests=[])
    assert value.run_once(run)["status"] == "blocked"
    evidence = json.loads((tmp_path / "state" / "runs" / run / "changed-paths.json").read_text())
    assert evidence["violations"]


def test_pause_resume_and_local_api(tmp_path):
    work = sandbox(tmp_path)
    value = runtime(tmp_path, lambda *_: {"ok": True})
    run = value.start(project_id="fixture", workspace=work, mission="pause", targeted_tests=[], full_tests=[])
    assert value.pause(run)["status"] == "paused"
    assert value.resume(run)["status"] == "ready"
    api = LocalAPIServer(value)
    thread = api.serve_in_thread()
    assert thread.is_alive()
    assert api.url.startswith("http://127.0.0.1:")
    api.close()
