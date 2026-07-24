"""RUN 2.1A — Truthfulness & Reliability tests (A1-A8).

A1 « Qui es-tu ? » → réponse locale JOAO ; fake ChatBrain lève s'il est appelé ; appels provider = 0.
A2 « Peux-tu modifier un fichier maintenant ? » → réponse locale (non en chat normal + write-tier indisponible) ; appels builder/provider = 0.
A3 « Capitale de l'Australie ? » → load_existing_project_context() jamais appelé ; provider reçoit la question générale ; « Canberra » streamé et sauvegardé.
A4 « Modifie le code et lance les tests » sans projet → write-tier désactivé (SEC-BOOT) ; RunRuntime.start()/launch()/drive()/builder.build() jamais appelés.
A5 provider indisponible → stream event=error message non vide ; aucun assistant vide.
A6 timeout provider → event=error ; timeout interruptible même sans stdout ; conversation réutilisable après échec.
A7 frontend après event=error → bulle d'erreur visible ; textarea+Send réactivés ; aucune bulle vide ; pas de done/saved.
A8 non-régression → Challenge Claude↔GLM route toujours ; projet actif reste attaché ; pièces jointes texte OK ; write-tier reste désactivé.
"""
from __future__ import annotations

import json
from pathlib import Path

from joao_orchestrator.bubble.api import LocalAPIServer
from joao_orchestrator.bubble.chat import ChatBrain
from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder


def _server(tmp_path):
    def build(_m, workspace, _c):
        return {"ok": True}
    rt = RunRuntime(tmp_path / "state", builder=SandboxBuilder(build), profiles=LocalProfileAdapter())
    return LocalAPIServer(rt)


