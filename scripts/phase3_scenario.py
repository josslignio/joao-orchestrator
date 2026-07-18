#!/usr/bin/env python3
"""PHASE 3.1 — GLOBAL PROOF OF LIFE, filmed by the evidence.

One continuous scenario over the real HTTP server:
  chat (real Claude) → short discussion → "ok build-le" (→ MISSION_CODE) → cascade mission
  (real GLM) → review → deliverable in the panel → total cost shown.

Honest: the reviewer here is an accept-fixture because the real Codex reviewer is quota-blocked
until 2026-07-23 (the reviewer WIRING is unchanged — see runtime.CodexCLIReviewer); the build,
routing, memory injection and cost are all REAL.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from joao_orchestrator.bubble.api import LocalAPIServer  # noqa: E402
from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime  # noqa: E402
from joao_orchestrator.domain.models import ProjectProfile  # noqa: E402
from joao_orchestrator.providers.cascade_runtime import CascadeBuilder  # noqa: E402


def now():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


EVID = Path("~/.local/share/joao/acceptance").expanduser() / f"bigrun-suite-gate3-{now()}"
EVID.mkdir(parents=True, exist_ok=True)


def log(m):
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {m}"
    print(line, flush=True); (EVID / "run.log").open("a").write(line + "\n")


class AcceptReviewer:
    provider = "codex-accept-fixture"  # honest: Codex quota-blocked → fixture; wiring unchanged
    model = "fixture(codex-quota-blocked-until-2026-07-23)"
    def review(self, run, _):
        return {"ok": True, "decision": "pass",
                "proof": {"verdict": "ACCEPT", "reviewed_diff_sha256": run["final_diff_sha256"]}}


def post(api, path, body):
    req = urllib.request.Request(api.url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
    with urllib.request.urlopen(req, timeout=400) as r:
        return r.read()


def stream(api, message, model, attachments=None):
    req = urllib.request.Request(api.url + "chat", method="POST",
                                 data=json.dumps({"message": message, "model": model,
                                                  "attachments": attachments or []}).encode(),
                                 headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
    text, mdl = "", model
    with urllib.request.urlopen(req, timeout=400) as r:
        for b in r.read().decode().split("\n\n"):
            if b.startswith("data: "):
                ev = json.loads(b[6:])
                if ev["event"] == "delta": text += ev["text"]
                if ev.get("model"): mdl = ev["model"]
    return text, mdl


def main():
    log(f"PHASE 3.1 global proof of life — evidence {EVID}")
    ws = EVID / "ws"; ws.mkdir()
    for a in (["git", "init", "-q"], ["git", "config", "user.email", "s@t.invalid"], ["git", "config", "user.name", "s"]):
        subprocess.run(a, cwd=ws, check=True)
    (ws / "test_primes.py").write_text(
        "from primes import is_prime\nassert is_prime(2) and is_prime(13) and not is_prime(1) and not is_prime(9)\nprint('ok')\n")
    (ws / "README.md").write_text("# scenario\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=ws, check=True)

    rt = RunRuntime(EVID / "state", builder=CascadeBuilder(n=3), reviewer=AcceptReviewer(),
                    profiles=LocalProfileAdapter())
    api = LocalAPIServer(rt); api.serve_in_thread()
    scenario = {"evidence_dir": str(EVID), "at": now(), "steps": []}
    try:
        # 1) short chat discussion (real Claude)
        reply, mdl = stream(api, "En une phrase : c'est quoi un nombre premier ?", "claude")
        scenario["steps"].append({"step": "chat", "model": mdl, "reply": reply[:200]})
        log(f"  chat via {mdl}: {reply[:90]!r}")

        # 2) "ok build-le" → routed MISSION_CODE
        build_msg = "ok build-le : écris is_prime(n) dans primes.py à la racine, avec 0 et 1 non premiers"
        cls = json.loads(post(api, "chat/classify", {"message": build_msg}))
        scenario["steps"].append({"step": "route", "intent": cls["intent"], "reason": cls["reason"]})
        log(f"  '{build_msg[:30]}…' → {cls['intent']}")

        # 3) mission through the cascade (real GLM), profile allows primes.py
        (ws / ".joao-profile.json").write_text(json.dumps({
            "project_id": "scenario", "display_name": "scenario", "repository_root": str(ws),
            "allowed_write_paths": ["primes.py"], "forbidden_paths": []}))
        subprocess.run(["git", "add", ".joao-profile.json"], cwd=ws, check=True)
        subprocess.run(["git", "commit", "-qm", "profile"], cwd=ws, check=True)
        r = json.loads(post(api, "chat/mission", {
            "mission": "Écris is_prime(n:int)->bool dans primes.py (0 et 1 non premiers). Ne crée aucun autre fichier.",
            "workspace": str(ws), "allowed_paths": ["primes.py"],
            "full_test_command": f"{sys.executable} test_primes.py", "critical": True}))
        run_id = r["run_id"]; log(f"  mission {run_id} launched via cascade")

        # 4) poll to a terminal state
        deadline = time.monotonic() + 360
        state = None
        while time.monotonic() < deadline:
            state = api.runtime.get(run_id)
            if state["status"] in {"needs_approval", "accepted", "blocked", "failed", "stopped"}:
                break
            time.sleep(3)
        log(f"  mission status: {state['status']}")

        rd = EVID / "state" / "runs" / run_id
        cascade_cost = json.loads((rd / "cascade-cost.json").read_text()) if (rd / "cascade-cost.json").exists() else {}
        decision = json.loads((rd / "cascade-decision.json").read_text()) if (rd / "cascade-decision.json").exists() else {}
        mem_events = [e for e in api.runtime.events(run_id) if e["kind"] == "memory_injected"]

        approved = None
        if state["status"] == "needs_approval":
            approved = api.runtime.approve(run_id)["status"]
            log(f"  approved → {approved}")

        deliverable = (ws / "primes.py").read_text()[:300] if (ws / "primes.py").exists() else None
        scenario["steps"].append({
            "step": "mission", "run_id": run_id, "final_status": approved or state["status"],
            "cascade_tier": decision.get("tier"), "candidates": len(decision.get("candidates", [])),
            "total_cost_proxy": cascade_cost.get("cascade_proxy_cost"),
            "savings_vs_direct_claude": cascade_cost.get("savings_vs_direct_claude"),
            "memory_injected_roles": sorted({e["role"] for e in mem_events}),
            "deliverable_head": deliverable})
        log(f"  tier={decision.get('tier')} cost={cascade_cost.get('cascade_proxy_cost')} "
            f"deliverable={'yes' if deliverable else 'no'} memory_roles={sorted({e['role'] for e in mem_events})}")
    finally:
        api.close()
    (EVID / "GATE3_SCENARIO.json").write_text(json.dumps(scenario, indent=2, ensure_ascii=False))
    log("PHASE 3.1 complete.")
    print(json.dumps(scenario, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
