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
        return {"ok": True, "decision": "pass",
                "proof": {"verdict": "ACCEPT", "candidate_tree": run["candidate_tree"],
                         "findings": [], "reviewer": {"provider": self.provider, "model": self.model}}}


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


def test_scope_comes_from_the_run_signed_at_start_not_a_stale_live_profile(tmp_path):
    # RI-7 non-regression: the run's own persisted profile snapshot (captured
    # at start()) stays authoritative — a later edit of the on-disk profile
    # file must never retroactively widen or narrow what an in-flight run
    # is allowed to touch.
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    value = runtime(tmp_path, build, AcceptedReviewer())
    run = value.start(project_id="fixture", workspace=work, mission="fix",
                      targeted_tests=[[sys.executable, "test_module.py"]],
                      full_tests=[[sys.executable, "test_module.py"]])
    # Widen the on-disk profile *after* start() captured its own snapshot.
    (work / ".joao-profile.json").write_text(json.dumps({
        "project_id": "fixture", "display_name": "fixture", "repository_root": str(work),
        "allowed_write_paths": ["module.py", "secret.txt"], "forbidden_paths": []}))
    state = value.run_once(run)
    assert state["status"] == "needs_approval"
    persisted_profile = json.loads((tmp_path / "state" / "runs" / run / "run.json").read_text())["profile"]
    assert persisted_profile["allowed_write_paths"] == ["module.py"]


def test_dirty_secret_and_unreviewed_approval_are_refused(tmp_path):
    work = sandbox(tmp_path)
    (work / ".env").write_text("secret=not-read\n")
    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}
    value = runtime(tmp_path, build)
    with pytest.raises(RuntimeStateError, match="RI-1"):
        value.start(project_id="fixture", workspace=work, mission="unsafe", targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    (work / ".env").unlink()
    run = value.start(project_id="fixture", workspace=work, mission="review", targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value.run_once(run)
    # Fail-closed (RI-4/RI-5): no review proof was ever imported, so the
    # default CodexEvidenceReviewer blocks rather than silently letting the
    # run reach human approval.
    assert state["status"] == "blocked"
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


def test_console_mission_launch_passes_network_capability_through(tmp_path):
    """Found via the real worker-host operational-closure proof
    (2026-07-21): `/missions` never exposed `network_capability` at all — a
    network-requiring builder (e.g. GLMBuilder) silently got `network=False`
    and hung until the sandbox's own timeout SIGTERM'd it. Regression:
    `network_capability` in the request body must reach the builder's
    `set_capabilities()`, for both `True` and the fail-closed `False` default."""
    class _CapturingBuilder:
        provider = "capturing"; model = "m"; provider_family = "f"
        def __init__(self):
            self.seen_network_capability = None
        def set_capabilities(self, capabilities):
            self.seen_network_capability = capabilities.get("network_capability")
        def build(self, mission, workspace, run_dir, allowed, correction):
            (workspace / "module.py").write_text("VALUE = 2\n")
            return {"ok": True, "provider": self.provider, "model": self.model}

    work = sandbox(tmp_path)
    builder = _CapturingBuilder()
    rt = RunRuntime(tmp_path / "state", builder=builder, reviewer=AcceptedReviewer(), profiles=LocalProfileAdapter())
    api = LocalAPIServer(rt)
    api.serve_in_thread()
    payload = json.dumps({
        "project_id": "fixture", "workspace": str(work), "mission": "Fix value",
        "allowed_paths": ["module.py"], "full_test_command": f"{sys.executable} test_module.py",
        "network_capability": True,
    }).encode()
    request = urllib.request.Request(api.url + "missions", data=payload, method="POST",
                                     headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
    with urllib.request.urlopen(request, timeout=10) as response:
        run_id = json.loads(response.read())["run_id"]
    for _ in range(30):
        if api.runtime.get(run_id)["status"] in {"needs_approval", "blocked", "failed"}:
            break
        time.sleep(.1)
    api.close()
    assert builder.seen_network_capability is True


def test_console_rejects_missing_token(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, lambda *_: {"ok": True}))
    api.serve_in_thread()
    request = urllib.request.Request(api.url + "missions", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 401
    api.close()


def test_console_rejects_wrong_token(tmp_path):
    """Boss negative matrix: wrong UI token => rejected (distinct from a
    MISSING token — both must fail closed, never a partial/lenient match)."""
    api = LocalAPIServer(runtime(tmp_path, lambda *_: {"ok": True}))
    api.serve_in_thread()
    request = urllib.request.Request(api.url + "missions", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json",
                                             "X-JOAO-Token": "definitely-not-the-real-token"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 401
    api.close()


def test_console_refuses_non_local_host_bind(tmp_path):
    """Boss negative matrix: non-local host bind => rejected. LocalAPIServer
    must never bind to a non-loopback address."""
    with pytest.raises(ValueError):
        LocalAPIServer(runtime(tmp_path, lambda *_: {"ok": True}), host="0.0.0.0")


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
