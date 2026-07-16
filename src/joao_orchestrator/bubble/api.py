"""Local-only JOAO chat console, backed by the bounded runtime."""
from __future__ import annotations

import json
import secrets
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ..domain.models import ProjectProfile
from .runtime import RunRuntime, RuntimeStateError


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
button:disabled{background:#30363d;border-color:#30363d;color:#8b949e;cursor:not-allowed}
.modes{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.mode{border:1px solid #30363d;border-radius:8px;padding:8px 10px;color:#c9d1d9;cursor:pointer}.mode:has(input:checked){border-color:#58a6ff;background:#0c2d4a}.mode input{accent-color:#58a6ff}
pre{white-space:pre-wrap;max-height:260px;overflow:auto;color:#c9d1d9}
.summary{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;margin:12px 0;font-size:13px}
label.no-review{color:#f85149;background:#3d1514;padding:4px 8px;border-radius:4px;font-weight:700}
label.self-review{color:#d29922;background:#4d3800;padding:4px 8px;border-radius:4px;font-weight:700}
</style>
<main class="app"><div class="top"><div class="title">JOAO</div><div id="capabilities" class="muted">Chargement…</div></div>
<div class="sub">Écris comme ici. JOAO pilote GLM, applique seulement les options que tu choisis, et conserve les preuves.</div>
<section id="chat" class="chat"><div class="empty">Écris une première tâche pour tester JOAO dans son sandbox isolée.</div></section>
<section class="composer"><div class="muted">Disposable workspace</div><select id="workspace" style="width:100%;box-sizing:border-box;margin:8px 0;border:1px solid #30363d;border-radius:8px;background:#161b22;color:#e6edf3;padding:10px;font:inherit"><option value="quick-sandbox">Quick Sandbox (auto-generated)</option></select>
<textarea id="prompt" autofocus placeholder="Ex. Ajoute une fonction qui normalise un titre et les tests associés."></textarea>
<div class="muted">Moteur de construction — un seul à la fois</div><div class="modes">
<label class="mode"><input type="radio" name="builder" value="glm" checked> GLM</label>
<label class="mode"><input type="radio" name="builder" value="codex"> Codex</label></div>
<div class="muted">Review indépendante à chaque gate (plan, diff, validation complète, livraison)</div><div class="modes">
<label class="mode"><input type="radio" name="review" value="none"> No Review</label>
<label class="mode"><input type="radio" name="review" value="codex" checked> Codex</label>
<label class="mode"><input type="radio" name="review" value="claude" disabled> Claude</label>
<label class="mode"><input type="radio" name="review" value="codex_and_claude" disabled> Codex + Claude</label>
</div>
<div id="safety-summary" class="summary" style="display:none">Provider preflight check pending...</div>
<button id="start-btn" onclick="send()" disabled>Run</button><span class="muted">Le test est isolé : aucune modification de tes projets.</span></section></main>
<script>
const TOKEN="__JOAO_TOKEN__";let active=null;const el=id=>document.getElementById(id);
async function req(url,opt={}){opt.headers={...(opt.headers||{}),'X-JOAO-Token':TOKEN};const r=await fetch(url,opt),v=await r.json();if(!r.ok)throw Error(v.error||'request failed');return v}
function esc(s){const n=document.createElement('span');n.textContent=s;return n.innerHTML}
function show(v){const isSelfReview=v.is_self_review;const noReview=v.no_review_label;let reviewLabel='';if(noReview)reviewLabel='<label class="no-review">NO REVIEW — HUMAN APPROVAL REQUIRED</label>';else if(isSelfReview)reviewLabel='<label class="self-review">SELF-REVIEW — NOT INDEPENDENT</label>';const progress=v.progress||{completed:0,total:0};const files=v.changed_files?JSON.stringify(v.changed_files.changed_by_builder||[]):'pending';const tests=v.test_results?JSON.stringify(v.test_results):'pending';const review=v.review_findings?JSON.stringify(v.review_findings):'pending';el('chat').innerHTML='<div class="run"><div class="state">'+esc(v.status)+' · '+progress.completed+'/'+progress.total+' gates · '+esc(v.elapsed_seconds)+'s</div>'+reviewLabel+'<div class="muted">'+esc(v.current_step)+'</div><div>Run <code>'+esc(v.run_id)+'</code></div><div>Builder: '+esc(v.builder_provider)+' / '+esc(v.builder_model)+'</div><div>Review: '+esc(v.review_policy)+' ('+esc((v.reviewer_providers||[]).join(' + ')||'none')+')</div><div class="muted">Modified: '+esc(files)+'</div><div class="muted">Tests: '+esc(tests)+'</div><div class="muted">Review: '+esc(review)+'</div><div class="muted">Evidence: '+esc(v.evidence_directory)+'</div><button class="ghost" onclick="openEvidence()">Open evidence</button><button class="ghost" onclick="openDiff()">Open final diff</button><pre id="detail">'+esc(JSON.stringify(v,null,2))+'</pre><button class="ghost" onclick="act(\\'pause\\')">Pause après étape</button><button class="ghost" onclick="act(\\'resume\\')">Resume</button><button class="ghost" onclick="act(\\'stop\\')">Stop après étape</button><button class="ghost" onclick="act(\\'retry\\')">Retry</button><button class="ghost" onclick="act(\\'approve\\')">Approve</button><button class="ghost" onclick="act(\\'reject\\')">Reject</button></div>'}
function selected(name){return document.querySelector('input[name="'+name+'"]:checked').value}
async function send(){const mission=el('prompt').value.trim();if(!mission)return;try{const v=await req('/quick-missions',{method:'POST',body:JSON.stringify({mission,builder_name:selected('builder'),review_mode:selected('review')})});active=v.run_id;el('prompt').value='';poll()}catch(e){el('chat').innerHTML='<div class="run">'+esc(e.message)+'</div>'}}
async function act(name){if(active){try{await req('/runs/'+active+'/'+name,{method:'POST'})}catch(e){el('chat').innerHTML='<div class="run">'+esc(e.message)+'</div>'}}poll()}
async function openEvidence(){if(active){try{el('detail').textContent=JSON.stringify(await req('/runs/'+active+'/evidence-metadata'),null,2)}catch(e){el('detail').textContent=e.message}}}
async function openDiff(){if(active){try{const v=await req('/runs/'+active+'/final-diff');el('detail').textContent=v.diff_content||v.error}catch(e){el('detail').textContent=e.message}}}
async function poll(){if(!active)return;try{show(await req('/runs/'+active))}catch(_){}}
function enableChoice(name,value,enabled){const input=document.querySelector('input[name="'+name+'"][value="'+value+'"]');input.disabled=!enabled;input.closest('.mode').style.opacity=enabled?'1':'.45'}
function ensureChoice(name){const current=document.querySelector('input[name="'+name+'"]:checked');if(!current||current.disabled){const fallback=document.querySelector('input[name="'+name+'"]:not(:disabled)');if(fallback)fallback.checked=true}}
function updateSafetySummary(caps){const summary=[];if(caps.glm.available)summary.push('GLM: ready ('+caps.glm.executable+')');else summary.push('GLM: unavailable ('+caps.glm.reason+')');if(caps.codex.available)summary.push('Codex: ready ('+caps.codex.executable+')');else summary.push('Codex: unavailable ('+caps.codex.reason+')');summary.push('Claude: '+caps.claude.reason);const isSelfReview=selected('builder')==='codex'&&selected('review').includes('codex');const noReview=selected('review')==='none';if(isSelfReview)summary.push('WARNING: Self-review (Codex builder + Codex reviewer)');if(noReview)summary.push('WARNING: No-review policy - changes will NOT be independently reviewed');el('safety-summary').textContent=summary.join(' | ');el('safety-summary').style.display='block';el('safety-summary').style.color=(isSelfReview||noReview)?'#f85149':'#8b949e';}
async function caps(){const v=await req('/capabilities');enableChoice('builder','glm',v.glm.available);enableChoice('builder','codex',v.codex.available);enableChoice('review','codex',v.codex.reviewer_available);enableChoice('review','claude',v.claude.reviewer_available);enableChoice('review','codex_and_claude',v.codex.reviewer_available&&v.claude.reviewer_available);ensureChoice('builder');ensureChoice('review');updateSafetySummary(v);el('capabilities').textContent='GLM '+(v.glm.available?'prêt':'indisponible')+' · Codex '+(v.codex.available?'prêt':'indisponible')+' · Claude '+(v.claude.available?'optionnel':'non configuré');validateSelection()}
function validateSelection(){const builder=selected('builder');const review=selected('review');const capsPromise=req('/capabilities');capsPromise.then(v=>{let valid=el('workspace').value==='quick-sandbox'&&el('prompt').value.trim().length>0&&v[builder].available;if(review==='codex'&&!v.codex.reviewer_available)valid=false;if(review==='claude'&&!v.claude.reviewer_available)valid=false;if(review==='codex_and_claude'&&!(v.codex.reviewer_available&&v.claude.reviewer_available))valid=false;el('start-btn').disabled=!valid;updateSafetySummary(v)}).catch(()=>{el('start-btn').disabled=true})}
document.querySelectorAll('input[name="builder"],input[name="review"]').forEach(i=>i.addEventListener('change',validateSelection));
el('prompt').addEventListener('input',validateSelection);el('workspace').addEventListener('change',validateSelection);
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
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "events":
                        return self.send(200, outer.runtime.events(bits[1]))
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "evidence-metadata":
                        return self.send(200, outer.runtime.get_evidence_metadata(bits[1]))
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "final-diff":
                        return self.send(200, outer.runtime.get_final_diff(bits[1]))
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
        def preflight(adapter, unavailable_reason):
            base = {
                "available": False, "executable": None, "model": None, "provider": None,
                "reason": unavailable_reason, "auth_status": "unknown",
                "config_status": "not_configured", "last_error": unavailable_reason,
                "real_or_mock": "unknown",
            }
            if adapter is None:
                return base
            if not hasattr(adapter, "preflight"):
                if self.runtime.allow_test_adapters:
                    return {**base, "available": True, "provider": getattr(adapter, "provider", "test"),
                            "model": getattr(adapter, "model", "test"), "real_or_mock": "mock",
                            "reason": "test adapter", "config_status": "test", "last_error": None}
                return base
            try:
                return {**base, **adapter.preflight()}
            except Exception as exc:
                return {**base, "reason": f"preflight exception: {exc}",
                        "config_status": "error", "last_error": str(exc)}

        glm = preflight(self.runtime.builders.get("glm"), "GLM builder not configured")
        codex = preflight(self.runtime.builders.get("codex"), "Codex builder not configured")
        codex_review = preflight(self.runtime.reviewers.get("codex"), "Codex reviewer not configured")
        claude_review = preflight(self.runtime.reviewers.get("claude"), "Claude reviewer not configured")
        codex.update({
            "reviewer_available": bool(codex_review["available"]),
            "reviewer_reason": codex_review["reason"],
            "reviewer_model": codex_review["model"],
            "reviewer_executable": codex_review["executable"],
            "reviewer_last_error": codex_review["last_error"],
        })
        claude_review.update({"reviewer_available": bool(claude_review["available"])})
        return {"glm": glm, "codex": codex, "claude": claude_review,
                "workspace_lock": {"active_count": len(self._active_workspaces)}}

    @staticmethod
    def command(text):
        argv = shlex.split(text)
        forbidden = "|&;$><" + chr(10) + chr(13)
        if not argv or any(any(char in item for char in forbidden) for item in argv):
            raise ValueError("test command must not use a shell")
        return argv

    def _start(self, project, workspace, mission, paths, full, target, builder_name, reviewer_names, review_policy, generated_paths=None):
        profile = ProjectProfile(project_id=project, display_name=project, repository_root=str(workspace), allowed_write_paths=paths, forbidden_paths=[], generated_paths=list(generated_paths or []), approval_required=True)
        run = self.runtime.start(project_id=project, workspace=workspace, mission=mission, targeted_tests=[target] if target else [], full_tests=[full], profile=profile, builder_name=builder_name, reviewer_names=reviewer_names, review_policy=review_policy)
        self.drive(run)
        return {"run_id": run, "status": "queued"}

    def quick_sandbox(self):
        root = self.runtime.root / "sandboxes" / ("quick-" + secrets.token_hex(5))
        (root / "src").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "tests" / "__init__.py").write_text("")
        (root / "src" / "task.py").write_text('"""Safe JOAO quick-task sandbox."""\n\ndef identity(value):\n    return value\n')
        (root / "tests" / "test_smoke.py").write_text(
            "import unittest\n\nfrom src.task import identity\n\n"
            "class SmokeTest(unittest.TestCase):\n"
            "    def test_identity(self):\n"
            "        self.assertEqual(identity('joao'), 'joao')\n"
        )
        for argv in (["git", "init", "-q"], ["git", "config", "user.email", "joao-sandbox@example.invalid"], ["git", "config", "user.name", "JOAO Sandbox"], ["git", "add", "."], ["git", "commit", "-qm", "sandbox baseline"]):
            subprocess.run(argv, cwd=str(root), check=True)
        return root

    def _quick_configuration(self, data):
        builder = str(data.get("builder_name", "glm"))
        mode = str(data.get("review_mode", "codex"))

        # Map review modes to reviewer names and policy
        review_mapping = {
            "none": ([], "none"),
            "codex": (["codex"], "codex"),
            "claude": (["claude"], "claude"),
            "codex_and_claude": (["codex", "claude"], "codex_and_claude"),
            "codex_claude": (["codex", "claude"], "codex_and_claude"),
        }

        if builder not in {"glm", "codex"} or mode not in review_mapping:
            raise ValueError("unknown builder or review mode")

        capabilities = self.capabilities()

        # Check builder availability
        if not capabilities[builder]["available"]:
            raise ValueError(f"Selected builder is unavailable: {capabilities[builder]['reason']}")

        # Check reviewer availability (skip for no-review)
        reviewer_names, review_policy = review_mapping[mode]
        for name in reviewer_names:
            reviewer_available = capabilities[name].get("reviewer_available", capabilities[name]["available"])
            if not reviewer_available:
                prefix = "Claude mode cannot run yet" if name == "claude" else "Selected reviewer is unavailable"
                raise ValueError(f"{prefix}: {capabilities[name]['reason']}")

        # Check builder configuration
        if builder not in self.runtime.builders:
            raise ValueError("Selected builder is not configured: " + builder)

        # Check reviewer configuration (skip for no-review)
        if reviewer_names and any(name not in self.runtime.reviewers for name in reviewer_names):
            raise ValueError("Selected reviewer is not configured")

        # Detect self-review
        is_self_review = builder == "codex" and "codex" in reviewer_names
        return builder, reviewer_names, review_policy, is_self_review

    def quick_launch(self, data):
        mission = str(data.get("mission", "")).strip()
        if not mission:
            raise ValueError("mission cannot be empty")
        builder_name, reviewer_names, review_policy, is_self_review = self._quick_configuration(data)
        default_quick_paths = ["todo.py", "test_todo.py", "todo.json", "test_tasks.json", "src/", "tests/"]
        safe_quick_paths = set(default_quick_paths)
        requested_paths = data.get("allowed_paths")
        allowed = [str(path) for path in requested_paths] if requested_paths is not None else default_quick_paths
        if not allowed or len(set(allowed)) != len(allowed) or any(path not in safe_quick_paths for path in allowed):
            raise ValueError("quick sandbox allowed_paths must be a non-empty subset of the safe quick paths")
        root = self.quick_sandbox()
        contract = (
            "Work only inside this disposable Git sandbox. Do not install packages, commit, "
            "push, access external paths, or modify the sandbox policy. Use only Python's "
            "standard-library unittest framework for tests, and run the recorded test command. "
            "The term needs_approval names a JOAO runtime state: never create a file or directory "
            "with that name. The sandbox-local todo.json and test_tasks.json paths may be used "
            "during validation, but remove test/runtime data before delivery.\n\n"
            "User task:\n" + mission
        )
        full_test = ["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test*.py"]
        generated = [path for path in ("todo.json", "test_tasks.json") if path in allowed]
        return self._start("quick-sandbox", root, contract, allowed, full_test, [], builder_name, reviewer_names, review_policy, generated)

    def launch(self, data):
        root = Path(data["workspace"]).expanduser().resolve()
        paths = [str(path).strip() for path in data.get("allowed_paths", []) if str(path).strip()]
        if not paths:
            raise ValueError("at least one allowed write path is required")
        builder_name, reviewer_names, review_policy, _ = self._quick_configuration(data)
        return self._start(str(data.get("project_id") or root.name), root, str(data["mission"]), paths, self.command(str(data["full_test_command"])), self.command(str(data["targeted_test_command"])) if str(data.get("targeted_test_command", "")).strip() else [], builder_name, reviewer_names, review_policy)

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