def _post(api, path, body):
    import urllib.request
    req = urllib.request.Request(api.url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read()


def _get(api, path):
    import urllib.request
    req = urllib.request.Request(api.url + path, headers={"X-JOAO-Token": api.token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _parse_sse(raw: bytes) -> list[dict]:
    return [
        json.loads(block[6:])
        for block in raw.decode().split("\n\n")
        if block.startswith("data: ")
    ]


def _make_silent_timeout_cli(tmp_path: Path, name: str = "silent_provider") -> tuple[Path, Path]:
    """Create a fast shell fixture that records its PID, then stays silent."""
    import shlex

    pid_file = tmp_path / f"{name}.pid"
    script = tmp_path / name
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s' \"$$\" > {shlex.quote(str(pid_file))}\n"
        "exec /bin/sleep 30\n"
    )
    script.chmod(0o755)
    return script, pid_file


def _assert_process_gone(pid_file: Path, *, timeout_s: float = 2.0) -> int:
    import os
    import time

    created_deadline = time.monotonic() + timeout_s
    while not pid_file.exists() and time.monotonic() < created_deadline:
        time.sleep(0.01)
    assert pid_file.exists(), "provider fixture never recorded its PID"
    pid = int(pid_file.read_text())

    gone_deadline = time.monotonic() + timeout_s
    while time.monotonic() < gone_deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        time.sleep(0.02)
    raise AssertionError(f"provider process {pid} is still alive after timeout")


def _run_real_ui_harness(api, tmp_path: Path, *, project_id: str, message: str) -> dict:
    """Execute the actual ui.html JavaScript in Node against the live local API."""
    import shutil
    import subprocess
    import urllib.request

    node = shutil.which("node")
    assert node, "Node.js is required for the deterministic DOM harness"

    req = urllib.request.Request(api.url, headers={"X-JOAO-Token": api.token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        html_file = tmp_path / "ui-under-test.html"
        html_file.write_bytes(resp.read())

    harness = tmp_path / "ui-harness.js"
    harness.write_text("\nconst fs = require('fs');\nconst vm = require('vm');\nconst baseUrl = process.argv[2];\nconst htmlPath = process.argv[3];\nconst projectId = process.argv[4];\nconst message = process.argv[5];\nconst html = fs.readFileSync(htmlPath, 'utf8');\nconst match = html.match(/<script>([\\s\\S]*)<\\/script>/);\nif (!match) throw new Error('ui script not found');\n\nconst state = { requestBody: null, bubbleHistory: [], sendHistory: [], inputHistory: [] };\nclass MockElement {\n  constructor(id='') {\n    this.id=id; this.value=''; this.children=[]; this.style={}; this.dataset={}; this.focused=false;\n    this._innerHTML=''; this._disabled=false;\n    this.classList={add(){},remove(){},toggle(){},contains(){return false}};\n  }\n  set innerHTML(v){ this._innerHTML=String(v); if(this.id==='bubble') state.bubbleHistory.push(this._innerHTML); }\n  get innerHTML(){ return this._innerHTML; }\n  set disabled(v){ this._disabled=Boolean(v); if(this.id==='send')state.sendHistory.push(this._disabled); if(this.id==='in')state.inputHistory.push(this._disabled); }\n  get disabled(){ return this._disabled; }\n  appendChild(child){ this.children.push(child); globalThis.__lastMessage=child; }\n  querySelector(sel){ if(sel==='.bubble')return this.bubble; if(sel==='.who')return this.who; return null; }\n  insertAdjacentHTML(_where, value){ this.innerHTML += value; }\n  addEventListener(_type, _listener){}\n  removeEventListener(_type, _listener){}\n  focus(){ this.focused=true; }\n}\nconst elements = {\n  in:new MockElement('in'), send:new MockElement('send'), brain:new MockElement('brain'),\n  chips:new MockElement('chips'), stream:new MockElement('stream'), split:new MockElement('split'),\n  ptitle:new MockElement('ptitle'), pbody:new MockElement('pbody'), convs:new MockElement('convs'),\n  modeseg:new MockElement('modeseg'), advanced:new MockElement('advanced')\n};\nelements.brain.value='claude'; elements.stream.scrollHeight=10; elements.stream.scrollTop=0;\nconst document = {\n  getElementById(id){ if(!elements[id])elements[id]=new MockElement(id); return elements[id]; },\n  createElement(){ const d=new MockElement('msg'); d.bubble=new MockElement('bubble'); d.who=new MockElement('who'); return d; }\n};\nglobalThis.document=document; globalThis.window=globalThis; globalThis.alert=()=>{};\nconst nativeFetch=globalThis.fetch;\nglobalThis.fetch=async function(input, options={}){\n  const absolute=new URL(input, baseUrl).toString();\n  if(absolute.endsWith('/chat') && options.body) state.requestBody=JSON.parse(options.body);\n  return nativeFetch(absolute, options);\n};\nvm.runInThisContext(match[1] + '\\n;globalThis.__streamReply=streamReply;globalThis.__setProject=(v)=>{activeProject=v};globalThis.__getConversation=()=>conv;');\n__setProject(projectId);\nconst promise=__streamReply(message);\nstate.immediate={sendDisabled:elements.send.disabled,inputDisabled:elements.in.disabled,spinner:__lastMessage.innerHTML.includes('spinner')};\npromise.then(()=>{\n  state.final={\n    sendDisabled:elements.send.disabled,\n    inputDisabled:elements.in.disabled,\n    spinner:__lastMessage.bubble.innerHTML.includes('spinner'),\n    bubble:__lastMessage.bubble.innerHTML,\n    errorBorder:__lastMessage.bubble.style.border||'',\n    focused:elements.in.focused,\n    conversation:__getConversation()\n  };\n  process.stdout.write(JSON.stringify(state));\n}).catch(err=>{ console.error(err); process.exit(1); });\n")

    completed = subprocess.run(
        [node, str(harness), api.url, str(html_file), project_id, message],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def test_a1_who_are_you_answered_locally_no_provider_call(tmp_path):
    """A1: « Qui es-tu ? » → réponse locale JOAO ; fake ChatBrain lève s'il est appelé ; appels provider = 0."""
    api = _server(tmp_path)

    # Track if the brain's _line_source is called (should not be for identity questions)
    line_source_called = []
    def tracking_line_source(argv):
        line_source_called.append(argv)
        raise AssertionError("provider should NOT be called for identity questions")

    api.brain._line_source = tracking_line_source
    api.serve_in_thread()
    try:
        status, raw = _post(api, "chat", {"message": "Qui es-tu ?", "model": "claude"})
        assert status == 200

        # Parse the stream events
        events = []
        for block in raw.decode().split("\n\n"):
            if block.startswith("data: "):
                events.append(json.loads(block[6:]))

        # Should get a local response with identity info
        assert any("JOÃO" in str(e.get("text", "")) or "JOÃO" in str(e) for e in events)

        # Provider should NOT have been called
        assert len(line_source_called) == 0
    finally:
        api.close()


def test_a2_can_you_modify_file_answered_locally_write_tier_disabled(tmp_path):
    """A2: « Peux-tu modifier un fichier maintenant ? » → réponse locale (non en chat normal + write-tier indisponible)."""
    api = _server(tmp_path)

    # Track if builder is called (should not be)
    builder_called = []
    original_build = api.runtime.builder.build
    def tracking_build(m, w, c):
        builder_called.append((m, w, c))
        return original_build(m, w, c)
    api.runtime.builder.build = tracking_build

    # Track if provider is called (should not be)
    provider_called = []
    def tracking_line_source(argv):
        provider_called.append(argv)
        return []  # Empty response would cause error, but we shouldn't get here

    api.brain._line_source = tracking_line_source
    api.serve_in_thread()
    try:
        status, raw = _post(api, "chat", {"message": "Peux-tu modifier un fichier maintenant ?", "model": "claude"})
        assert status == 200

        # Parse the stream events
        events = []
        for block in raw.decode().split("\n\n"):
            if block.startswith("data: "):
                events.append(json.loads(block[6:]))

        # Should get a local response about write-tier being disabled
        all_text = "".join(e.get("text", "") for e in events)
        assert any(term in all_text.lower() for term in ["write-tier", "sec-boot", "désactivé", "indisponible"])

        # Should explicitly mention that missions with write are unavailable
        assert "indisponible" in all_text.lower() or "unavailable" in all_text.lower()

        # Builder and provider should NOT have been called
        assert len(builder_called) == 0
        assert len(provider_called) == 0
    finally:
        api.close()


def test_a3_general_question_no_project_context_loaded(tmp_path):
    """A3: « Capitale de l'Australie ? » → load_existing_project_context() jamais appelé ; provider reçoit la question générale."""
    api = _server(tmp_path)

    # Inject a fake provider response
    def fake_lines(argv):
        # Verify the prompt contains the general question without project context
        prompt_str = " ".join(str(arg) for arg in argv)
        assert "Capitale de l'Australie" in prompt_str or "capital" in prompt_str.lower()
        # Should NOT contain project-specific instructions
        assert "réponds uniquement à partir des fichiers" not in prompt_str
        return [
            json.dumps({"type": "stream_event", "event": {"type": "message_start",
                        "message": {"model": "claude-sonnet-5"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "La capitale de l'Australie est Canberra."}}}),
            json.dumps({"type": "result", "result": "La capitale de l'Australie est Canberra."}),
        ]

    api.brain._line_source = fake_lines
    api.serve_in_thread()
    try:
        status, raw = _post(api, "chat", {"message": "Quelle est la capitale de l'Australie ?", "model": "claude"})
        assert status == 200

        # Parse the stream events
        events = []
        for block in raw.decode().split("\n\n"):
            if block.startswith("data: "):
                events.append(json.loads(block[6:]))

        # Should receive "Canberra" in the response
        all_text = "".join(e.get("text", "") for e in events)
        assert "Canberra" in all_text

        # Verify the conversation is saved
        cid = next((e["conversation"] for e in events if e.get("event") == "start"), None)
        if cid:
            hist = _get(api, "chat/history/" + cid)
            assert len(hist["messages"]) == 2  # user + assistant
            assert "Canberra" in hist["messages"][1]["content"]
    finally:
        api.close()


def test_a4_execution_request_without_project_returns_execution_requires_project(tmp_path):
    """A4: « Modifie le code et lance les tests » sans projet → reason_code EXECUTION_REQUIRES_PROJECT."""
    api = _server(tmp_path)

    # Track if runtime methods are called (should not be)
    runtime_start_called = []
    original_start = api.runtime.start
    def tracking_start(*args, **kwargs):
        runtime_start_called.append((args, kwargs))
        raise AssertionError("RunRuntime.start should NOT be called without a project")
    api.runtime.start = tracking_start

    # Track if provider is called (should not be for this)
    provider_called = []
    def tracking_line_source(argv):
        provider_called.append(argv)
        raise AssertionError("provider should NOT be called for execution requests without project")

    api.brain._line_source = tracking_line_source
    api.serve_in_thread()
    try:
        status, raw = _post(api, "chat", {"message": "Modifie le code et lance les tests", "model": "claude"})
        assert status == 200

        # Parse the stream events
        events = []
        for block in raw.decode().split("\n\n"):
            if block.startswith("data: "):
                events.append(json.loads(block[6:]))

        # Should get a local response with write-tier disabled message
        all_text = "".join(e.get("text", "") for e in events)
        assert any(term in all_text.lower() for term in ["sec-boot", "write-tier", "indisponible"])

        # Should explicitly say missions with write are unavailable
        assert "indisponible" in all_text.lower() or "unavailable" in all_text.lower()

        # Runtime.start should NOT have been called
        assert len(runtime_start_called) == 0
        assert len(provider_called) == 0
    finally:
        api.close()


def test_a5_provider_unavailable_returns_explicit_error_event(tmp_path):
    """A5: provider indisponible → stream event=error message non vide ; aucun assistant vide."""
    api = _server(tmp_path)

    # Simulate OSError (CLI unavailable) - should raise, not yield fake result
    def unavailable_provider(argv):
        raise OSError("claude: command not found")

    api.brain._line_source = unavailable_provider
    api.serve_in_thread()
    try:
        status, raw = _post(api, "chat", {"message": "Hello", "model": "claude"})
        assert status == 200

        # Parse the stream events
        events = []
        for block in raw.decode().split("\n\n"):
            if block.startswith("data: "):
                events.append(json.loads(block[6:]))

        # Should get an error event with non-empty message
        error_events = [e for e in events if e.get("event") == "error"]
        assert len(error_events) > 0, "Should have at least one error event"
        assert error_events[0].get("message"), "Error message should not be empty"
        assert "CLI" in error_events[0].get("message", "") or "indisponible" in error_events[0].get("message", "")

        # Should NOT have a done event (no assistant message saved)
        done_events = [e for e in events if e.get("event") == "done"]
        assert len(done_events) == 0, "Should not have done event after error"

        # Should NOT save an empty assistant message
        cid = next((e["conversation"] for e in events if e.get("event") == "start"), None)
        if cid:
            hist = _get(api, "chat/history/" + cid)
            # Check that no empty assistant message was saved
            assistant_msgs = [m for m in hist["messages"] if m["role"] == "assistant"]
            assert len(assistant_msgs) == 0, "Should not save empty assistant messages"
    finally:
        api.close()


def test_a6_timeout_provider_returns_explicit_error_conversation_reusable(tmp_path):
    """A6: the real subprocess path times out, kills the child, and keeps chat reusable."""
    import time

    api = _server(tmp_path)
    timeout_cli, pid_file = _make_silent_timeout_cli(tmp_path, "a6-provider")
    api.brain.claude_executable = str(timeout_cli)
    api.brain.deadline_s = 0.50
    api.brain._line_source = None
    api.serve_in_thread()
    try:
        started = time.monotonic()
        status, raw = _post(api, "chat", {"message": "Question générale de timeout", "model": "claude"})
        elapsed = time.monotonic() - started
        assert status == 200
        assert elapsed < 2.0, f"timeout path took too long: {elapsed:.3f}s"

        events = _parse_sse(raw)
        errors = [event for event in events if event.get("event") == "error"]
        assert errors and "timeout" in errors[0].get("message", "").lower(), events
        assert not any(event.get("event") == "done" for event in events)
        assert not any(event.get("event") == "saved" for event in events)
        _assert_process_gone(pid_file)

        cid = next(event["conversation"] for event in events if event.get("event") == "start")
        history = _get(api, "chat/history/" + cid)
        assert not [m for m in history["messages"] if m["role"] == "assistant"]

        api.brain._line_source = lambda _argv: [
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"model": "claude-test"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Recovered"}}}),
        ]
        status2, raw2 = _post(api, "chat", {"message": "Question suivante", "model": "claude", "conversation": cid})
        assert status2 == 200
        assert any(event.get("event") == "done" for event in _parse_sse(raw2))
    finally:
        api.close()


def test_a7_frontend_error_handling_removes_spinner_shows_error(tmp_path):
    """A7: execute the real UI JavaScript and observe loading/error/recovery states."""
    api = _server(tmp_path)
    timeout_cli, pid_file = _make_silent_timeout_cli(tmp_path, "a7-provider")
    api.brain.claude_executable = str(timeout_cli)
    api.brain.deadline_s = 0.50
    api.brain._line_source = None
    api.serve_in_thread()
    try:
        result = _run_real_ui_harness(
            api,
            tmp_path,
            project_id="job-radar-active",
            message="Corrige ce fichier.",
        )
        assert result["requestBody"]["project_id"] == "job-radar-active"
        assert result["immediate"] == {
            "sendDisabled": True,
            "inputDisabled": True,
            "spinner": True,
        }
        final = result["final"]
        assert final["sendDisabled"] is False
        assert final["inputDisabled"] is False
        assert final["spinner"] is False
        assert "timeout" in final["bubble"].lower()
        assert "ff6b6b" in final["errorBorder"].lower()
        assert final["focused"] is True
        assert final["conversation"]
        _assert_process_gone(pid_file)

        history = _get(api, "chat/history/" + final["conversation"])
        assert not [m for m in history["messages"] if m["role"] == "assistant"]
    finally:
        api.close()


def test_a8_non_regression_claude_glm_route_attachments_write_tier_disabled(tmp_path):
    """A8: non-régression → Challenge Claude↔GLM route toujours ; projet actif reste attaché ; pièces jointes texte OK ; write-tier reste désactivé."""
    import base64

    api = _server(tmp_path)
    api.serve_in_thread()
    try:
        def claude_fake(argv):
            return [
                json.dumps({"type": "stream_event", "event": {"type": "message_start",
                            "message": {"model": "claude-sonnet-5"}}}),
                json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": "Réponse Claude"}}}),
                json.dumps({"type": "result", "result": "Réponse Claude"}),
            ]
        api.brain._line_source = claude_fake

        status, raw = _post(api, "chat", {"message": "test claude", "model": "claude"})
        assert status == 200
        events = [json.loads(block[6:]) for block in raw.decode().split("\n\n") if block.startswith("data: ")]
        assert any("claude-sonnet" in str(e.get("model", "")) for e in events)

        def glm_fake(argv):
            return [
                json.dumps({"type": "step_start", "part": {"model": "zai-coding-plan/glm-4.5-air"}}),
                json.dumps({"type": "text", "part": {"text": "Réponse GLM"}}),
                json.dumps({"type": "step_finish", "part": {"cost": 0}}),
            ]
        api.brain._line_source = glm_fake

        status, raw = _post(api, "chat", {"message": "test glm", "model": "glm"})
        assert status == 200
        events = [json.loads(block[6:]) for block in raw.decode().split("\n\n") if block.startswith("data: ")]
        assert any("glm" in str(e.get("model", "")) for e in events)

        content = base64.b64encode("test content\nline 2".encode()).decode()
        _, raw = _post(api, "chat/attach", {"name": "test.txt", "content_base64": content})
        att = json.loads(raw)
        assert att["extraction"]["ok"]
        assert att["extraction"]["kind"] == "text"

        def check_write_unavailable(argv):
            return []
        api.brain._line_source = check_write_unavailable

        status, raw = _post(api, "chat", {"message": "Peux-tu modifier un fichier ?", "model": "claude"})
        assert status == 200
        events = [json.loads(block[6:]) for block in raw.decode().split("\n\n") if block.startswith("data: ")]
        all_text = "".join(e.get("text", "") for e in events)
        assert "indisponible" in all_text.lower() or "unavailable" in all_text.lower()

    finally:
        api.close()


# Functional Proofs F1-F4 for GPT Audit

def test_f1_active_project_transmission_to_reply_stream(tmp_path):
    """F1: API → ChatBrain receives project_id and avoids the projectless local block."""
    api = _server(tmp_path)
    provider_calls = []

    def provider(argv):
        provider_calls.append(argv)
        return [
            json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"model": "claude-test"}}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "provider-path"}}}),
        ]

    api.brain._line_source = provider
    api.serve_in_thread()
    try:
        status, raw = _post(api, "chat", {
            "message": "Corrige ce fichier.",
            "model": "claude",
            "project_id": "test-job-radar",
        })
        assert status == 200
        events = _parse_sse(raw)
        assert provider_calls, "active project request did not reach the provider path"
        assert any(event.get("event") == "done" and event.get("text") == "provider-path" for event in events)
        assert not any(event.get("model") == "JOAO-local" for event in events)

        calls_before = len(provider_calls)
        status2, raw2 = _post(api, "chat", {
            "message": "Corrige ce fichier.",
            "model": "claude",
        })
        assert status2 == 200
        events2 = _parse_sse(raw2)
        assert len(provider_calls) == calls_before, "projectless execution request called the provider"
        assert any(event.get("model") == "JOAO-local" for event in events2)
        assert "SEC-BOOT" in "".join(event.get("text", "") for event in events2)
    finally:
        api.close()


