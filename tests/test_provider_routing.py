"""Focused acceptance tests for Bubble provider routing and evidence."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from joao_orchestrator.bubble.api import LocalAPIServer
from joao_orchestrator.bubble.runtime import (
    BuilderAdapter,
    ClaudeBuilder,
    ClaudeCLIReviewer,
    CodexBuilder,
    CodexCLIReviewer,
    ReviewerAdapter,
    RunRuntime,
    RuntimeStateError,
    SandboxBuilder,
    bounded_provider_env,
    parse_review_verdict,
)
from joao_orchestrator.domain.models import ProjectProfile


def git_workspace(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    root = path / "workspace"
    root.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "fixture@example.invalid"],
        ["git", "config", "user.name", "fixture"],
    ):
        subprocess.run(argv, cwd=root, check=True)
    (root / "test_todo.py").write_text("from todo import VALUE\n\ndef test_value():\n    assert VALUE == 2\n")
    (root / "todo.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
    return root


def profile(root: Path) -> ProjectProfile:
    return ProjectProfile(
        project_id="fixture", display_name="fixture", repository_root=str(root),
        allowed_write_paths=["todo.py", "tests/"], forbidden_paths=[],
    )


class FixtureBuilder(BuilderAdapter):
    provider = "fixture-real-provider"
    model = "fixture-real-model"

    def __init__(self, *, result_ok: bool = True, value: int = 2):
        self.result_ok = result_ok
        self.value = value
        self.calls = 0

    def preflight(self):
        return {
            "available": True, "provider": self.provider, "model": self.model,
            "executable": "/fixture/builder", "auth_status": "fixture",
            "config_status": "configured", "last_error": None,
            "reason": "fixture available", "real_or_mock": "real",
        }

    def build(self, mission, workspace, run_dir, allowed, correction):
        self.calls += 1
        (workspace / "todo.py").write_text(f"VALUE = {self.value}\n")
        evidence = run_dir / "fixture-builder.log"
        evidence.write_text("fixture invocation")
        return {
            "ok": self.result_ok, "provider": self.provider, "model": self.model,
            "executable": "/fixture/builder", "adapter_command": ["fixture-builder"],
            "timestamp_start": "2026-07-16T00:00:00Z",
            "timestamp_end": "2026-07-16T00:00:01Z", "returncode": 0,
            "evidence_paths": [str(evidence)], "real_or_mock": "real",
        }


class FixtureReviewer(ReviewerAdapter):
    def __init__(self, provider: str, decision: str = "pass"):
        self.provider = provider
        self.model = provider + "-model"
        self.decision = decision
        self.calls: list[str] = []

    def preflight(self):
        return {
            "available": True, "provider": self.provider, "model": self.model,
            "executable": "/fixture/reviewer", "auth_status": "fixture",
            "config_status": "configured", "last_error": None,
            "reason": "fixture available", "real_or_mock": "real",
        }

    def review_stage(self, run, run_dir, stage):
        self.calls.append(stage)
        verdict = "ACCEPT" if self.decision == "pass" else "P1" if self.decision == "p1" else "BLOCK"
        evidence = run_dir / f"{self.provider}-{stage}.log"
        evidence.write_text(verdict)
        return {
            "ok": self.decision == "pass", "decision": self.decision,
            "provider": self.provider, "model": self.model,
            "real_or_mock": "real", "stage": stage, "returncode": 0,
            "timestamp_start": "2026-07-16T00:00:00Z",
            "timestamp_end": "2026-07-16T00:00:01Z",
            "evidence_paths": [str(evidence)],
            "proof": {"verdict": verdict,
                      "reviewed_diff_sha256": run.get("final_diff_sha256")},
        }

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


def runtime(path: Path, *, builder=None, codex=None, claude=None, production=False):
    builder = builder or FixtureBuilder()
    codex = codex or FixtureReviewer("codex-fixture")
    claude = claude or FixtureReviewer("claude-fixture")
    return RunRuntime(
        path / "state", builder=builder,
        builders={"glm": builder, "codex": builder, "claude": builder},
        reviewer=codex, reviewers={"codex": codex, "claude": claude},
        allow_test_adapters=not production,
    )


def start(value: RunRuntime, root: Path, *, builder="glm", reviewers=None, policy="none"):
    return value.start(
        project_id="fixture", workspace=root, mission="Create the bounded TODO fixture",
        targeted_tests=[[sys.executable, "-m", "pytest", "-q", "test_todo.py"]],
        full_tests=[[sys.executable, "-m", "pytest", "-q"]], profile=profile(root),
        builder_name=builder, reviewer_names=reviewers or [], review_policy=policy,
    )


@pytest.mark.parametrize(
    ("builder", "mode", "expected_reviewers", "policy", "self_review"),
    [
        ("glm", "none", [], "none", False),
        ("glm", "codex", ["codex"], "codex", False),
        ("glm", "claude", ["claude"], "claude", False),
        ("glm", "codex_and_claude", ["codex", "claude"], "codex_and_claude", False),
        ("codex", "none", [], "none", False),
        ("codex", "codex", ["codex"], "codex", True),
        ("codex", "claude", ["claude"], "claude", False),
        ("codex", "codex_and_claude", ["codex", "claude"], "codex_and_claude", True),
        ("claude", "none", [], "none", False),
        ("claude", "codex", ["codex"], "codex", False),
        ("claude", "claude", ["claude"], "claude", True),
        ("claude", "codex_and_claude", ["codex", "claude"], "codex_and_claude", True),
    ],
)
def test_all_twelve_ui_selections_route_exactly(tmp_path, builder, mode, expected_reviewers, policy, self_review):
    api = LocalAPIServer(runtime(tmp_path))
    try:
        selected_builder, reviewers, selected_policy, selected_self_review = api._quick_configuration(
            {"builder_name": builder, "review_mode": mode}
        )
        assert (selected_builder, reviewers, selected_policy, selected_self_review) == (
            builder, expected_reviewers, policy, self_review,
        )
    finally:
        api.close()


def test_unavailable_claude_is_disabled_and_refused_before_sandbox(tmp_path):
    builder = FixtureBuilder()
    value = RunRuntime(
        tmp_path / "state", builder=builder,
        builders={"glm": builder, "codex": builder, "claude": builder},
        reviewers={"codex": FixtureReviewer("codex-fixture"), "claude": ClaudeCLIReviewer(executable="/missing/claude")},
        allow_test_adapters=False,
    )
    api = LocalAPIServer(value)
    try:
        capabilities = api.capabilities()
        assert capabilities["claude"]["reviewer_available"] is False
        assert capabilities["claude"]["config_status"] in {"not_configured", "configured"}
        assert capabilities["claude"]["reviewer_last_error"]
        with pytest.raises(ValueError, match="Selected Claude reviewer is unavailable"):
            api.quick_launch({"mission": "test", "builder_name": "glm", "review_mode": "claude"})
        assert not (tmp_path / "state" / "sandboxes").exists()
    finally:
        api.close()


def test_installed_but_unauthenticated_claude_is_unavailable(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="2.1.211", stderr="")
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"is_error": True, "result": "Not logged in"}),
            stderr="",
        )

    monkeypatch.setattr(
        "joao_orchestrator.bubble.runtime.resolve_executable",
        lambda executable, fallback: "/mock/claude",
    )
    monkeypatch.setattr("joao_orchestrator.bubble.runtime.subprocess.run", fake_run)
    capability = ClaudeCLIReviewer().preflight()
    assert capability["available"] is False
    assert capability["auth_status"] == "not_logged_in"
    assert calls[-1][-4:] == ["--max-turns", "1", "--output-format", "json"]


def test_claude_builder_and_reviewer_use_real_bounded_cli_preflight(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="2.1.211", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"is_error": False, "result": "CLAUDE_JOAO_OK", "model": "claude-test"}),
            stderr="",
        )

    monkeypatch.setattr(
        "joao_orchestrator.bubble.runtime.resolve_claude_executable",
        lambda executable: "/mock/claude",
    )
    monkeypatch.setattr("joao_orchestrator.bubble.runtime.subprocess.run", fake_run)
    for adapter in (ClaudeBuilder(), ClaudeCLIReviewer()):
        capability = adapter.preflight()
        assert capability["available"] is True
        assert capability["actual_model"] == "claude-test"
        assert capability["real_or_mock"] == "real"
    assert sum(any("CLAUDE_JOAO_OK" in item for item in call) for call in calls) == 2


def test_provider_environment_disables_python_and_pytest_cache_drift():
    env = bounded_provider_env()
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTEST_ADDOPTS"] == "-p no:cacheprovider"


def test_quick_sandbox_rejects_unsafe_requested_paths(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    try:
        with pytest.raises(ValueError, match="safe quick paths"):
            api.quick_launch({
                "mission": "test", "builder_name": "glm", "review_mode": "none",
                "allowed_paths": [".git/"],
            })
        assert not (tmp_path / "state" / "sandboxes").exists()
    finally:
        api.close()


def test_no_review_runs_real_builder_and_requires_human_approval(tmp_path):
    root = git_workspace(tmp_path)
    builder = FixtureBuilder()
    value = runtime(tmp_path, builder=builder)
    run_id = start(value, root)
    state = value.run_once(run_id)
    assert state["status"] == "needs_approval"
    assert builder.calls == 1
    assert state["review_policy"] == "none"
    assert state["review_verified"] is False
    assert value.approve(run_id)["status"] == "accepted"
    plan = json.loads((tmp_path / "state" / "runs" / run_id / "plan.json").read_text())
    assert plan["objective_verbatim"] == "Create the bounded TODO fixture"
    assert plan["implementation_scope"]["allowed_write_paths"] == ["todo.py", "tests/"]
    assert plan["validation_contract"]["all_commands_must_pass"] is True
    assert [step["gate"] for step in plan["execution_steps"]] == ["plan", "build", "test", "cleanup", "review", "delivery"]


def test_reviewed_run_calls_every_gate_and_verifies_exact_diff(tmp_path):
    root = git_workspace(tmp_path)
    reviewer = FixtureReviewer("codex-fixture")
    value = runtime(tmp_path, codex=reviewer)
    run_id = start(value, root, reviewers=["codex"], policy="codex")
    state = value.run_once(run_id)
    assert state["status"] == "needs_approval"
    assert reviewer.calls == ["plan", "build", "test", "final"]
    assert state["review_verified"] is True
    evidence = json.loads((tmp_path / "state" / "runs" / run_id / "review-evidence.json").read_text())
    assert evidence["proof"]["reviewed_diff_sha256"] == state["final_diff_sha256"]


def test_stacked_review_requires_both_reviewers(tmp_path):
    root = git_workspace(tmp_path)
    value = runtime(tmp_path, claude=FixtureReviewer("claude-fixture", decision="block"))
    run_id = start(value, root, reviewers=["codex", "claude"], policy="codex_and_claude")
    state = value.run_once(run_id)
    assert state["status"] == "needs_approval"
    assert state["review_verified"] is False


def test_single_p1_triggers_one_bounded_repair_with_exact_finding(tmp_path):
    root = git_workspace(tmp_path)

    class P1AtFirstTest(FixtureReviewer):
        def __init__(self):
            super().__init__("codex-fixture")
            self.test_calls = 0

        def review_stage(self, run, run_dir, stage):
            if stage == "test":
                self.test_calls += 1
                self.decision = "p1" if self.test_calls == 1 else "pass"
            else:
                self.decision = "pass"
            result = super().review_stage(run, run_dir, stage)
            if stage == "test" and self.test_calls == 1:
                result["finding"] = "P1: verify persisted done and deleted state"
            return result

    builder = FixtureBuilder()
    reviewer = P1AtFirstTest()
    value = runtime(tmp_path, builder=builder, codex=reviewer)
    run_id = start(value, root, reviewers=["codex"], policy="codex")
    assert value.run_once(run_id)["status"] == "correcting"
    first = value.get(run_id)
    assert first["repair_request"]["stage"] == "test"
    assert "persisted done" in first["repair_request"]["findings"][0]["finding"]
    assert value.run_once(run_id)["status"] == "needs_approval"
    assert builder.calls == 2
    folder = tmp_path / "state" / "runs" / run_id
    assert (folder / "builder-initial-evidence.json").is_file()
    assert (folder / "builder-repair-1-evidence.json").is_file()


def test_build_gate_p1_can_enter_the_same_bounded_repair(tmp_path):
    root = git_workspace(tmp_path)

    class P1AtFirstBuild(FixtureReviewer):
        def __init__(self):
            super().__init__("codex-fixture")
            self.build_calls = 0

        def review_stage(self, run, run_dir, stage):
            if stage == "build":
                self.build_calls += 1
                self.decision = "p1" if self.build_calls == 1 else "pass"
            else:
                self.decision = "pass"
            result = super().review_stage(run, run_dir, stage)
            if stage == "build" and self.build_calls == 1:
                result["finding"] = "P1: repair atomic write default directory"
            return result

    builder = FixtureBuilder()
    reviewer = P1AtFirstBuild()
    value = runtime(tmp_path, builder=builder, codex=reviewer)
    run_id = start(value, root, reviewers=["codex"], policy="codex")
    assert value.run_once(run_id)["status"] == "correcting"
    assert value.get(run_id)["repair_request"]["stage"] == "build"
    assert value.run_once(run_id)["status"] == "needs_approval"
    assert value.get(run_id)["corrections_used"] == 1


def test_generated_data_artifact_triggers_cleanup_repair(tmp_path):
    root = git_workspace(tmp_path)

    class CleanupBuilder(FixtureBuilder):
        def build(self, mission, workspace, run_dir, allowed, correction):
            result = super().build(mission, workspace, run_dir, allowed, correction)
            data = workspace / "test_tasks.json"
            if correction:
                data.unlink(missing_ok=True)
            else:
                data.write_text("[]")
            return result

    builder = CleanupBuilder()
    value = runtime(tmp_path, builder=builder)
    cleanup_profile = profile(root)
    cleanup_profile.allowed_write_paths.append("test_tasks.json")
    cleanup_profile.generated_paths.append("test_tasks.json")
    run_id = value.start(
        project_id="fixture", workspace=root, mission="Create the bounded TODO fixture",
        targeted_tests=[], full_tests=[[sys.executable, "-m", "pytest", "-q"]],
        profile=cleanup_profile, builder_name="glm", reviewer_names=[], review_policy="none",
    )
    assert value.run_once(run_id)["status"] == "correcting"
    assert value.get(run_id)["repair_request"]["stage"] == "cleanup"
    assert value.run_once(run_id)["status"] == "needs_approval"
    assert not (root / "test_tasks.json").exists()


def test_pause_after_plan_review_does_not_repeat_the_review(tmp_path):
    root = git_workspace(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class SlowPlanReviewer(FixtureReviewer):
        def review_stage(self, run, run_dir, stage):
            if stage == "plan" and not entered.is_set():
                entered.set()
                assert release.wait(5)
            return super().review_stage(run, run_dir, stage)

    reviewer = SlowPlanReviewer("codex-fixture")
    value = runtime(tmp_path, codex=reviewer)
    run_id = start(value, root, reviewers=["codex"], policy="codex")
    worker = threading.Thread(target=value.run_once, args=(run_id,))
    worker.start()
    assert entered.wait(5)
    assert value.pause(run_id)["control_request"] == "pause"
    release.set()
    worker.join(5)
    assert value.get(run_id)["status"] == "paused"
    assert value.get(run_id)["plan_review_completed"] is True
    assert value.resume(run_id)["status"] == "ready"
    assert value.run_once(run_id)["status"] == "needs_approval"
    assert reviewer.calls.count("plan") == 1


def test_codex_self_review_is_labelled_not_independent(tmp_path):
    root = git_workspace(tmp_path)
    value = runtime(tmp_path)
    run_id = start(value, root, builder="codex", reviewers=["codex"], policy="codex")
    run = value.get(run_id)
    assert run["is_self_review"] is True
    assert run["review_semantics"] == "self-review"


def test_production_rejects_mock_adapters(tmp_path):
    root = git_workspace(tmp_path)
    mock = SandboxBuilder(lambda *_: {"ok": True})
    value = RunRuntime(tmp_path / "state", builder=mock, builders={"glm": mock}, allow_test_adapters=False)
    with pytest.raises(RuntimeStateError, match="Mock adapter"):
        start(value, root)


def test_builder_provider_mismatch_fails_closed(tmp_path):
    root = git_workspace(tmp_path)

    class MismatchBuilder(FixtureBuilder):
        def build(self, *args):
            result = super().build(*args)
            result["provider"] = "silent-fallback"
            return result

    value = runtime(tmp_path, builder=MismatchBuilder(), production=True)
    run_id = start(value, root)
    assert value.run_once(run_id)["status"] == "blocked"


def test_failed_tests_fail_closed(tmp_path):
    root = git_workspace(tmp_path)
    value = runtime(tmp_path, builder=FixtureBuilder(value=3))
    run_id = start(value, root)
    assert value.run_once(run_id)["status"] == "failed"


def test_evidence_metadata_and_diff_include_untracked_files(tmp_path):
    root = git_workspace(tmp_path)

    class NewFileBuilder(FixtureBuilder):
        def build(self, mission, workspace, run_dir, allowed, correction):
            result = super().build(mission, workspace, run_dir, allowed, correction)
            (workspace / "tests").mkdir()
            (workspace / "tests" / "new_test.py").write_text("def test_new():\n    assert True\n")
            return result

    value = runtime(tmp_path, builder=NewFileBuilder())
    run_id = start(value, root)
    assert value.run_once(run_id)["status"] == "needs_approval"
    diff = value.get_final_diff(run_id)
    assert "tests/new_test.py" in diff["diff_content"]
    assert diff["diff_sha256"]
    metadata = value.get_evidence_metadata(run_id)
    assert "builder-evidence.json" in metadata["evidence_files"]
    final = json.loads((tmp_path / "state" / "runs" / run_id / "final-status.json").read_text())
    assert final["review_policy"] == "none"
    assert final["builder_provider"] == "fixture-real-provider"


def test_http_bubble_start_passes_selectors_to_runtime(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        payload = json.dumps({"mission": "Keep the sandbox smoke test green", "builder_name": "glm", "review_mode": "none"}).encode()
        request = urllib.request.Request(
            api.url + "quick-missions", data=payload, method="POST",
            headers={"Content-Type": "application/json", "X-JOAO-Token": api.token},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            run_id = json.loads(response.read())["run_id"]
        api.workers[run_id].join(timeout=10)
        run = api.runtime.get(run_id)
        assert run["builder_name"] == "glm"
        assert run["review_policy"] == "none"
        assert "Work only inside this disposable Git sandbox." in run["mission"]
        assert "Work only in src/ and tests/." not in run["mission"]
        assert "never create a file or directory with that name" in run["mission"]
        assert run["profile"]["allowed_write_paths"] == ["todo.py", "test_todo.py", "todo.json", "test_tasks.json", "src/", "tests/"]
        assert run["full_tests"] == [["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test*.py"]]
        assert run["profile"]["generated_paths"] == ["todo.json", "test_tasks.json"]
        assert run["status"] == "needs_approval"
        assert run["evidence_directory"]
        assert run["progress"]["total"] == 4
    finally:
        api.close()


def test_http_bubble_rejects_unauthenticated_requests(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        request = urllib.request.Request(api.url + "quick-missions", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 401
    finally:
        api.close()


def test_capabilities_expose_provider_model_auth_and_last_error(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    try:
        capabilities = api.capabilities()
        for provider in ("glm", "codex", "claude"):
            for field in ("available", "executable", "model", "provider", "auth_status",
                          "config_status", "last_error", "reason", "real_or_mock"):
                assert field in capabilities[provider]
        assert capabilities["codex"]["reviewer_available"] is True
    finally:
        api.close()


def test_codex_preflight_resolves_mac_app_minimal_path():
    builder = CodexBuilder(executable="/Users/jocelyngrosjean/.local/bin/codex")
    reviewer = CodexCLIReviewer(executable="/Users/jocelyngrosjean/.local/bin/codex")
    assert builder.preflight()["available"] is True
    assert reviewer.preflight()["available"] is True
    assert builder.preflight()["model"] == "gpt-5.6-terra"


def test_reviewer_verdict_parser_reads_jsonl_agent_messages():
    text = '{"type":"agent_message","text":"JOAO_REVIEW: ACCEPT"}'
    assert parse_review_verdict(text, "JOAO_REVIEW") == "ACCEPT"
    assert parse_review_verdict("CLAUDE_REVIEW: P1", "CLAUDE_REVIEW") == "P1"


def test_run_state_is_restored_from_disk(tmp_path):
    root = git_workspace(tmp_path)
    value = runtime(tmp_path)
    run_id = start(value, root)
    assert value.run_once(run_id)["status"] == "needs_approval"
    restored = runtime(tmp_path)
    assert restored.get(run_id)["status"] == "needs_approval"


def test_pause_resume_stop_retry_and_reject_controls(tmp_path):
    root = git_workspace(tmp_path)
    value = runtime(tmp_path)
    run_id = start(value, root)
    assert value.pause(run_id)["status"] == "paused"
    assert value.resume(run_id)["status"] == "ready"
    assert value.stop(run_id)["status"] == "stopped"

    root2 = git_workspace(tmp_path / "second")
    failed = runtime(tmp_path / "second", builder=FixtureBuilder(value=3))
    failed_id = start(failed, root2)
    assert failed.run_once(failed_id)["status"] == "failed"
    assert failed.retry(failed_id)["corrections_used"] == 1

    root3 = git_workspace(tmp_path / "third")
    rejected = runtime(tmp_path / "third")
    rejected_id = start(rejected, root3)
    assert rejected.run_once(rejected_id)["status"] == "needs_approval"
    assert rejected.reject(rejected_id)["status"] == "stopped"
