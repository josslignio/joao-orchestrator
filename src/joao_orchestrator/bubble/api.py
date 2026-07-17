"""Local-only JOAO chat console, backed by the bounded runtime."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..domain.models import ProjectProfile
from .runtime import CODE_INTENT_RE, RunRuntime, RuntimeStateError, mission_allowed_paths


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ASSET_ROUTES = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/assets/mascot.png": ("mascot-neon.png", "image/png"),
    "/assets/icon-1024.png": ("icon-1024.png", "image/png"),
}


def safe_disposition_filename(name: str) -> str:
    """Builder-controlled names must never reach raw HTTP headers.

    Keeps printable ASCII minus the quote/backslash; anything else (CR/LF
    header injection, non-latin-1 crash material) becomes an underscore.
    """
    cleaned = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name)
    return cleaned.strip() or "download"


HTML = """<!doctype html>
<meta charset="utf-8"><title>JOÃO.AI</title>
<link rel="icon" href="/favicon.ico">
<style>
:root{color-scheme:dark;
 --bg:#0b0e1a;--bg2:#1a1440;--panel:#11141c;--panel2:#161a24;--panel3:#1b2030;
 --line:rgba(255,255,255,.07);--line2:rgba(255,255,255,.12);
 --txt:#e8eaf0;--txt2:#9aa1b5;--txt3:#5c6478;
 --green:#3ddc85;--orange:#ffb454;--red:#ff5c5c;--blue:#6ea8ff;--violet:#a78bfa;--r:14px}
*{box-sizing:border-box}
body{margin:0;color:var(--txt);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
 background:linear-gradient(160deg,#0b0e1a,#1a1440) fixed;height:100vh;overflow:hidden}
body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.05;z-index:0;
 background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:5px}
@keyframes flowS{0%{background-position:0% 50%}100%{background-position:300% 50%}}
.wm{position:relative;font-weight:800;font-size:20px;letter-spacing:.3px;white-space:nowrap}
.wm small{font-size:.62em;font-weight:600;opacity:.9}
.a1{background:linear-gradient(100deg,#ff6ec7,#ffd36e,#6effb0,#6ec3ff,#c96eff,#ff6ec7);background-size:300% auto;
 -webkit-background-clip:text;background-clip:text;color:transparent;animation:flowS 9s linear infinite;filter:saturate(1.35)}
.bloomD{position:absolute;inset:0;z-index:-1;background:linear-gradient(100deg,#ff6ec7,#ffd36e,#6effb0,#6ec3ff,#c96eff,#ff6ec7);
 background-size:300% auto;-webkit-background-clip:text;background-clip:text;color:transparent;animation:flowS 9s linear infinite;filter:blur(5px);opacity:.5}
.app{display:grid;grid-template-columns:250px 1fr;grid-template-rows:auto 1fr;height:100vh;position:relative;z-index:1}
.header{grid-column:1/3;display:flex;align-items:center;gap:14px;padding:8px 22px;border-bottom:1px solid var(--line);
 background:rgba(13,15,26,.72);backdrop-filter:blur(10px);min-height:56px;overflow:visible}
.header .mascot{position:absolute;left:50%;transform:translateX(-50%);height:96px;filter:drop-shadow(0 0 10px rgba(201,110,255,.45));pointer-events:none}
.chips{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.chip{font-size:11.5px;font-weight:600;padding:4px 11px;border-radius:20px;border:1px solid var(--line2)}
.chip.ok{color:var(--green);border-color:rgba(61,220,133,.35);background:rgba(61,220,133,.07)}
.chip.err{color:var(--red);border-color:rgba(255,92,92,.4);background:rgba(255,92,92,.08)}
.chip.warn{color:var(--orange);border-color:rgba(255,180,84,.4);background:rgba(255,180,84,.08)}
.sidebar{border-right:1px solid var(--line);display:flex;flex-direction:column;padding:12px 10px;gap:8px;overflow:hidden;background:rgba(13,15,26,.4)}
.newbtn{display:flex;align-items:center;gap:8px;justify-content:center;padding:9px;border-radius:10px;cursor:pointer;
 background:linear-gradient(120deg,#3d6ef7,#7c5cff);color:#fff;border:0;font:inherit;font-weight:650}
#search{background:var(--panel2);border:1px solid var(--line);border-radius:9px;color:var(--txt);padding:7px 10px;font:inherit;font-size:13px}
.convs{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:2px}
.conv{padding:8px 10px;border-radius:9px;color:var(--txt2);cursor:pointer;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conv:hover{background:rgba(255,255,255,.04);color:var(--txt)}
.conv.active{background:rgba(110,168,255,.13);color:#cfe0ff}
.side-h{font-size:11px;font-weight:700;letter-spacing:.6px;color:var(--txt3);padding:2px 10px;text-transform:uppercase}
.main{display:flex;flex-direction:column;overflow:hidden;min-width:0}
.feed{flex:1;overflow-y:auto;padding:20px 26px;display:flex;flex-direction:column;gap:16px}
.empty{margin:auto;color:var(--txt3);text-align:center;font-size:14px}
.msg-user{align-self:flex-end;max-width:78%;background:#1a2130;border:1px solid var(--line2);border-radius:14px 14px 4px 14px;padding:11px 15px}
.msg-user .who{font-size:11px;color:var(--txt3);font-weight:600;margin-bottom:3px}
.msg-user .txt{white-space:pre-wrap;word-break:break-word}
.msg-user .att{display:inline-flex;gap:6px;align-items:center;margin-top:7px;margin-right:6px;font-size:12px;
 background:rgba(255,255,255,.05);border:1px solid var(--line2);border-radius:8px;padding:3px 9px;color:var(--txt2)}
.runcard{align-self:flex-start;max-width:86%;width:640px;max-width:min(86%,640px);background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.run-head{display:flex;align-items:center;gap:9px;padding:11px 15px;flex-wrap:wrap}
.st{font-size:11px;font-weight:800;padding:3px 10px;border-radius:7px;letter-spacing:.3px}
.st.run{background:rgba(110,168,255,.14);color:var(--blue)}.st.done{background:rgba(61,220,133,.14);color:var(--green)}
.st.fail{background:rgba(255,92,92,.14);color:var(--red)}.st.off{background:rgba(255,255,255,.07);color:var(--txt2)}
.st.queued{background:rgba(255,180,84,.14);color:var(--orange)}
.lbl-rev{font-size:10px;font-weight:800;padding:3px 8px;border-radius:6px;letter-spacing:.3px}
.lbl-rev.ind{background:rgba(61,220,133,.1);color:var(--green)}.lbl-rev.self{background:rgba(255,180,84,.12);color:var(--orange)}
.lbl-rev.none{background:rgba(255,92,92,.12);color:var(--red)}
.run-meta{font-size:12px;color:var(--txt3);margin-left:auto;white-space:nowrap}
.gates{display:flex;gap:4px;margin:0 4px}.gates i{width:15px;height:5px;border-radius:3px;background:rgba(255,255,255,.09)}.gates i.done{background:var(--green)}
.run-body{border-top:1px solid var(--line);padding:12px 15px}
.narr{font-size:13.5px;color:var(--txt);margin-bottom:6px}
.steptime{color:var(--txt3);font-size:12px;margin-left:6px}.eta{color:var(--violet);font-size:12px;margin-left:6px}
.blockbox{border:1px solid rgba(255,92,92,.4);background:rgba(255,92,92,.07);border-radius:10px;padding:9px 12px;margin:8px 0;font-size:13px}
.blockbox .hint2{color:var(--orange);font-size:12px;margin-top:4px}
.resultbox{border:1px solid rgba(61,220,133,.3);background:rgba(61,220,133,.05);border-radius:11px;padding:11px 13px;margin:9px 0}
.deliv-h{font-size:11px;font-weight:800;letter-spacing:.6px;color:var(--txt3);text-transform:uppercase;margin:11px 0 7px}
.deliv-h:first-child{margin-top:0}
.built{font-size:13px;color:var(--txt2);line-height:1.7}.built b{color:var(--txt)}
.files{display:flex;flex-direction:column;gap:6px;margin-top:7px}
.file{display:flex;align-items:center;gap:10px;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:8px 12px;cursor:pointer}
.file:hover{border-color:var(--line2)}
.file .fname{font-weight:600;font-size:13px}.file .fmeta{font-size:11.5px;color:var(--txt3)}
.file .actions{margin-left:auto;display:flex;gap:6px}
.btn{border:1px solid var(--line2);background:rgba(255,255,255,.04);color:var(--txt);font:inherit;font-size:12px;font-weight:600;padding:5px 12px;border-radius:9px;cursor:pointer}
.btn:hover{background:rgba(255,255,255,.09)}.btn.primary{background:linear-gradient(120deg,#3d6ef7,#7c5cff);border-color:transparent}
.btn.ghost{border-color:transparent;color:var(--txt2)}
.zipbar{display:flex;align-items:center;gap:9px;margin-top:9px;padding:8px 12px;border:1px dashed var(--line2);border-radius:10px;color:var(--txt2);font-size:12.5px}
.feedbk{display:flex;gap:8px;align-items:center;margin-top:9px;font-size:12px;color:var(--txt3)}
.fb-btn{cursor:pointer;padding:3px 9px;border-radius:8px;border:1px solid var(--line);background:rgba(255,255,255,.03)}
.fb-btn:hover{background:rgba(255,255,255,.08)}
.ok{color:var(--green)}.warn{color:var(--orange)}.err{color:var(--red)}.muted{color:var(--txt3);font-size:12px}
details{margin-top:7px}summary{cursor:pointer;color:var(--txt3);font-size:12px}
pre{white-space:pre-wrap;max-height:220px;overflow:auto;color:var(--txt2);font-size:11px}
.composer-zone{border-top:1px solid var(--line);background:rgba(13,15,26,.85);backdrop-filter:blur(12px);padding:12px 24px 14px}
.composer{max-width:820px;margin:0 auto}
.attachrow{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:7px}
.attchip{display:inline-flex;align-items:center;gap:7px;font-size:12px;background:var(--panel2);border:1px solid var(--line2);border-radius:9px;padding:4px 10px;color:var(--txt2)}
.attchip .x{cursor:pointer;color:var(--txt3);font-weight:700}.attchip .x:hover{color:var(--red)}
.inputbox{display:flex;align-items:flex-end;gap:10px;background:var(--panel2);border:1px solid var(--line2);border-radius:16px;padding:10px 12px;transition:.15s}
.inputbox.drag{border-color:var(--violet);box-shadow:0 0 0 3px rgba(167,139,250,.18)}
.inputbox:focus-within{border-color:rgba(110,168,255,.5);box-shadow:0 0 0 3px rgba(110,168,255,.12)}
.inputbox textarea{flex:1;background:none;border:0;outline:0;color:var(--txt);font:inherit;resize:none;min-height:24px;max-height:180px}
.icobtn{width:34px;height:34px;border-radius:10px;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--txt2);cursor:pointer;font-size:16px;flex-shrink:0}
.icobtn:hover{background:rgba(255,255,255,.08);color:var(--txt)}
.sendbtn{width:34px;height:34px;border-radius:10px;border:0;color:#fff;cursor:pointer;font-size:15px;flex-shrink:0;background:linear-gradient(120deg,#3d6ef7,#7c5cff)}
.sendbtn:disabled{opacity:.35;cursor:default}
.selectors{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
.sel{display:flex;background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:2px;align-items:center}
.sel .lbl{font-size:10px;color:var(--txt3);padding:0 4px 0 9px;text-transform:uppercase;letter-spacing:.4px}
.sel label{color:var(--txt3);font-size:11.5px;font-weight:600;padding:4px 11px;border-radius:7px;cursor:pointer}
.sel label:has(input:checked){background:rgba(110,168,255,.16);color:#cfe0ff}
.sel label:has(input:disabled){opacity:.35;cursor:default}.sel input{display:none}
.hint{font-size:11px;color:var(--txt3);margin-top:6px;text-align:center}
#quota-warning{display:none;color:var(--orange);font-size:12.5px;margin-top:6px;text-align:center}
.artifacts{position:fixed;top:0;right:0;height:100vh;width:min(46vw,620px);background:var(--panel);border-left:1px solid var(--line2);
 z-index:20;display:none;flex-direction:column;box-shadow:-16px 0 40px rgba(0,0,0,.4)}
.artifacts.open{display:flex}
.art-head{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line)}
.art-head .aname{font-weight:650;font-size:13px}
.art-body{flex:1;overflow:auto;padding:0}
.art-body pre{margin:0;padding:16px;max-height:none;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:#cdd6e6;line-height:1.65}
.art-body .md{padding:20px;line-height:1.7}.art-body .md h1,.art-body .md h2{border-bottom:1px solid var(--line);padding-bottom:5px}
.art-body .md code{background:rgba(255,255,255,.08);padding:2px 5px;border-radius:5px;font-family:ui-monospace,monospace;font-size:.9em}
.art-body iframe{width:100%;height:100%;border:0;background:#fff}
.art-body img{max-width:100%;display:block;margin:16px auto}
</style>
<div class="app">
<div class="header">
<span class="wm"><span class="a1">JOÃO<small>.AI</small></span><span class="bloomD">JOÃO<small>.AI</small></span></span>
<img class="mascot" src="/assets/mascot.png" alt="JOÃO">
<div class="chips" id="capabilities"><span class="muted">Chargement…</span></div>
</div>
<aside class="sidebar">
<button class="newbtn" id="new-conv">+ Nouvelle conversation</button>
<input id="search" placeholder="Rechercher…" autocomplete="off">
<div class="side-h">Conversations</div>
<div class="convs" id="convs"></div>
</aside>
<main class="main">
<div class="feed" id="feed"><div class="empty">Écris une demande en bas pour lancer une mission.<br>JOÃO construit, teste, review, et te montre le résultat directement.</div></div>
<div class="composer-zone"><div class="composer">
<div class="attachrow" id="attachrow"></div>
<div class="inputbox" id="inputbox">
<button class="icobtn" id="attach-btn" title="Joindre des fichiers">📎</button>
<textarea id="prompt" rows="1" placeholder="Décris ta mission — code + tests, ou un texte. Entrée pour envoyer, Maj+Entrée pour un retour ligne."></textarea>
<input type="file" id="file-input" multiple hidden>
<button class="sendbtn" id="send-btn" title="Envoyer" disabled>➤</button>
</div>
<div class="selectors">
<div class="sel"><span class="lbl">Moteur</span>
<label><input type="radio" name="builder" value="glm" checked> GLM</label>
<label><input type="radio" name="builder" value="claude"> Claude</label>
<label><input type="radio" name="builder" value="codex"> Codex</label></div>
<div class="sel"><span class="lbl">Review</span>
<label><input type="radio" name="review" value="none"> Aucune</label>
<label><input type="radio" name="review" value="claude" checked> Claude</label>
<label><input type="radio" name="review" value="glm"> GLM</label>
<label><input type="radio" name="review" value="codex"> Codex</label>
<label><input type="radio" name="review" value="claude_and_codex"> Claude+Codex</label></div>
</div>
<div id="quota-warning"></div>
<div class="hint">Sandbox Git jetable · moteur & modèle réels toujours affichés · review indépendante à chaque gate · évidence signée archivée</div>
</div></div>
</main>
</div>
<div class="artifacts" id="artifacts">
<div class="art-head"><span class="aname" id="art-name"></span>
<button class="btn ghost" id="art-copy" style="margin-left:auto">Copier</button>
<button class="btn ghost" id="art-dl">Télécharger</button>
<button class="btn ghost" id="art-close">✕</button></div>
<div class="art-body" id="art-body"></div>
</div>
<script>
const TOKEN="__JOAO_TOKEN__";const el=id=>document.getElementById(id);
let CAPS=null,LIST=[],pending=[];const cards={};let artifact=null;
// Conversations: localStorage grouping of run_ids into named threads.
function loadConvs(){try{return JSON.parse(localStorage.getItem('joao_convs')||'[]')}catch(_){return[]}}
function saveConvs(c){localStorage.setItem('joao_convs',JSON.stringify(c))}
let CONVS=loadConvs();let activeConv=localStorage.getItem('joao_active')||null;
if(!CONVS.length){newConversation()}
if(!activeConv||!CONVS.find(c=>c.id===activeConv))activeConv=CONVS[0].id;
function newConversation(){const id='c'+Date.now().toString(36);CONVS.unshift({id,title:'Nouvelle conversation',runs:[]});activeConv=id;saveConvs(CONVS);localStorage.setItem('joao_active',id);return id}
function activeConvObj(){return CONVS.find(c=>c.id===activeConv)}
async function req(url,opt={}){opt.headers={...(opt.headers||{}),'X-JOAO-Token':TOKEN};
 const r=await fetch(url,opt),v=await r.json();if(!r.ok)throw Error(v.error||'requête refusée');return v}
function esc(s){const n=document.createElement('span');n.textContent=s==null?'':s;return n.innerHTML.replace(/"/g,'&quot;')}
function selected(name){return document.querySelector('input[name="'+name+'"]:checked').value}
function reviewParts(r){return r==='none'?[]:r.split('_and_')}
function fmt(s){if(s==null)return '';s=Math.max(0,Math.round(s));const m=Math.floor(s/60);return m?m+'′'+String(s%60).padStart(2,'0')+'″':s+'″'}
function C(id){return cards[id]=cards[id]||{open:{}}}
// ---------- sidebar ----------
function renderConvs(){const q=(el('search').value||'').toLowerCase();
 el('convs').innerHTML=CONVS.filter(c=>!q||c.title.toLowerCase().includes(q)).map(c=>
  '<div class="conv'+(c.id===activeConv?' active':'')+'" data-conv="'+esc(c.id)+'">'+esc(c.title||'Sans titre')+'</div>').join('')}
// ---------- feed ----------
function stClass(v){const p=v.phase_label||'';if(p.startsWith('running'))return 'run';if(p==='done')return 'done';
 if(p==='failed')return 'fail';if(p==='queued')return 'queued';return 'off'}
function labels(v){if(v.no_review_label||v.review_policy==='none')return '<span class="lbl-rev none">SANS REVIEW</span>';
 if(v.is_self_review)return '<span class="lbl-rev self">SELF-REVIEW</span>';return '<span class="lbl-rev ind">REVIEW INDÉPENDANTE</span>'}
function gatesBar(v){const done=(v.progress?v.progress.completed:0);let h='<div class="gates">';for(let i=0;i<4;i++)h+='<i class="'+(i<done?'done':'')+'"></i>';return h+'</div>'}
function testLine(s){if(!s.tests)return '';const t=s.tests;
 if(t.cases_collected!=null&&t.cases_collected>0)return ' · <b class="'+(t.all_passed?'ok':'err')+'">'+t.cases_passed+'/'+t.cases_collected+' cas de test passés ('+t.commands+' commande'+(t.commands>1?'s':'')+')</b>';
 if(t.cases_collected===0)return ' · <span class="muted">aucun test réel exécuté</span>';
 if(t.error)return ' · <span class="warn">résultats de tests illisibles</span>';return ' · <span class="muted">tests non exécutés</span>'}
function resultBlock(v){const c=C(v.run_id);
 if(v.phase_label!=='done')return '';
 if(!v.result_available||v.nothing_produced)return '';
 const s=c.sum;if(!s)return '<div class="resultbox muted">Chargement du résultat…</div>';
 const delivered=s.files.filter(f=>f.exists);
 let h='<div class="resultbox"><div class="deliv-h">Résultat construit</div>';
 h+='<div class="built"><b>'+delivered.length+' fichier'+(delivered.length>1?'s':'')+' livré'+(delivered.length>1?'s':'')+'</b>'+testLine(s);
 if(s.review_policy&&s.review_policy!=='none')h+=' · review <b>'+esc(s.review_policy)+'</b>';h+='</div>';
 h+='<div class="deliv-h">Fichiers</div><div class="files">';
 for(const f of s.files){h+='<div class="file" data-open-run="'+esc(v.run_id)+'" data-open-path="'+esc(f.path)+'">'
  +'<div><div class="fname">'+esc(f.path)+'</div><div class="fmeta">'+(f.exists?f.bytes+' o · '+(f.is_text?'texte':'binaire'):'<span class="err">absent</span>')+'</div></div>'
  +'<div class="actions">'+(f.exists&&f.is_text?'<button class="btn ghost" data-open-run="'+esc(v.run_id)+'" data-open-path="'+esc(f.path)+'">Aperçu</button>':'')
  +(f.exists?'<button class="btn" data-dl-run="'+esc(v.run_id)+'" data-dl-path="'+esc(f.path)+'">⬇︎</button>':'')+'</div></div>'}
 h+='</div>';
 h+='<div class="zipbar">📦 Bundle complet (livrables + diff + résumé) <button class="btn primary" style="margin-left:auto" data-zip="'+esc(v.run_id)+'">Télécharger (zip)</button></div>';
 const fb=c.feedback;
 h+='<div class="feedbk">Ce résultat te convient ? <span class="fb-btn" data-fb="up" data-fb-run="'+esc(v.run_id)+'">👍</span>'
  +'<span class="fb-btn" data-fb="down" data-fb-run="'+esc(v.run_id)+'">👎</span>'
  +'<span class="fb-btn" data-refaire="'+esc(v.run_id)+'">↻ Refaire</span>'+(fb?'<span class="muted">'+esc(fb)+'</span>':'')+'</div>';
 return h+'</div>'}
function card(v){
 let h='<div class="runcard" id="rc-'+esc(v.run_id)+'">';
 h+='<div class="run-head"><span class="st '+stClass(v)+'">'+esc(v.phase_label||v.status)+'</span>'+labels(v)+gatesBar(v)
  +'<span class="run-meta">'+esc(v.builder_provider||v.builder_name)+' · review '+esc(v.review_policy||'none')+' · '+fmt(v.elapsed_seconds)+'</span></div>';
 h+='<div class="run-body"><div class="narr">'+esc(v.narration||v.current_step||'')
  +'<span class="steptime">étape '+fmt(v.step_elapsed_seconds)+'</span>';
 if(v.eta&&v.eta.eta_seconds!=null){const rest=v.eta.eta_seconds-v.elapsed_seconds;
  h+='<span class="eta">'+(rest>15?'résultat estimé dans ~'+fmt(rest):'imminent')+' (base '+v.eta.based_on_runs+' run'+(v.eta.based_on_runs>1?'s':'')+')</span>'}
 h+='</div>';
 if(v.block_cause){h+='<div class="blockbox"><b>'+esc(v.block_cause)+'</b>';
  if(v.block_verdicts&&v.block_verdicts.length)h+='<details><summary>Verdict complet du reviewer</summary><pre>'+esc(v.block_verdicts.map(x=>'['+x.stage+'/'+(x.reviewer||'?')+' '+(x.verdict||'')+'] '+x.finding).join("\\n\\n"))+'</pre></details>';
  if(v.unblock_hint)h+='<div class="hint2">'+esc(v.unblock_hint)+'</div>';h+='</div>'}
 h+=resultBlock(v);
 const acts=(v.phase_label&&v.phase_label.startsWith('running'));
 if(acts)h+='<div style="margin-top:9px"><button class="btn ghost" data-stop="'+esc(v.run_id)+'">Stop</button></div>';
 h+='<details><summary>Évidence '+esc(v.evidence_directory||'')+'</summary><pre>'+esc(JSON.stringify(v,null,2))+'</pre></details>';
 return h+'</div></div>'}
function feedItem(v){let h='<div class="msg-user"><div class="who">Toi</div><div class="txt">'+esc(v.mission_display||'')+'</div>';
 for(const a of (v.attachments||[]))h+='<span class="att">📎 '+esc(a.name)+'</span>';
 h+='</div>';return h+card(v)}
function render(){const conv=activeConvObj();const ids=conv?conv.runs:[];
 const runs=ids.map(id=>LIST.find(r=>r.run_id===id)).filter(Boolean);
 const feed=el('feed');
 if(!runs.length&&!pending.length){feed.innerHTML='<div class="empty">Écris une demande en bas pour lancer une mission.<br>JOÃO construit, teste, review, et te montre le résultat directement.</div>';return}
 let h=runs.map(feedItem).join('');
 for(const p of pending)h+='<div class="msg-user"><div class="who">Toi</div><div class="txt">'+esc(p)+'</div></div><div class="runcard"><div class="run-body muted">Lancement…</div></div>';
 feed.innerHTML=h}
// ---------- artifacts panel ----------
function renderMd(t){return esc(t).replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>')
 .replace(/\\*\\*([^*]+)\\*\\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\\n/g,'<br>')}
async function openArtifact(run,path){artifact={run,path};el('artifacts').classList.add('open');el('art-name').textContent=path;
 const body=el('art-body');body.innerHTML='<pre>chargement…</pre>';
 try{const d=await req('/runs/'+run+'/result/file?path='+encodeURIComponent(path));artifact.text=d.content;
  const lower=path.toLowerCase();
  if(d.content==null){const url=api('/runs/'+run+'/result/file?path='+encodeURIComponent(path)+'&download=1');body.innerHTML='<img src="'+url+'">'}
  else if(lower.endsWith('.md'))body.innerHTML='<div class="md">'+renderMd(d.content)+'</div>';
  else if(lower.endsWith('.html')||lower.endsWith('.htm')){const f=document.createElement('iframe');f.setAttribute('sandbox','allow-same-origin');body.innerHTML='';body.appendChild(f);f.srcdoc=d.content}
  else body.innerHTML='<pre></pre>',body.querySelector('pre').textContent=d.content;
 }catch(e){body.innerHTML='<pre>'+esc(e.message)+'</pre>'}}
function api(u){return u}
function tokenFetch(url){return fetch(url,{headers:{'X-JOAO-Token':TOKEN}})}
async function download(url,filename){const r=await tokenFetch(url);if(!r.ok)throw Error('téléchargement refusé ('+r.status+')');
 const blob=await r.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),9000)}
// image src needs the token; use a blob URL
async function hydrateImages(){document.querySelectorAll('.art-body img[src^="/runs"]').forEach(async img=>{
 if(img.dataset.done)return;img.dataset.done=1;const r=await tokenFetch(img.src);const b=await r.blob();img.src=URL.createObjectURL(b)})}
// ---------- refresh loop ----------
async function refresh(){try{const v=await req('/runs');LIST=v.runs||[];
 // hydrate details + result summaries for the active conversation's runs
 const conv=activeConvObj();const ids=conv?conv.runs:[];
 await Promise.all(LIST.filter(r=>ids.includes(r.run_id)).map(async s=>{const idx=LIST.indexOf(s);
  try{const d=await req('/runs/'+s.run_id);LIST[idx]=d;
   if(d.phase_label==='done'&&d.result_available&&!d.nothing_produced){const c=C(s.run_id);
    if(!c.sum||c.sumStatus!==d.status){try{c.sum=await req('/runs/'+s.run_id+'/result');c.sumStatus=d.status}catch(_){}}}
  }catch(_){}}));
 // auto-title conversation from its first mission
 if(conv&&conv.runs.length){const first=LIST.find(r=>r.run_id===conv.runs[0]);
  if(first&&first.mission_display&&(conv.title==='Nouvelle conversation'||!conv.title)){conv.title=first.mission_display.slice(0,42);saveConvs(CONVS)}}
 render();renderConvs();hydrateImages()}catch(_){}}
// ---------- send ----------
function fileToB64(file){return new Promise((res,rej)=>{const r=new FileReader();
 r.onload=()=>res(r.result.split(',')[1]);r.onerror=rej;r.readAsDataURL(file)})}
function renderAttach(){el('attachrow').innerHTML=pendingAtt.map((a,i)=>
 '<span class="attchip">📎 '+esc(a.name)+' <span class="x" data-rmatt="'+i+'">✕</span></span>').join('')}
let pendingAtt=[];
async function addFiles(files){for(const f of files){if(f.size>8*1024*1024){alert('Fichier trop volumineux (>8 Mo): '+f.name);continue}
 pendingAtt.push({name:f.name,b64:await fileToB64(f)})}renderAttach()}
async function send(){const mission=el('prompt').value.trim();if(!mission)return;
 const payload={mission,builder_name:selected('builder'),review_mode:selected('review'),
  attachments:pendingAtt.map(a=>({name:a.name,content_b64:a.b64}))};
 el('prompt').value='';const att=pendingAtt.slice();pendingAtt=[];renderAttach();pending.push(mission);render();
 try{const v=await req('/quick-missions',{method:'POST',body:JSON.stringify(payload)});
  const conv=activeConvObj();conv.runs.push(v.run_id);saveConvs(CONVS);
  pending=pending.filter(m=>m!==mission);refresh()}
 catch(e){pending=pending.filter(m=>m!==mission);alert('Lancement refusé — '+e.message);render()}}
// ---------- capabilities ----------
function updateSafety(){if(!CAPS)return;const b=selected('builder'),r=selected('review');
 let valid=el('prompt').value.trim().length>0&&CAPS[b]&&CAPS[b].available;
 for(const name of reviewParts(r))if(!CAPS[name]||!CAPS[name].reviewer_available)valid=false;
 el('send-btn').disabled=!valid;
 const q=el('quota-warning');const doomed=CAPS.codex&&CAPS.codex.quota_warning&&(b==='codex'||reviewParts(r).includes('codex'));
 if(doomed){q.style.display='block';q.textContent='⚠ Quota Codex épuisé ('+(CAPS.codex.quota_warning.at||'récemment')+') — le run se bloquera à la gate Codex; choisis une review sans Codex.'}else q.style.display='none'}
function enableChoice(name,value,enabled){const i=document.querySelector('input[name="'+name+'"][value="'+value+'"]');if(i)i.disabled=!enabled}
function ensureChoice(name){const cur=document.querySelector('input[name="'+name+'"]:checked');if(!cur||cur.disabled){const f=document.querySelector('input[name="'+name+'"]:not(:disabled)');if(f)f.checked=true}}
async function caps(){try{CAPS=await req('/capabilities');const v=CAPS;
 enableChoice('builder','glm',v.glm.available);enableChoice('builder','codex',v.codex.available);enableChoice('builder','claude',v.claude.available);
 document.querySelectorAll('input[name="review"]').forEach(i=>enableChoice('review',i.value,reviewParts(i.value).every(n=>v[n]&&v[n].reviewer_available)));
 ensureChoice('builder');ensureChoice('review');
 el('capabilities').innerHTML=['glm','codex','claude'].map(n=>'<span class="chip '+(v[n].available?'ok':'err')+'">'+n.toUpperCase()+' '+(v[n].available?'prêt':'indispo')+'</span>').join('')
  +(v.codex.quota_warning?'<span class="chip warn">Codex quota</span>':'');updateSafety()}catch(_){el('send-btn').disabled=true}}
// ---------- events ----------
el('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(!el('send-btn').disabled)send()}});
el('prompt').addEventListener('input',()=>{updateSafety();el('prompt').style.height='auto';el('prompt').style.height=Math.min(180,el('prompt').scrollHeight)+'px'});
document.querySelectorAll('input[name="builder"],input[name="review"]').forEach(i=>i.addEventListener('change',updateSafety));
el('send-btn').onclick=send;el('new-conv').onclick=()=>{newConversation();render();renderConvs()};
el('search').addEventListener('input',renderConvs);
el('attach-btn').onclick=()=>el('file-input').click();
el('file-input').addEventListener('change',e=>{addFiles(e.target.files);e.target.value=''});
const ibox=el('inputbox');
;['dragenter','dragover'].forEach(ev=>ibox.addEventListener(ev,e=>{e.preventDefault();ibox.classList.add('drag')}));
;['dragleave','drop'].forEach(ev=>ibox.addEventListener(ev,e=>{e.preventDefault();ibox.classList.remove('drag')}));
ibox.addEventListener('drop',e=>{if(e.dataTransfer&&e.dataTransfer.files.length)addFiles(e.dataTransfer.files)});
el('prompt').addEventListener('paste',e=>{const items=(e.clipboardData||{}).items||[];for(const it of items){if(it.kind==='file'){const f=it.getAsFile();if(f)addFiles([f])}}});
document.addEventListener('click',e=>{const t=e.target;
 if(t.dataset.conv){activeConv=t.dataset.conv;localStorage.setItem('joao_active',activeConv);render();renderConvs();refresh();return}
 if(t.dataset.rmatt!=null){pendingAtt.splice(+t.dataset.rmatt,1);renderAttach();return}
 const openR=t.dataset.openRun||t.closest('[data-open-run]')&&t.closest('[data-open-run]').dataset.openRun;
 const openP=t.dataset.openPath||t.closest('[data-open-path]')&&t.closest('[data-open-path]').dataset.openPath;
 if(openR&&openP){openArtifact(openR,openP);return}
 if(t.dataset.dlRun)return download('/runs/'+t.dataset.dlRun+'/result/file?path='+encodeURIComponent(t.dataset.dlPath)+'&download=1',t.dataset.dlPath.split('/').pop());
 if(t.dataset.zip)return download('/runs/'+t.dataset.zip+'/result/zip',t.dataset.zip+'-result.zip');
 if(t.dataset.stop){req('/runs/'+t.dataset.stop+'/stop',{method:'POST'}).then(refresh).catch(()=>{});return}
 if(t.dataset.fbRun){C(t.dataset.fbRun).feedback=t.dataset.fb==='up'?'👍 merci':'👎 noté — relance ou reformule';
  if(t.dataset.fb==='down')req('/runs/'+t.dataset.fbRun+'/feedback',{method:'POST',body:JSON.stringify({vote:'down'})}).catch(()=>{});render();return}
 if(t.dataset.refaire){const r=LIST.find(x=>x.run_id===t.dataset.refaire);if(r){el('prompt').value=r.mission_display||'';updateSafety();el('prompt').focus()}return}
});
el('art-close').onclick=()=>el('artifacts').classList.remove('open');
el('art-copy').onclick=()=>{if(artifact&&artifact.text!=null)navigator.clipboard.writeText(artifact.text)};
el('art-dl').onclick=()=>{if(artifact)download('/runs/'+artifact.run+'/result/file?path='+encodeURIComponent(artifact.path)+'&download=1',artifact.path.split('/').pop())};
renderConvs();caps();refresh();setInterval(refresh,2500);setInterval(caps,30000);
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
                    # Static brand assets (mascot, favicon) — public like the page itself.
                    if path in ASSET_ROUTES:
                        raw, kind = outer._asset(ASSET_ROUTES[path])
                        if raw is None:
                            return self.send(404, {"error": "asset not found"})
                        self.send_response(200)
                        self.send_header("Content-Type", kind)
                        self.send_header("Content-Length", str(len(raw)))
                        self.send_header("Cache-Control", "max-age=86400")
                        self.end_headers()
                        return self.wfile.write(raw)
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

    def _asset(self, spec):
        """Read a bundled brand asset (confined to the assets directory)."""
        filename, kind = spec
        path = (ASSETS_DIR / filename).resolve()
        if ASSETS_DIR not in path.parents or not path.is_file():
            return None, None
        return path.read_bytes(), kind

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
            if action == "resume" and not Path(before["workspace"]).is_dir():
                # Refuse BEFORE leaving paused: nothing can drive a dead sandbox.
                return {"action": action, "accepted": False, "status": before["status"],
                        "reason": "le sandbox de ce run n'existe plus — utilise Stop pour l'archiver"}
            state = getattr(self.runtime, action)(run_id)
            if action == "resume":
                # Judge the feedback on resume()'s own transition; the driver
                # relaunched below may already have moved the run further.
                try:
                    self.resume_drive(run_id)
                except RuntimeStateError as exc:
                    # The run already left paused; stranding it in an active
                    # state with no worker would lie to the card. Stop honestly.
                    stopped = self.runtime.stop(run_id)
                    return {"action": action, "accepted": False, "status": stopped["status"],
                            "reason": f"reprise impossible ({exc}) — run arrêté proprement"}
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

    def _start(self, project, workspace, mission, paths, full, target, builder_name, reviewer_names, review_policy, generated_paths=None, attachments_manifest=None):
        profile = ProjectProfile(project_id=project, display_name=project, repository_root=str(workspace), allowed_write_paths=paths, forbidden_paths=[], generated_paths=list(generated_paths or []), approval_required=True)
        run = self.runtime.start(project_id=project, workspace=workspace, mission=mission, targeted_tests=[target] if target else [], full_tests=[full], profile=profile, builder_name=builder_name, reviewer_names=reviewer_names, review_policy=review_policy)
        if attachments_manifest:
            self.runtime.record_attachments(run, attachments_manifest)
        self.drive(run)
        return {"run_id": run, "status": "queued", "attachments": attachments_manifest or []}

    MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

    @staticmethod
    def _decode_attachments(raw_attachments):
        """Validate + decode composer attachments (BLOC E). Fail loud, never silent."""
        attachments = []
        for entry in raw_attachments or []:
            name = str((entry or {}).get("name", "")).strip()
            safe = Path(name).name  # strip any directory component / traversal
            if not safe or safe in {".", ".."} or "/" in name or "\\" in name or safe.startswith("."):
                raise ValueError(f"nom de pièce jointe invalide: {name!r}")
            payload = (entry or {}).get("content_b64") or ""
            try:
                data = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"pièce jointe illisible (base64 invalide): {safe}")
            if not data:
                raise ValueError(f"pièce jointe vide: {safe}")
            if len(data) > LocalAPIServer.MAX_ATTACHMENT_BYTES:
                raise ValueError(f"pièce jointe trop volumineuse (> 8 Mo): {safe}")
            attachments.append((safe, data))
        names = [name for name, _ in attachments]
        if len(set(names)) != len(names):
            raise ValueError("noms de pièces jointes en double")
        return attachments

    def quick_sandbox(self, attachments=None):
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
        # BLOC E: attachments land in inputs/ inside the run workspace and are
        # committed to the baseline (so they are available to the builder but do
        # not count as builder output). Their SHA-256 is recorded as evidence.
        manifest = []
        if attachments:
            (root / "inputs").mkdir()
            for name, data in attachments:
                (root / "inputs" / name).write_bytes(data)
                manifest.append({"name": name, "bytes": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest(),
                                 "path": f"inputs/{name}"})
        for argv in (["git", "init", "-q"], ["git", "config", "user.email", "joao-sandbox@example.invalid"], ["git", "config", "user.name", "JOAO Sandbox"], ["git", "add", "."], ["git", "commit", "-qm", "sandbox baseline"]):
            subprocess.run(argv, cwd=str(root), check=True)
        return root, manifest

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
        # A5b/V13-F14: allowed paths are a PERMISSIVE SUPERSET — the files the
        # mission actually names, unioned with the conventional quick base — so a
        # legitimately-named file (roman.py) is always in scope and the plan
        # gate never blocks for a stale static todo profile. Confined to safe
        # relative paths inside the disposable sandbox.
        quick_base = {"todo.py", "test_todo.py", "todo.json", "test_tasks.json",
                      "todo.json.tmp", "test_tasks.json.tmp", "src/", "tests/"}
        derived = set(mission_allowed_paths(mission)) | quick_base
        # A3: a text/no-code request must still deliver a real file (.md).
        text_intent = not CODE_INTENT_RE.search(mission)
        if text_intent and not any(p.endswith(".md") for p in derived):
            derived.add("output.md")
        attachments = self._decode_attachments(data.get("attachments"))
        requested_paths = data.get("allowed_paths")
        if requested_paths is not None:
            requested = [str(path).strip() for path in requested_paths if str(path).strip()]
            if not requested or len(set(requested)) != len(requested) \
                    or any(path not in derived for path in requested):
                raise ValueError("allowed_paths must be a subset of the mission-scoped safe paths")
            allowed = requested
        else:
            allowed = sorted(derived)
        if not allowed or len(set(allowed)) != len(allowed):
            raise ValueError("quick sandbox allowed_paths must be a non-empty unique set")
        root, attachments_manifest = self.quick_sandbox(attachments)
        contract = (
            "Work only inside this disposable Git sandbox. Do not install packages, commit, "
            "push, access external paths, or modify the sandbox policy. Use only Python's "
            "standard-library unittest framework for tests, and run the recorded test command. "
            "The term needs_approval names a JOAO runtime state: never create a file or directory "
            "with that name. Runtime/staging files (todo.json, test_tasks.json and their .tmp "
            "siblings) must be removed before delivery. You MUST create at least one real "
            "deliverable file; an empty result is rejected.\n\n"
        )
        if attachments_manifest:
            names = ", ".join(item["name"] for item in attachments_manifest)
            contract += (f"The user attached input file(s) available in the read-only inputs/ "
                         f"directory: {names}. Read them as needed for the task.\n\n")
        if text_intent:
            contract += (
                "This is a text/writing request (no code required). Deliver the full result as a "
                "Markdown file named output.md — write the actual content into that file. "
                "No unit tests are required for a pure text deliverable.\n\n"
            )
        contract += "User task:\n" + mission
        full_test = ["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test*.py"]
        generated = [path for path in ("todo.json", "test_tasks.json",
                                       "todo.json.tmp", "test_tasks.json.tmp") if path in allowed]
        return self._start("quick-sandbox", root, contract, allowed, full_test, [], builder_name, reviewer_names, review_policy, generated, attachments_manifest)

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
                try:
                    self.drive(run_id)
                except RuntimeStateError as exc:
                    # Never leave a resumed run stranded without a worker.
                    try:
                        current = self.runtime.get(run_id)
                        if current["status"] not in {"stopped", "accepted", "blocked", "failed"}:
                            self.runtime._event(current, "resume_failed", error=str(exc))
                            self.runtime.stop(run_id)
                    except Exception:
                        pass
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
