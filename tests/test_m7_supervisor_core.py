from __future__ import annotations

import json
import time
from pathlib import Path

from joao_orchestrator.providers.base import ProviderAdapter, ProviderRequest, ProviderResponse
from joao_orchestrator.supervisor import (
    ProviderBridge,
    SupervisorCore,
    SupervisorMode,
    SupervisorRequest,
)


class ScriptedProvider(ProviderAdapter):
    def __init__(self, name: str, roles, responses, *, enabled=True, delay=0.0):
        self.name = name
        self.supported_roles = tuple(roles)
        self.responses = list(responses)
        self.enabled = enabled
        self.delay = delay
        self.requests = []

    def is_enabled(self):
        return self.enabled

    def invoke(self, request: ProviderRequest):
        self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        value = self.responses.pop(0) if self.responses else "default"
        if isinstance(value, Exception):
            raise value
        if isinstance(value, dict):
            ok = value.get("ok", True)
            content = value.get("content", "")
            error = value.get("error", "")
            provider_name = value.get("provider_name", self.name)
            task_id = value.get("task_id", request.task_id)
            role = value.get("role", request.role)
        else:
            ok, content, error = True, str(value), ""
            provider_name, task_id, role = self.name, request.task_id, request.role
        return ProviderResponse(
            role=role,
            task_id=task_id,
            ok=ok,
            content=content,
            error=error,
            provider_name=provider_name,
        )


def register(bridge, provider, family, priority):
    bridge.register(provider, family=family, model=provider.name + "-model", priority=priority)


def request(mode, **kwargs):
    return SupervisorRequest(
        task_id="task-1",
        project_id="project-1",
        prompt="Solve this carefully",
        mode=mode,
        max_provider_calls=kwargs.pop("max_provider_calls", 5),
        **kwargs,
    )


def test_direct_calls_exact_provider_and_persists_no_raw_prompt(tmp_path):
    bridge = ProviderBridge()
    alpha = ScriptedProvider("alpha", ("planner",), ["answer-a"])
    beta = ScriptedProvider("beta", ("planner",), ["answer-b"])
    register(bridge, alpha, "family-a", 10)
    register(bridge, beta, "family-b", 20)
    core = SupervisorCore(bridge, tmp_path)

    result = core.execute(request(SupervisorMode.DIRECT.value, preferred_provider="alpha"))

    assert result.status == "completed"
    assert result.selected_provider == "alpha"
    assert result.final_content == "answer-a"
    assert len(alpha.requests) == 1
    assert beta.requests == []
    saved = json.loads((tmp_path / "supervisor" / "runs" / f"{result.run_id}.json").read_text())
    assert "Solve this carefully" not in json.dumps(saved)
    assert saved["prompt_sha256"] == result.prompt_sha256
    assert saved["calls"][0]["content_chars"] == len("answer-a")


def test_auto_priority_then_bounded_fallback(tmp_path):
    bridge = ProviderBridge()
    first = ScriptedProvider("first", ("planner",), [{"ok": False, "error": "down"}])
    second = ScriptedProvider("second", ("planner",), ["recovered"])
    register(bridge, first, "f1", 100)
    register(bridge, second, "f2", 50)

    result = SupervisorCore(bridge, tmp_path).execute(request(SupervisorMode.AUTO.value))

    assert result.status == "completed"
    assert result.selected_provider == "second"
    assert [c.provider for c in result.calls] == ["first", "second"]
    assert result.routing["policy"] == "priority_then_bounded_fallback"


def test_auto_respects_call_budget(tmp_path):
    bridge = ProviderBridge()
    for index in range(3):
        provider = ScriptedProvider(f"p{index}", ("planner",), [{"ok": False, "error": "no"}])
        register(bridge, provider, f"family-{index}", 100-index)
    result = SupervisorCore(bridge, tmp_path).execute(
        request(SupervisorMode.AUTO.value, max_provider_calls=2)
    )
    assert result.status == "blocked"
    assert len(result.calls) == 2
    assert "all auto candidates failed" in result.reason


def test_bridge_rejects_forged_provider_identity(tmp_path):
    bridge = ProviderBridge()
    forged = ScriptedProvider(
        "registered",
        ("planner",),
        [{"provider_name": "different", "content": "forged"}],
    )
    register(bridge, forged, "family-a", 10)
    result = SupervisorCore(bridge, tmp_path).execute(
        request(SupervisorMode.DIRECT.value, preferred_provider="registered")
    )
    assert result.status == "blocked"
    assert "identity mismatch" in result.reason


def test_challenge_requires_three_independent_families(tmp_path):
    bridge = ProviderBridge()
    register(bridge, ScriptedProvider("a", ("planner",), ["a"]), "fa", 100)
    register(bridge, ScriptedProvider("b", ("planner",), ["b"]), "fb", 90)
    result = SupervisorCore(bridge, tmp_path).execute(request(SupervisorMode.CHALLENGE.value))
    assert result.status == "blocked"
    assert "independent provider" in result.reason


