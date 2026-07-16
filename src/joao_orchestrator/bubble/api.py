"""Local-only HTTP API and compact polling bubble; stdlib only."""
from __future__ import annotations
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from .runtime import RunRuntime, RuntimeStateError

HTML = """<!doctype html><meta charset=utf-8><title>JOAO bubble</title><style>body{font:14px system-ui;background:#10131a;color:#eef;margin:16px}.bubble{max-width:820px;border:1px solid #456;border-radius:12px;padding:16px}button{margin:3px;padding:7px}pre{max-height:50vh;overflow:auto;background:#080b10;padding:10px}</style><div class=bubble><h2>JOAO <span id=s>loading</span></h2><div id=n></div><p><button onclick="a('pause')">Pause</button><button onclick="a('resume')">Resume</button><button onclick="a('stop')">Stop</button><button onclick="a('retry')">Retry</button><button onclick="a('approve')">Approve</button><button onclick="a('reject')">Reject</button></p><pre id=d></pre></div><script>let id=new URLSearchParams(location.search).get('run');async function g(){if(!id)return;let j=await (await fetch('/runs/'+id)).json();s.textContent=j.status;n.textContent=(j.project_id||'')+' · '+(j.current_step||'');d.textContent=JSON.stringify(j,null,2)}async function a(x){if(id)await fetch('/runs/'+id+'/'+x,{method:'POST'});g()}setInterval(g,1000);g()</script>"""

class LocalAPIServer:
    def __init__(self, runtime: RunRuntime, host="127.0.0.1", port=0):
        if host not in {"127.0.0.1", "localhost", "::1"}: raise ValueError("localhost only")
        outer = self; self.runtime = runtime
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def send_data(self, code, value, kind="application/json"):
                raw=value.encode() if isinstance(value,str) else json.dumps(value).encode()
                self.send_response(code); self.send_header("Content-Type",kind); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                bits=urlparse(self.path).path.strip("/").split("/")
                try:
                    if self.path=="/": return self.send_data(200,HTML,"text/html; charset=utf-8")
                    if len(bits)==2 and bits[0]=="runs": return self.send_data(200,outer.runtime.get(bits[1]))
                    if len(bits)==3 and bits[0]=="runs" and bits[2]=="events": return self.send_data(200,outer.runtime.events(bits[1]))
                    self.send_data(404,{"error":"not found"})
                except RuntimeStateError as exc: self.send_data(404,{"error":str(exc)})
            def do_POST(self):
                bits=urlparse(self.path).path.strip("/").split("/")
                try:
                    if len(bits)==3 and bits[0]=="runs" and bits[2] in {"pause","resume","stop","retry","approve","reject"}: return self.send_data(200,getattr(outer.runtime,bits[2])(bits[1]))
                    self.send_data(404,{"error":"not found"})
                except RuntimeStateError as exc: self.send_data(409,{"error":str(exc)})
        self.server=ThreadingHTTPServer((host,port),Handler)
    @property
    def url(self): return f"http://127.0.0.1:{self.server.server_port}/"
    def serve_in_thread(self):
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start();return thread
    def close(self): self.server.shutdown();self.server.server_close()
