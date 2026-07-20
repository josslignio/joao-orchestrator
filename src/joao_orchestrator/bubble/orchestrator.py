"""C8-B thin orchestrator (`JOAO_C8_GATES_ROADMAP.md` LOT C8-B item 3,
`JOAO_WORKER_INTEGRATION_SPEC.md` §2.2).

Sequences the EXISTING `RunRuntime` primitives — never reimplements the
engine, never duplicates `ExecutionBackend`, never adds a second dispatch
path, never adds a new `RunStatus`:

    Boss GO (authority hash present — refuses to act without one, D5)
      -> RunRuntime.start(...)        [existing — writes frozen_mission.json]
      -> RunRuntime.run_once(...)     [existing — build, tests, freeze, PRIMARY review]
      -> if risk_tier == critical: dispatch a SECOND, independent reviewer
         directly (RunRuntime itself only carries one `self.reviewer` slot)
      -> gate_dbl_audit(...) mechanically, over every collected verdict
      -> write c8b-eligibility.json — a MECHANICAL artefact bound to the
         exact candidate_tree, not a new RunStatus, not a promotion-readiness
         claim (that name/artefact is C8-C scope, gated on a canary this lot
         never runs)
      -> [C8-B STOPS HERE] — never calls approve()/promote(), never
         fabricates Boss approval, never runs a canary.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..storage.atomic import atomic_write_json
from . import gates as gates_mod
from .runtime import ReviewerAdapter, RunRuntime, RuntimeStateError, now


class OrchestratorError(RuntimeError):
    pass


def _eligibility_block(run_id: str | None, reason_code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False, "run_id": run_id, "reason_code": reason_code, "reason": reason,
        "boss_approval_status": "not_requested", "promotion_status": "not_attempted",
        **extra,
    }


def run_c8b_mission(*, runtime: RunRuntime, project_id: str, workspace: Path, mission: str,
                    targeted_tests: list[list[str]], full_tests: list[list[str]],
                    risk_tier: str, canary_required: bool, spec_sha: str, roadmap_sha: str,
                    authority_instruction_hash: str, forbidden_paths: list[str],
                    criterion_bindings: dict[str, Any], second_reviewer: ReviewerAdapter | None = None,
                    profile=None, tags: list[str] | None = None) -> dict[str, Any]:
    """Run one bounded C8-B mission through to mechanical eligibility, then
    stop. Never calls `runtime.approve()`/`runtime.promote()` — a Boss
    approval and any promotion/canary step are explicitly C8-C+ scope this
    function refuses to reach.

    Returns the `c8b-eligibility.json` payload (also persisted to
    `<state_root>/runs/<run_id>/c8b-eligibility.json`).
    """
    # D5 (WORKER §3): the controller refuses to act without a hashed
    # authority reference — a Boss GO is a named, datable instruction, never
    # an implicit default.
    for field_name, value in (("spec_sha", spec_sha), ("roadmap_sha", roadmap_sha),
                              ("authority_instruction_hash", authority_instruction_hash)):
        if not isinstance(value, str) or not value.strip():
            return _eligibility_block(None, "C8B_MISSING_AUTHORITY",
                                      f"{field_name} is missing/empty — the controller refuses to act "
                                      "without a hashed Boss GO authority reference")

    if risk_tier not in ("normal", "critical"):
        return _eligibility_block(None, "C8B_INVALID_RISK_TIER",
                                  f"risk_tier must be 'normal' or 'critical', got {risk_tier!r}")

    if risk_tier == "critical" and second_reviewer is None:
        return _eligibility_block(None, "C8B_MISSING_SECOND_REVIEWER",
                                  "critical tier requires a second, independently-dispatched reviewer — "
                                  "none was supplied")

    try:
        run_id = runtime.start(
            project_id=project_id, workspace=workspace, mission=mission,
            targeted_tests=targeted_tests, full_tests=full_tests,
            critical=(risk_tier == "critical"), tags=tags,
            risk_tier=risk_tier, canary_required=canary_required,
            spec_sha=spec_sha, roadmap_sha=roadmap_sha,
            authority_instruction_hash=authority_instruction_hash,
            forbidden_paths=forbidden_paths, criterion_bindings=criterion_bindings,
            profile=profile,
        )
    except RuntimeStateError as exc:
        return _eligibility_block(None, "C8B_START_REFUSED", str(exc))

    folder = runtime.root / "runs" / run_id
    run = runtime.run_once(run_id)

    if run["status"] != "needs_approval":
        # RunRuntime's own fail-closed pipeline (builder dispatch, scope
        # checks, tests, primary review) already reached a terminal
        # non-approval state — mechanically reflect that, fabricate nothing.
        eligibility = _eligibility_block(
            run_id, "C8B_PIPELINE_NOT_ELIGIBLE",
            f"run did not reach needs_approval (status={run['status']!r}) — builder/tests/primary "
            "review did not all succeed",
            status=run["status"], risk_tier=risk_tier)
        atomic_write_json(folder / "c8b-eligibility.json", eligibility)
        return eligibility

    candidate = run["candidate"]
    candidate_tree = run["candidate_tree"]

    review_path = folder / "review-evidence.json"
    primary_review = _read_json(review_path)
    primary_proof = (primary_review or {}).get("proof") or {}
    primary_verdict = {
        "provider": primary_proof.get("reviewer", {}).get("provider", runtime.reviewer.provider),
        "provider_family": runtime.reviewer.provider_family,
        "model": primary_proof.get("reviewer", {}).get("model", runtime.reviewer.model),
        "ok": bool((primary_review or {}).get("ok")),
        "decision": (primary_review or {}).get("decision", "block"),
        "candidate_tree": primary_proof.get("candidate_tree"),
    }
    verdicts = [primary_verdict]

    second_verdict_raw = None
    if risk_tier == "critical":
        try:
            if hasattr(second_reviewer, "review_stage"):
                second_verdict_raw = second_reviewer.review_stage(run, folder, "final", active_rules="")
            else:
                second_verdict_raw = second_reviewer.review(run, folder)
        except Exception as exc:  # a reviewer that raises must fail-closed, never strand the mission
            second_verdict_raw = {"ok": False, "decision": "block",
                                  "reason": f"second reviewer raised: {type(exc).__name__}: {exc}"}
        atomic_write_json(folder / "second-review-evidence.json", second_verdict_raw)
        second_proof = (second_verdict_raw or {}).get("proof") or {}
        verdicts.append({
            "provider": second_proof.get("reviewer", {}).get("provider", getattr(second_reviewer, "provider", None)),
            "provider_family": getattr(second_reviewer, "provider_family", "unknown"),
            "model": second_proof.get("reviewer", {}).get("model", getattr(second_reviewer, "model", None)),
            "ok": bool((second_verdict_raw or {}).get("ok")),
            "decision": (second_verdict_raw or {}).get("decision", "block"),
            "candidate_tree": second_proof.get("candidate_tree"),
        })

    dbl_audit = gates_mod.gate_dbl_audit(
        risk_tier=risk_tier, builder_provider=runtime.builder.provider,
        builder_family=runtime.builder.provider_family, reviewer_verdicts=verdicts,
        candidate_tree=candidate_tree)

    eligibility = {
        "ok": bool(dbl_audit["ok"]), "run_id": run_id, "risk_tier": risk_tier,
        "candidate_tree": candidate_tree, "candidate_commit": candidate.get("candidate_commit"),
        "builder_provider": runtime.builder.provider, "builder_provider_family": runtime.builder.provider_family,
        "reviewer_verdicts": verdicts, "dbl_audit": dbl_audit,
        "reason_code": dbl_audit["reason_code"], "reason": dbl_audit["reason"],
        "generated_at": now(),
        # Explicit, never-fabricated statement of C8-B's actual scope boundary —
        # this lot STOPS here; canary and promotion are C8-C+.
        "boss_approval_status": "not_requested", "promotion_status": "not_attempted",
        "canary_status": "not_run (C8-C scope)",
    }
    atomic_write_json(folder / "c8b-eligibility.json", eligibility)
    return eligibility


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    import json
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
