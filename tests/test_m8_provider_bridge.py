from __future__ import annotations

import json
import urllib.request

from joao_orchestrator.bubble.api import LocalAPIServer
from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder
from joao_orchestrator.providers.base import ProviderAdapter, ProviderRequest, ProviderResponse
from joao_orchestrator.providers.bridge_factory import build_default_bridge
from joao_orchestrator.providers.chat_cli import ChatCLIProvider
from joao_orchestrator.supervisor import ProviderBridge, SupervisorCore


class FakeBrain:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []

    def reply_stream(self, prompt, *, model):
        self.calls.append((prompt, model))
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
        {"event": "model", "model": "glm-real"},
        {"event": "delta", "text": "hello "},
        {"event": "delta", "text": "world"},
        {"event": "done", "text": "hello world", "model": "glm-real"},
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
    assert brain.calls == [("review this", "glm")]


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
        "codex-subscription",
        "glm-chat",
        "opencode-zai",
        "zai-coding-plan",
    )
    assert bridge.descriptor("claude-chat").family == "anthropic"
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
