from __future__ import annotations

import json
import urllib.request

from joao_orchestrator.bubble.api import LocalAPIServer
from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder
from joao_orchestrator.providers.base import ProviderAdapter, ProviderRequest, ProviderResponse
from joao_orchestrator.providers.bridge_factory import build_default_bridge
from joao_orchestrator.providers.chat_cli import ChatCLIProvider
from joao_orchestrator.providers.codex_review import CodexReviewProvider, parse_codex_jsonl
from joao_orchestrator.providers.subprocess_cli import CLIExecutionResult
from joao_orchestrator.supervisor import ProviderBridge, SupervisorCore


class FakeBrain:
    claude_model = "sonnet"
    glm_model = "zai-coding-plan/glm-4.5-air"

    def __init__(self, events):
        self.events = list(events)
        self.calls = []

    def reply_stream(self, prompt, *, model, execution_root=None, read_only=False, **kwargs):
        self.calls.append((prompt, model, execution_root, read_only))
        yield from self.events


class OneShotProvider(ProviderAdapter):
    name = "one-shot"
    supported_roles = ("planner",)

    def is_enabled(self):
        return True

    def invoke(self, request):
        return ProviderResponse(
            role=request.role,
            task_id=request.task_id,
            ok=True,
            content="supervised answer",
            provider_name=self.name,
        )


def _runtime(tmp_path):
    def build(_mission, _workspace, _correction):
        return {"ok": True}
    return RunRuntime(
        tmp_path / "state",
        builder=SandboxBuilder(build),
        profiles=LocalProfileAdapter(),
    )


def _get(api, path):
    req = urllib.request.Request(
        api.url + path,
        headers={"X-JOAO-Token": api.token},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.status, json.loads(response.read())


def _post(api, path, body):
    req = urllib.request.Request(
        api.url + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-JOAO-Token": api.token,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.status, json.loads(response.read())


def test_chat_cli_provider_collects_stream_without_write_tier(monkeypatch):
    brain = FakeBrain([
        {"event": "model", "model": "zai-coding-plan/glm-4.5-air"},
        {"event": "delta", "text": "hello "},
        {"event": "delta", "text": "world"},
        {"event": "done", "text": "hello world", "model": "zai-coding-plan/glm-4.5-air"},
    ])
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"glm": {"available": True}},
    )
    provider = ChatCLIProvider("glm", brain=brain)
    response = provider.invoke(ProviderRequest(
        role="reviewer",
        task_id="t1",
        project_id="p1",
        prompt="review this",
    ))
    assert provider.is_enabled() is True
    assert response.ok is True
    assert response.content == "hello world"
    assert brain.calls[0][0] == "review this"
    assert brain.calls[0][1] == "glm"
    assert brain.calls[0][3] is True  # read_only enforcement


def test_chat_cli_provider_fails_honestly_on_error(monkeypatch):
    brain = FakeBrain([{"event": "error", "message": "provider timeout"}])
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"claude": {"available": True}},
    )
    provider = ChatCLIProvider("claude", brain=brain)
    response = provider.invoke(ProviderRequest(
        role="planner",
        task_id="t1",
        project_id="p1",
        prompt="plan",
    ))
    assert response.ok is False
    assert "timeout" in response.error


def test_chat_cli_provider_rejects_coder_role(monkeypatch):
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"glm": {"available": True}},
    )
    provider = ChatCLIProvider("glm", brain=FakeBrain([]))
    response = provider.invoke(ProviderRequest(
        role="coder",
        task_id="t1",
        project_id="p1",
        prompt="write",
    ))
    assert response.ok is False
    assert "unsupported role" in response.error


def test_default_bridge_registers_real_adapters_without_paid_fallback():
    bridge = build_default_bridge(timeout_seconds=1)
    assert bridge.names() == (
        "claude-chat",
        "codex-review",
        "codex-subscription",
        "glm-chat",
        "opencode-zai",
        "zai-coding-plan",
    )
    assert bridge.descriptor("claude-chat").family == "anthropic"
    assert bridge.descriptor("codex-review").family == "openai"
    assert "reviewer" in bridge.descriptor("codex-review").roles
    assert bridge.descriptor("codex-subscription").family == "openai"
    assert bridge.descriptor("glm-chat").family == "zai"
    assert "reviewer" in bridge.descriptor("glm-chat").roles
    assert bridge.descriptor("opencode-zai").roles == ("coder",)


