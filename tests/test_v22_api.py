"""V2.2 — chat endpoints over the real local HTTP server (fake brain, no real CLI)."""
from __future__ import annotations

import base64
import json
import subprocess
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


def test_ui_html_served_with_token(tmp_path):
    api = _server(tmp_path); api.serve_in_thread()
    try:
        with urllib.request.urlopen(api.url, timeout=10) as resp:
            html = resp.read().decode()
        assert api.token in html and "__JOAO_TOKEN__" not in html
        assert "Chat Era" in html
    finally:
        api.close()