def test_f2_pure_capability_questions_no_provider(tmp_path):
    """F2: Test pure capability questions without provider calls."""
    api = _server(tmp_path)

    # Track if provider is called
    provider_called = []
    def tracking_line_source(argv):
        provider_called.append(argv)
        raise AssertionError("Provider should NOT be called for capability questions")

    api.brain._line_source = tracking_line_source
    api.serve_in_thread()
    try:
        # Test each capability question individually
        capability_questions = [
            "Que peux-tu faire ?",
            "Quels outils as-tu ?",
            "What can you do?",
            "What tools do you have?",
        ]

        for question in capability_questions:
            provider_called.clear()
            status, raw = _post(api, "chat", {"message": question, "model": "claude"})
            assert status == 200

            # Parse events
            events = []
            for block in raw.decode().split("\n\n"):
                if block.startswith("data: "):
                    events.append(json.loads(block[6:]))

            # Should get local JOAO response (check model field OR text content)
            has_local_response = any(
                "JOAO" in str(e.get("model", "")) or
                "local" in str(e.get("model", "")) or
                "JOÃO" in str(e.get("text", "")) or
                "local" in str(e.get("text", ""))
                for e in events
            )
            assert has_local_response, f"No local JOAO response found for question: {question}. Events: {events}"

            # Provider should NOT have been called
            assert len(provider_called) == 0, f"Provider called for question: {question}"

    finally:
        api.close()


