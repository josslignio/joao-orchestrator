"""Local-only JOAO chat console, backed by the bounded runtime."""
from __future__ import annotations

import json
import secrets
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..domain.models import ProjectProfile
from .runtime import RunRuntime, RuntimeStateError


def safe_disposition_filename(name: str) -> str:
    """Builder-controlled names must never reach raw HTTP headers.

    Keeps printable ASCII minus the quote/backslash; anything else (CR/LF
    header injection, non-latin-1 crash material) becomes an underscore.
    """
    cleaned = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name)
    return cleaned.strip() or "download"


HTML = """<!doctype html>
<meta charset="utf-8"><title>JOÃO.AI</title>
<style>
:root{color-scheme:dark}body{margin:0;background:#0d1117;color:#e6edf3;font:14px system-ui}
.app{max-width:940px;margin:auto;padding:14px 20px}
.top{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.title{font-size:20px;font-weight:750}
.muted{color:#8b949e;font-size:12px}
.ok{color:#3fb950}.warn{color:#d29922}.err{color:#f85149}
.chip{display:inline-block;border:1px solid #30363d;border-radius:999px;padding:2px 9px;font-size:12px;margin-left:5px}
.chip.ok{border-color:#238636}.chip.err{border-color:#f85149}.chip.warn{border-color:#d29922}
textarea{width:100%;box-sizing:border-box;min-height:64px;border:1px solid #30363d;border-radius:8px;background:#161b22;color:#e6edf3;padding:9px;font:inherit;resize:vertical}
button{background:#238636;border:1px solid #2ea043;color:white;border-radius:7px;padding:6px 12px;font-weight:650;margin-right:6px;cursor:pointer}
.ghost{background:#21262d;border-color:#30363d}
button:disabled{background:#161b22;border-color:#21262d;color:#484f58;cursor:not-allowed}
.modes{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.mode{border:1px solid #30363d;border-radius:7px;padding:4px 9px;color:#c9d1d9;cursor:pointer;font-size:13px}
.mode:has(input:checked){border-color:#58a6ff;background:#0c2d4a}.mode input{accent-color:#58a6ff}
.form{border:1px solid #30363d;border-radius:10px;background:#161b22;padding:10px 12px;margin:10px 0}
.card{border:1px solid #30363d;border-radius:10px;background:#161b22;padding:10px 12px;margin:8px 0}
.pill{display:inline-block;border-radius:999px;padding:1px 10px;font-weight:700;font-size:12px}
.pill.run{background:#0c2d4a;color:#58a6ff}.pill.good{background:#0f2e18;color:#3fb950}
.pill.wait{background:#4d3800;color:#d29922}.pill.bad{background:#3d1514;color:#f85149}
.pill.off{background:#21262d;color:#8b949e}
.label{padding:2px 7px;border-radius:4px;font-weight:700;font-size:11px;margin-left:6px}
.label.no-review{color:#f85149;background:#3d1514}
.label.self-review{color:#d29922;background:#4d3800}
.label.independent{color:#3fb950;background:#0f2e18}
.blockbox{border:1px solid #f85149;background:#3d1514;border-radius:7px;padding:7px 10px;margin:7px 0;font-size:13px}
.resultbox{border:1px solid #238636;background:#0f2e18;border-radius:7px;padding:7px 10px;margin:7px 0;font-size:13px}
.hint{color:#d29922;font-size:12px;margin-top:3px}
.feedback{min-height:16px;font-size:12px;margin-top:5px}
details{margin-top:6px}summary{cursor:pointer;color:#8b949e;font-size:12px}
pre{white-space:pre-wrap;max-height:220px;overflow:auto;color:#c9d1d9;font-size:11px}
.row{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;margin:3px 0}
#runs{max-height:58vh;overflow-y:auto}
</style>
<main class="app">
<div class="top"><div class="title">JOÃO.AI</div><div id="capabilities" class="muted">Chargement…</div></div>
<section class="form">
<textarea id="prompt" autofocus placeholder="Ex. Ajoute une fonction qui normalise un titre et les tests associés."></textarea>
<div class="muted">Moteur — un seul à la fois</div><div class="modes">
<label class="mode"><input type="radio" name="builder" value="glm" checked> GLM</label>
<label class="mode"><input type="radio" name="builder" value="codex"> Codex</label>
<label class="mode"><input type="radio" name="builder" value="claude"> Claude</label></div>
<div class="muted">Review à chaque gate (plan, diff, tests, livraison)</div><div class="modes">
<label class="mode"><input type="radio" name="review" value="none"> No Review</label>
<label class="mode"><input type="radio" name="review" value="codex" checked> Codex</label>
<label class="mode"><input type="radio" name="review" value="claude"> Claude</label>
<label class="mode"><input type="radio" name="review" value="glm"> GLM</label>
<label class="mode"><input type="radio" name="review" value="codex_and_claude"> Codex + Claude</label>
<label class="mode"><input type="radio" name="review" value="claude_and_glm"> Claude + GLM</label>
</div>
<div id="safety-summary" class="muted"></div>
<div id="quota-warning" class="warn" style="display:none;font-size:13px;margin:4px 0"></div>
<button id="start-btn" onclick="send()" disabled>Run</button>
<span class="muted">Sandbox Git jetable : aucune modification de tes projets.</span>
<div id="launch-feedback" class="feedback"></div>
</section>
<section id="runs"><div class="muted">Aucun run pour l'instant. Les runs persistés réapparaissent ici après redémarrage.</div></section>
</main>
<script>
const TOKEN="__JOAO_TOKEN__";const el=id=>document.getElementById(id);
let CAPS=null,expanded=null,detail=null;const feedback={};
const ACTIONS={pending:["pause","stop"],planning:["pause","stop"],ready:["pause","stop"],
 building:["pause","stop"],testing:["pause","stop"],reviewing:["pause","stop"],
 needs_approval:["approve","reject","stop"],paused:["resume","stop"],correcting:["stop"],
 blocked:["retry","reject"],failed:["retry","reject"],accepted:[],stopped:[]};
const ALL=["pause","resume","stop","retry","approve","reject"];
const PILL={pending:"run",planning:"run",ready:"run",building:"run",testing:"run",reviewing:"run",
 correcting:"run",needs_approval:"wait",paused:"wait",blocked:"bad",failed:"bad",accepted:"good",stopped:"off"};
async function req(url,opt={}){opt.headers={...(opt.headers||{}),'X-JOAO-Token':TOKEN};
 const r=await fetch(url,opt),v=await r.json();if(!r.ok)throw Error(v.error||'requête refusée');return v}
function esc(s){const n=document.createElement('span');n.textContent=s==null?'':s;return n.innerHTML.replace(/"/g,'&quot;')}
function selected(name){return document.querySelector('input[name="'+name+'"]:checked').value}
function labels(v){let out='';if(v.no_review_label)out+='<span class="label no-review">NO REVIEW — HUMAN APPROVAL REQUIRED</span>';
 else if(v.is_self_review)out+='<span class="label self-review">SELF-REVIEW — NON INDÉPENDANTE</span>';
 else if((v.review_policy||'none')!=='none')out+='<span class="label independent">REVIEW INDÉPENDANTE</span>';return out}
function buttons(v){return ALL.map(a=>{const on=(ACTIONS[v.status]||[]).includes(a);
 return '<button class="ghost" data-run="'+esc(v.run_id)+'" data-act="'+a+'"'+(on?'':' disabled')+'>'+a+'</button>'}).join('')}
function card(v,full){const fb=feedback[v.run_id]||{};
 let html='<div class="card" id="card-'+esc(v.run_id)+'">';
 html+='<div class="row"><span class="pill '+(PILL[v.status]||'off')+'">'+esc(v.status)+'</span>'
  +'<code>'+esc(v.run_id)+'</code>'+labels(v)+'</div>';
 html+='<div class="row"><span>Builder: <b>'+esc(v.builder_provider||v.builder_name)+'</b> / '+esc(v.builder_model)+'</span>'
  +'<span>Review: '+esc(v.review_policy||'none')+'</span>'
  +(full&&v.progress?'<span>'+v.progress.completed+'/'+v.progress.total+' gates · '+esc(v.elapsed_seconds)+'s</span>':'')+'</div>';
 html+='<div class="muted">'+esc(v.current_step)+'</div>';
 if(v.block_cause){html+='<div class="blockbox">Cause: '+esc(v.block_cause)
  +'<div class="hint">Déblocage: '+esc(v.unblock_hint)+'</div></div>'}
 html+='<div>'+buttons(v)+'</div>';
 html+='<div class="feedback '+(fb.ok?'ok':'err')+'">'+esc(fb.text||'')+'</div>';
 if(full)html+=resultBlock(v);
 if(full){html+='<details><summary>Évidence JSON (replié)</summary><pre>'+esc(JSON.stringify(detail,null,2))+'</pre></details>'
  +'<div class="muted">Evidence: '+esc(v.evidence_directory||'')+'</div>'}
 else{html+='<div class="muted" style="cursor:pointer" data-expand="'+esc(v.run_id)+'">détails…</div>'}
 return html+'</div>'}
const resZ={};
function resultBlock(v){
 if(!v.result_available||!['needs_approval','accepted'].includes(v.status))return '';
 const st=resZ[v.run_id];
 if(!st||!st.sum)return '<div class="resultbox muted">Résultat en cours de chargement…</div>';
 const s=st.sum;
 let h='<div class="resultbox"><div class="row"><b>Résultat construit</b>'
  +'<span>'+s.files.filter(f=>f.exists).length+' fichier(s) livrés</span>'
  +(s.tests&&s.tests.commands!=null?'<span class="'+(s.tests.all_passed?'ok':'err')+'">tests '+s.tests.passed+'/'+s.tests.commands+(s.tests.all_passed?' OK':'')+'</span>'
    :s.tests&&s.tests.error?'<span class="warn">résultats de tests illisibles</span>'
    :'<span class="warn">tests non exécutés</span>')+'</div>';
 h+='<div><button class="ghost" data-res="diff" data-run="'+esc(v.run_id)+'">'+(st.open.diff?'Masquer le diff':'Voir le diff')+'</button>'
  +'<button class="ghost" data-res="files" data-run="'+esc(v.run_id)+'">'+(st.open.files?'Masquer les fichiers':'Fichiers')+'</button>'
  +'<button class="ghost" data-res="zip" data-run="'+esc(v.run_id)+'">Télécharger tout (zip)</button></div>';
 if(st.open.diff)h+='<pre>'+esc(st.diff==null?'chargement…':st.diff)+'</pre>';
 if(st.open.files){h+=s.files.map(f=>'<div class="row"><code>'+esc(f.path)+'</code>'
   +(f.exists?'<span class="muted">'+f.bytes+' o</span>'
     +(f.is_text?'<button class="ghost" data-res="preview" data-run="'+esc(v.run_id)+'" data-path="'+esc(f.path)+'">aperçu</button>':'')
     +'<button class="ghost" data-res="download" data-run="'+esc(v.run_id)+'" data-path="'+esc(f.path)+'">télécharger</button>'
    :'<span class="err">absent du workspace</span>')+'</div>').join('');
  if(st.preview!=null)h+='<div class="muted">aperçu: '+esc(st.preview)+'</div><pre>'+esc(st.previewData==null?'chargement…':st.previewData)+'</pre>'}
 return h+'</div>'}
async function download(url,filename){const r=await fetch(url,{headers:{'X-JOAO-Token':TOKEN}});
 if(!r.ok)throw Error('téléchargement refusé ('+r.status+')');
 const blob=await r.blob();const a=document.createElement('a');
 a.href=URL.createObjectURL(blob);a.download=filename;a.click();
 setTimeout(()=>URL.revokeObjectURL(a.href),10000)}
async function resultAction(kind,run,path){const st=resZ[run]=resZ[run]||{open:{}};
 try{
  if(kind==='diff'){st.open.diff=!st.open.diff;
   if(st.open.diff&&st.diff==null){const d=await req('/runs/'+run+'/final-diff');st.diff=d.diff_content||d.error}}
  else if(kind==='files'){st.open.files=!st.open.files}
  else if(kind==='zip'){await download('/runs/'+run+'/result/zip',run+'-result.zip')}
  else if(kind==='preview'){st.open.files=true;st.preview=path;st.previewData=null;refresh();
   const d=await req('/runs/'+run+'/result/file?path='+encodeURIComponent(path));
   st.previewData=d.content==null?'(fichier binaire — utilise télécharger)':d.content}
  else if(kind==='download'){await download('/runs/'+run+'/result/file?path='+encodeURIComponent(path)+'&download=1',path.split('/').pop())}
 }catch(err){feedback[run]={ok:false,text:'✗ résultat — '+err.message}}
 refresh()}
async function refresh(){try{const v=await req('/runs');const list=v.runs||[];
 if(!list.length)return;
 if(expanded===null&&list.length)expanded=list[0].run_id;
 if(expanded){try{detail=await req('/runs/'+expanded)}catch(_){detail=null}
  if(detail&&detail.result_available&&['needs_approval','accepted'].includes(detail.status)){
   const st=resZ[expanded]=resZ[expanded]||{open:{}};
   if(!st.sum||st.sumStatus!==detail.status){
    try{st.sum=await req('/runs/'+expanded+'/result');st.sumStatus=detail.status}catch(_){}}}}
 el('runs').innerHTML=list.map(s=>s.run_id===expanded&&detail?card(detail,true):card(s,false)).join('');
 }catch(_){}}
document.addEventListener('click',async e=>{const t=e.target;
 if(t.dataset&&t.dataset.expand){expanded=t.dataset.expand;refresh();return}
 if(t.dataset&&t.dataset.res){resultAction(t.dataset.res,t.dataset.run,t.dataset.path);return}
 if(!(t.dataset&&t.dataset.act))return;
 t.disabled=true;
 try{const r=await req('/runs/'+t.dataset.run+'/'+t.dataset.act,{method:'POST'});
  feedback[t.dataset.run]={ok:r.accepted,text:(r.accepted?'✓ '+t.dataset.act+' acceptée — ':'✗ '+t.dataset.act+' refusée — ')+r.reason};
 }catch(err){feedback[t.dataset.run]={ok:false,text:'✗ '+t.dataset.act+' — '+err.message}}
 refresh()});
async function send(){const mission=el('prompt').value.trim();if(!mission)return;
 el('launch-feedback').textContent='';
 try{const v=await req('/quick-missions',{method:'POST',body:JSON.stringify({mission,builder_name:selected('builder'),review_mode:selected('review')})});
  expanded=v.run_id;el('prompt').value='';
  el('launch-feedback').innerHTML='<span class="ok">✓ run lancé: '+esc(v.run_id)+'</span>';refresh()}
 catch(e){el('launch-feedback').innerHTML='<span class="err">✗ lancement refusé — '+esc(e.message)+'</span>'}}
function enableChoice(name,value,enabled){const input=document.querySelector('input[name="'+name+'"][value="'+value+'"]');
 input.disabled=!enabled;input.closest('.mode').style.opacity=enabled?'1':'.45'}
function ensureChoice(name){const current=document.querySelector('input[name="'+name+'"]:checked');
 if(!current||current.disabled){const fallback=document.querySelector('input[name="'+name+'"]:not(:disabled)');if(fallback)fallback.checked=true}}
function reviewParts(r){return r==='none'?[]:r.split('_and_')}
function quotaDoomed(){if(!CAPS||!CAPS.codex.quota_warning)return false;
 const b=selected('builder'),r=selected('review');return b==='codex'||reviewParts(r).includes('codex')}
function updateSafety(){if(!CAPS)return;const v=CAPS;const parts=[];
 for(const name of ['glm','codex','claude']){const cap=v[name];
  parts.push('<span class="'+(cap.available?'ok':'err')+'">'+name.toUpperCase()+': '+(cap.available?'prêt':'indisponible')+'</span>')}
 const b=selected('builder'),r=selected('review');
 if(reviewParts(r).includes(b))
  parts.push('<span class="warn">AVERTISSEMENT: self-review — '+b+' construit ET review</span>');
 if(r==='none')parts.push('<span class="err">NO REVIEW — approbation humaine seule</span>');
 el('safety-summary').innerHTML=parts.join(' · ');
 const q=el('quota-warning');
 if(quotaDoomed()){q.style.display='block';
  q.textContent='⚠ Quota Codex épuisé ('+(v.codex.quota_warning.at||'récemment')+') — si tu lances quand même, le run se bloquera à la première gate Codex; utilise alors Reject pour le terminer, ou choisis une review sans Codex.'}
 else q.style.display='none';
 let valid=el('prompt').value.trim().length>0&&v[b].available;
 for(const name of reviewParts(r))if(!v[name].reviewer_available)valid=false;
 el('start-btn').disabled=!valid}
async function caps(){try{CAPS=await req('/capabilities');const v=CAPS;
 enableChoice('builder','glm',v.glm.available);enableChoice('builder','codex',v.codex.available);
 enableChoice('builder','claude',v.claude.available);
 document.querySelectorAll('input[name="review"]').forEach(i=>{
  enableChoice('review',i.value,reviewParts(i.value).every(n=>v[n].reviewer_available))});
 ensureChoice('builder');ensureChoice('review');
 el('capabilities').innerHTML=['glm','codex','claude'].map(n=>'<span class="chip '+(v[n].available?'ok':'err')+'">'
  +n.toUpperCase()+' '+(v[n].available?'prêt':'indisponible')+'</span>').join('')
  +(v.codex.quota_warning?'<span class="chip warn">quota Codex épuisé</span>':'');
 updateSafety()}catch(_){el('start-btn').disabled=true}}
document.querySelectorAll('input[name="builder"],input[name="review"]').forEach(i=>i.addEventListener('change',updateSafety));
el('prompt').addEventListener('input',updateSafety);
setInterval(refresh,2000);setInterval(caps,30000);caps();refresh();
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

            def send_blob(self, raw, kind, filename):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{safe_disposition_filename(filename)}"')
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
                    if path == "/runs":
                        return self.send(200, {"runs": outer.runtime.list_runs()})
                    if len(bits) == 2 and bits[0] == "runs":
                        return self.send(200, outer.runtime.get(bits[1]))
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "events":
                        return self.send(200, outer.runtime.events(bits[1]))
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "timeline":
                        return self.send(200, {"steps": outer.runtime.get_timeline(bits[1])})
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "evidence-metadata":
                        return self.send(200, outer.runtime.get_evidence_metadata(bits[1]))
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "final-diff":
                        return self.send(200, outer.runtime.get_final_diff(bits[1]))
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] == "result":
                        return self.send(200, outer.runtime.get_result_summary(bits[1]))
                    if len(bits) == 4 and bits[0] == "runs" and bits[2] == "result" and bits[3] == "zip":
                        bundle = outer.runtime.build_result_zip(bits[1])
                        return self.send_blob(bundle["content"], "application/zip", bundle["filename"])
                    if len(bits) == 4 and bits[0] == "runs" and bits[2] == "result" and bits[3] == "file":
                        query = parse_qs(urlparse(self.path).query)
                        relative = (query.get("path") or [""])[0]
                        record = outer.runtime.read_result_file(bits[1], relative)
                        if query.get("download"):
                            name = Path(relative).name or "result-file"
                            return self.send_blob(record["content"], "application/octet-stream", name)
                        content = record.pop("content")
                        record["truncated"] = len(content) > 200_000
                        record["content"] = (content[:200_000].decode("utf-8", errors="replace")
                                             if record["is_text"] else None)
                        return self.send(200, record)
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
                    if len(bits) == 3 and bits[0] == "runs" and bits[2] in {"pause", "resume", "stop", "approve", "reject", "retry"}:
                        return self.send(200, outer.control(bits[1], bits[2]))
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
        claude = preflight(self.runtime.builders.get("claude"), "Claude builder not configured")
        codex_review = preflight(self.runtime.reviewers.get("codex"), "Codex reviewer not configured")
        claude_review = preflight(self.runtime.reviewers.get("claude"), "Claude reviewer not configured")
        glm_review = preflight(self.runtime.reviewers.get("glm"), "GLM reviewer not configured")
        glm.update({
            "reviewer_available": bool(glm_review["available"]),
            "reviewer_reason": glm_review["reason"],
            "reviewer_model": glm_review["model"],
            "reviewer_executable": glm_review["executable"],
            "reviewer_last_error": glm_review["last_error"],
        })
        codex.update({
            "reviewer_available": bool(codex_review["available"]),
            "reviewer_reason": codex_review["reason"],
            "reviewer_model": codex_review["model"],
            "reviewer_executable": codex_review["executable"],
            "reviewer_last_error": codex_review["last_error"],
        })
        claude.update({
            "reviewer_available": bool(claude_review["available"]),
            "reviewer_reason": claude_review["reason"],
            "reviewer_model": claude_review["model"],
            "reviewer_executable": claude_review["executable"],
            "reviewer_last_error": claude_review["last_error"],
        })
        codex["quota_warning"] = self._codex_quota_warning()
        return {"glm": glm, "codex": codex, "claude": claude,
                "workspace_lock": {"active_count": len(self._active_workspaces)}}

    def _codex_quota_warning(self):
        """Latest observed Codex quota evidence, whatever the run's terminal state."""
        for summary in self.runtime.list_runs(limit=10):
            if summary.get("status") not in {"blocked", "failed", "stopped"}:
                continue
            try:
                trace = self.runtime.quota_trace(summary["run_id"])
            except Exception:
                continue
            if trace.get("quota_blocked"):
                message = "Quota fournisseur épuisé (Codex usage limit)"
                if trace.get("reset_hint"):
                    message += f" — reset annoncé: {trace['reset_hint']}"
                return {"at": summary.get("updated_at"),
                        "run_id": summary.get("run_id"), "message": message}
        return None

    # Actions applicable per persisted state; everything else must be refused.
    APPLICABLE = {
        "pending": {"pause", "stop"}, "planning": {"pause", "stop"},
        "ready": {"pause", "stop"}, "building": {"pause", "stop"},
        "testing": {"pause", "stop"}, "reviewing": {"pause", "stop"},
        "needs_approval": {"approve", "reject", "stop"},
        "paused": {"resume", "stop"}, "blocked": {"retry", "reject"},
        "failed": {"retry", "reject"}, "correcting": {"stop"},
        "accepted": set(), "stopped": set(),
    }

    def control(self, run_id, action):
        """Apply a UI control and always answer with explicit accepted/refused feedback."""
        before = self.runtime.get(run_id)
        allowed = self.APPLICABLE.get(before["status"], set())
        if action not in allowed:
            applicable = ", ".join(sorted(allowed)) or "aucune"
            return {"action": action, "accepted": False, "status": before["status"],
                    "reason": f"action non applicable à l'état {before['status']} (possibles: {applicable})"}
        try:
            if action == "retry":
                if before.get("corrections_used", 0) >= before.get("max_corrections", 1):
                    return {"action": action, "accepted": False, "status": before["status"],
                            "reason": "budget de réparation épuisé — utilise Reject pour terminer ce run"}
                self.drive(run_id)
                after = self.runtime.get(run_id)
                return {"action": action, "accepted": True, "status": after["status"],
                        "reason": "réparation bornée relancée"}
            state = getattr(self.runtime, action)(run_id)
            if action == "resume":
                # Judge the feedback on resume()'s own transition; the driver
                # relaunched below may already have moved the run further.
                self.resume_drive(run_id)
        except RuntimeStateError as exc:
            current = self.runtime.get(run_id)
            return {"action": action, "accepted": False, "status": current["status"],
                    "reason": str(exc)}
        queued = state.get("control_request") == action
        # Accept only an outcome that matches the action's intent, never a
        # coincidental state change that happened mid-flight.
        intent = {"pause": {"paused"}, "stop": {"stopped"}, "reject": {"stopped"},
                  "approve": {"accepted"},
                  "resume": {"ready", "building", "testing", "reviewing", "needs_approval"}}
        if action in intent and not queued and state["status"] not in intent[action]:
            return {"action": action, "accepted": False, "status": state["status"],
                    "reason": f"aucun effet: l'état est {state['status']}, pas "
                              f"{'/'.join(sorted(intent[action]))}"}
        reason = ("demande enregistrée; appliquée au prochain point sûr" if queued
                  else f"état: {before['status']} → {state['status']}")
        return {"action": action, "accepted": True, "status": state["status"], "reason": reason}

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

        # Any duplicate-free combination of known reviewers is a valid mode;
        # names are canonicalized so glm_and_claude and claude_and_glm agree.
        if builder not in {"glm", "codex", "claude"}:
            raise ValueError("unknown builder or review mode")
        legacy_modes = {"codex_claude": "codex_and_claude"}
        mode = legacy_modes.get(mode, mode)
        parts = [] if mode == "none" else mode.split("_and_")
        known = ["codex", "claude", "glm"]
        if len(set(parts)) != len(parts) or any(name not in known for name in parts):
            raise ValueError("unknown builder or review mode")
        reviewer_names = [name for name in known if name in parts]
        review_policy = "_and_".join(reviewer_names) or "none"

        capabilities = self.capabilities()

        # Check builder availability
        if not capabilities[builder]["available"]:
            raise ValueError(f"Selected builder is unavailable: {capabilities[builder]['reason']}")

        # Check reviewer availability (skip for no-review)
        for name in reviewer_names:
            reviewer_available = capabilities[name].get("reviewer_available", capabilities[name]["available"])
            if not reviewer_available:
                prefix = "Selected Claude reviewer is unavailable" if name == "claude" else "Selected reviewer is unavailable"
                raise ValueError(f"{prefix}: {capabilities[name]['reason']}")

        # Check builder configuration
        if builder not in self.runtime.builders:
            raise ValueError("Selected builder is not configured: " + builder)

        # Check reviewer configuration (skip for no-review)
        if reviewer_names and any(name not in self.runtime.reviewers for name in reviewer_names):
            raise ValueError("Selected reviewer is not configured")

        # Detect self-review
        is_self_review = builder in reviewer_names
        return builder, reviewer_names, review_policy, is_self_review

    def quick_launch(self, data):
        mission = str(data.get("mission", "")).strip()
        if not mission:
            raise ValueError("mission cannot be empty")
        builder_name, reviewer_names, review_policy, is_self_review = self._quick_configuration(data)
        default_quick_paths = ["todo.py", "test_todo.py", "todo.json", "test_tasks.json",
                               "todo.json.tmp", "test_tasks.json.tmp", "src/", "tests/"]
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
            "during validation, and their .tmp siblings are the only permitted atomic-write "
            "staging files; remove test/runtime data before delivery.\n\n"
            "User task:\n" + mission
        )
        full_test = ["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test*.py"]
        generated = [path for path in ("todo.json", "test_tasks.json",
                                       "todo.json.tmp", "test_tasks.json.tmp") if path in allowed]
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
                current = self.runtime.get(run_id)
                if isinstance(exc, RuntimeStateError) and current["status"] in {"stopped", "accepted"}:
                    pass  # a user control landed between iterations; not a failure
                else:
                    self.runtime._event(current, "runtime_exception", error=f"{type(exc).__name__}: {exc}")
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
