"""V2 ``joss_v2`` CLI (§28).

Commands::

    status      — render project state + roadmap + next single action
    preflight   — run the §11 evidence-first preflight
    plan        — write a backward-planned objective contract (§10)
    resume      — verify drift + resume an interrupted run (§20)
    benchmark   — run the §26 historical fixtures + §27 sharpness score
    lessons     — list/manage the §24 learning ladder
    review      — run the §21 review gate (honest Codex fallback)
    genesis     — Product Genesis Engine: gate production coding behind an
                  explicitly human-accepted Product Blueprint (V2.4 PR-A)

All commands support ``--json`` and never print secrets. A blocker exits
nonzero with the exact blocker + resume command (§28).

Import path: ``python -m joao_orchestrator.cli.joss_v2`` requires ``src`` on
``sys.path`` (no package install, §5). The repo's existing scripts use
``sys.path.insert(0, ROOT/'src')``; this module does the same so it also works
as a plain script.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make `src` importable when run as a script (matches existing repo convention).
_HERE = Path(__file__).resolve()
_SRC = _HERE.parents[2]  # .../src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from joao_orchestrator.v2 import (  # noqa: E402
    profiles, fixtures, telemetry as tele,
)
from joao_orchestrator.v2.state import ProjectStateStore, StateValidationError  # noqa: E402
from joao_orchestrator.v2.roadmap import RoadmapEngine, Roadmap  # noqa: E402
from joao_orchestrator.v2.objective import ObjectivePlanner  # noqa: E402
from joao_orchestrator.v2.preflight import run_preflight, resolve_gh, default_runner  # noqa: E402
from joao_orchestrator.v2.resume import RunStore, verify_resume  # noqa: E402
from joao_orchestrator.v2.review import ReviewPacket, ReviewGate  # noqa: E402
from joao_orchestrator.v2.learning import LessonLifecycle  # noqa: E402
from joao_orchestrator.v2.genesis import (  # noqa: E402
    GenesisEngine, GenesisBlockError, GenesisError, ProductBlueprint,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# m3 closure: every git/subprocess invocation from the CLI routes through the
# single safe runner (list argv, shell=False, explicit cwd, bounded timeout).
# No CLI command calls subprocess.run directly. Timeouts are mandatory.
GIT_TIMEOUT = 15


def _git_capture(argv: list[str], *, cwd: Path | None = None,
                 timeout: int = GIT_TIMEOUT) -> str:
    """Run a read-only git command via the safe runner; return stdout.

    Uses :func:`preflight.default_runner` (list argv, ``shell=False``, timeout)
    so there is exactly one subprocess surface in V2. Never raises on non-zero;
    callers that need the exit code should call ``default_runner`` directly.
    """
    rc, out, _err = default_runner(["git", *argv], cwd, timeout)
    return out


def _emit(args, obj) -> None:
    if getattr(args, "json", False):
        print(json.dumps(obj, indent=2, sort_keys=True, default=str))
    else:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    print(f"{k}: {json.dumps(v, default=str)}")
                else:
                    print(f"{k}: {v}")
        else:
            print(obj)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    project = args.project
    try:
        store = ProjectStateStore(project)
        state = store.load()
    except StateValidationError as e:
        _emit(args, {"project_id": project, "blocker": str(e),
                     "resume_command": f"joss_v2 preflight --project {project}"})
        return 2
    out = {
        "project_id": project,
        "next_single_action": state.get("next_single_action"),
        "current_objective": state.get("current_objective"),
        "confidence": state.get("confidence"),
        "passed_gates": state.get("passed_gates", []),
        "blocked_gates": state.get("blocked_gates", []),
        "current_branch": state.get("current_branch"),
        "current_head": state.get("current_head"),
        "updated_at": state.get("updated_at"),
    }
    # If a roadmap exists, add its render + next gate.
    try:
        engine = RoadmapEngine.load(project)
        out["roadmap_next_gate"] = (engine.next_gate().gate_id
                                    if engine.next_gate() else None)
        out["roadmap_next_action"] = engine.next_single_action()
    except FileNotFoundError:
        pass
    _emit(args, out)
    return 0


def cmd_preflight(args) -> int:
    project = args.project
    repo = Path(args.repo) if args.repo else Path.cwd()
    # Look up the profile to find the dashboard URL (deterministic).
    url = None
    try:
        prof = profiles.load_profile(project)
        url = prof.dashboard_urls[0] if prof.dashboard_urls else None
    except KeyError:
        pass
    report = run_preflight(project, repo_path=repo, check_public_url=url)
    out = report.to_dict()
    out["resume_command"] = (
        f"joss_v2 status --project {project}" if report.ok
        else f"joss_v2 preflight --project {project}")
    _emit(args, out)
    return 0 if report.ok else 3  # nonzero blocker exit (§28)


def cmd_plan(args) -> int:
    project = args.project
    planner = ObjectivePlanner(project_id=project, run_id=args.run_id or "")
    chain = [s.strip() for s in args.backward_chain.split("<-")]
    contract = planner.plan_backward(
        requested_user_outcome=args.outcome,
        visible_acceptance_artifact=args.artifact,
        backward_chain=chain,
        single_objective=args.objective or args.outcome,
        acceptance_checks=(args.acceptance.split("|") if args.acceptance else []),
    )
    path = planner.save(contract)
    # Update project state with the single next action (§8 invariant).
    store = ProjectStateStore(project)
    state = store.load_or_init(repository_path=str(Path.cwd()))
    state["current_objective"] = contract.single_objective
    state["next_single_action"] = chain[-1] if chain else contract.single_objective
    store.write(state, reason="plan")
    _emit(args, {"objective_contract": str(path),
                 "next_single_action": state["next_single_action"]})
    return 0


def cmd_resume(args) -> int:
    run_store = RunStore(args.run_id)
    manifest = run_store.read_manifest()
    if manifest is None:
        _emit(args, {"run_id": args.run_id, "blocker": "no run_manifest found",
                     "resume_command": "start a fresh run"})
        return 2
    # Drift check uses current repo HEAD/branch (live evidence). Routed
    # through the single safe runner with a bounded timeout (m3).
    head = _git_capture(["rev-parse", "HEAD"], cwd=Path.cwd()).strip()
    branch = _git_capture(["rev-parse", "--abbrev-ref", "HEAD"],
                          cwd=Path.cwd()).strip()
    check = verify_resume(manifest, current_head=head, current_branch=branch)
    _emit(args, {"run_id": args.run_id,
                 "can_resume": check.can_resume,
                 "drift_detected": check.drift_detected,
                 "invalidated_gates": check.invalidated_gates,
                 "reason": check.reason,
                 "next_action": manifest.next_action or manifest.current_gate_id,
                 "resume_command": manifest.resume_command})
    return 0 if check.can_resume else 2


def cmd_benchmark(args) -> int:
    import tempfile
    from joao_orchestrator.v2 import benchmark as bench_mod
    tmp = Path(tempfile.mkdtemp(prefix="joss_v2_bench_"))
    summary = bench_mod.run_benchmark(tmp_root=tmp)
    out = summary.to_dict()
    _emit(args, out)
    total = len(out["fixture_results"])
    passed = sum(1 for r in out["fixture_results"].values()
                 if r["status"] == "PASS")
    return 0 if passed == total else 1


def cmd_lessons(args) -> int:
    lc = LessonLifecycle()
    if args.lessons_cmd == "list":
        lessons = lc.list_lessons(status=args.status)
        _emit(args, {"lessons": [l.to_dict() for l in lessons]})
        return 0
    _emit(args, {"error": f"unknown lessons subcommand: {args.lessons_cmd}"})
    return 2


def cmd_review(args) -> int:
    gate = ReviewGate(codex_available=False)  # honest fallback (§21)
    packet = ReviewPacket(
        acceptance_criteria=(args.acceptance.split("|") if args.acceptance else []),
        tests=(args.tests.split("|") if args.tests else []),
        limitations=["live Codex reviewer unavailable — deterministic self-review"])
    import glob
    # Generic core = everything under src/joao_orchestrator/v2 (the new code).
    generic = [Path(p) for p in glob.glob("src/joao_orchestrator/v2/**/*.py",
                                          recursive=True)]
    diff_files = generic  # for the unsafe-subprocess scan, new files = diff
    verdict = gate.review(packet, generic_core_files=generic,
                          diff_files=diff_files, all_files=generic)
    _emit(args, verdict.to_dict())
    return 0 if not verdict.blocks_pr else 4


def cmd_gh_discover(args) -> int:
    """Item 1: discover gh, recover GH_CONFIG_DIR, classify auth."""
    from joao_orchestrator.v2.gh_discovery import discover_gh_environment
    res = discover_gh_environment(persist=not args.no_persist)
    out = res.to_dict()
    out["resume_command"] = (
        "joss_v2 preflight --project weekly-trading-radar"
        if res.classification == "GH_AVAILABLE_AND_AUTHENTICATED"
        else "gh auth login  # then re-run joss_v2 gh-discover")
    _emit(args, out)
    return 0 if res.classification == "GH_AVAILABLE_AND_AUTHENTICATED" else 3


def cmd_review_packet(args) -> int:
    """Item 5: generate the full 11-file review packet."""
    import tempfile
    from pathlib import Path as _P
    from joao_orchestrator.v2.review import (
        ReviewGate, ReviewPacket, generate_review_packet, REQUIRED_PACKET_FILES)
    from joao_orchestrator.v2 import benchmark as bench_mod, telemetry

    run_id = args.run_id or "joss-v2-review"
    out_dir = _P.home() / ".local/share/joss-orchestrator/joss-v2-upgrade" \
        / run_id / "review_packet"

    # Gather diff + changed symbols deterministically via the safe runner (m3).
    diff = _git_capture(["diff", "main", "--no-color"], cwd=Path.cwd())
    names = _git_capture(["diff", "--name-only", "main"],
                         cwd=Path.cwd()).split()

    # Run the deterministic review.
    gate = ReviewGate(codex_available=False)
    import glob
    generic = [Path(p) for p in glob.glob("src/joao_orchestrator/v2/**/*.py",
                                          recursive=True)]
    packet = ReviewPacket(
        acceptance_criteria=["all 24 fixtures pass", "811 existing tests pass",
                             "generic core pure", "no unsafe subprocess"],
        tests=["scripts/test_joss_v2.py", "scripts/test_orchestrator_core.py"],
        limitations=["live Codex reviewer UNAVAILABLE — deterministic fallback"])
    verdict = gate.review(packet, generic_core_files=generic,
                          diff_files=generic, all_files=generic)

    # Benchmark + sharpness for the packet.
    tmp = Path(tempfile.mkdtemp(prefix="joss_v2_pkt_"))
    bm = bench_mod.run_benchmark(tmp_root=tmp)
    base_score, v2_score = telemetry.score_from_benchmark(
        bm.baseline_median, bm.v2_median)

    out_dir = generate_review_packet(
        out_dir, verdict=verdict,
        objective_contract={"single_objective": "V2 delivery engine upgrade",
                            "visible_acceptance_artifact": "24/24 fixtures + CLI"},
        final_diff=diff[:200000],   # bounded
        changed_symbols=names,
        acceptance_results=bm.fixture_results,
        benchmark_summary={"baseline_median": bm.baseline_median,
                           "v2_median": bm.v2_median,
                           "baseline_sharpness": base_score.to_dict(),
                           "v2_sharpness": v2_score.to_dict()},
        telemetry={"verdict": verdict.verdict, "checks": len(verdict.checks)},
        gate_ledger_lines=[],
        known_limitations=verdict.limitations + [
            "no speedup multiple claimed without measurement",
            "sharpness score is not a safety guarantee (§27)"],
    )
    written = sorted(p.name for p in out_dir.iterdir())
    missing = [f for f in REQUIRED_PACKET_FILES if f not in written]
    _emit(args, {"review_packet_dir": str(out_dir),
                 "files_written": written,
                 "missing": missing,
                 "verdict": verdict.verdict,
                 "baseline_sharpness": base_score.total,
                 "v2_sharpness": v2_score.total})
    return 0 if not missing else 4


# ---------------------------------------------------------------------------
# V2.4 PR-A — Product Genesis Engine
# ---------------------------------------------------------------------------

def cmd_genesis(args) -> int:
    """Product Genesis Engine: gate production coding behind a human-accepted
    Product Blueprint.

    Subcommands:
        status    — blueprint + gate + readiness status (0 accepted / 2 blocked)
        draft     — validate + persist a blueprint DRAFT (new version; BLOCKED)
        ready     — re-run the deterministic ambiguity gate over a draft
        accept    — OPERATOR-ONLY explicit human acceptance (requires NAME)
        versions  — list blueprint versions + accepted pointer
        rollback  — re-accept a previously-accepted version
        contract  — show the compiled product contract (drift check)
        guard     — hard check before production coding (exit 0/2)

    Note: ``accept`` is operator-only. An autonomous build may only reach
    BLUEPRINT_READY_FOR_HUMAN_REVIEW via ``draft``/``ready``; it must NOT call
    ``accept`` to self-attest its own blueprint.
    """
    project = args.project
    engine = GenesisEngine(project)

    if args.genesis_cmd == "status":
        _emit(args, engine.blueprint_status())
        return 0 if engine.can_code() else 2

    if args.genesis_cmd == "draft":
        blueprint_path = Path(args.blueprint)
        if not blueprint_path.exists():
            _emit(args, {"project_id": project,
                         "error": f"blueprint file not found: {blueprint_path}"})
            return 2
        data = json.loads(blueprint_path.read_text(encoding="utf-8"))
        bp = ProductBlueprint.from_dict(data)
        try:
            result = engine.draft_blueprint(bp)
        except ValueError as e:  # fail-closed validation
            _emit(args, {"project_id": project,
                         "error": f"blueprint rejected: {e}",
                         "complete": False})
            return 2
        _emit(args, result)
        return 0

    if args.genesis_cmd == "ready":
        report = engine.evaluate_readiness(
            getattr(args, "version", None))
        _emit(args, {"project_id": project, **report.to_dict()})
        return 0 if report.ready_for_human_review else 2

    if args.genesis_cmd == "accept":
        try:
            result = engine.accept_blueprint(
                accepted_by=args.accepted_by,
                source=getattr(args, "source", "operator-cli") or "operator-cli",
                approval_evidence_ref=getattr(args, "approval_evidence", "") or "",
                visually_reviewed=not getattr(args, "no_visual_review", False),
                note=getattr(args, "note", "") or "",
                version=getattr(args, "version", None),
            )
        except (GenesisError, ValueError) as e:
            _emit(args, {"project_id": project, "error": str(e),
                         "next_action": engine.next_action()})
            return 2
        _emit(args, result)
        return 0

    if args.genesis_cmd == "versions":
        latest = engine.latest_version()
        accepted = engine.accepted_version()
        versions = []
        for v in range(1, latest + 1):
            try:
                bp = engine.load_blueprint(v)
                versions.append({
                    "version": v,
                    "blueprint_id": bp.blueprint_id if bp else None,
                    "accepted": v == accepted,
                    "created_at": bp.created_at if bp else None,
                })
            except Exception:
                versions.append({"version": v, "error": "unloadable"})
        _emit(args, {"project_id": project,
                     "latest_version": latest,
                     "accepted_version": accepted,
                     "versions": versions})
        return 0

    if args.genesis_cmd == "rollback":
        try:
            result = engine.rollback_to_version(
                args.version, accepted_by=args.accepted_by,
                source=getattr(args, "source", "operator-cli") or "operator-cli",
                note=getattr(args, "note", "") or "")
        except (GenesisError, ValueError) as e:
            _emit(args, {"project_id": project, "error": str(e)})
            return 2
        _emit(args, result)
        return 0

    if args.genesis_cmd == "contract":
        if not engine.contract_path.exists():
            _emit(args, {"project_id": project,
                         "error": "no compiled contract (accept a blueprint first)",
                         "drift": False})
            return 2
        contract = json.loads(engine.contract_path.read_text(encoding="utf-8"))
        _emit(args, {"project_id": project,
                     "contract": contract,
                     "drift": engine.contract_drift(),
                     "fingerprint_consistent": engine.fingerprint_consistent()})
        return 0 if not engine.contract_drift() else 2

    if args.genesis_cmd == "guard":
        # The hard pre-coding guard. Exit 0 = free to code; 2 = blocked.
        try:
            engine.require_acceptance_for_coding()
        except GenesisBlockError as e:
            _emit(args, {"project_id": project, "can_code": False,
                         "blocker": str(e),
                         "next_action": engine.next_action()})
            return 2
        _emit(args, {"project_id": project, "can_code": True,
                     "next_action": engine.next_action()})
        return 0

    _emit(args, {"error": f"unknown genesis subcommand: {args.genesis_cmd}"})
    return 2


# ---------------------------------------------------------------------------
# V2.4 PR-A2 — Control Plane + Reviewer Bridge
# ---------------------------------------------------------------------------

def cmd_cp(args) -> int:
    """Control Plane: classify + route a proposed decision (D0/D1/H1/H2).

    Subcommands:
        submit  — submit a proposal JSON (classified + routed automatically)
        show    — show a proposal's final state
        queue   — list the decision queue
        status  — control-plane status (routine questions, batched count)
    """
    from joao_orchestrator.v2.control_plane import ControlPlane, Proposal
    project = args.project
    cp = ControlPlane(project)

    if args.cp_cmd == "submit":
        data = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
        prop = Proposal.from_dict(data)
        prop = cp.submit(prop)
        _emit(args, {"project_id": project,
                     "proposal_id": prop.proposal_id,
                     "decision_class": prop.decision_class,
                     "status": prop.status,
                     "resolution": prop.resolution})
        # H2 stops are the one allowed nonzero (block) exit; everything else 0.
        return 2 if prop.decision_class == "H2" else 0

    if args.cp_cmd == "show":
        prop = cp.load(args.proposal_id)
        if prop is None:
            _emit(args, {"project_id": project, "error": "not found"})
            return 2
        _emit(args, prop.to_dict())
        return 0

    if args.cp_cmd == "queue":
        _emit(args, {"project_id": project, "queue": cp.queue()})
        return 0

    if args.cp_cmd == "status":
        _emit(args, {"project_id": project,
                     "routine_questions_in_run": cp.routine_questions_in_run(),
                     "batched_count": len(cp.batched()),
                     "events": len(cp.events())})
        return 0

    _emit(args, {"error": f"unknown cp subcommand: {args.cp_cmd}"})
    return 2


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="joss_v2",
        description="JOSS V2 ultra-sharp delivery engine (§28)")
    p.add_argument("--json", action="store_true", help="emit JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Per-subcommand --json so the flag works both before AND after the verb.
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true", help="emit JSON")

    s = sub.add_parser("status", help="project state + next action",
                       parents=[json_parent])
    s.add_argument("--project", required=True)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("preflight", help="§11 evidence-first preflight",
                       parents=[json_parent])
    s.add_argument("--project", required=True)
    s.add_argument("--repo", default=None)
    s.set_defaults(func=cmd_preflight)

    s = sub.add_parser("plan", help="§10 backward-planned objective contract",
                       parents=[json_parent])
    s.add_argument("--project", required=True)
    s.add_argument("--objective", default="")
    s.add_argument("--outcome", required=True)
    s.add_argument("--artifact", required=True)
    s.add_argument("--backward-chain", required=True,
                   help="goal <- step <- step (goal first)")
    s.add_argument("--acceptance", default="")
    s.add_argument("--run-id", default="")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("resume", help="§20 resume an interrupted run")
    s.add_argument("--run-id", required=True)
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("benchmark", help="§26 fixtures + §27 sharpness",
                       parents=[json_parent])
    s.add_argument("--project", default="")
    s.set_defaults(func=cmd_benchmark)

    s = sub.add_parser("lessons", help="§24 learning ladder",
                       parents=[json_parent])
    s.add_argument("lessons_cmd", choices=["list"])
    s.add_argument("--status", default=None)
    s.set_defaults(func=cmd_lessons)

    s = sub.add_parser("review", help="§21 review gate",
                       parents=[json_parent])
    s.add_argument("--acceptance", default="")
    s.add_argument("--tests", default="")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("gh-discover", help="item 1: gh + GH_CONFIG_DIR discovery",
                       parents=[json_parent])
    s.add_argument("--no-persist", action="store_true",
                   help="do not persist paths to known-good registry")
    s.set_defaults(func=cmd_gh_discover)

    s = sub.add_parser("review-packet", help="item 5: generate 11-file review packet",
                       parents=[json_parent])
    s.add_argument("--run-id", default="joss-v2-review")
    s.set_defaults(func=cmd_review_packet)

    # -- V2.4 PR-A: Product Genesis Engine --------------------------------
    s = sub.add_parser("genesis",
                       help="Product Genesis Engine (V2.4 PR-A)",
                       parents=[json_parent])
    gen_sub = s.add_subparsers(dest="genesis_cmd", required=True)

    g = gen_sub.add_parser("status", help="blueprint + gate + readiness status",
                           parents=[json_parent])
    g.add_argument("--project", required=True)
    g.set_defaults(func=cmd_genesis)

    g = gen_sub.add_parser("draft", help="validate + persist a blueprint draft",
                           parents=[json_parent])
    g.add_argument("--project", required=True)
    g.add_argument("--blueprint", required=True,
                   help="path to a Product Blueprint JSON file")
    g.set_defaults(func=cmd_genesis)

    g = gen_sub.add_parser("ready",
                           help="re-run the deterministic ambiguity gate",
                           parents=[json_parent])
    g.add_argument("--project", required=True)
    g.add_argument("--version", type=int, default=None)
    g.set_defaults(func=cmd_genesis)

    g = gen_sub.add_parser("accept",
                           help="OPERATOR-ONLY explicit human acceptance",
                           parents=[json_parent])
    g.add_argument("--project", required=True)
    g.add_argument("--accepted-by", required=True,
                   help="human attester name (recorded immutably in the ledger)")
    g.add_argument("--source", default="operator-cli",
                   help="acceptance source (operator-cli|operator-ui|...)")
    g.add_argument("--approval-evidence", default="",
                   help="optional reference to approval evidence")
    g.add_argument("--no-visual-review", action="store_true",
                   help="refuse (autonomous builds must not self-attest review)")
    g.add_argument("--note", default="")
    g.add_argument("--version", type=int, default=None)
    g.set_defaults(func=cmd_genesis)

    g = gen_sub.add_parser("versions", help="list blueprint versions",
                           parents=[json_parent])
    g.add_argument("--project", required=True)
    g.set_defaults(func=cmd_genesis)

    g = gen_sub.add_parser("rollback",
                           help="re-accept a previously-accepted version",
                           parents=[json_parent])
    g.add_argument("--project", required=True)
    g.add_argument("--version", type=int, required=True)
    g.add_argument("--accepted-by", required=True)
    g.add_argument("--source", default="operator-cli")
    g.add_argument("--note", default="")
    g.set_defaults(func=cmd_genesis)

    g = gen_sub.add_parser("contract",
                           help="show compiled contract + drift check",
                           parents=[json_parent])
    g.add_argument("--project", required=True)
    g.set_defaults(func=cmd_genesis)

    g = gen_sub.add_parser("guard",
                           help="hard pre-coding gate (exit 0/2)",
                           parents=[json_parent])
    g.add_argument("--project", required=True)
    g.set_defaults(func=cmd_genesis)

    # -- V2.4 PR-A2: Control Plane ---------------------------------------
    s = sub.add_parser("cp", help="Control Plane + Reviewer Bridge (V2.4 PR-A2)",
                       parents=[json_parent])
    cp_sub = s.add_subparsers(dest="cp_cmd", required=True)

    c = cp_sub.add_parser("submit", help="classify + route a proposal",
                          parents=[json_parent])
    c.add_argument("--project", required=True)
    c.add_argument("--proposal", required=True, help="path to a Proposal JSON")
    c.set_defaults(func=cmd_cp)

    c = cp_sub.add_parser("show", help="show a proposal's final state",
                          parents=[json_parent])
    c.add_argument("--project", required=True)
    c.add_argument("--proposal-id", required=True)
    c.set_defaults(func=cmd_cp)

    c = cp_sub.add_parser("queue", help="list the decision queue",
                          parents=[json_parent])
    c.add_argument("--project", required=True)
    c.set_defaults(func=cmd_cp)

    c = cp_sub.add_parser("status", help="control-plane status",
                          parents=[json_parent])
    c.add_argument("--project", required=True)
    c.set_defaults(func=cmd_cp)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
