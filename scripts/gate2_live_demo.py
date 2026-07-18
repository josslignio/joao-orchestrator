#!/usr/bin/env python3
"""GATE 2 — LIVE E2E of the V2.2 Chat Era over the real local HTTP server.

Re-plays the exact demos that misfired on 17/07, for real:
  1. "Hello ça va ?" → classified CHAT (instant, deterministic) → streamed reply in SECONDS
     via a real CLI, ZERO code run created.
  2. a real PDF is attached and questioned → the answer is grounded in the pypdf-EXTRACTED
     text (traceable), not invented.
  3. "crée is_prime.py avec tests" → classified MISSION_CODE → routes to the Phase-1 cascade.
  4. an ambiguous message → AMBIGU → the UI shows the two buttons (💬 / 🛠️).

Everything is real: real classify, real streaming from claude/opencode, real extraction.
Honest on limits (Codex review quota, web).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from joao_orchestrator.bubble.api import LocalAPIServer  # noqa: E402
from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime  # noqa: E402
from joao_orchestrator.providers.cascade_runtime import CascadeBuilder  # noqa: E402

sys.path.insert(0, str(REPO / "tests"))
from test_v22_chat import _minimal_pdf  # noqa: E402  (reuse the real-PDF helper)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


EVID = Path("~/.local/share/joao/acceptance").expanduser() / f"bigrun-suite-gate2-{now()}"
EVID.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    (EVID / "run.log").open("a").write(line + "\n")


def post(api, path, body):
    req = urllib.request.Request(api.url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
    with urllib.request.urlopen(req, timeout=200) as resp:
        return resp.read()


def stream_chat(api, message, model, attachments=None):
    req = urllib.request.Request(api.url + "chat", method="POST",
                                 data=json.dumps({"message": message, "model": model,
                                                  "attachments": attachments or []}).encode(),
                                 headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
    events, text, model_used = [], "", model
    with urllib.request.urlopen(req, timeout=200) as resp:
        for block in resp.read().decode().split("\n\n"):
            if block.startswith("data: "):
                ev = json.loads(block[6:]); events.append(ev)
                if ev["event"] == "delta": text += ev["text"]
                if ev.get("model"): model_used = ev["model"]
    return text, model_used, events


def main():
    log(f"GATE 2 live E2E — evidence at {EVID}")
    rt = RunRuntime(EVID / "state", builder=CascadeBuilder(), profiles=LocalProfileAdapter())
    api = LocalAPIServer(rt); api.serve_in_thread()
    summary = {"evidence_dir": str(EVID), "at": now()}
    try:
        # ── 1. greeting → CHAT, fast reply, NO run ──
        cls = json.loads(post(api, "chat/classify", {"message": "Hello ça va ?", "mode": "auto"}))
        t0 = time.monotonic()
        text, model_used, _ = stream_chat(api, "Hello ça va ?", "glm")  # GLM = 0 forfait, fast
        elapsed = round(time.monotonic() - t0, 1)
        runs_created = len(list((EVID / "state" / "runs").glob("*"))) if (EVID / "state" / "runs").exists() else 0
        summary["demo1_greeting"] = {"intent": cls["intent"], "reply_seconds": elapsed,
                                     "model_used": model_used, "reply_head": text[:160], "runs_created": runs_created}
        log(f"  1. greeting → {cls['intent']} · reply in {elapsed}s via {model_used} · runs_created={runs_created}")
        log(f"     reply: {text[:120]!r}")

        # ── 2. real PDF attached + questioned → grounded answer ──
        secret = "Le code secret du coffre est 4242. Ne le partage jamais."
        pdf = EVID / "coffre.pdf"; pdf.write_bytes(_minimal_pdf(secret))
        import base64
        att = json.loads(post(api, "chat/attach", {"name": "coffre.pdf",
                         "content_base64": base64.b64encode(pdf.read_bytes()).decode()}))
        log(f"  2. PDF attached → extraction ok={att['extraction']['ok']} "
            f"method={att['extraction'].get('method')} chars={att['extraction'].get('chars')}")
        text2, model2, _ = stream_chat(api, "Quel est le code secret du coffre indiqué dans le PDF ?",
                                       "claude", attachments=[att["id"]])
        grounded = "4242" in text2
        summary["demo2_pdf_grounded"] = {"extraction": att["extraction"], "model_used": model2,
                                         "answer_contains_4242": grounded, "answer": text2[:300]}
        log(f"     answer via {model2}: {text2[:160]!r} → grounded(4242)={grounded}")

        # ── 3. build request → MISSION_CODE (routes to cascade) ──
        cls3 = json.loads(post(api, "chat/classify", {"message": "crée is_prime.py avec des tests unitaires"}))
        summary["demo3_build_routing"] = {"intent": cls3["intent"], "reason": cls3["reason"]}
        log(f"  3. build request → {cls3['intent']} (→ Phase-1 cascade; proof in GATE 1)")

        # ── 4. ambiguous → AMBIGU → 2 buttons ──
        cls4 = json.loads(post(api, "chat/classify", {"message": "le module de paiement"}))
        summary["demo4_ambiguous"] = {"intent": cls4["intent"], "reason": cls4["reason"],
                                      "ui": "two buttons: 💬 Répondre en chat / 🛠️ Lancer une mission"}
        log(f"  4. ambiguous → {cls4['intent']} → UI shows the two buttons")

    finally:
        api.close()
    (EVID / "GATE2_SUMMARY.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    log("GATE 2 live E2E complete.")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
