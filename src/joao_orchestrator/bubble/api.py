"""Local-only JOAO chat console, backed by the bounded runtime."""
from __future__ import annotations

import json
import os
import shutil
import secrets
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ..domain.models import ProjectProfile
from .runtime import RunRuntime, RuntimeStateError, claude_capability


HTML = """<!doctype html>
<meta charset="utf-8"><title>JOAO</title>
<style>
:root{color-scheme:dark}body{margin:0;background:#0d1117;color:#e6edf3;font:15px system-ui}
.app{max-width:880px;min-height:100vh;margin:auto;border-inline:1px solid #30363d;padding:28px}
.top{display:flex;justify-content:space-between;align-items:center}.title{font-size:25px;font-weight:750}
.sub,.muted{color:#8b949e}.sub{margin:6px 0 28px}.muted{font-size:12px}.chat{min-height:45vh}
.empty{color:#8b949e;text-align:center;padding:100px 20px}.run{border:1px solid #30363d;border-radius:12px;padding:14px;background:#161b22;margin:12px 0}
.state{font-weight:700}.composer{position:sticky;bottom:18px;background:#0d1117;padding-top:12px}
textarea{width:100%;box-sizing:border-box;min-height:130px;border:1px solid #30363d;border-radius:12px;background:#161b22;color:#e6edf3;padding:15px;font:inherit;resize:vertical}
button{background:#238636;border:1px solid #2ea043;color:white;border-radius:8px;padding:9px 14px;font-weight:700;margin-right:8px}.ghost{background:#21262d;border-color:#30363d}
.modes{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.mode{border:1px solid #30363d;border-radius:8px;padding:8px 10px;color:#c9d1d9;cursor:pointer}.mode:has(input:checked){border-color:#58a6ff;background:#0c2d4a}.mode input{accent-color:#58a6ff}
pre{white-space:pre-wrap;max-height:260px;overflow:auto;color:#c9d1d9}
</style>
<main class="app"><div class="top"><div class="title">JOAO</div><div id="capabilities" class="muted">Chargement…</div></div>
<div class="sub">Écris comme ici. JOAO pilote GLM, applique seulement les options que tu choisis, et conserve les preuves.</div>
<section id="chat" class="chat"><div class="empty">Écris une première tâche pour tester JOAO dans son sandbox isolée.</div></section>
<section class="composer"><textarea id="prompt" autofocus placeholder="Ex. Ajoute une fonction qui normalise un titre et les tests associés."></textarea>
<div class="muted">Moteur de construction — un seul à la fois</div><div class="modes">
<label class="mode"><input type="radio" name="builder" value="glm" checked> GLM</label>
<label class="mode"><input type="radio" name="builder" value="codex"> Codex</label>
<label class="mode"><input type="radio" name="builder" value="claude"> Claude</label></div>
<div class="muted">Review indépendante à chaque gate (plan, diff, validation complète, livraison)</div><div class="modes">
<label class="mode"><input type="radio" name="review" value="codex" checked> Codex</label>
<label class="mode"><input type="radio" name="review" value="claude"> Claude</label>
<label class="mode"><input type="radio" name="review" value="codex_claude"> Codex + Claude</label>
</div><button onclick="send()">Run</button><span class="muted">Le test est isolé : aucune modification de tes projets.</span></section></main>
<script>
const TOKEN="__JOAO_TOKEN__";let active=null;const el=id=>document.getElementById(id);
async function req(url,opt={}){opt.headers={...(opt.headers||{}),'X-JOAO-Token':TOKEN};const r=await fetch(url,opt),v=await r.json();if(!r.ok)throw Error(v.error||'request failed');return v}
function esc(s){const n=document.createElement('span');n.textContent=s;return n.innerHTML}
function show(v){el('chat').innerHTML='<div class="run"><div class="state">'+esc(v.status)+'</div><div class="muted">'+esc(v.current_step)+'</div><pre>'+esc(JSON.stringify(v,null,2))+'</pre><button class="ghost" onclick="act(\\'pause\\')">Pause après étape</button><button class="ghost" onclick="act(\\'resume\\')">Resume</button><button class="ghost" onclick="act(\\'stop\\')">Stop après étape</button><button class="ghost" onclick="act(\\'retry\\')">Retry</button><button class="ghost" onclick="act(\\'approve\\')">Approve</button></div>'}
function selected(name){return document.querySelector('input[name="'+name+'"]:checked').value}
async function send(){const mission=el('prompt').value.trim();if(!mission)return;try{const v=await req('/quick-missions',{method:'POST',body:JSON.stringify({mission,builder_name:selected('builder'),review_mode:selected('review')})});active=v.run_id;el('prompt').value='';poll()}catch(e){el('chat').innerHTML='<div class="run">'+esc(e.message)+'</div>'}}
async function act(name){if(active){try{await req('/runs/'+active+'/'+name,{method:'POST'})}catch(e){el('chat').innerHTML='<div class="run">'+esc(e.message)+'</div>'}}poll()}
async function poll(){if(!active)return;try{show(await req('/runs/'+active))}catch(_){}}
function enableChoice(name,value,enabled){const input=document.querySelector('input[name="'+name+'"][value="'+value+'"]');input.disabled=!enabled;input.closest('.mode').style.opacity=enabled?'1':'.45'}
function ensureChoice(name){const current=document.querySelector('input[name="'+name+'"]:checked');if(!current||current.disabled){const fallback=document.querySelector('input[name="'+name+'"]:not(:disabled)');if(fallback)fallback.checked=true}}
async function caps(){const v=await req('/capabilities');enableChoice('builder','glm',v.glm.available);enableChoice('builder','codex',v.codex.available);enableChoice('builder','claude',v.claude.available);enableChoice('review','codex',v.codex.available);enableChoice('review','claude',v.claude.available);enableChoice('review','codex_claude',v.codex.available&&v.claude.available);ensureChoice('builder');ensureChoice('review');el('capabilities').textContent='GLM '+(v.glm.available?'prêt':'indisponible')+' · Codex '+(v.codex.available?'optionnel':'indisponible')+' · Claude '+(v.claude.available?'optionnel':'non configuré')}
setInterval(poll,1200);caps();
</script>"""


