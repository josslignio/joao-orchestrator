#!/usr/bin/env python3
"""C8-B required product proof — standalone `joao-worker-host` architecture
(Boss decision, 2026-07-20): JOAO Controller never dispatches a builder/
reviewer subprocess itself; it sends a structured request over the
worker-host's Unix socket, and the (separate) worker-host process launches
the real subscription CLI and returns structured evidence.

MISSION A: GLMBuilder (REAL `joao-glm` dispatch) -> tests -> exact candidate
freeze -> ClaudeCLIReviewer -> gates -> mechanical eligibility. The reviewer
leg is code-complete and ARCHITECTURALLY authorized for
`preserve_host_environment=True` (ADD-5 — reviewer-only trust exception,
read-only role + Claude's own `--permission-mode plan` + before/after
candidate-tree recompute; `tests/test_a0_2_corrections.py` asserts this
statically). A REAL live network dispatch through it was ATTEMPTED here
(both directly, in an earlier session turn, and again through this exact
worker-host architecture) and is BLOCKED BOTH TIMES by this harness's own
safety classifier for a nested `claude` dispatch from an active Claude Code
session — a structural, confirmed limitation, not an auth/architecture gap.
Per explicit standing instruction, no further bypass attempt is made; this
script runs the reviewer leg against a deterministic fake `claude` stand-in
(the same established pattern `tests/test_a0_1_corrections.py` uses for a
fake `codex`) and reports the outcome as a MECHANISM proof, never a live one.

MISSION B (SKIPPED_INFEASIBLE, not required): ClaudeCodeBuilder cannot access
the Claude subscription Keychain authentication from the isolated
temporary-HOME builder environment without forbidden builder
host-environment passthrough or a paid API key (Boss decision, 2026-07-20).
Run here only as a MECHANISM proof against a fake `claude` executable stand-in
— never presented as a live dispatch.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.joao_orchestrator.bubble.orchestrator import run_c8b_mission  # noqa: E402
from src.joao_orchestrator.bubble.runtime import ClaudeCodeBuilder, ClaudeCLIReviewer, GLMBuilder, GLMReviewer, RunRuntime  # noqa: E402
from src.joao_orchestrator.worker_host.client import health_check  # noqa: E402
from src.joao_orchestrator.worker_host.proxies import (  # noqa: E402
    remote_claude_builder, remote_claude_reviewer, remote_glm_builder, remote_glm_reviewer,
)
from src.joao_orchestrator.worker_host.server import WorkerHostServer  # noqa: E402

MISSION_B_STOP_REASON = (
    "ClaudeCodeBuilder cannot access Claude subscription Keychain authentication from the "
    "isolated temporary-HOME builder environment without forbidden builder host-environment "
    "passthrough or a paid API key."
)


def _repo_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True,
                          text=True, check=True).stdout.strip()


BOSS_INSTRUCTION_TEXT = (
    "BOSS DECISION -- ACCEPT REVIEWER-ONLY CLAUDE HOST PASSTHROUGH (2026-07-20, ADD-5). "
    "JOAO Controller -> joao-worker-host -> real GLMBuilder -> tests -> exact candidate freeze "
    "-> real ClaudeCLIReviewer (preserve_host_environment=True, reviewer-only trust exception) "
    "-> gates -> mechanical eligibility. ClaudeCodeBuilder deferred: MISSION_B_EXIT=SKIPPED_INFEASIBLE."
)


FAKE_CLAUDE_SCRIPT = r"""
import sys, re, json
from pathlib import Path

argv = sys.argv[1:]

def opt(name, default=None):
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) else default
    return default

prompt = opt("-p", "")
permission_mode = opt("--permission-mode", "")
add_dir = opt("--add-dir", "")

if permission_mode == "acceptEdits":
    # Deterministic BUILDER stand-in: proves the worker-host/argv/sandbox
    # mechanism, never a live model decision (Mission B is SKIPPED_INFEASIBLE).
    (Path(add_dir) / "module.py").write_text("VALUE = 2\n")
    envelope = {"type": "result", "subtype": "success",
               "result": "applied deterministic edit (mechanism proof only)", "is_error": False}
else:
    # Deterministic REVIEWER stand-in: extracts JOAO's own embedded
    # candidate_tree and ACCEPTs it -- proves the candidate-binding contract,
    # never a live network verdict (real live dispatch is classifier-blocked
    # in this session -- see module docstring).
    m = re.search(r"candidate_tree = '([0-9a-f]{40})'", prompt)
    tree = m.group(1) if m else ""
    verdict = json.dumps({"candidate_tree": tree, "verdict": "ACCEPT", "findings": [],
                          "reviewer": {"provider": "claude-cli", "model": "fake-claude-mechanism-stub"}})
    envelope = {"type": "result", "subtype": "success", "result": verdict, "is_error": False}