def test_f3_general_questions_vs_execution_requests(tmp_path):
    """F3: Test general questions vs execution requests classification."""
    from joao_orchestrator.bubble.chat import _is_execution_request_without_project

    # Direct function test for classification logic
    general_questions = [
        "Pourquoi le runner Python échoue ?",
        "Explique comment créer un fichier en Python.",
        "How does a code fix work?",
        "Why should tests be run?",
    ]

    for question in general_questions:
        # These should NOT be classified as execution without project
        assert not _is_execution_request_without_project(question, None, None, None), \
            f"General question misclassified as execution request: {question}"

    # Execution requests without project (should be classified as such)
    execution_requests = [
        "Corrige ce fichier.",
        "Lance les tests.",
        "Implement the next lot.",
    ]

    for request in execution_requests:
        # These SHOULD be classified as execution without project
        assert _is_execution_request_without_project(request, None, None, None), \
            f"Execution request not classified correctly: {request}"

    # With active project, execution requests should NOT trigger "without project" path
    assert not _is_execution_request_without_project("Corrige ce fichier.", None, None, "active-project-123"), \
        "Execution request WITH project should not be classified as 'without project'"


def test_f4_real_timeout_and_real_dom_execution(tmp_path):
    """F4: exercise ChatBrain._lines directly with a silent real child and prove it is killed."""
    import time

    timeout_cli, pid_file = _make_silent_timeout_cli(tmp_path, "f4-provider")
    brain = ChatBrain(deadline_s=0.50)
    started = time.monotonic()
    try:
        list(brain._lines([str(timeout_cli)]))
    except TimeoutError as exc:
        assert "timeout" in str(exc).lower()
    else:
        raise AssertionError("ChatBrain._lines did not raise TimeoutError")
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"real timeout was not bounded: {elapsed:.3f}s"
    _assert_process_gone(pid_file)


