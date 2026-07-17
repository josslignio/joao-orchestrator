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
:root{color-scheme:dark;
 --bg:#0b0e1a;--bg2:#1a1440;--panel:#11141c;--panel2:#161a24;
 --line:rgba(255,255,255,.07);--line2:rgba(255,255,255,.12);
 --txt:#e8eaf0;--txt2:#9aa1b5;--txt3:#5c6478;
 --green:#3ddc85;--orange:#ffb454;--red:#ff5c5c;--blue:#6ea8ff;--violet:#a78bfa;--r:14px}
*{box-sizing:border-box}
body{margin:0;color:var(--txt);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
 background:linear-gradient(160deg,#0b0e1a,#1a1440) fixed;height:100vh;display:flex;flex-direction:column;overflow:hidden}
body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.05;z-index:0;
 background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
::-webkit-scrollbar{width:8px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:4px}
@keyframes hueShift{0%{filter:hue-rotate(0deg) saturate(1.3)}50%{filter:hue-rotate(160deg) saturate(1.6)}100%{filter:hue-rotate(360deg) saturate(1.3)}}
@keyframes shine{0%{background-position:0% 50%}100%{background-position:200% 50%}}
.wordmark{font-size:21px;font-weight:800;letter-spacing:.5px;
 background:linear-gradient(110deg,#7dd3fc,#c084fc,#f0abfc,#67e8f9,#a5f3fc,#7dd3fc);
 background-size:200% auto;-webkit-background-clip:text;background-clip:text;color:transparent;
 animation:shine 6s linear infinite,hueShift 14s linear infinite;
 text-shadow:2px 0 rgba(240,171,252,.28),-2px 0 rgba(110,168,255,.28)}
.wordmark small{font-weight:600;opacity:.9}
.mascot{width:34px;height:34px;flex-shrink:0}
.topbar{position:relative;z-index:1;display:flex;align-items:center;gap:10px;padding:12px 22px;
 border-bottom:1px solid var(--line);background:rgba(13,15,21,.7);backdrop-filter:blur(12px)}
.chip{font-size:11.5px;font-weight:600;padding:4px 11px;border-radius:20px;border:1px solid}
.chip.ok{color:var(--green);border-color:rgba(61,220,133,.35);background:rgba(61,220,133,.07)}
.chip.warn{color:var(--orange);border-color:rgba(255,180,84,.4);background:rgba(255,180,84,.08)}
.chip.err{color:var(--red);border-color:rgba(255,92,92,.4);background:rgba(255,92,92,.08)}
.ok{color:var(--green)}.warn{color:var(--orange)}.err{color:var(--red)}
.main{position:relative;z-index:1;flex:1;overflow-y:auto}
.wrap{max-width:860px;margin:0 auto;padding:20px 24px 30px;display:flex;flex-direction:column;gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px}
.card h3{font-size:13px;font-weight:700;margin:0 0 11px;color:var(--txt)}
.inputbox{display:flex;align-items:flex-end;gap:10px;background:var(--panel2);border:1px solid var(--line2);
 border-radius:16px;padding:11px 13px;transition:.2s;margin-bottom:10px}
.inputbox:focus-within{border-color:rgba(110,168,255,.5);box-shadow:0 0 0 3px rgba(110,168,255,.12)}
.inputbox textarea{flex:1;background:none;border:0;outline:0;color:var(--txt);font:inherit;resize:none;min-height:40px;max-height:130px}
.sendbtn{width:34px;height:34px;border-radius:10px;border:0;color:#fff;cursor:pointer;font-size:15px;flex-shrink:0;
 background:linear-gradient(120deg,#3d6ef7,#7c5cff)}
.sendbtn:disabled{opacity:.35;cursor:default}
.selectors{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.sel{display:flex;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:2px;gap:1px;align-items:center}
.sel .lbl{font-size:10px;color:var(--txt3);padding:0 4px 0 9px;text-transform:uppercase;letter-spacing:.5px}
.sel label{border:0;color:var(--txt3);font-size:11.5px;font-weight:600;padding:5px 12px;border-radius:8px;cursor:pointer}
.sel label:has(input:checked){background:rgba(110,168,255,.16);color:#cfe0ff}
.sel label:has(input:disabled){opacity:.35;cursor:default}
.sel input{display:none}
.hint{font-size:11px;color:var(--txt3);margin-top:7px;text-align:center}
.runcard{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.run-head{display:flex;align-items:center;gap:10px;padding:12px 16px;cursor:pointer;flex-wrap:wrap}
.run-head:hover{background:rgba(255,255,255,.02)}
.st{font-size:11px;font-weight:800;padding:3px 10px;border-radius:7px;letter-spacing:.4px}
.st.acc{background:rgba(61,220,133,.14);color:var(--green)}
.st.build{background:rgba(110,168,255,.14);color:var(--blue)}
.st.wait{background:rgba(255,180,84,.14);color:var(--orange)}
.st.block{background:rgba(255,92,92,.13);color:var(--red)}
.st.off{background:rgba(255,255,255,.07);color:var(--txt2)}
.lbl-rev{font-size:10.5px;font-weight:800;padding:3px 9px;border-radius:6px;letter-spacing:.4px}
.lbl-rev.ind{background:rgba(61,220,133,.1);color:var(--green)}
.lbl-rev.self{background:rgba(255,180,84,.12);color:var(--orange)}
.lbl-rev.none{background:rgba(255,92,92,.12);color:var(--red)}
.runid{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--txt2)}
.run-meta{font-size:12px;color:var(--txt3);margin-left:auto;white-space:nowrap}
.gates{display:flex;gap:4px;margin:0 6px}
.gates i{width:16px;height:5px;border-radius:3px;background:rgba(255,255,255,.09)}
.gates i.done{background:var(--green)}
.run-body{border-top:1px solid var(--line);padding:14px 16px}
.asked{font-size:13px;color:var(--txt2);background:rgba(110,168,255,.06);border:1px solid rgba(110,168,255,.15);
 border-radius:10px;padding:9px 12px;margin-bottom:10px}
.asked b{color:var(--txt)}
.narration{font-size:13.5px;color:var(--txt);margin-bottom:8px}
.steptime{color:var(--txt3);font-size:12px;margin-left:8px}
.eta{color:var(--violet);font-size:12px;margin-left:8px}
.deliv-h{font-size:11px;font-weight:800;letter-spacing:.7px;color:var(--txt3);text-transform:uppercase;margin:13px 0 8px}
.built{font-size:13px;color:var(--txt2);line-height:1.7}
.built b{color:var(--txt)}
.files{display:flex;flex-direction:column;gap:7px;margin-top:8px}
.file{display:flex;align-items:center;gap:11px;background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:9px 13px}
.file .fname{font-weight:600;font-size:13px}
.file .fmeta{font-size:11.5px;color:var(--txt3)}
.file .actions{margin-left:auto;display:flex;gap:6px}
.btn{border:1px solid var(--line2);background:rgba(255,255,255,.04);color:var(--txt);font:inherit;font-size:12px;
 font-weight:600;padding:6px 13px;border-radius:9px;cursor:pointer;transition:.15s}
.btn:hover{background:rgba(255,255,255,.09)}
.btn.primary{background:linear-gradient(120deg,#3d6ef7,#7c5cff);border-color:transparent}
.btn.good{background:rgba(61,220,133,.15);border-color:rgba(61,220,133,.4);color:var(--green)}
.btn.bad{background:rgba(255,92,92,.12);border-color:rgba(255,92,92,.35);color:var(--red)}
.btn.ghost{border-color:transparent;color:var(--txt2)}
.zipbar{display:flex;align-items:center;gap:10px;margin-top:10px;padding:9px 13px;border:1px dashed var(--line2);
 border-radius:11px;color:var(--txt2);font-size:12.5px}
.diff{background:#0b0e14;border:1px solid var(--line);border-radius:10px;padding:11px 14px;
 font-family:ui-monospace,Menlo,monospace;font-size:12px;line-height:1.6;overflow:auto;max-height:300px;
 margin-top:4px;white-space:pre-wrap}
.resultbox{border:1px solid rgba(61,220,133,.3);background:rgba(61,220,133,.05);border-radius:11px;padding:12px 14px;margin:10px 0}
.blockbox{border:1px solid rgba(255,92,92,.4);background:rgba(255,92,92,.07);border-radius:11px;padding:10px 13px;margin:9px 0;font-size:13px}
.blockbox .hint2{color:var(--orange);font-size:12px;margin-top:4px}
.ctrls{display:flex;gap:7px;margin-top:12px;align-items:center}
.consequence{font-size:12px;color:var(--txt3);margin-top:7px}
.feedback{min-height:15px;font-size:12px;margin-top:6px}
.timeline{margin-top:8px}
.timeline .tstep{display:flex;gap:10px;font-size:12.5px;color:var(--txt2);padding:3px 0}
.timeline .tstep .tdur{margin-left:auto;color:var(--txt3);font-variant-numeric:tabular-nums}
details{margin-top:8px}summary{cursor:pointer;color:var(--txt3);font-size:12px}
pre{white-space:pre-wrap;max-height:240px;overflow:auto;color:var(--txt2);font-size:11px}
.muted{color:var(--txt3);font-size:12px}
#quota-warning{display:none;font-size:12.5px;margin-top:8px}
</style>
<div class="topbar">
<svg class="mascot" viewBox="0 0 64 64"><circle cx="32" cy="36" r="21" fill="#f2c49b"/><path d="M11 32 Q11 12 32 12 Q53 12 53 32 L53 34 L11 34 Z" fill="#ffcf3f"/><rect x="8" y="31" width="48" height="6" rx="3" fill="#f5b91e"/><rect x="28" y="8" width="8" height="8" rx="2" fill="#ffcf3f"/><circle cx="24" cy="42" r="6.5" fill="#fff"/><circle cx="40" cy="42" r="6.5" fill="#fff"/><circle cx="24" cy="42" r="4.6" fill="#1a1208"/><circle cx="40" cy="42" r="4.6" fill="#1a1208"/><circle cx="25.5" cy="40.5" r="1.4" fill="#fff"/><circle cx="41.5" cy="40.5" r="1.4" fill="#fff"/><path d="M22 52 Q27 49 32 51.5 Q37 49 42 52 Q37 56.5 32 54.5 Q27 56.5 22 52 Z" fill="#4a2f1a"/></svg>
<div class="wordmark">JOÃO<small>.AI</small></div>
<div id="capabilities" style="display:flex;gap:8px;flex-wrap:wrap;margin-left:auto"><span class="muted">Chargement…</span></div>
</div>
<div class="main"><div class="wrap">
<div class="card">
<h3>Nouvelle mission</h3>
<div class="inputbox">
<textarea id="prompt" rows="2" autofocus placeholder="Décris la mission — JOÃO orchestre : sandbox, build, tests, review, evidence. Entrée pour lancer, Shift+Entrée pour une nouvelle ligne."></textarea>
<button id="start-btn" class="sendbtn" onclick="send()" disabled title="Lancer">➤</button>
</div>
<div class="selectors">
<div class="sel"><span class="lbl">Moteur</span>
<label><input type="radio" name="builder" value="glm" checked> GLM</label>
<label><input type="radio" name="builder" value="codex"> Codex</label>
<label><input type="radio" name="builder" value="claude"> Claude</label></div>
<div class="sel"><span class="lbl">Review</span>
<label><input type="radio" name="review" value="none"> Aucune</label>
<label><input type="radio" name="review" value="claude" checked> Claude</label>
<label><input type="radio" name="review" value="glm"> GLM</label>
<label><input type="radio" name="review" value="codex"> Codex</label>
<label><input type="radio" name="review" value="codex_and_claude"> Codex + Claude</label>
<label><input type="radio" name="review" value="claude_and_glm"> Claude + GLM</label></div>
</div>
<div id="safety-summary" class="muted" style="margin-top:8px"></div>
<div id="quota-warning" class="warn"></div>
<div id="launch-feedback" class="feedback"></div>
<div class="hint">Sandbox Git jetable · le moteur et le modèle réellement utilisés sont toujours affichés · reviews à chaque gate (plan, diff, tests, livraison)</div>
</div>
<div id="runs"><div class="muted">Aucun run pour l'instant. Les runs persistés réapparaissent ici après redémarrage.</div></div>
</div></div>
<script>
const TOKEN="__JOAO_TOKEN__";const el=id=>document.getElementById(id);
let CAPS=null,LIST=[];const cards={};
const ACTIONS={pending:["stop"],planning:["stop"],ready:["stop"],
 building:["stop"],testing:["stop"],reviewing:["stop"],
 needs_approval:["approve","reject"],paused:["resume","stop"],correcting:["stop"],
 blocked:["retry","reject"],failed:["retry","reject"],accepted:[],stopped:[]};
const STCLASS={pending:"build",planning:"build",ready:"build",building:"build",testing:"build",
 reviewing:"build",correcting:"build",needs_approval:"wait",paused:"wait",
 blocked:"block",failed:"block",accepted:"acc",stopped:"off"};
async function req(url,opt={}){opt.headers={...(opt.headers||{}),'X-JOAO-Token':TOKEN};
 const r=await fetch(url,opt),v=await r.json();if(!r.ok)throw Error(v.error||'requête refusée');return v}
function esc(s){const n=document.createElement('span');n.textContent=s==null?'':s;return n.innerHTML.replace(/"/g,'&quot;')}
function C(id){return cards[id]=cards[id]||{open:false,userClosed:false}}
function selected(name){return document.querySelector('input[name="'+name+'"]:checked').value}
function reviewParts(r){return r==='none'?[]:r.split('_and_')}
function fmt(s){if(s==null)return '';s=Math.max(0,Math.round(s));const m=Math.floor(s/60);
 return m?m+'′'+String(s%60).padStart(2,'0')+'″':s+'″'}
function labels(v){let out='';if(v.no_review_label||v.review_policy==='none')out+='<span class="lbl-rev none">NO REVIEW — APPROBATION HUMAINE</span>';
 else if(v.is_self_review)out+='<span class="lbl-rev self">SELF-REVIEW — NON INDÉPENDANTE</span>';
 else out+='<span class="lbl-rev ind">REVIEW INDÉPENDANTE</span>';return out}
function gates(v){const done=(v.progress?v.progress.completed:0);let h='<div class="gates">';
 for(let i=0;i<4;i++)h+='<i class="'+(i<done?'done':'')+'"></i>';return h+'</div>'}
function buttons(v){const acts=ACTIONS[v.status]||[];
 const style={approve:'good',reject:'bad',stop:'bad',retry:''};
 return acts.map(a=>'<button class="btn '+(style[a]||'')+'" data-run="'+esc(v.run_id)+'" data-act="'+a+'">'
  +({approve:'Approve',reject:'Reject',stop:'Stop',retry:'Retry',resume:'Resume'}[a]||a)+'</button>').join('')}
function narration(d){if(!d)return '';
 let h='<div class="narration">'+esc(d.narration||d.current_step||'')
  +'<span class="steptime">étape '+fmt(d.step_elapsed_seconds)+' · total '+fmt(d.elapsed_seconds)+'</span>';
 if(d.eta&&d.eta.eta_seconds!=null){const rest=d.eta.eta_seconds-d.elapsed_seconds;
  h+='<span class="eta">'+(rest>15?'résultat estimé dans ~'+fmt(rest):'résultat imminent')
   +' (basé sur '+d.eta.based_on_runs+' run'+(d.eta.based_on_runs>1?'s':'')+')</span>'}
 return h+'</div>'}
function resultBlock(v){const c=C(v.run_id);
 if(!['needs_approval','accepted'].includes(v.status))return '';
 if(!v.result_available)return '<div class="blockbox">Aucun résultat enregistré pour ce run — le diff final est absent de l\\'évidence.</div>';
 if(c.sumError)return '<div class="blockbox">Résultat indisponible : '+esc(c.sumError)+' — réessaie ou consulte le dossier d\\'évidence.</div>';
 const s=c.sum;if(!s)return '<div class="resultbox muted">Chargement du résultat…</div>';
 const delivered=s.files.filter(f=>f.exists);
 let h='<div class="resultbox"><div class="deliv-h" style="margin-top:0">Ce qui a été construit</div>';
 h+='<div class="built"><b>'+delivered.length+' fichier'+(delivered.length>1?'s':'')+' livré'+(delivered.length>1?'s':'')+'</b>';
 if(s.tests&&s.tests.commands!=null)h+=' · <b class="'+(s.tests.all_passed?'ok':'err')+'">'+s.tests.passed+'/'+s.tests.commands+' tests verts</b>';
 else if(s.tests&&s.tests.error)h+=' · <span class="warn">résultats de tests illisibles</span>';
 else h+=' · <span class="warn">tests non exécutés</span>';
 h+=' · builder <b>'+esc(s.builder_provider||s.builder_name)+'</b>';
 if(s.review_policy&&s.review_policy!=='none')h+=' · review <b>'+esc(s.review_policy)+'</b>';
 h+='</div>';
 h+='<div class="deliv-h">Fichiers livrés (servis depuis l\\'évidence)</div><div class="files">';
 for(const f of s.files){h+='<div class="file"><div><div class="fname">'+esc(f.path)+'</div>'
  +'<div class="fmeta">'+(f.exists?f.bytes+' o · '+(f.is_text?'texte':'binaire'):'<span class="err">introuvable</span>')+'</div></div>'
  +'<div class="actions">'+(f.exists&&f.is_text?'<button class="btn ghost" data-res="preview" data-run="'+esc(v.run_id)+'" data-path="'+esc(f.path)+'">Aperçu</button>':'')
  +(f.exists?'<button class="btn" data-res="download" data-run="'+esc(v.run_id)+'" data-path="'+esc(f.path)+'">⬇︎</button>':'')+'</div></div>'}
 h+='</div>';
 if(c.preview!=null)h+='<div class="muted" style="margin-top:8px">aperçu: '+esc(c.preview)+'</div><pre>'+esc(c.previewData==null?'chargement…':c.previewData)+'</pre>';
 h+='<div class="zipbar">📦 Bundle complet (livrables + diff + résumé) <button class="btn primary" style="margin-left:auto" data-res="zip" data-run="'+esc(v.run_id)+'">Télécharger tout (zip)</button></div>';
 h+='<div class="deliv-h">Aperçu du diff <button class="btn ghost" data-res="diff" data-run="'+esc(v.run_id)+'">'+(c.diffOpen===false?'Afficher':'Masquer')+'</button></div>';
 if(c.diffOpen!==false)h+='<div class="diff">'+esc(c.diff==null?'chargement…':c.diff)+'</div>';
 return h+'</div>'}
function timelineBlock(v){const c=C(v.run_id);
 let h='<details'+(c.tlOpen?' open':'')+' data-tl="'+esc(v.run_id)+'"><summary>Ce qui s\\'est passé</summary><div class="timeline">';
 if(!c.timeline)h+='<div class="muted">chargement…</div>';
 else for(const s of c.timeline)h+='<div class="tstep"><span>'+esc(s.label)+'</span>'
  +'<span class="tdur">'+(s.duration_seconds!=null?fmt(s.duration_seconds):'')+'</span></div>';
 return h+'</div></details>'}
function card(s){const c=C(s.run_id);const d=c.detail;const v=d||s;
 let h='<div class="runcard">';
 h+='<div class="run-head" data-toggle="'+esc(s.run_id)+'">'
  +'<span class="st '+(STCLASS[v.status]||'off')+'">'+esc(v.status)+'</span>'
  +'<span class="runid">'+esc(s.run_id.slice(-12))+'</span>'+labels(v)
  +(d?gates(d):'')
  +'<span class="run-meta">'+esc(v.builder_name||'?')+' · review '+esc(v.review_policy||'none')
  +(d?' · '+fmt(d.elapsed_seconds):'')+'</span></div>';
 if(!c.open)return h+'</div>';
 h+='<div class="run-body">';
 h+='<div class="asked"><b>Tu as demandé :</b> '+esc(s.mission_excerpt||(d&&d.mission_display)||'')+'</div>';
 h+=narration(d);
 if(d&&d.block_cause){h+='<div class="blockbox">Cause: '+esc(d.block_cause)
  +'<div class="hint2">Déblocage: '+esc(d.unblock_hint)+'</div></div>'}
 h+=resultBlock(v);
 h+='<div class="ctrls">'+buttons(v)+'</div>';
 if(v.status==='needs_approval')h+='<div class="consequence">Approve : résultat conservé, run archivé accepté · Reject : résultat écarté, run archivé rejeté</div>';
 h+='<div class="feedback '+((c.fb||{}).ok?'ok':'err')+'">'+esc((c.fb||{}).text||'')+'</div>';
 h+=timelineBlock(v);
 if(d)h+='<details><summary>Évidence JSON (replié)</summary><pre>'+esc(JSON.stringify(d,null,2))+'</pre></details>'
  +'<div class="muted">Evidence: '+esc(d.evidence_directory||'')+'</div>';
 return h+'</div></div>'}
function render(){if(!LIST.length)return;
 el('runs').innerHTML=LIST.map(card).join('')}
async function hydrate(s){const c=C(s.run_id);
 const active=!['accepted','stopped','blocked','failed'].includes(s.status);
 if(s.status==='needs_approval'&&!c.userClosed&&!c.open){c.open=true}
 if(!(active||c.open))return;
 try{c.detail=await req('/runs/'+s.run_id)}catch(_){return}
 const d=c.detail;
 if(['needs_approval','accepted'].includes(d.status)&&d.result_available){
  if(!c.sum||c.sumStatus!==d.status){
   try{c.sum=await req('/runs/'+s.run_id+'/result');c.sumStatus=d.status;c.sumError=null}
   catch(e){c.sumError=e.message}}
  if(c.diff==null&&!c.diffLoading&&c.diffOpen!==false){c.diffLoading=true;
   req('/runs/'+s.run_id+'/final-diff').then(x=>{c.diff=x.diff_content||x.error;render()})
    .catch(()=>{c.diff='(diff indisponible)';render()})}}
 if(c.tlOpen&&(!c.timeline||active)){
  try{c.timeline=(await req('/runs/'+s.run_id+'/timeline')).steps}catch(_){}}}
async function refresh(){try{const v=await req('/runs');LIST=v.runs||[];
 await Promise.all(LIST.slice(0,12).map(hydrate));render()}catch(_){}}
document.addEventListener('click',async e=>{const t=e.target;
 if(t.closest&&!t.dataset.act&&!t.dataset.res){const head=t.closest('.run-head');
  if(head){const c=C(head.dataset.toggle);c.open=!c.open;c.userClosed=!c.open;render();
   if(c.open)refresh();return}
  const tl=t.closest('details[data-tl]');
  if(tl&&t.tagName==='SUMMARY'){const c=C(tl.dataset.tl);c.tlOpen=!tl.open;
   if(c.tlOpen&&!c.timeline)req('/runs/'+tl.dataset.tl+'/timeline').then(x=>{c.timeline=x.steps;render()}).catch(()=>{});
   return}}
 if(t.dataset&&t.dataset.res){resultAction(t.dataset.res,t.dataset.run,t.dataset.path);return}
 if(!(t.dataset&&t.dataset.act))return;
 t.disabled=true;
 try{const r=await req('/runs/'+t.dataset.run+'/'+t.dataset.act,{method:'POST'});
  C(t.dataset.run).fb={ok:r.accepted,text:(r.accepted?'✓ '+t.dataset.act+' acceptée — ':'✗ '+t.dataset.act+' refusée — ')+r.reason};
 }catch(err){C(t.dataset.run).fb={ok:false,text:'✗ '+t.dataset.act+' — '+err.message}}
 refresh()});
async function download(url,filename){const r=await fetch(url,{headers:{'X-JOAO-Token':TOKEN}});
 if(!r.ok)throw Error('téléchargement refusé ('+r.status+')');
 const blob=await r.blob();const a=document.createElement('a');
 a.href=URL.createObjectURL(blob);a.download=filename;a.click();
 setTimeout(()=>URL.revokeObjectURL(a.href),10000)}
async function resultAction(kind,run,path){const c=C(run);
 try{
  if(kind==='diff'){c.diffOpen=c.diffOpen===false?true:false;
   if(c.diffOpen!==false&&c.diff==null){const d=await req('/runs/'+run+'/final-diff');c.diff=d.diff_content||d.error}}
  else if(kind==='zip'){await download('/runs/'+run+'/result/zip',run+'-result.zip')}
  else if(kind==='preview'){c.preview=path;c.previewData=null;render();
   const d=await req('/runs/'+run+'/result/file?path='+encodeURIComponent(path));
   c.previewData=d.content==null?'(fichier binaire — utilise télécharger)':d.content}
  else if(kind==='download'){await download('/runs/'+run+'/result/file?path='+encodeURIComponent(path)+'&download=1',path.split('/').pop())}
 }catch(err){c.fb={ok:false,text:'✗ résultat — '+err.message}}
 render()}
async function send(){const mission=el('prompt').value.trim();if(!mission)return;
 el('launch-feedback').textContent='';
 try{const v=await req('/quick-missions',{method:'POST',body:JSON.stringify({mission,builder_name:selected('builder'),review_mode:selected('review')})});
  C(v.run_id).open=true;el('prompt').value='';
  el('launch-feedback').innerHTML='<span class="ok">✓ run lancé: '+esc(v.run_id)+'</span>';refresh()}
 catch(e){el('launch-feedback').innerHTML='<span class="err">✗ lancement refusé — '+esc(e.message)+'</span>'}}
el('prompt').addEventListener('keydown',e=>{
 if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(!el('start-btn').disabled)send()}});
function enableChoice(name,value,enabled){const input=document.querySelector('input[name="'+name+'"][value="'+value+'"]');
 input.disabled=!enabled}
function ensureChoice(name){const current=document.querySelector('input[name="'+name+'"]:checked');
 if(!current||current.disabled){const fallback=document.querySelector('input[name="'+name+'"]:not(:disabled)');if(fallback)fallback.checked=true}}
function quotaDoomed(){if(!CAPS||!CAPS.codex.quota_warning)return false;
 const b=selected('builder'),r=selected('review');return b==='codex'||reviewParts(r).includes('codex')}
function updateSafety(){if(!CAPS)return;const v=CAPS;const parts=[];
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
  +(v.codex.quota_warning?'<span class="chip warn">Codex — quota · reset annoncé</span>':'');
 updateSafety()}catch(_){el('start-btn').disabled=true}}
document.querySelectorAll('input[name="builder"],input[name="review"]').forEach(i=>i.addEventListener('change',updateSafety));
el('prompt').addEventListener('input',updateSafety);
setInterval(refresh,2500);setInterval(caps,30000);caps();refresh();
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

            def host_ok(self):
                # The token page is served on GET /; with a fixed port a DNS
                # rebinding page could otherwise read it. Loopback hosts only.
                host = self.headers.get("Host", "")
                port = outer.server.server_port
                return host in {f"127.0.0.1:{port}", f"localhost:{port}",
                                f"[::1]:{port}", "127.0.0.1", "localhost", "[::1]"}

            def do_GET(self):
                path = urlparse(self.path).path
                bits = path.strip("/").split("/")
                try:
                    if not self.host_ok():
                        return self.send(403, {"error": "invalid Host header"})
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
                    if not self.host_ok():
                        return self.send(403, {"error": "invalid Host header"})
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
