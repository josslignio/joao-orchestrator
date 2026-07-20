"""Local-only JOAO command console + V2.2 Chat Era API and browser UI."""
from __future__ import annotations
import base64
import json
import secrets
import shlex
import threading
from datetime import date, timezone, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from ..domain.models import ProjectProfile
from ..policy.paths import _norm
from ..storage.atomic import atomic_write_json, atomic_write_text
from . import chat as chatmod
from . import intent as intentmod
from .chat import Attachment, ChatBrain
from .kickoff import Kickoff, KickoffError
from .runtime import RunRuntime, RuntimeStateError, claude_capability

HTML = (Path(__file__).resolve().parent / "ui.html").read_text()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class LocalAPIServer:
    def __init__(self, runtime: RunRuntime, host="127.0.0.1", port=0):
        if host not in {"127.0.0.1", "localhost", "::1"}: raise ValueError("localhost only")
        self.runtime, self.workers = runtime, {}
        self.kickoff = Kickoff(runtime.projects_root)
        self.brain = ChatBrain()
        self.chat_root = runtime.root / "chat"
        self.token = secrets.token_urlsafe(32)
        self._dispatch_lock = threading.RLock()
        self._chat_lock = threading.RLock()
        self._active_workspaces = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def send(self, code, value, kind="application/json"):
                raw = value.encode() if isinstance(value, str) else json.dumps(value).encode()
                self.send_response(code); self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def payload(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
            def authorized(self): return secrets.compare_digest(self.headers.get("X-JOAO-Token", ""), outer.token)
            def do_GET(self):
                path = urlparse(self.path).path; bits = path.strip("/").split("/")
                try:
                    if path == "/": return self.send(200, HTML.replace("__JOAO_TOKEN__", outer.token), "text/html; charset=utf-8")
                    if not self.authorized(): return self.send(401, {"error": "missing or invalid local session token"})
                    if path == "/capabilities": return self.send(200, outer.capabilities())
                    if path == "/chat/conversations": return self.send(200, outer.chat_conversations())
                    if len(bits) == 3 and bits[0] == "chat" and bits[1] == "history": return self.send(200, outer.chat_history(bits[2]))
                    if len(bits) == 2 and bits[0] == "runs": return self.send(200, outer.runtime.get(bits[1]))
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "events": return self.send(200, outer.runtime.events(bits[1]))
                    if len(bits) == 2 and bits[0] == "kickoff": return self.send(200, outer.kickoff_state(bits[1]))
                    self.send(404, {"error": "not found"})
                except (RuntimeStateError, KickoffError) as exc: self.send(404, {"error": str(exc)})
            def do_POST(self):
                path = urlparse(self.path).path; bits = path.strip("/").split("/")
                try:
                    if not self.authorized(): return self.send(401, {"error": "missing or invalid local session token"})
                    if path == "/chat/classify": return self.send(200, outer.chat_classify(self.payload()))
                    if path == "/chat/mission-intent": return self.send(200, outer.chat_mission_intent(self.payload()))
                    if path == "/chat/attach": return self.send(200, outer.chat_attach(self.payload()))
                    if path == "/chat/mission": return self.send(202, outer.launch(self.payload()))
                    if path == "/chat": return self.stream_chat(self.payload())
                    if path == "/missions": return self.send(202, outer.launch(self.payload()))
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] in {"pause", "resume", "stop", "approve", "reject"}: return self.send(200, getattr(outer.runtime, bits[2])(bits[1]))
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "retry": return self.send(202, outer.drive(bits[1]))
                    if len(bits) == 3 and bits[0] == "kickoff" and bits[2] in {"start", "answer", "sign"}: return self.send(200, outer.kickoff_action(bits[1], bits[2], self.payload() if bits[2] == "answer" else {}))
                    self.send(404, {"error": "not found"})
                except (RuntimeStateError, KickoffError, ValueError, json.JSONDecodeError) as exc: self.send(409, {"error": str(exc)})
            def stream_chat(self, data):
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache"); self.send_header("Connection", "close"); self.end_headers()
                try:
                    for event in outer.chat_stream(data):
                        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:  # never fabricate a reply; surface the failure honestly
                    self.wfile.write(f"data: {json.dumps({'event': 'error', 'message': f'{type(exc).__name__}: {exc}'})}\n\n".encode())
                    self.wfile.flush()
        self.server = ThreadingHTTPServer((host, port), Handler)

    def capabilities(self):
        import shutil
        from .worker_topology import check_builder_availability
        from ..worker_host.client import health_check as worker_host_health_check
        # M0 safe-stop (D-043): release_stage is stated structurally, never inferred by a caller
        # from a test count — see SYSTEM_CONSTITUTION_V4.md §4, ROADMAP_V4.md GA checklist.
        return {"glm": {"available": bool(shutil.which("opencode"))},
                "codex": {"available": bool(shutil.which("codex"))},
                "claude": claude_capability(),
                "chat": chatmod.available_brains(),
                "web_search": {"available": False, "reason": "recherche web non branchée (annoncé honnêtement)"},
                "release_stage": "ALPHA",
                # Boss architecture decision (2026-07-20/21): the normal
                # mission path talks ONLY to the standalone joao-worker-host
                # — surfaced here so the UI can show worker-host health,
                # the active builder/reviewer pairing, and any unavailable-
                # worker reason (e.g. ClaudeCodeBuilder, disabled by
                # standing policy) rather than silently failing on dispatch.
                "worker_host": worker_host_health_check(),
                "active_builder": {"provider": getattr(self.runtime.builder, "provider", None),
                                   "provider_family": getattr(self.runtime.builder, "provider_family", None)},
                "active_reviewer": {"provider": getattr(self.runtime.reviewer, "provider", None),
                                    "provider_family": getattr(self.runtime.reviewer, "provider_family", None)},
                "claude_builder_status": check_builder_availability("anthropic")}

    @staticmethod
    def command(text):
        argv = shlex.split(text)
        forbidden = "|&;$><" + chr(10) + chr(13)
        if not argv or any(any(ch in part for ch in forbidden) for part in argv): raise ValueError("test command must not use a shell")
        return argv

    # ─────────────── Phase 0 Kickoff ───────────────
    def kickoff_state(self, project):
        q = self.kickoff.current_question(project)
        return {"project": project, "question": ({"block": q.block, "key": q.key, "text": q.text} if q else None),
                "complete": q is None, "signed": self.kickoff.is_signed(project)}
    def kickoff_action(self, project, action, data):
        if action == "start": self.kickoff.start(project); return self.kickoff_state(project)
        if action == "answer":
            text = str(data.get("text", "")).strip()
            if not text: raise ValueError("answer text is required")
            self.kickoff.answer(project, text); return self.kickoff_state(project)
        self.kickoff.sign(project, date_str=datetime.now(timezone.utc).date().isoformat())
        return self.kickoff_state(project)

    # ─────────────── V2.2 intent router (BLOC A) ───────────────
    def chat_classify(self, data):
        message = str(data.get("message", ""))
        mode = str(data.get("mode", "auto"))
        has_attachment = bool(data.get("has_attachment"))
        result = intentmod.classify(message, mode=mode, has_attachment=has_attachment)
        # every routing decision is auditable (2.1 / A3)
        with self._chat_lock:
            self.chat_root.mkdir(parents=True, exist_ok=True)
            with (self.chat_root / "routing-log.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": _now(), "message": message[:200], **result.to_dict()}, ensure_ascii=False) + "\n")
        return result.to_dict()

    # ─────────────── chat message -> mission intent (Boss directive, 2026-07-21) ───────────────
    def chat_mission_intent(self, data):
        """Resolve a natural-language chat message (e.g. "Termine le prochain
        lot <project>") into a real, honest next action — never a fabricated
        mission. See `bubble.mission_intent` for why: no lot/task-level
        roadmap data source is wired in for any project yet, so this never
        returns a constructed mission; it routes to the real Phase-0 kickoff
        state instead when a project is recognized."""
        from . import mission_intent as mission_intent_mod
        text = str(data.get("message", "")).strip()
        if not text:
            raise ValueError("message is required")
        return mission_intent_mod.resolve_chat_intent(text, projects_root=self.runtime.projects_root)

    # ─────────────── V2.2 attachments (BLOC B3) ───────────────
    def chat_attach(self, data):
        name = _norm(str(data.get("name", "attachment")).strip()) or "attachment"
        if "/" in name or ".." in name: raise ValueError("invalid attachment name")
        try:
            blob = base64.b64decode(str(data.get("content_base64", "")), validate=True)
        except Exception as exc:
            raise ValueError(f"attachment is not valid base64: {exc}")
        att_id = "att-" + secrets.token_hex(6)
        folder = self.chat_root / "attachments"; folder.mkdir(parents=True, exist_ok=True)
        blob_path = folder / f"{att_id}__{name}"
        blob_path.write_bytes(blob)
        att = chatmod.extract_attachment(blob_path)
        atomic_write_json(folder / f"{att_id}.json", {"id": att_id, "path": str(blob_path),
                          "attachment": att.to_dict(), "text": att.text})
        return {"id": att_id, "extraction": att.to_dict()}
    def _load_attachment(self, att_id: str) -> Attachment | None:
        meta_path = self.chat_root / "attachments" / f"{att_id}.json"
        if not meta_path.is_file(): return None
        data = json.loads(meta_path.read_text()); a = data["attachment"]
        return Attachment(name=a["name"], ok=a["ok"], kind=a["kind"], text=data.get("text", ""),
                          chars=a["chars"], method=a["method"], truncated=a["truncated"], error=a["error"])

    # ─────────────── V2.2 chat brain (BLOC B, streaming) ───────────────
    def _conv_path(self, cid: str) -> Path: return self.chat_root / "conversations" / f"{cid}.json"
    def _load_conversation(self, cid: str) -> dict:
        p = self._conv_path(cid)
        if p.is_file(): return json.loads(p.read_text())
        return {"id": cid, "title": "", "messages": [], "created_at": _now()}
    def _append_message(self, cid: str, role: str, content: str, model: str = "") -> None:
        with self._chat_lock:
            conv = self._load_conversation(cid)
            if not conv["title"] and role == "user": conv["title"] = content[:60]
            conv["messages"].append({"role": role, "content": content, "model": model, "at": _now()})
            conv["updated_at"] = _now()
            atomic_write_json(self._conv_path(cid), conv)
    def chat_history(self, cid): return self._load_conversation(cid)
    def chat_conversations(self):
        folder = self.chat_root / "conversations"
        out = []
        if folder.is_dir():
            for p in folder.glob("*.json"):
                c = json.loads(p.read_text())
                out.append({"id": c["id"], "title": c.get("title", ""), "updated_at": c.get("updated_at", c.get("created_at", ""))})
        return sorted(out, key=lambda c: c.get("updated_at", ""), reverse=True)
    def chat_stream(self, data):
        message = str(data.get("message", "")).strip()
        if not message: yield {"event": "error", "message": "message vide"}; return
        model = str(data.get("model", "claude"))
        cid = str(data.get("conversation") or ("conv-" + secrets.token_hex(6)))
        atts = [a for a in (self._load_attachment(i) for i in data.get("attachments", [])) if a]
        history = self._load_conversation(cid)["messages"]
        self._append_message(cid, "user", message)
        yield {"event": "start", "conversation": cid}
        full, model_used = [], model
        for event in self.brain.reply_stream(message, history=history, model=model, attachments=atts):
            if event.get("event") == "delta": full.append(event["text"])
            if event.get("event") in {"model", "done"} and event.get("model"): model_used = event["model"]
            yield event
        self._append_message(cid, "assistant", "".join(full), model=model_used)
        yield {"event": "saved", "conversation": cid, "model": model_used}

    # ─────────────── missions (through the Phase-1 cascade when so wired) ───────────────
    def launch(self, data):
        root = Path(data["workspace"]).expanduser().resolve()
        paths = [str(p).strip() for p in data.get("allowed_paths", []) if str(p).strip()]
        if not paths: raise ValueError("at least one allowed write path is required")
        ident = str(data.get("project_id") or root.name)
        profile = ProjectProfile(project_id=ident, display_name=ident, repository_root=str(root),
                                 allowed_write_paths=paths, forbidden_paths=[], approval_required=True)
        target = str(data.get("targeted_test_command", "")).strip()
        # Found via the real worker-host operational-closure proof: a
        # network-requiring builder (GLMBuilder/ClaudeCodeBuilder both
        # declare `requires_network_transport`) silently got `network=False`
        # here — this call site never exposed `network_capability` at all —
        # and hung until the sandbox's own timeout SIGTERM'd it, exactly the
        # same class of gap `orchestrator.run_c8b_mission` had (also fixed).
        # Default False is still fail-closed; the UI/chat caller must ask.
        run = self.runtime.start(project_id=ident, workspace=root, mission=str(data["mission"]),
                                 targeted_tests=[self.command(target)] if target else [],
                                 full_tests=[self.command(str(data["full_test_command"]))],
                                 profile=profile, critical=bool(data.get("critical")),
                                 network_capability=bool(data.get("network_capability", False)))
        self.drive(run); return {"run_id": run, "status": "queued"}
    def drive(self, run_id):
        run = self.runtime.get(run_id)
        workspace = str(Path(run["workspace"]).resolve())
        def worker():
            try:
                while True:
                    state = self.runtime.run_once(run_id)
                    if state["status"] != "correcting": break
            except Exception as exc:
                run = self.runtime.get(run_id); self.runtime._event(run, "runtime_exception", error=f"{type(exc).__name__}: {exc}")
            finally:
                with self._dispatch_lock:
                    if self._active_workspaces.get(workspace) == run_id: self._active_workspaces.pop(workspace, None)
        thread = threading.Thread(target=worker, name="joao-" + run_id, daemon=True)
        with self._dispatch_lock:
            prior = self._active_workspaces.get(workspace)
            if prior and self.workers.get(prior) and self.workers[prior].is_alive():
                raise RuntimeStateError("another JOAO builder is active for this worktree")
            self.workers[run_id] = thread; self._active_workspaces[workspace] = run_id; thread.start()
        return {"run_id": run_id, "status": "queued"}
    @property
    def url(self): return f"http://127.0.0.1:{self.server.server_port}/"
    def serve_in_thread(self):
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start(); return thread
    def close(self): self.server.shutdown(); self.server.server_close()