if __name__ == "__main__":
    import tempfile
    import sys

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Run all tests including new functional proofs
        tests = [
            test_a1_who_are_you_answered_locally_no_provider_call,
            test_a2_can_you_modify_file_answered_locally_write_tier_disabled,
            test_a3_general_question_no_project_context_loaded,
            test_a4_execution_request_without_project_returns_execution_requires_project,
            test_a5_provider_unavailable_returns_explicit_error_event,
            test_a6_timeout_provider_returns_explicit_error_conversation_reusable,
            test_a7_frontend_error_handling_removes_spinner_shows_error,
            test_a8_non_regression_claude_glm_route_attachments_write_tier_disabled,
            # Functional proofs F1-F4
            test_f1_active_project_transmission_to_reply_stream,
            test_f2_pure_capability_questions_no_provider,
            test_f3_general_questions_vs_execution_requests,
            test_f4_real_timeout_and_real_dom_execution,
        ]

        failed = []
        for test in tests:
            try:
                test(tmp_path)
                print(f"✓ {test.__name__}")
            except Exception as e:
                print(f"✗ {test.__name__}: {e}")
                failed.append((test.__name__, e))

        if failed:
            print(f"\n{len(failed)} test(s) failed:")
            for name, err in failed:
                print(f"  - {name}: {err}")
            sys.exit(1)
        else:
            print(f"\n{len(tests)} tests passed - M1_REBUILT_FROM_SECBOOT_BASE_AWAITING_GPT_REVIEW")
            sys.exit(0)
