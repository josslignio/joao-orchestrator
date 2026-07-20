#!/usr/bin/env python3
"""joao — the canonical JOÃO.AI CLI.

This is the canonical entry point. The legacy aliases ``joss`` and ``joss_v2``
call into this implementation and emit deprecation output.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (src/joao_orchestrator/cli/joao.py -> root)
sys.path.insert(0, str(ROOT / "src"))

from joao_orchestrator.v2.autonomy import classify_current_state  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="joao", description="JOÃO.AI canonical CLI")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("autonomy", help="print the current autonomy classification")
    sub.add_parser("version", help="print the product version + identity")
    ui = sub.add_parser("ui", help="start the local-only JOAO bubble")
    ui.add_argument("--state-root", default="~/.local/share/joao")
    ui.add_argument("--no-open", action="store_true", help="do not open the local browser automatically")

    mission = sub.add_parser("mission", help="C8-B: run a bounded mission through joao-worker-host")
    mission_sub = mission.add_subparsers(dest="mission_cmd")
    mission_run = mission_sub.add_parser("run", help="run one C8-B mission spec file to mechanical eligibility")
    mission_run.add_argument("mission_file", help="path to a JSON mission spec (see docs/JOAO_C8_GATES_ROADMAP.md)")
    mission_run.add_argument("--state-root", default="~/.local/share/joao/c8b-missions")
    mission_run.add_argument("--socket-path", default=None,
                             help="joao-worker-host Unix socket (defaults to the standard path)")

    args = parser.parse_args(argv)
    if args.cmd == "autonomy":
        c = classify_current_state()
        print(json.dumps(c.to_dict(), indent=2))
        return 0
    if args.cmd == "version":
        # M0 safe-stop (D-043): the release stage is stated honestly, never inferred from a
        # test count or a run report — see SYSTEM_CONSTITUTION_V4.md §4, checklist ROADMAP_V4.md.
        print("JOÃO.AI joao-orchestrator (canonical); technical_id=joao; release_stage=ALPHA")
        return 0
    if args.cmd == "ui":
        from joao_orchestrator.bubble.api import LocalAPIServer
        from joao_orchestrator.bubble.runtime import RunRuntime
        from joao_orchestrator.worker_host.proxies import remote_claude_reviewer, remote_glm_builder
        state_root = Path(args.state_root).expanduser()
        # Boss architecture decision (2026-07-20/21): the normal UI execution
        # path talks ONLY to the standalone joao-worker-host (never an
        # in-process CascadeBuilder/CodexCLIReviewer dispatch) — GLMBuilder
        # -> ClaudeCLIReviewer is the active zero-cost autonomous topology
        # (`JOAO_C8_GATES_ROADMAP.md` "Statut C8-B"). An unreachable
        # worker-host is a controlled BLOCK on the next mission dispatch
        # (`RemoteBuilderProxy`/`RemoteReviewerProxy` never fall back to a
        # local/legacy path), never a silent substitution.
        server = LocalAPIServer(RunRuntime(state_root, builder=remote_glm_builder(),
                                           reviewer=remote_claude_reviewer(),
                                           enforce_phase0=True, projects_root=state_root / "projects",
                                           ledger_sync=True))  # B-37: brain re-synced from the ledger at launch
        print(server.url)
        if not args.no_open:
            webbrowser.open(server.url, new=2)
        try: server.server.serve_forever()
        except KeyboardInterrupt: server.close()
        return 0
    if args.cmd == "mission":
        if getattr(args, "mission_cmd", None) != "run":
            parser.print_help()
            return 0
        return _mission_run(Path(args.mission_file), Path(args.state_root).expanduser(), args.socket_path)
    parser.print_help()
    return 0


def _mission_run(mission_file: Path, state_root: Path, socket_path: str | None) -> int:
    """C8-B: `joao mission run <mission-file>` — the one Boss-facing command
    (Boss architecture decision, 2026-07-20). Talks ONLY to the standalone
    `joao-worker-host` over its Unix socket (`RemoteBuilderProxy`/
    `RemoteReviewerProxy` — never an in-process builder/reviewer dispatch);
    an unreachable worker-host is a controlled BLOCK, never a silent
    fallback. Never runs any mission on its own — a mission spec must be
    handed to it explicitly."""
    from joao_orchestrator.bubble.orchestrator import run_c8b_mission
    from joao_orchestrator.bubble.runtime import RunRuntime
    from joao_orchestrator.bubble.worker_topology import select_normal_reviewer_family
    from joao_orchestrator.worker_host.client import health_check
    from joao_orchestrator.worker_host.proxies import (
        remote_claude_builder, remote_claude_reviewer, remote_glm_builder, remote_glm_reviewer,
    )

    sock = Path(socket_path).expanduser() if socket_path else None
    health = health_check(socket_path=sock)
    if not (health.get("ok") and health.get("pong")):
        print(json.dumps({"ok": False, "reason_code": "WORKER_HOST_UNAVAILABLE",
                          "reason": "joao-worker-host is not reachable — run "
                                    "`scripts/joao_worker_host_bootstrap.sh install` first",
                          "detail": health}, indent=2))
        return 1

    spec = json.loads(mission_file.read_text())
    builder_worker = spec.get("builder_worker", "zai-coding-plan")
    reviewer_worker = spec.get("reviewer_worker")
    family_of = {"zai-coding-plan": "zai", "claude-cli": "anthropic"}
    if not reviewer_worker:
        selection = select_normal_reviewer_family(family_of.get(builder_worker, ""))
        if not selection.get("ok"):
            print(json.dumps({"ok": False, **selection}, indent=2))
            return 1
        reviewer_worker = {"zai": "zai-coding-plan", "anthropic": "claude-cli"}[selection["reviewer_family"]]

    builder_factories = {"zai-coding-plan": remote_glm_builder, "claude-cli": remote_claude_builder}
    reviewer_factories = {"zai-coding-plan": remote_glm_reviewer, "claude-cli": remote_claude_reviewer}
    if builder_worker not in builder_factories or reviewer_worker not in reviewer_factories:
        print(json.dumps({"ok": False, "reason_code": "C8B_UNKNOWN_WORKER",
                          "reason": f"builder_worker={builder_worker!r} / "
                                    f"reviewer_worker={reviewer_worker!r} — expected one of "
                                    f"{sorted(builder_factories)}"}, indent=2))
        return 1

    runtime = RunRuntime(state_root, builder=builder_factories[builder_worker](socket_path=sock),
                         reviewer=reviewer_factories[reviewer_worker](socket_path=sock))
    result = run_c8b_mission(
        runtime=runtime, project_id=spec["project_id"], workspace=Path(spec["workspace"]).expanduser(),
        mission=spec["mission"], targeted_tests=spec.get("targeted_tests", []),
        full_tests=spec["full_tests"], risk_tier=spec.get("risk_tier", "normal"),
        canary_required=spec.get("canary_required", False), spec_sha=spec["spec_sha"],
        roadmap_sha=spec["roadmap_sha"], authority_instruction_hash=spec["authority_instruction_hash"],
        forbidden_paths=spec.get("forbidden_paths", []), criterion_bindings=spec.get("criterion_bindings", {}))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
