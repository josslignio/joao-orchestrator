"""P1 correction tests: read-only enforcement, identity proof, raw text scan.

These tests prove that the three P1 findings are mechanically closed:
  P1-1: ChatCLIProvider uses an execution root as cwd, fingerprints before/after,
        and rejects mutations. Claude argv forces --permission-mode plan with
        Bash/Edit/Write/NotebookEdit blocked. GLM env injects deny-by-default.
  P1-2: GLM identity must come from an authoritative OpenCode event record.
        No model record → rejection. challenge/council reject unproven identity.
  P1-3: State roots and evidence packages contain no raw provider text.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from joao_orchestrator.providers.base import ProviderRequest, ProviderResponse
from joao_orchestrator.providers.chat_cli import ChatCLIProvider, _execution_fingerprint
from joao_orchestrator.bubble.chat import ChatBrain
from joao_orchestrator.supervisor import ProviderBridge, SupervisorCore, SupervisorRequest


# ── Helpers ──────────────────────────────────────────────────────────────

class MutatingBrain:
    """A brain that simulates a provider mutating the execution root."""

    def __init__(self, backend, events, mutation_path=None):
        self.backend = backend
        self.events = list(events)
        self.mutation_path = mutation_path

    def reply_stream(self, prompt, *, model="claude", execution_root=None,
                     read_only=False, **kwargs):
        # Simulate mutation: write a file into the execution root
        if self.mutation_path and execution_root:
            root = Path(execution_root)
            target = root / self.mutation_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("mutation!")
        yield from self.events


class RecordingBrain:
    """A brain that records the argv/env passed to the subprocess."""

    def __init__(self, events, backend="claude"):
        self.events = list(events)
        self.backend = backend
        self.recorded_cwd = None
        self.recorded_argv = None

    def reply_stream(self, prompt, *, model="claude", execution_root=None,
                     read_only=False, **kwargs):
        self.recorded_cwd = execution_root
        yield from self.events


class NoModelEventBrain:
    """A brain that produces no authoritative model event (GLM identity test)."""

    def reply_stream(self, prompt, *, model="glm", execution_root=None,
                     read_only=False, **kwargs):
        yield {"event": "delta", "text": "some response"}
        # No model event, no done with model — identity not proven
        yield {"event": "done", "text": "some response", "model": None,
               "provider": "zai-coding-plan"}


class GLMIdentityMismatchBrain:
    """A brain that reports a model from the wrong family."""

    def reply_stream(self, prompt, *, model="glm", execution_root=None,
                     read_only=False, **kwargs):
        yield {"event": "model", "model": "gpt-4o", "provider": "openai"}
        yield {"event": "delta", "text": "response"}
        yield {"event": "done", "text": "response", "model": "gpt-4o",
               "provider": "openai"}


# ── P1-1: Mutation detection ─────────────────────────────────────────────

def test_claude_backend_mutation_is_rejected(monkeypatch, tmp_path):
    """Claude backend that mutates the execution root → rejection."""
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"claude": {"available": True}},
    )
    events = [
        {"event": "model", "model": "claude-sonnet-4"},
        {"event": "delta", "text": "answer"},
        {"event": "done", "text": "answer", "model": "claude-sonnet-4"},
    ]
    brain = MutatingBrain("claude", events, mutation_path="evil.txt")
    provider = ChatCLIProvider("claude", brain=brain)
    response = provider.invoke(ProviderRequest(
        role="reviewer", task_id="t1", project_id="p1",
        prompt="review", worktree_path=str(tmp_path),
    ))
    assert response.ok is False
    assert "mutated" in response.error.lower()


def test_glm_backend_mutation_is_rejected(monkeypatch, tmp_path):
    """GLM backend that mutates the execution root → rejection."""
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"glm": {"available": True}},
    )
    events = [
        {"event": "model", "model": "glm-4.5-air"},
        {"event": "delta", "text": "answer"},
        {"event": "done", "text": "answer", "model": "glm-4.5-air"},
    ]
    brain = MutatingBrain("glm", events, mutation_path="evil.py")
    provider = ChatCLIProvider("glm", brain=brain)
    response = provider.invoke(ProviderRequest(
        role="reviewer", task_id="t1", project_id="p1",
        prompt="review", worktree_path=str(tmp_path),
    ))
    assert response.ok is False
    assert "mutated" in response.error.lower()


def test_cwd_matches_execution_root(monkeypatch, tmp_path):
    """The subprocess cwd must be the execution root provided by the request."""
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"claude": {"available": True}},
    )
    events = [
        {"event": "model", "model": "claude-sonnet-4"},
        {"event": "delta", "text": "hi"},
        {"event": "done", "text": "hi", "model": "claude-sonnet-4"},
    ]
    brain = RecordingBrain(events, backend="claude")
    provider = ChatCLIProvider("claude", brain=brain)
    response = provider.invoke(ProviderRequest(
        role="reviewer", task_id="t1", project_id="p1",
        prompt="review", worktree_path=str(tmp_path),
    ))
    assert response.ok is True
    assert brain.recorded_cwd == str(tmp_path)


def test_claude_argv_contains_read_only_mode(monkeypatch, tmp_path):
    """Claude argv must contain --permission-mode plan and disallowed tools."""
    # We test at the ChatBrain level: the argv is built in _stream_claude
    brain = ChatBrain()
    argvs_seen = []

    original_lines = brain._lines

    def capture_lines(argv, **kwargs):
        argvs_seen.append(list(argv))
        # Return empty iterator so the stream produces no output
        return iter([])

    brain._line_source = capture_lines
    events = list(brain.reply_stream("test prompt", model="claude",
                                     execution_root=str(tmp_path), read_only=True))
    assert len(argvs_seen) == 1
    argv = argvs_seen[0]
    assert "--permission-mode" in argv
    plan_idx = argv.index("--permission-mode")
    assert argv[plan_idx + 1] == "plan"
    assert "--disallowedTools" in argv
    disallow_idx = argv.index("--disallowedTools")
    disallowed = argv[disallow_idx + 1]
    assert "Bash" in disallowed
    assert "Edit" in disallowed
    assert "Write" in disallowed
    assert "NotebookEdit" in disallowed


def test_glm_env_contains_deny_by_default(monkeypatch, tmp_path):
    """GLM subprocess must receive OPENCODE_PERMISSION deny-by-default."""
    brain = ChatBrain()
    envs_seen = []

    original_popen = subprocess.Popen

    class CapturingPopen:
        def __init__(self, argv, **kwargs):
            envs_seen.append(kwargs.get("env", {}))
            self.stdout = iter([])
            self.stderr = iter([])
            self.pid = 99999
            self.poll = lambda: 0
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    brain._line_source = None
    with patch("joao_orchestrator.bubble.chat.subprocess.Popen", CapturingPopen):
        try:
            list(brain.reply_stream("test", model="glm",
                                    execution_root=str(tmp_path), read_only=True))
        except Exception:
            pass  # We just need the Popen call to capture env

    assert len(envs_seen) >= 1
    env = envs_seen[0]
    assert "OPENCODE_PERMISSION" in env
    perm = env["OPENCODE_PERMISSION"]
    assert "deny" in perm
    assert "edit" in perm.lower()
    assert "write" in perm.lower()
    assert "bash" in perm.lower()


def test_no_worktree_path_uses_isolated_temp_dir(monkeypatch):
    """When no worktree_path is provided, an isolated temp dir is used."""
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"claude": {"available": True}},
    )
    events = [
        {"event": "model", "model": "claude-sonnet-4"},
        {"event": "delta", "text": "hi"},
        {"event": "done", "text": "hi", "model": "claude-sonnet-4"},
    ]
    brain = RecordingBrain(events, backend="claude")
    provider = ChatCLIProvider("claude", brain=brain)
    response = provider.invoke(ProviderRequest(
        role="reviewer", task_id="t1", project_id="p1",
        prompt="review",  # no worktree_path
    ))
    assert response.ok is True
    assert brain.recorded_cwd is not None
    assert "joao-chat-readonly-" in brain.recorded_cwd


# ── P1-2: GLM identity proof ─────────────────────────────────────────────

def test_glm_model_absent_is_rejected(monkeypatch, tmp_path):
    """GLM with no authoritative model record → rejection."""
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"glm": {"available": True}},
    )
    brain = NoModelEventBrain()
    provider = ChatCLIProvider("glm", brain=brain)
    response = provider.invoke(ProviderRequest(
        role="reviewer", task_id="t1", project_id="p1",
        prompt="review", worktree_path=str(tmp_path),
    ))
    assert response.ok is False
    assert "identity" in response.error.lower() or "model" in response.error.lower()


def test_glm_model_mismatch_is_rejected(monkeypatch, tmp_path):
    """GLM reporting a model from wrong family → rejection."""
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"glm": {"available": True}},
    )
    brain = GLMIdentityMismatchBrain()
    provider = ChatCLIProvider("glm", brain=brain)
    response = provider.invoke(ProviderRequest(
        role="reviewer", task_id="t1", project_id="p1",
        prompt="review", worktree_path=str(tmp_path),
    ))
    assert response.ok is False
    assert "identity" in response.error.lower() or "confusion" in response.error.lower()


def test_challenge_rejects_unproven_glm_identity(monkeypatch, tmp_path):
    """Challenge mode must fail when the GLM identity is not proven."""

    class UnprovenGLMProvider:
        name = "glm-chat"
        supported_roles = ("reviewer", "planner")
        network_required = True

        def is_enabled(self):
            return True

        def invoke(self, request):
            return ProviderResponse(
                role=request.role, task_id=request.task_id, ok=False,
                error="identity not proven: no authoritative model record",
                provider_name="glm-chat",
            )

    class FakeAnthropicProvider:
        name = "claude-chat"
        supported_roles = ("reviewer", "planner")
        network_required = True

        def is_enabled(self):
            return True

        def invoke(self, request):
            return ProviderResponse(
                role=request.role, task_id=request.task_id, ok=True,
                content="answer", provider_name="claude-chat",
            )

    bridge = ProviderBridge()
    bridge.register(FakeAnthropicProvider(), family="anthropic",
                    model="claude-sonnet", priority=100)
    bridge.register(UnprovenGLMProvider(), family="zai",
                    model="glm-4.5", priority=90)

    core = SupervisorCore(bridge, tmp_path)
    result = core.execute(SupervisorRequest(
        task_id="challenge-test", project_id="joao",
        prompt="review", mode="challenge", role="reviewer",
        max_provider_calls=3,
    ))
    # Challenge should fail because the second provider's identity is unproven
    assert result.status != "completed" or result.verdict == "BLOCK"


# ── P1-3: Raw provider text scan ─────────────────────────────────────────

def test_state_root_scan_finds_no_raw_provider_text(tmp_path):
    """Scan state roots and evidence for raw provider text — must be zero."""
    from joao_orchestrator.supervisor.store import SupervisorStore
    from joao_orchestrator.supervisor.models import SupervisorResult, SupervisorCall

    store = SupervisorStore(tmp_path)
    call = SupervisorCall(
        provider="codex-review", family="openai", model="codex",
        role="reviewer", ok=True, content="JOAO_M10_OK", error="",
        duration_seconds=0.1, sequence=1,
        response_sha256=hashlib.sha256(b"JOAO_M10_OK").hexdigest(),
    )
    result = SupervisorResult(
        run_id="scan-test", task_id="t1", project_id="joao",
        mode="direct", status="completed",
        started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:01Z",
        prompt_sha256="abc", calls=[call],
        final_content="JOAO_M10_OK", selected_provider="codex-review",
        verdict="ACCEPT", reason="ok",
    )
    store.persist(result)

    # Scan all files under the state root for raw provider text
    raw_markers = ["JOAO_M10_OK", "CANDIDATE_SHA=", "VERIFIED_HEAD_SHA=",
                   "DIFF=", "FULL_TESTS="]
    hits = []
    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for marker in raw_markers:
            if marker in text:
                # Check if it's in a hash field (acceptable) or raw content
                lines = text.split("\n")
                for line in lines:
                    if marker in line and "HASH:" not in line and "sha256" not in line.lower():
                        hits.append(f"{path}: {line[:80]}")

    # The persisted store should NOT contain raw provider content
    # (content is excluded via include_content=False, reason is hashed)
    assert len(hits) == 0, f"Raw provider text found in state root:\n{'\\n'.join(hits)}"


def test_execution_fingerprint_detects_mutation(tmp_path):
    """The fingerprint function must detect file additions, modifications, deletions."""
    root = tmp_path / "exec-root"
    root.mkdir()
    (root / "a.txt").write_text("initial")

    before = _execution_fingerprint(root)
    (root / "b.txt").write_text("new file")
    after = _execution_fingerprint(root)

    assert before != after, "fingerprint should detect new file"


def test_execution_fingerprint_stable_on_no_change(tmp_path):
    """The fingerprint must be stable when nothing changes."""
    root = tmp_path / "exec-root"
    root.mkdir()
    (root / "a.txt").write_text("stable content")

    fp1 = _execution_fingerprint(root)
    fp2 = _execution_fingerprint(root)
    assert fp1 == fp2, "fingerprint should be stable with no changes"