def test_supervisor_api_exposes_health_and_runs_direct_mode(tmp_path):
    bridge = ProviderBridge()
    bridge.register(
        OneShotProvider(),
        family="test-family",
        model="test-model",
        priority=10,
    )
    supervisor = SupervisorCore(bridge, tmp_path / "supervisor-state")
    api = LocalAPIServer(_runtime(tmp_path), supervisor=supervisor)
    api.serve_in_thread()
    try:
        status, health = _get(api, "supervisor/providers")
        assert status == 200
        assert health["available"] is True
        assert health["providers"][0]["name"] == "one-shot"
        assert health["safety"]["auto_promote"] is False

        status, result = _post(api, "supervisor/run", {
            "task_id": "task-api",
            "project_id": "project-api",
            "prompt": "answer safely",
            "mode": "direct",
            "role": "planner",
            "preferred_provider": "one-shot",
        })
        assert status == 200
        assert result["status"] == "completed"
        assert result["selected_provider"] == "one-shot"
        assert result["final_content"] == "supervised answer"
    finally:
        api.close()


def test_supervisor_api_reports_not_configured(tmp_path):
    api = LocalAPIServer(_runtime(tmp_path))
    api.serve_in_thread()
    try:
        status, health = _get(api, "supervisor/providers")
        assert status == 200
        assert health == {
            "available": False,
            "reason": "supervisor not configured",
            "providers": [],
        }
    finally:
        api.close()


class FakeCodexEngine:
    def __init__(self, stdout: str, *, returncode: int = 0, mutate=None):
        self.stdout = stdout
        self.returncode = returncode
        self.mutate = mutate
        self.calls = []

    def is_available(self):
        return True

    def has_required_auth(self):
        return True

    def run_argv(self, cwd, argv_tail, **kwargs):
        self.calls.append((cwd, list(argv_tail), kwargs))
        if self.mutate:
            self.mutate(cwd)
        return CLIExecutionResult(
            engine_name="codex-review",
            argv=["codex", *list(argv_tail)],
            cwd=str(cwd),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr="" if self.returncode == 0 else "failure",
            started_at="start",
            finished_at="finish",
            duration_seconds=0.01,
        )


def _git_repo(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    return repo


def _codex_stream(*messages, completed=True):
    rows = [{"type": "turn.started"}]
    for index, message in enumerate(messages):
        rows.append({
            "type": "item.completed",
            "item": {"id": f"item-{index}", "type": "agent_message", "text": message},
        })
    if completed:
        rows.append({"type": "turn.completed", "usage": {}})
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def test_parse_codex_jsonl_uses_last_completed_agent_message():
    assert parse_codex_jsonl(_codex_stream("progress", "final answer")) == "final answer"


def test_parse_codex_jsonl_rejects_incomplete_or_quota_content():
    import pytest
    with pytest.raises(ValueError, match="did not complete"):
        parse_codex_jsonl(_codex_stream("partial", completed=False))
    with pytest.raises(ValueError, match="weekly limit"):
        parse_codex_jsonl(_codex_stream("You've hit your weekly limit"))


def test_codex_review_provider_is_read_only_and_parses_final_message(tmp_path):
    repo = _git_repo(tmp_path)
    engine = FakeCodexEngine(_codex_stream("JOAO_M10_OK"))
    provider = CodexReviewProvider(engine)
    response = provider.invoke(ProviderRequest(
        role="reviewer", task_id="t", project_id="p", prompt="marker",
        worktree_path=str(repo),
    ))
    assert response.ok is True
    assert response.content == "JOAO_M10_OK"
    assert "read-only" in engine.calls[0][1]
    assert "--ephemeral" in engine.calls[0][1]


def test_codex_review_provider_rejects_execution_root_mutation(tmp_path):
    repo = _git_repo(tmp_path)
    engine = FakeCodexEngine(
        _codex_stream("answer"),
        mutate=lambda root: (root / "unexpected.txt").write_text("mutation\n"),
    )
    response = CodexReviewProvider(engine).invoke(ProviderRequest(
        role="reviewer", task_id="t", project_id="p", prompt="review",
        worktree_path=str(repo),
    ))
    assert response.ok is False
    assert "mutated" in response.error


def test_codex_review_provider_detects_non_git_sandbox_mutation(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    engine = FakeCodexEngine(
        _codex_stream("answer"),
        mutate=lambda path: (path / "created.txt").write_text("mutation\n"),
    )
    response = CodexReviewProvider(engine).invoke(ProviderRequest(
        role="reviewer", task_id="t", project_id="p", prompt="review",
        worktree_path=str(root),
    ))
    assert response.ok is False
    assert "mutated" in response.error


def test_chat_cli_provider_rejects_quota_message(monkeypatch):
    brain = FakeBrain([
        {"event": "delta", "text": "You've hit your weekly limit"},
        {"event": "done", "text": "You've hit your weekly limit", "model": "claude-sonnet-4"},
    ])
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"claude": {"available": True}},
    )
    response = ChatCLIProvider("claude", brain=brain).invoke(ProviderRequest(
        role="reviewer", task_id="t", project_id="p", prompt="review",
    ))
    assert response.ok is False
    assert "weekly limit" in response.error
