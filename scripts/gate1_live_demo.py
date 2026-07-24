#!/usr/bin/env python3
"""GATE 1 — LIVE demo of the REAL cost cascade (not simulated).

Runs, against a real temp git worktree and the REAL memory brain:
  A. a simple mission → deterministic tier (a), cost 0, through the full RunRuntime live path
     (proves the cascade is wired into the launch path + real memory injection events).
  B. a critical mission → best-of-3 REAL joao-glm, objective tests judge the winner, 3 GLM
     outputs archived, real cost compared vs going straight to Claude.

Everything is real: real git isolation, real joao-glm subprocess, real inject.build_injection
block prefixed to EACH candidate. Evidence lands under the acceptance dir. Honest on limits.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
MEM = REPO / "memory"
sys.path.insert(0, str(MEM))

from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime  # noqa: E402
from joao_orchestrator.domain.models import ProjectProfile  # noqa: E402
from joao_orchestrator.providers.cascade import WorkerResult  # noqa: E402
from joao_orchestrator.providers.cascade_runtime import CascadeBuilder  # noqa: E402
import inject as inject_mod  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


EVID = Path("~/.local/share/joao/acceptance").expanduser() / f"bigrun-suite-gate1-{now()}"
EVID.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with (EVID / "run.log").open("a") as fh:
        fh.write(line + "\n")


def git_repo(where: Path, files: dict[str, str]) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    for argv in (["git", "init", "-q"], ["git", "config", "user.email", "gate1@t.invalid"],
                 ["git", "config", "user.name", "gate1"]):
        subprocess.run(argv, cwd=where, check=True)
    for rel, content in files.items():
        p = where / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "add", "."], cwd=where, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=where, check=True)
    return where


# ─────────────────────────── DEMO A — deterministic tier via full RunRuntime ───────────────────────────
def demo_a() -> dict:
    log("DEMO A — simple mission → deterministic tier (a), full live RunRuntime path")
    ws = git_repo(EVID / "demoA-ws", {
        "version.py": "VERSION = 1\n",
        "test_version.py": "from version import VERSION\nassert VERSION == 2\n",
    })

    def set_version_tool(_task, workspace, _allowed):
        (Path(workspace) / "version.py").write_text("VERSION = 2\n")
        return WorkerResult(provider="deterministic", cost=0.0, ok=True, text="set VERSION=2")

    builder = CascadeBuilder(deterministic=set_version_tool)
    profile = ProjectProfile(project_id="joao-gate1", display_name="joao-gate1",
                             repository_root=str(ws), allowed_write_paths=["version.py"],
                             forbidden_paths=[])
    rt = RunRuntime(EVID / "demoA-state", builder=builder, profiles=LocalProfileAdapter())
    run_id = rt.start(project_id="joao-gate1", workspace=ws, mission="Passer VERSION à 2 dans version.py.",
                      targeted_tests=[[sys.executable, "test_version.py"]],
                      full_tests=[[sys.executable, "test_version.py"]], profile=profile)
    state = rt.run_once(run_id)
    rd = EVID / "demoA-state" / "runs" / run_id
    decision = json.loads((rd / "cascade-decision.json").read_text())
    cost = json.loads((rd / "cascade-cost.json").read_text())
    events = rt.events(run_id)
    mem_events = [e for e in events if e["kind"] == "memory_injected"]
    active_rules = sorted(p.name for p in rd.glob("active-rules-*.md"))
    result = {"run_id": run_id, "status": state["status"], "tier": decision["tier"],
              "cost": cost["cascade_proxy_cost"], "version_after": (ws / "version.py").read_text().strip(),
              "memory_injected_roles": sorted({e["role"] for e in mem_events}),
              "active_rules_files": active_rules,
              "injected_ids_sample": (mem_events[0]["injected_ids"][:5] if mem_events else [])}
    log(f"  → status={result['status']} tier={result['tier']} cost={result['cost']} "
        f"version={result['version_after']} memory_roles={result['memory_injected_roles']}")
    (EVID / "demoA-result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


# ─────────────────────────── DEMO B — critical best-of-3 REAL GLM ───────────────────────────
IS_PRIME_TEST = (
    "from primes import is_prime\n"
    "assert is_prime(2) and is_prime(3) and is_prime(13) and is_prime(97)\n"
    "assert not is_prime(0) and not is_prime(1) and not is_prime(4) and not is_prime(100)\n"
    "print('is_prime OK')\n"
)
MISSION_B = (
    "Écris une fonction `is_prime(n: int) -> bool` dans le fichier `primes.py` à la RACINE du "
    "worktree : elle renvoie True si n est un nombre premier, False sinon (0 et 1 ne sont pas "
    "premiers). Ne crée AUCUN autre fichier ; ne modifie pas les tests."
)


def demo_b() -> dict:
    log("DEMO B — critical mission → best-of-3 REAL joao-glm (this calls GLM 3 times, be patient)")
    ws = git_repo(EVID / "demoB-ws", {
        "test_primes.py": IS_PRIME_TEST,
        "README.md": "# gate1 demo B\n",
    })
    rd = EVID / "demoB-run"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps({
        "run_id": "gate1-demoB", "workspace": str(ws), "critical": True, "recurrence": False,
        "tags": ["cascade", "gate1"], "targeted_tests": [],
        "full_tests": [[sys.executable, "test_primes.py"]],
    }))

    # REAL B-28 injection — the actual RÈGLES ACTIVES block, prefixed to EACH candidate's task.
    injection = inject_mod.build_injection("builder", project="joao",
                                           mission_type="cascade best-of-n is_prime critical",
                                           files_touched=["primes.py"])
    (EVID / "demoB-active-rules-builder.md").write_text(injection.block or "(no lessons matched)\n")
    log(f"  injected {len(injection.ids)} lessons into the builder prompt: {injection.ids[:6]}...")
    mission_for_builder = f"{injection.block}\n\n---\n\n{MISSION_B}" if injection.block else MISSION_B

    builder = CascadeBuilder(n=3, angles=("performance", "readability", "edge-cases"), timeout=420)
    started = time.monotonic()
    out = builder.build(mission_for_builder, ws, rd, ["primes.py"], correction=False)
    elapsed = round(time.monotonic() - started, 1)

    decision = json.loads((rd / "cascade-decision.json").read_text())
    cost = json.loads((rd / "cascade-cost.json").read_text())
    injv = json.loads((rd / "cascade-injection.json").read_text())
    # confirm each candidate task file carries the real injected block
    per_worker_injected = all("RÈGLES ACTIVES" in Path(c["task_file"]).read_text()
                              for c in injv["candidates"]) if injv["candidates"] else False
    result = {
        "ok": out["ok"], "tier": out.get("tier"), "winner_provider": out.get("provider"),
        "winner_model": out.get("model"), "winner_angle": out.get("winner_angle"),
        "elapsed_s": elapsed, "candidates_archived": len(decision["candidates"]),
        "candidate_scores": [{"angle": c["angle"], "tests_passed": c["tests_passed"],
                              "score": c["score"]} for c in decision["candidates"]],
        "judge_method": (decision.get("judge_verdict") or {}).get("method"),
        "proxy_cost": cost["cascade_proxy_cost"], "direct_claude_proxy": cost["direct_claude_proxy"],
        "savings_vs_direct_claude": cost["savings_vs_direct_claude"],
        "real_cost_signal_tokens": cost["real_cost_signal"],
        "worker_durations_s": cost["worker_durations_s"],
        "per_worker_injection_confirmed": per_worker_injected,
        "primes_py_head": (ws / "primes.py").read_text()[:400] if (ws / "primes.py").exists() else None,
    }
    log(f"  → ok={result['ok']} tier={result['tier']} winner={result['winner_provider']} "
        f"angle={result['winner_angle']} cost={result['proxy_cost']} vs direct-Claude "
        f"{result['direct_claude_proxy']} (saved {result['savings_vs_direct_claude']}) in {elapsed}s")
    log(f"  candidate scores: {result['candidate_scores']}")
    log(f"  per-worker injection confirmed: {result['per_worker_injection_confirmed']}")
    (EVID / "demoB-result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == "__main__":
    log(f"GATE 1 live demo — evidence at {EVID}")
    summary = {"evidence_dir": str(EVID), "at": now()}
    try:
        summary["demo_a_deterministic"] = demo_a()
    except Exception as exc:  # honest: record the failure, do not fake a pass
        summary["demo_a_deterministic"] = {"error": f"{type(exc).__name__}: {exc}"}
        log(f"  DEMO A FAILED: {exc}")
    try:
        summary["demo_b_best_of_3_real_glm"] = demo_b()
    except Exception as exc:
        summary["demo_b_best_of_3_real_glm"] = {"error": f"{type(exc).__name__}: {exc}"}
        log(f"  DEMO B FAILED: {exc}")
    (EVID / "GATE1_SUMMARY.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    log("GATE 1 live demo complete.")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