def test_challenge_selects_judged_winner(tmp_path):
    bridge = ProviderBridge()
    a = ScriptedProvider("a", ("planner",), ["primary answer"])
    b = ScriptedProvider("b", ("planner",), ["challenger answer"])
    judge = ScriptedProvider(
        "judge",
        ("reviewer",),
        [json.dumps({"verdict": "ACCEPT", "winner_provider": "b", "reason": "b is better"})],
    )
    register(bridge, a, "fa", 100)
    register(bridge, b, "fb", 90)
    register(bridge, judge, "fj", 80)

    result = SupervisorCore(bridge, tmp_path).execute(request(SupervisorMode.CHALLENGE.value))

    assert result.status == "completed"
    assert result.selected_provider == "b"
    assert result.final_content == "challenger answer"
    assert [c.provider for c in result.calls] == ["a", "b", "judge"]
    assert "candidates=" in judge.requests[0].prompt


def test_challenge_malformed_judge_fails_closed(tmp_path):
    bridge = ProviderBridge()
    register(bridge, ScriptedProvider("a", ("planner",), ["a"]), "fa", 100)
    register(bridge, ScriptedProvider("b", ("planner",), ["b"]), "fb", 90)
    register(bridge, ScriptedProvider("judge", ("reviewer",), ["not-json"]), "fj", 80)
    result = SupervisorCore(bridge, tmp_path).execute(request(SupervisorMode.CHALLENGE.value))
    assert result.status == "blocked"
    assert result.verdict == "BLOCK"
    assert "invalid JSON" in result.reason


def test_council_runs_participants_in_parallel_and_uses_independent_judge(tmp_path):
    bridge = ProviderBridge()
    a = ScriptedProvider("a", ("planner",), ["a-answer"], delay=0.12)
    b = ScriptedProvider("b", ("planner",), ["b-answer"], delay=0.12)
    judge = ScriptedProvider(
        "judge", ("reviewer",),
        [json.dumps({"verdict": "ACCEPT", "winner_provider": "a", "reason": "best evidence"})],
    )
    register(bridge, a, "fa", 100)
    register(bridge, b, "fb", 90)
    register(bridge, judge, "fj", 80)
    core = SupervisorCore(bridge, tmp_path, max_parallel_workers=2)

    started = time.monotonic()
    result = core.execute(request(SupervisorMode.COUNCIL.value, max_provider_calls=3))
    elapsed = time.monotonic() - started

    assert result.status == "completed"
    assert result.selected_provider == "a"
    assert elapsed < 0.22
    assert result.calls[-1].provider == "judge"


def test_builder_reviewer_requires_different_family_and_accepts_json(tmp_path):
    bridge = ProviderBridge()
    builder = ScriptedProvider("builder", ("coder",), ["candidate patch"])
    reviewer = ScriptedProvider(
        "reviewer", ("reviewer",),
        [json.dumps({"verdict": "ACCEPT", "reason": "requirements met"})],
    )
    register(bridge, builder, "build-family", 100)
    register(bridge, reviewer, "review-family", 90)

    result = SupervisorCore(bridge, tmp_path).execute(
        request(
            SupervisorMode.BUILDER_REVIEWER.value,
            preferred_provider="builder",
            judge_provider="reviewer",
        )
    )

    assert result.status == "completed"
    assert result.verdict == "ACCEPT"
    assert result.final_content == "candidate patch"
    assert reviewer.requests[0].role == "reviewer"


def test_builder_reviewer_fix_required_never_auto_approves(tmp_path):
    bridge = ProviderBridge()
    register(bridge, ScriptedProvider("builder", ("coder",), ["candidate"]), "fa", 100)
    register(
        bridge,
        ScriptedProvider(
            "reviewer", ("reviewer",),
            [json.dumps({"verdict": "FIX_REQUIRED", "reason": "missing test"})],
        ),
        "fb", 90,
    )
    result = SupervisorCore(bridge, tmp_path).execute(
        request(SupervisorMode.BUILDER_REVIEWER.value)
    )
    assert result.status == "needs_human"
    assert result.needs_human is True
    assert result.verdict == "FIX_REQUIRED"


def test_same_family_reviewer_is_rejected(tmp_path):
    bridge = ProviderBridge()
    register(bridge, ScriptedProvider("builder", ("coder",), ["candidate"]), "same", 100)
    register(
        bridge,
        ScriptedProvider("reviewer", ("reviewer",), [json.dumps({"verdict": "ACCEPT", "reason": "ok"})]),
        "same", 90,
    )
    result = SupervisorCore(bridge, tmp_path).execute(
        request(SupervisorMode.BUILDER_REVIEWER.value)
    )
    assert result.status == "blocked"
    assert "independent provider" in result.reason