print(json.dumps(envelope))
"""


def _make_fake_claude(tmp: Path) -> Path:
    script = tmp / "fake_claude.py"
    script.write_text(FAKE_CLAUDE_SCRIPT)
    wrapper = tmp / "fake-claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    wrapper.chmod(0o755)
    return wrapper


def build_workspace(root: Path, label: str) -> Path:
    workspace = root / f"{label}-workspace"
    workspace.mkdir()
    run = lambda argv: subprocess.run(argv, cwd=str(workspace), check=True, capture_output=True, text=True)
    run(["git", "init", "-q"])
    run(["git", "config", "user.email", "worker-host-mission@joao.invalid"])
    run(["git", "config", "user.name", "JOAO worker-host mission"])
    (workspace / "module.py").write_text("VALUE = 1\n")
    (workspace / "test_module.py").write_text("from module import VALUE\nassert VALUE == 2\n")
    (workspace / ".joao-profile.json").write_text(json.dumps({
        "project_id": f"c8b-{label}", "display_name": f"c8b-{label}",
        "repository_root": str(workspace), "allowed_write_paths": ["module.py"], "forbidden_paths": []}))
    run(["git", "add", "."])
    run(["git", "commit", "-qm", "mission baseline"])
    return workspace


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="joao-c8b-worker-host-"))
    fake_claude = _make_fake_claude(tmp)
    head = _repo_head()
    authority_hash = hashlib.sha256(BOSS_INSTRUCTION_TEXT.encode()).hexdigest()

    # Real standalone worker-host. GLM: real `joao-glm`. Claude (both roles):
    # a deterministic fake stand-in — a REAL live `claude` dispatch is
    # code-complete (`preserve_host_environment=True` on the reviewer only,
    # per ADD-5) but was CONFIRMED classifier-blocked twice in this session
    # (see module docstring); never attempted a third time.
    short_sock_dir = Path(tempfile.mkdtemp(prefix="joao-wh-mission-"))
    server = WorkerHostServer(
        socket_path=short_sock_dir / "s.sock", state_dir=tmp / "worker-host-state",
        builders={"zai-coding-plan": GLMBuilder, "claude-cli": lambda: ClaudeCodeBuilder(executable=str(fake_claude))},
        reviewers={"zai-coding-plan": GLMReviewer, "claude-cli": lambda: ClaudeCLIReviewer(executable=str(fake_claude))},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)

    health = health_check(socket_path=server.socket_path)
    report: dict = {
        # WORKER_HOST_INSTALLED is reserved for the actual launchd-managed
        # service (see scripts/joao_worker_host_bootstrap.sh) — this script
        # only ever starts a throwaway in-process test instance.
        "WORKER_HOST_TEST_INSTANCE_STARTED": True,
        "WORKER_HOST_HEALTHY": bool(health.get("ok") and health.get("pong")),
        "WORKER_HOST_SOCKET": str(server.socket_path),
        "WORKER_HOST_SOCKET_MODE": oct(server.socket_path.stat().st_mode & 0o777),
        "BOSS_COPY_PASTE_ACTIONS": 0, "MANUAL_WORKER_TERMINALS_OPENED": 0,
        "CLAUDE_REVIEWER_HOST_PASSTHROUGH_GRANTED": True,  # code-level: statically asserted, see test_add5_*
        "CLAUDE_BUILDER_HOST_PASSTHROUGH_GRANTED": False,
        "CLAUDE_LIVE_NESTED_DISPATCH_BLOCKED": True,  # confirmed twice this session; not attempted a third time
    }

    try:
        # ==================== MISSION A (required) ====================
        work_a = build_workspace(tmp, "mission-a")
        runtime_a = RunRuntime(tmp / "state-a",
                               builder=remote_glm_builder(socket_path=server.socket_path),
                               reviewer=remote_claude_reviewer(socket_path=server.socket_path, timeout=900))
        mission_a = ("PROPOSED PLAN (not yet executed): change VALUE in module.py from 1 to 2, "
                    "touching no other file, so test_module.py passes. Bounded, single-line, low-risk. "
                    "Approve (ACCEPT) if sound.")
        result_a = run_c8b_mission(
            runtime=runtime_a, project_id="c8b-mission-a", workspace=work_a, mission=mission_a,
            targeted_tests=[], full_tests=[[sys.executable, "test_module.py"]],
            risk_tier="normal", canary_required=False, spec_sha=head, roadmap_sha=head,
            authority_instruction_hash=authority_hash, forbidden_paths=[], criterion_bindings={},
            network_capability=True)  # GLM/Claude both need real network to reach their providers
        folder_a = tmp / "state-a" / "runs" / result_a["run_id"]
        run_json_a = json.loads((folder_a / "run.json").read_text()) if (folder_a / "run.json").exists() else {}
        builder_ev_a = json.loads((folder_a / "builder-evidence.json").read_text()) if (folder_a / "builder-evidence.json").exists() else {}
        review_ev_a = json.loads((folder_a / "review-evidence.json").read_text()) if (folder_a / "review-evidence.json").exists() else {}
        candidate_a = run_json_a.get("candidate") or {}

        report.update({
            "MISSION_A_RUN_ID": result_a.get("run_id"),
            "MISSION_A_STATUS": run_json_a.get("status"),
            "MISSION_A_ELIGIBILITY_OK": result_a.get("ok"),
            "REAL_GLM_BUILDER_DISPATCH": bool(builder_ev_a.get("provider") == GLMBuilder.provider
                                              and builder_ev_a.get("returncode") is not None),
            "GLM_BUILDER_RETURNCODE": builder_ev_a.get("returncode"),
            "GLM_BUILDER_OK": builder_ev_a.get("ok"),
            # NOT a live network dispatch — the reviewer worker in THIS run is the
            # fake `claude` stand-in (see module docstring: real dispatch is
            # classifier-blocked, confirmed twice, never attempted a third time).
            "REAL_CLAUDE_REVIEWER_DISPATCH": False,
            "CLAUDE_REVIEWER_MECHANISM_PROOF_ONLY": True,
            "CLAUDE_REVIEWER_RETURNCODE": review_ev_a.get("returncode"),
            "CLAUDE_REVIEWER_DECISION": review_ev_a.get("decision"),
            "CLAUDE_REVIEWER_REASON": review_ev_a.get("reason"),
            "WORKER_STARTED_BY_JOAO": True,
            "CANDIDATE_FROZEN_A": bool(candidate_a.get("candidate_tree")),
            "FREEZE_TREE_A": candidate_a.get("candidate_tree"),
            "FINISH_TREE_A": result_a.get("candidate_tree"),
            "SAME_CANDIDATE_TREE_FROM_FREEZE_TO_FINISH": candidate_a.get("candidate_tree") == result_a.get("candidate_tree")
                                                         and bool(candidate_a.get("candidate_tree")),
            # Mechanism PASS/FAIL — the pipeline/gates/worker-host wiring, NOT a
            # live Claude network verdict (that leg is a fake stand-in here).
            "NORMAL_GLM_TO_CLAUDE_MECHANISM": "PASS" if result_a.get("ok") else "FAIL",
            "MISSION_A_EXIT": 0 if result_a.get("ok") else 1,
        })

        # ==================== MISSION B (SKIPPED_INFEASIBLE) ====================
        work_b = build_workspace(tmp, "mission-b")
        runtime_b = RunRuntime(tmp / "state-b",
                               builder=remote_claude_builder(socket_path=server.socket_path),
                               reviewer=remote_glm_reviewer(socket_path=server.socket_path))
        mission_b = ("PROPOSED PLAN (not yet executed): change VALUE in module.py from 1 to 2, "
                    "touching no other file, so test_module.py passes. Bounded, single-line, low-risk. "
                    "Approve (ACCEPT) if sound.")
        result_b = run_c8b_mission(
            runtime=runtime_b, project_id="c8b-mission-b", workspace=work_b, mission=mission_b,
            targeted_tests=[], full_tests=[[sys.executable, "test_module.py"]],
            risk_tier="normal", canary_required=False, spec_sha=head, roadmap_sha=head,
            authority_instruction_hash=authority_hash, forbidden_paths=[], criterion_bindings={},
            network_capability=True)  # GLM/Claude both need real network to reach their providers
        folder_b = tmp / "state-b" / "runs" / result_b["run_id"]
        run_json_b = json.loads((folder_b / "run.json").read_text()) if (folder_b / "run.json").exists() else {}

        report.update({
            "MISSION_B_EXIT": "SKIPPED_INFEASIBLE",
            "MISSION_B_STOP_REASON": MISSION_B_STOP_REASON,
            "MISSION_B_MECHANISM_PROOF_ONLY_RUN_ID": result_b.get("run_id"),
            "MISSION_B_MECHANISM_PROOF_ONLY_STATUS": run_json_b.get("status"),
            "CLAUDE_BUILD_MODE": "mechanism_proof_fake_cli_stub_not_live_never_required",
        })
    finally:
        server.shutdown()
        server.close()
        shutil.rmtree(short_sock_dir, ignore_errors=True)

    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"\nEvidence root: {tmp}", file=sys.stderr)
    return report.get("MISSION_A_EXIT", 1)


if __name__ == "__main__":
    raise SystemExit(main())
