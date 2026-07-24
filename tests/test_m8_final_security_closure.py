from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

from joao_orchestrator.bubble.chat import ChatBrain
from joao_orchestrator.providers.base import ProviderRequest
from joao_orchestrator.providers.chat_cli import ChatCLIProvider, _execution_fingerprint


def _load_scan():
    path = Path(__file__).resolve().parents[1] / "scripts/scan_raw_provider_text.py"
    spec = importlib.util.spec_from_file_location("scan_raw_provider_text_v2", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_glm_permission_is_valid_json_and_fail_closed(tmp_path):
    brain = ChatBrain()
    captured = {}

    def lines(argv, *, cwd=None, env_overrides=None):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        captured["env"] = dict(env_overrides or {})
        yield json.dumps({
            "type": "session_start",
            "part": {"model": brain.glm_model},
        })
        yield json.dumps({"type": "text", "part": {"text": "ok"}})

    brain._lines = lines
    events = list(brain.reply_stream(
        "review", model="glm", execution_root=str(tmp_path), read_only=True
    ))
    permission = json.loads(captured["env"]["OPENCODE_PERMISSION"])
    assert permission["*"] == "deny"
    for key in ("edit", "bash", "task", "external_directory", "skill", "lsp"):
        assert permission[key] == "deny"
    for key in ("read", "glob", "grep", "list"):
        assert permission[key] == "allow"
    inline = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])
    assert inline["permission"] == permission
    assert captured["env"]["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "true"
    assert any(event.get("event") == "done" for event in events)


def test_claude_disallowed_tools_are_distinct_values(tmp_path):
    brain = ChatBrain()
    captured = {}

    def lines(argv, *, cwd=None, env_overrides=None):
        captured["argv"] = list(argv)
        yield json.dumps({
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {"model": "claude-sonnet-4"},
            },
        })
        yield json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "ok"},
            },
        })

    brain._lines = lines
    list(brain.reply_stream(
        "review", model="claude", execution_root=str(tmp_path), read_only=True
    ))
    argv = captured["argv"]
    index = argv.index("--disallowedTools")
    assert argv[index + 1:index + 5] == ["Bash", "Edit", "Write", "NotebookEdit"]
    assert "Bash,Edit,Write,NotebookEdit" not in argv


def test_git_control_mutation_changes_fingerprint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    before = _execution_fingerprint(repo)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-checkout").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    after = _execution_fingerprint(repo)
    assert before != after


class SpoofedGLMBrain:
    glm_model = "zai-coding-plan/glm-4.5-air"
    claude_model = "sonnet"

    def reply_stream(self, *args, **kwargs):
        yield {"event": "model", "model": "evil-glm-proxy"}
        yield {"event": "delta", "text": "answer"}
        yield {"event": "done", "text": "answer", "model": "evil-glm-proxy"}


def test_glm_substring_identity_spoof_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "joao_orchestrator.providers.chat_cli.available_brains",
        lambda: {"glm": {"available": True}},
    )
    response = ChatCLIProvider("glm", brain=SpoofedGLMBrain()).invoke(
        ProviderRequest(
            role="reviewer",
            task_id="identity",
            project_id="joao",
            prompt="review",
            worktree_path=str(tmp_path),
        )
    )
    assert response.ok is False
    assert "identity confusion" in response.error


def test_raw_scanner_detects_arbitrary_block_reason(tmp_path):
    root = tmp_path / "evidence"
    state = root / "m9-state"
    state.mkdir(parents=True)
    (state / "M9_BLOCK_REASON.txt").write_text(
        "arbitrary provider prose not present in any marker list\n",
        encoding="utf-8",
    )
    module = _load_scan()
    violations = module.scan_root(root)
    assert violations
    assert any("M9_BLOCK_REASON" in item for item in violations)


def test_raw_scanner_rejects_unhashed_reason_json(tmp_path):
    root = tmp_path / "evidence"
    runs = root / "m10-state" / "supervisor" / "runs"
    runs.mkdir(parents=True)
    (runs / "run.json").write_text(json.dumps({
        "reason": "provider returned a detailed sentence",
        "calls": [{"error": "another raw sentence"}],
    }), encoding="utf-8")
    module = _load_scan()
    violations = module.scan_root(root)
    assert any("unhashed provider text" in item for item in violations)


def test_raw_scanner_accepts_hashes_and_controller_codes(tmp_path):
    root = tmp_path / "evidence"
    runs = root / "m10-state" / "supervisor" / "runs"
    runs.mkdir(parents=True)
    (runs / "run.json").write_text(json.dumps({
        "reason": "direct provider completed",
        "calls": [{"error": "HASH:" + "a" * 16}],
    }), encoding="utf-8")
    module = _load_scan()
    assert module.scan_root(root) == []
