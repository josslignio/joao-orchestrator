"""V2.2 — chat endpoints over the real local HTTP server (fake brain, no real CLI)."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from joao_orchestrator.bubble.api import LocalAPIServer
from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder


def _server(tmp_path):
    def build(_m, workspace, _c):
        return {"ok": True}
    rt = RunRuntime(tmp_path / "state", builder=SandboxBuilder(build), profiles=LocalProfileAdapter())
    return LocalAPIServer(rt)


def _post(api, path, body):
    req = urllib.request.Request(api.url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read()


def _get(api, path):
    req = urllib.request.Request(api.url + path, headers={"X-JOAO-Token": api.token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def test_classify_endpoint_routes_hello_to_chat(tmp_path):
    api = _server(tmp_path); api.serve_in_thread()
    try:
        _, raw = _post(api, "chat/classify", {"message": "Hello ca va ?", "mode": "auto"})
        assert json.loads(raw)["intent"] == "CHAT"
        # a build request routes to MISSION_CODE
        _, raw = _post(api, "chat/classify", {"message": "crée un script primes.py avec tests"})
        assert json.loads(raw)["intent"] == "MISSION_CODE"
        # the routing decision is logged (auditable)
        assert (tmp_path / "state" / "chat" / "routing-log.jsonl").is_file()
    finally:
        api.close()


def test_attach_endpoint_extracts_text(tmp_path):
    api = _server(tmp_path); api.serve_in_thread()
    try:
        content = base64.b64encode("colonne_a,colonne_b\n1,2\n".encode()).decode()
        _, raw = _post(api, "chat/attach", {"name": "data.csv", "content_base64": content})
        out = json.loads(raw)
        assert out["extraction"]["ok"] and out["extraction"]["kind"] == "text"
        assert out["id"].startswith("att-")
    finally:
        api.close()


def test_chat_stream_persists_and_shows_model(tmp_path):
    api = _server(tmp_path)
    # inject a fake claude stream so no real CLI runs
    def fake_lines(_argv):
        return [
            json.dumps({"type": "stream_event", "event": {"type": "message_start",
                        "message": {"model": "claude-sonnet-5"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "Ça va bien, merci !"}}}),
            json.dumps({"type": "result", "result": "Ça va bien, merci !"}),
        ]
    api.brain._line_source = fake_lines
    api.serve_in_thread()
    try:
        req = urllib.request.Request(api.url + "chat", method="POST",
                                     data=json.dumps({"message": "ça va ?", "model": "claude"}).encode(),
                                     headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
        events = []
        with urllib.request.urlopen(req, timeout=10) as resp:
            for block in resp.read().decode().split("\n\n"):
                if block.startswith("data: "):
                    events.append(json.loads(block[6:]))
        kinds = [e["event"] for e in events]
        assert "model" in kinds and "delta" in kinds and "done" in kinds and "saved" in kinds
        model_ev = next(e for e in events if e["event"] == "model")
        assert model_ev["model"] == "claude-sonnet-5"
        cid = next(e for e in events if e["event"] == "start")["conversation"]
        # persisted: history has the user + assistant turn with the real model recorded
        hist = _get(api, "chat/history/" + cid)
        roles = [m["role"] for m in hist["messages"]]
        assert roles == ["user", "assistant"]
        assert hist["messages"][1]["model"] == "claude-sonnet-5"
        assert hist["messages"][1]["content"] == "Ça va bien, merci !"
    finally:
        api.close()


def test_capabilities_declares_web_unavailable(tmp_path):
    api = _server(tmp_path); api.serve_in_thread()
    try:
        caps = _get(api, "capabilities")
        assert caps["web_search"]["available"] is False   # honest: web not wired
        assert "chat" in caps and "claude" in caps["chat"] and "glm" in caps["chat"]
    finally:
        api.close()


def test_capabilities_reports_worker_host_and_active_worker_identity(tmp_path):
    """Boss directive (2026-07-20/21): the UI must be able to show worker-host
    health, the selected builder/reviewer, and ClaudeCodeBuilder's disabled
    reason — never silently succeed or fail with no visible cause."""
    api = _server(tmp_path); api.serve_in_thread()
    try:
        caps = _get(api, "capabilities")
        assert "worker_host" in caps
        assert "ok" in caps["worker_host"]  # a controlled shape either way
        assert caps["active_builder"]["provider"] == "sandbox"  # this fixture's own SandboxBuilder
        assert caps["claude_builder_status"]["ok"] is False
        assert caps["claude_builder_status"]["reason_code"] == "CLAUDE_BUILDER_UNAVAILABLE"
    finally:
        api.close()


def test_chat_mission_intent_route_never_fabricates_a_mission(tmp_path):
    api = _server(tmp_path); api.serve_in_thread()
    try:
        status, body = _post(api, "chat/mission-intent", {"message": "what's the weather"})
        result = json.loads(body)
        assert result["ok"] is False
        assert result["action_type"] == "NOT_A_MISSION_INTENT"
    finally:
        api.close()


def test_chat_mission_intent_route_requires_a_message(tmp_path):
    import urllib.error
    api = _server(tmp_path); api.serve_in_thread()
    try:
        try:
            _post(api, "chat/mission-intent", {})
            assert False, "expected an HTTP error for a missing message"
        except urllib.error.HTTPError as exc:
            assert exc.code == 409  # ValueError -> 409, matching every other malformed-request route here
    finally:
        api.close()


def test_chat_mission_intent_confirm_rejects_stale_or_hand_built_payloads(tmp_path):
    """No project has a real lot/task-level roadmap wired in today (Boss
    directive, 2026-07-21) — this endpoint must fail closed rather than ever
    fabricate a mission, both for a wrong action_type and for the (currently
    unreachable) real one."""
    import urllib.error
    from joao_orchestrator.bubble.write_tier_policy import WriteTierDisabled
    api = _server(tmp_path); api.serve_in_thread()
    try:
        try:
            _post(api, "chat/mission-intent/confirm", {"project_id": "job-opportunity-radar",
                                                       "action_type": "CLARIFICATION_REQUIRED"})
            assert False, "expected an HTTP error for a non-NEXT_ROADMAP_LOT_READY payload"
        except urllib.error.HTTPError as exc:
            assert exc.code == 409

        try:
            _post(api, "chat/mission-intent/confirm", {"project_id": "job-opportunity-radar",
                                                       "action_type": "NEXT_ROADMAP_LOT_READY"})
            assert False, "expected an HTTP error — no roadmap data source is wired in yet"
        except urllib.error.HTTPError as exc:
            assert exc.code == 409
    finally:
        api.close()


def test_chat_mission_intent_confirm_write_tier_disabled_by_sec_boot(tmp_path):
    # Valid declared lot reaches confirm; SEC-BOOT must block writes.
    import json
    import subprocess
    import sys
    import time as time_mod

    workspace = tmp_path / "fixture-repo"
    workspace.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "f@example.invalid"],
        ["git", "config", "user.name", "f"],
    ):
        subprocess.run(argv, cwd=workspace, check=True)
    (workspace / "module.py").write_text("VALUE = 1\\n")
    (workspace / "test_module.py").write_text(
        "from module import VALUE\\nassert VALUE == 2\\n"
    )
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)

    projects_root = tmp_path / "projects"
    profiles_root = tmp_path / "project_profiles"
    project_dir = projects_root / "fixture-project"
    project_dir.mkdir(parents=True)
    (project_dir / "PROJECT_SPEC.md").write_text(
        "SIGNÉ : ✅ GO Boss\\n\\nfixture spec\\n"
    )
    profile_dir = profiles_root / "fixture-project"
    profile_dir.mkdir(parents=True)
    profile_dir_data = {
        "project_id": "fixture-project",
        "aliases": ["Fixture Project"],
        "repository_path": str(workspace),
        "next_lot": {
            "source_file": "ROADMAP.md#lot-1",
            "item": "bump VALUE to 2",
            "test_command": f"{sys.executable} test_module.py",
            "allowed_paths": ["module.py"],
            "risk_tier": "normal",
        },
    }
    (profile_dir / "profile.json").write_text(json.dumps(profile_dir_data))

    def build(_m, ws, _c):
        (ws / "module.py").write_text("VALUE = 2\\n")
        return {"ok": True}

    class _AcceptedReviewer:
        provider = "codex"
        model = "fixture-independent"

        def review(self, run, _):
            return {
                "ok": True,
                "decision": "pass",
                "proof": {
                    "verdict": "ACCEPT",
                    "candidate_tree": run["candidate_tree"],
                    "findings": [],
                    "reviewer": {
                        "provider": self.provider,
                        "model": self.model,
                    },
                },
            }

    rt = RunRuntime(
        tmp_path / "state",
        builder=SandboxBuilder(build),
        reviewer=_AcceptedReviewer(),
        profiles=LocalProfileAdapter(),
        projects_root=projects_root,
        profiles_root=profiles_root,
    )
    api = LocalAPIServer(rt)
    api.serve_in_thread()

    _, body = _post(
        api,
        "chat/mission-intent",
        {"message": "Termine le prochain lot Fixture Project"},
    )
    mi = json.loads(body)
    assert mi["action_type"] == "NEXT_ROADMAP_LOT_READY"
    assert mi["project_id"] == "fixture-project"
    assert mi["lot"]["item"] == "bump VALUE to 2"

    _, body = _post(api, "chat/mission-intent/confirm", mi)
    confirmed = json.loads(body)
    run_id = confirmed["run_id"]

    for _ in range(50):
        run = api.runtime.get(run_id)
        if run["status"] in {"blocked", "failed", "needs_approval", "accepted"}:
            break
        time_mod.sleep(0.05)

    run = api.runtime.get(run_id)
    assert run["status"] == "blocked", run
    assert (workspace / "module.py").read_text() == "VALUE = 1\\n"


def test_ui_html_served_with_token(tmp_path):
    api = _server(tmp_path); api.serve_in_thread()
    try:
        with urllib.request.urlopen(api.url, timeout=10) as resp:
            html = resp.read().decode()
        assert api.token in html and "__JOAO_TOKEN__" not in html
        assert "Chat Era" in html
    finally:
        api.close()
