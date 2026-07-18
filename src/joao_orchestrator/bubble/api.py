"""Local-only JOAO command console API and browser UI."""
from __future__ import annotations
import json
import secrets
import shlex
import threading
from datetime import date, timezone, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from ..domain.models import ProjectProfile
from .kickoff import Kickoff, KickoffError
from .runtime import RunRuntime, RuntimeStateError, claude_capability

HTML = """<!doctype html><meta charset=utf-8><title>JOAO Command Center</title>
<style>:root{color-scheme:dark}body{margin:0;background:#091019;color:#e8f0fa;font:14px system-ui}main{max-width:1050px;margin:24px auto;padding:0 20px}.shell{background:#101925;border:1px solid #38516d;border-radius:16px;padding:22px}h1{margin:0}.sub,label,.note{color:#9db1c9}.sub{margin:5px 0 18px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.wide{grid-column:1/-1}label{display:block;font-size:12px;margin-bottom:5px}input,textarea{box-sizing:border-box;width:100%;color:#eef;background:#080e15;border:1px solid #40546d;border-radius:8px;padding:10px;font:inherit}textarea{min-height:130px}button{background:#1e65a7;color:white;border:1px solid #5b8fc0;border-radius:8px;padding:9px 13px;margin:4px 5px 4px 0;font-weight:700}.alt{background:#182938}.card{border:1px solid #2d435d;border-radius:10px;padding:14px;margin-top:16px}.status{font-weight:800}.ok{color:#75dda5}.warn{color:#ffce70}.bad{color:#ff8f8f}pre{white-space:pre-wrap;max-height:42vh;overflow:auto}</style>
<main><div class=shell><h1>JOAO Command Center</h1><div class=sub>Mission locale → GLM/ZCode → reviews Codex → validation humaine.</div>
<div class=card><b>Phase 0 — Kickoff (obligatoire avant toute mission)</b><div class=grid><div><label>Projet</label><input id=kp value=local-project></div><div style="align-self:end"><button class=alt onclick="kstart()">Démarrer le Kickoff</button></div></div><div id=kq class=note style="margin-top:8px">—</div><div class=grid><div class=wide><label>Ta réponse</label><input id=ka placeholder="réponse courte"></div></div><p><button class=alt onclick="kanswer()">Répondre</button><button id=kgo onclick="ksign()" title="Signature explicite du Boss">✅ GO Boss</button><span id=ks class=note></span></p></div>
<form id=f><div class=grid><div><label>Projet</label><input id=p value=local-project required></div><div><label>Worktree Git cible</label><input id=w placeholder="/chemin/vers/repo-git" required></div><div class=wide><label>Ta mission</label><textarea id=m placeholder="Décris précisément ce que JOAO doit faire." required></textarea></div><div><label>Chemins modifiables (un par ligne)</label><textarea id=a style="min-height:80px" placeholder="src/&#10;tests/" required></textarea></div><div><label>Test complet obligatoire</label><input id=t value="python3 -m pytest -q" required><label style="margin-top:8px">Test ciblé optionnel</label><input id=q placeholder="python3 -m pytest -q tests/test_x.py"></div></div><p><button>Lancer avec GLM</button><span id=c class=note></span></p></form>
<div class=card><span id=s class=status>Aucune mission active</span><div id=n class=note></div><p><button class=alt onclick="act('pause')">Pause</button><button class=alt onclick="act('resume')">Resume</button><button class=alt onclick="act('stop')">Stop</button><button class=alt onclick="act('retry')">Retry</button><button onclick="act('approve')">Approve</button><button class=alt onclick="act('reject')">Reject</button></p><pre id=d></pre></div></div></main>
<script>const TOKEN="__JOAO_TOKEN__";let run=null;let x=id=>document.getElementById(id);let lines=id=>x(id).value.split('\\n').map(v=>v.trim()).filter(Boolean);async function j(u,o={}){o.headers={...(o.headers||{}),'X-JOAO-Token':TOKEN};let r=await fetch(u,o),v=await r.json();if(!r.ok)throw Error(v.error||'request failed');return v}async function caps(){let v=await j('/capabilities');x('c').textContent='GLM '+(v.glm.available?'connected':'unavailable')+' · Codex '+(v.codex.available?'connected':'unavailable')+' · Claude '+(v.claude.available?'optional connected':'optional unavailable')}async function refresh(){if(!run)return;try{let v=await j('/runs/'+run);x('s').textContent=v.status;x('s').className='status '+(['blocked','failed'].includes(v.status)?'bad':v.status==='accepted'?'ok':'warn');x('n').textContent=v.project_id+' · '+v.current_step;x('d').textContent=JSON.stringify(v,null,2)}catch(e){x('d').textContent=e.message}}async function act(a){if(!run)return;try{await j('/runs/'+run+'/'+a,{method:'POST'});refresh()}catch(e){x('d').textContent=e.message}}x('f').onsubmit=async e=>{e.preventDefault();try{let v={project_id:x('p').value,workspace:x('w').value,mission:x('m').value,allowed_paths:lines('a'),full_test_command:x('t').value,targeted_test_command:x('q').value};let z=await j('/missions',{method:'POST',body:JSON.stringify(v)});run=z.run_id;x('s').textContent='queued '+run;refresh()}catch(e){x('d').textContent=e.message}};async function kshow(v){let q=v.question;x('kq').textContent=v.complete?(v.signed?'✅ Spec signée — missions autorisées':'Interview terminée — clique « GO Boss » pour signer'):(q?('['+q.key+'] '+q.text):'—');x('ks').textContent=v.signed?'signé':(v.complete?'en attente de signature':'')}
async function kstart(){try{await kshow(await j('/kickoff/'+x('kp').value+'/start',{method:'POST'}))}catch(e){x('ks').textContent=e.message}}
async function kanswer(){try{let v=await j('/kickoff/'+x('kp').value+'/answer',{method:'POST',body:JSON.stringify({text:x('ka').value})});x('ka').value='';kshow(v)}catch(e){x('ks').textContent=e.message}}
async function ksign(){try{kshow(await j('/kickoff/'+x('kp').value+'/sign',{method:'POST'}))}catch(e){x('ks').textContent=e.message}}
setInterval(refresh,1200);caps()</script>"""