class LocalAPIServer:
    """Loopback-only UI with a per-worktree builder dispatch lock."""

    def __init__(self, runtime: RunRuntime, host: str = "127.0.0.1", port: int = 0):
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("localhost only")
        self.runtime, self.workers = runtime, {}
        self.token = secrets.token_urlsafe(32)
        self._dispatch_lock = threading.RLock()
        self._active_workspaces: dict[str, str] = {}
        self._server_thread: threading.Thread | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, code, value, kind="application/json"):
                raw = value.encode() if isinstance(value, str) else json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def payload(self):
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())

            def authorized(self):
                return secrets.compare_digest(self.headers.get("X-JOAO-Token", ""), outer.token)

            def do_GET(self):
                path = urlparse(self.path).path
                bits = path.strip("/").split("/")
                try:
                    if path == "/":
                        return self.send(200, HTML.replace("__JOAO_TOKEN__", outer.token), "text/html; charset=utf-8")
                    if not self.authorized():
                        return self.send(401, {"error": "missing or invalid local session token"})
                    if path == "/capabilities":
                        return self.send(200, outer.capabilities())
                    if len(bits) == 2 and bits[0] == "runs":
                        return self.send(200, outer.runtime.get(bits[1]))
                    if len(bits) == 3 and bits[2] == "events":
                        return self.send(200, outer.runtime.events(bits[1]))
                    self.send(404, {"error": "not found"})
                except RuntimeStateError as exc:
                    self.send(404, {"error": str(exc)})

            def do_POST(self):
                path = urlparse(self.path).path
                bits = path.strip("/").split("/")
                try:
                    if not self.authorized():
                        return self.send(401, {"error": "missing or invalid local session token"})
                    if path == "/quick-missions":
                        return self.send(202, outer.quick_launch(self.payload()))
                    if path == "/missions":
                        return self.send(202, outer.launch(self.payload()))
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] in {"pause", "resume", "stop", "approve", "reject"}:
                        state = getattr(outer.runtime, bits[2])(bits[1])
                        if bits[2] == "resume":
                            outer.resume_drive(bits[1])
                        return self.send(200, state)
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "retry":
                        return self.send(202, outer.drive(bits[1]))
                    self.send(404, {"error": "not found"})
                except (RuntimeStateError, ValueError, json.JSONDecodeError) as exc:
                    self.send(409, {"error": str(exc)})

        self.server = ThreadingHTTPServer((host, port), Handler)

    def capabilities(self):
        glm_builder = self.runtime.builders.get("glm")
        glm_value = getattr(glm_builder, "executable", "") if glm_builder is not None else ""
        glm_path = Path(glm_value).expanduser() if glm_value else None
        glm_found = str(glm_path) if glm_path and glm_path.is_absolute() else shutil.which(str(glm_path or ""))
        glm = {"available": bool(glm_found and Path(glm_found).is_file() and os.access(glm_found, os.X_OK))}
        try:
            codex_login = subprocess.run(["codex", "login", "status"], shell=False, capture_output=True, text=True, timeout=3)
            codex = {"available": bool("codex" in self.runtime.builders and "codex" in self.runtime.reviewers and codex_login.returncode == 0 and "Logged in" in codex_login.stdout + codex_login.stderr)}
        except (OSError, subprocess.TimeoutExpired):
            codex = {"available": False}
        claude = claude_capability()
        claude.update({"available": False, "reason": "Claude has no configured JOAO quick-task adapter"})
        return {"glm": glm, "codex": codex, "claude": claude}

    @staticmethod
    def command(text):
        argv = shlex.split(text)
        forbidden = "|&;$><" + chr(10) + chr(13)
        if not argv or any(any(char in item for char in forbidden) for item in argv):
            raise ValueError("test command must not use a shell")
        return argv

    def _start(self, project, workspace, mission, paths, full, target, builder_name, reviewer_names):
        profile = ProjectProfile(project_id=project, display_name=project, repository_root=str(workspace), allowed_write_paths=paths, forbidden_paths=[], approval_required=True)
        run = self.runtime.start(project_id=project, workspace=workspace, mission=mission, targeted_tests=[target] if target else [], full_tests=[full], profile=profile, builder_name=builder_name, reviewer_names=reviewer_names)
        self.drive(run)
        return {"run_id": run, "status": "queued"}

    def quick_sandbox(self):
        root = self.runtime.root / "sandboxes" / ("quick-" + secrets.token_hex(5))
        (root / "src").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "src" / "task.py").write_text('"""Safe JOAO quick-task sandbox."""\n\ndef identity(value):\n    return value\n')
        (root / "tests" / "test_smoke.py").write_text("from src.task import identity\n\ndef test_identity():\n    assert identity('joao') == 'joao'\n")
        for argv in (["git", "init", "-q"], ["git", "config", "user.email", "joao-sandbox@example.invalid"], ["git", "config", "user.name", "JOAO Sandbox"], ["git", "add", "."], ["git", "commit", "-qm", "sandbox baseline"]):
            subprocess.run(argv, cwd=str(root), check=True)
        return root

    def _quick_configuration(self, data):
        builder = str(data.get("builder_name", "glm"))
        mode = str(data.get("review_mode", "codex"))
        if builder not in {"glm", "codex", "claude"} or mode not in {"codex", "claude", "codex_claude"}:
            raise ValueError("unknown builder or review mode")
        if builder == "claude" or "claude" in mode:
            capability = claude_capability()
            detail = capability["reason"] or "the Claude reviewer adapter is not configured"
            raise ValueError("Claude mode cannot run yet: " + detail + ". Choose GLM or Codex with Codex review.")
        capabilities = self.capabilities()
        if not capabilities[builder]["available"]:
            raise ValueError("Selected builder is unavailable")
        if not capabilities["codex"]["available"]:
            raise ValueError("Codex review is unavailable")
        if builder not in self.runtime.builders:
            raise ValueError("Selected builder is not configured: " + builder)
        reviewers = ["codex"] if mode == "codex" else []
        if not reviewers or any(name not in self.runtime.reviewers for name in reviewers):
            raise ValueError("Selected reviewer is not configured")
        return builder, reviewers

    def quick_launch(self, data):
        mission = str(data.get("mission", "")).strip()
        if not mission:
            raise ValueError("mission cannot be empty")
        builder_name, reviewer_names = self._quick_configuration(data)
        root = self.quick_sandbox()
        contract = "Work only in src/ and tests/. Do not install packages, commit, push, access external paths, or modify the sandbox policy. Run the tests.\n\nUser task:\n" + mission
        return self._start("quick-sandbox", root, contract, ["src/", "tests/"], ["python3", "-m", "pytest", "-q"], [], builder_name, reviewer_names)

    def launch(self, data):
        root = Path(data["workspace"]).expanduser().resolve()
        paths = [str(path).strip() for path in data.get("allowed_paths", []) if str(path).strip()]
        if not paths:
            raise ValueError("at least one allowed write path is required")
        builder_name = str(data.get("builder_name") or next(iter(self.runtime.builders)))
        return self._start(str(data.get("project_id") or root.name), root, str(data["mission"]), paths, self.command(str(data["full_test_command"])), self.command(str(data["targeted_test_command"])) if str(data.get("targeted_test_command", "")).strip() else [], builder_name, ["codex"] if bool(data.get("codex_review", True)) else [])

    def drive(self, run_id):
        run = self.runtime.get(run_id)
        workspace = str(Path(run["workspace"]).resolve())

        def worker():
            try:
                while True:
                    state = self.runtime.run_once(run_id)
                    if state["status"] != "correcting":
                        break
            except Exception as exc:
                self.runtime._event(self.runtime.get(run_id), "runtime_exception", error=f"{type(exc).__name__}: {exc}")
            finally:
                with self._dispatch_lock:
                    if self._active_workspaces.get(workspace) == run_id:
                        self._active_workspaces.pop(workspace, None)

        thread = threading.Thread(target=worker, name="joao-" + run_id, daemon=True)
        with self._dispatch_lock:
            prior = self._active_workspaces.get(workspace)
            if prior and self.workers.get(prior) and self.workers[prior].is_alive():
                raise RuntimeStateError("another JOAO builder is active for this worktree")
            self.workers[run_id] = thread
            self._active_workspaces[workspace] = run_id
            thread.start()
        return {"run_id": run_id, "status": "queued"}

    def resume_drive(self, run_id):
        previous = self.workers.get(run_id)
        if previous is not None and previous.is_alive():
            def deferred_restart():
                previous.join()
                self.drive(run_id)
            threading.Thread(target=deferred_restart, name="joao-resume-" + run_id, daemon=True).start()
            return {"run_id": run_id, "status": "resume_queued"}
        return self.drive(run_id)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/"

    def serve_in_thread(self):
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self._server_thread = thread
        return thread

    def close(self):
        if self._server_thread is not None:
            self.server.shutdown()
        self.server.server_close()