class LocalAPIServer:
    def __init__(self, runtime: RunRuntime, host="127.0.0.1", port=0):
        if host not in {"127.0.0.1","localhost","::1"}: raise ValueError("localhost only")
        self.runtime,self.workers=runtime,{}
        self.kickoff=Kickoff(runtime.projects_root)
        self.token=secrets.token_urlsafe(32)
        self._dispatch_lock=threading.RLock()
        self._active_workspaces={}
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def send(self,code,value,kind="application/json"):
                raw=value.encode() if isinstance(value,str) else json.dumps(value).encode()
                self.send_response(code);self.send_header("Content-Type",kind);self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
            def payload(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))).decode())
            def authorized(self): return secrets.compare_digest(self.headers.get("X-JOAO-Token",""),outer.token)
            def do_GET(self):
                path=urlparse(self.path).path;bits=path.strip("/").split("/")
                try:
                    if path=="/": return self.send(200,HTML.replace("__JOAO_TOKEN__",outer.token),"text/html; charset=utf-8")
                    if not self.authorized(): return self.send(401,{"error":"missing or invalid local session token"})
                    if path=="/capabilities": return self.send(200,outer.capabilities())
                    if len(bits)==2 and bits[0]=="runs": return self.send(200,outer.runtime.get(bits[1]))
                    if len(bits)==3 and bits[2]=="events": return self.send(200,outer.runtime.events(bits[1]))
                    if len(bits)==2 and bits[0]=="kickoff": return self.send(200,outer.kickoff_state(bits[1]))
                    self.send(404,{"error":"not found"})
                except (RuntimeStateError,KickoffError) as exc:self.send(404,{"error":str(exc)})
            def do_POST(self):
                bits=urlparse(self.path).path.strip("/").split("/")
                try:
                    if not self.authorized(): return self.send(401,{"error":"missing or invalid local session token"})
                    if self.path=="/missions":return self.send(202,outer.launch(self.payload()))
                    if len(bits)==3 and bits[0]=="runs" and bits[2] in {"pause","resume","stop","approve","reject"}:return self.send(200,getattr(outer.runtime,bits[2])(bits[1]))
                    if len(bits)==3 and bits[0]=="runs" and bits[2]=="retry":return self.send(202,outer.drive(bits[1]))
                    if len(bits)==3 and bits[0]=="kickoff" and bits[2] in {"start","answer","sign"}:return self.send(200,outer.kickoff_action(bits[1],bits[2],self.payload() if bits[2]=="answer" else {}))
                    self.send(404,{"error":"not found"})
                except (RuntimeStateError,KickoffError,ValueError,json.JSONDecodeError) as exc:self.send(409,{"error":str(exc)})
        self.server=ThreadingHTTPServer((host,port),Handler)
    def capabilities(self):
        import shutil
        return {"glm":{"available":bool(shutil.which("opencode"))},"codex":{"available":bool(shutil.which("codex"))},"claude":claude_capability()}
    @staticmethod
    def command(text):
        argv=shlex.split(text)
        forbidden = "|&;$><" + chr(10) + chr(13)
        if not argv or any(any(ch in part for ch in forbidden) for part in argv):raise ValueError("test command must not use a shell")
        return argv
    def kickoff_state(self,project):
        q=self.kickoff.current_question(project)
        return {"project":project,"question":({"block":q.block,"key":q.key,"text":q.text} if q else None),
                "complete":q is None,"signed":self.kickoff.is_signed(project)}
    def kickoff_action(self,project,action,data):
        if action=="start":self.kickoff.start(project);return self.kickoff_state(project)
        if action=="answer":
            text=str(data.get("text","")).strip()
            if not text:raise ValueError("answer text is required")
            self.kickoff.answer(project,text);return self.kickoff_state(project)
        # sign — the explicit "GO Boss" action; stamped with today's UTC date, never automatic
        self.kickoff.sign(project,date_str=datetime.now(timezone.utc).date().isoformat())
        return self.kickoff_state(project)
    def launch(self,data):
        root=Path(data["workspace"]).expanduser().resolve(); paths=[str(p).strip() for p in data.get("allowed_paths",[]) if str(p).strip()]
        if not paths:raise ValueError("at least one allowed write path is required")
        ident=str(data.get("project_id") or root.name)
        profile=ProjectProfile(project_id=ident,display_name=ident,repository_root=str(root),allowed_write_paths=paths,forbidden_paths=[],approval_required=True)
        target=str(data.get("targeted_test_command","")).strip()
        run=self.runtime.start(project_id=ident,workspace=root,mission=str(data["mission"]),targeted_tests=[self.command(target)] if target else [],full_tests=[self.command(str(data["full_test_command"]))],profile=profile)
        self.drive(run);return {"run_id":run,"status":"queued"}
    def drive(self,run_id):
        run=self.runtime.get(run_id)
        workspace=str(Path(run["workspace"]).resolve())
        def worker():
            try:
                while True:
                    state=self.runtime.run_once(run_id)
                    if state["status"]!="correcting":break
            except Exception as exc:
                run=self.runtime.get(run_id);self.runtime._event(run,"runtime_exception",error=f"{type(exc).__name__}: {exc}")
            finally:
                with self._dispatch_lock:
                    if self._active_workspaces.get(workspace)==run_id:self._active_workspaces.pop(workspace,None)
        thread=threading.Thread(target=worker,name="joao-"+run_id,daemon=True)
        with self._dispatch_lock:
            prior=self._active_workspaces.get(workspace)
            if prior and self.workers.get(prior) and self.workers[prior].is_alive():
                raise RuntimeStateError("another JOAO builder is active for this worktree")
            self.workers[run_id]=thread;self._active_workspaces[workspace]=run_id;thread.start()
        return {"run_id":run_id,"status":"queued"}
    @property
    def url(self):return f"http://127.0.0.1:{self.server.server_port}/"
    def serve_in_thread(self):
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start();return thread
    def close(self):self.server.shutdown();self.server.server_close()
