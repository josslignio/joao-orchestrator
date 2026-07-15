"""V2 resume + recovery + run manifest (§20).

Adds a unified ``run_manifest.json`` on top of the EXISTING resume machinery
(:mod:`joao_orchestrator.runtime.drift` ResumePoint/resume_hash,
:mod:`joao_orchestrator.runtime.reconciler`).

A run manifest is the single per-run record that lets V2 resume *exactly* after
interruption (§0) and prevents "interrupted daily runs restarting from zero"
(Job risk #5). On resume (§20):

1. load manifest;
2. verify drift (HEAD/branch unchanged, artifacts present);
3. invalidate ONLY evidence affected by drift (not all gates);
4. resume the incomplete gate;
5. never restart without proof state is invalid.

Run artifacts (§20):

    run_manifest.json objective_contract.json progress.json
    command_log.jsonl  file_access_log.jsonl   gate_results.jsonl
    telemetry.json     resume.json
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..storage.atomic import atomic_write_json, append_line  # reuse
from . import V2_SCHEMA_VERSION
from .state import JOSS_ROOT

RUNS_ROOT = JOSS_ROOT / "runs"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_dir(run_id: str) -> Path:
    d = RUNS_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Run manifest
# ---------------------------------------------------------------------------

@dataclass
class RunManifest:
    run_id: str
    project_id: str
    repository_path: str
    head: str
    branch: str
    started_at: str = ""
    finished_at: str = ""
    status: str = "RUNNING"   # RUNNING | COMPLETED | INTERRUPTED | BLOCKED
    current_gate_id: str = ""
    passed_gates: list[str] = field(default_factory=list)
    failed_gate_id: str = ""
    last_command: str = ""
    last_command_rc: int = 0
    open_pr_number: int = 0
    next_action: str = ""
    resume_command: str = ""
    state_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = V2_SCHEMA_VERSION
        return d


class RunStore:
    """Persists the §20 per-run artifacts (manifest + logs + telemetry)."""

    FILES = {
        "manifest": "run_manifest.json",
        "command_log": "command_log.jsonl",
        "file_access_log": "file_access_log.jsonl",
        "gate_results": "gate_results.jsonl",
        "telemetry": "telemetry.json",
        "resume": "resume.json",
    }

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.dir = run_dir(run_id)

    def manifest_path(self) -> Path:
        return self.dir / self.FILES["manifest"]

    def write_manifest(self, m: RunManifest) -> None:
        atomic_write_json(self.manifest_path(), m.to_dict())

    def read_manifest(self) -> RunManifest | None:
        p = self.manifest_path()
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        return RunManifest(**{k: data[k] for k in RunManifest.__dataclass_fields__
                              if k in data})

    # -- append-only logs -------------------------------------------------
    def log_command(self, argv: list[str], rc: int, duration_s: float) -> None:
        append_line(self.dir / self.FILES["command_log"], json.dumps({
            "argv": argv, "rc": rc, "duration_s": round(duration_s, 3),
            "ts": _utcnow()}, sort_keys=True))

    def log_file_access(self, path: str, op: str) -> None:
        append_line(self.dir / self.FILES["file_access_log"], json.dumps({
            "path": path, "op": op, "ts": _utcnow()}, sort_keys=True))

    def log_gate_result(self, gate_id: str, status: str,
                        evidence: list[str]) -> None:
        append_line(self.dir / self.FILES["gate_results"], json.dumps({
            "gate_id": gate_id, "status": status, "evidence": evidence,
            "ts": _utcnow()}, sort_keys=True))

    def write_telemetry(self, telemetry: Mapping[str, Any]) -> None:
        atomic_write_json(self.dir / self.FILES["telemetry"], dict(telemetry))


# ---------------------------------------------------------------------------
# Resume / drift verification
# ---------------------------------------------------------------------------

@dataclass
class ResumeCheck:
    """Outcome of a §20 resume drift check."""
    can_resume: bool
    drift_detected: bool = False
    invalidated_gates: list[str] = field(default_factory=list)
    reason: str = ""
    next_single_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


def verify_resume(
    manifest: RunManifest, *, current_head: str, current_branch: str,
    artifact_paths_present: list[Path] | None = None,
    is_descendant: Callable[[str, str], bool] | None = None,
) -> ResumeCheck:
    """§20 resume rules. Never restart without proof state is invalid.

    Drift = HEAD moved BACKWARDS or to an UNRELATED commit, branch changed, or a
    previously-produced artifact is gone. An ADVANCING HEAD (current_head is a
    descendant of the recorded manifest.head — i.e. new commits landed on the
    same branch during the interruption) is explicitly NOT drift (m5 closure:
    the previous implementation flagged any head change as drift, contradicting
    its own docstring).

    ``is_descendant(ancestor_sha, descendant_sha) -> bool`` lets callers supply
    a real git ancestry check (e.g. ``git merge-base --is-ancestor``). When not
    provided, a conservative prefix-length heuristic is used: a longer sha with
    the recorded sha as its prefix is treated as an advance; anything else with
    a different sha is treated as drift (fail-safe toward re-verification).
    """
    drift = False
    reasons: list[str] = []
    if manifest.head and current_head and manifest.head != current_head:
        if _head_advanced(manifest.head, current_head, is_descendant):
            # Advancing HEAD on the same line of development is expected and
            # fine — NOT drift.
            pass
        else:
            drift = True
            reasons.append(
                f"HEAD drifted: {manifest.head[:8]} -> {current_head[:8]}")
    if manifest.branch and current_branch and manifest.branch != current_branch:
        drift = True
        reasons.append(f"branch changed: {manifest.branch} -> {current_branch}")
    for ap in (artifact_paths_present or []):
        if not Path(ap).exists():
            drift = True
            reasons.append(f"artifact missing: {ap}")

    if not drift:
        action = (manifest.next_action or manifest.current_gate_id
                  or "resume the incomplete gate")
        return ResumeCheck(can_resume=True, drift_detected=False,
                           reason="no drift; resume incomplete gate",
                           next_single_action=action)
    return ResumeCheck(
        can_resume=True, drift_detected=True,
        invalidated_gates=[],
        reason="drift detected — re-verify affected gates only (§20.4): "
               + "; ".join(reasons),
        next_single_action=(
            "re-verify gates whose evidence depended on drifted artifacts, "
            "then resume the incomplete gate (do NOT restart from zero)"))


def _head_advanced(
    recorded: str, current: str,
    is_descendant: Callable[[str, str], bool] | None,
) -> bool:
    """Return True iff ``current`` is an advance on ``recorded`` (not drift)."""
    if is_descendant is not None:
        try:
            return bool(is_descendant(recorded, current))
        except Exception:  # noqa: BLE001 — fall back to heuristic on any error
            pass
    # Conservative heuristic: same-branch advance typically yields a longer
    # sha that still starts with the recorded (possibly short) sha. Anything
    # that does not share the prefix is treated as unrelated → drift.
    short = recorded[: min(len(recorded), 7)]
    return current.startswith(short) and len(current) >= len(recorded)


def verify_resume_with_pr(
    manifest: RunManifest, *, current_head: str, current_branch: str,
    current_pr_state: str,  # "OPEN" | "MERGED" | "CLOSED" | "NONE"
    artifact_paths_present: list[Path] | None = None,
) -> ResumeCheck:
    """Resume check that also handles 'PR becomes merged during interruption'.

    If the PR was OPEN when the run was interrupted and is now MERGED, that is
    NOT drift to block on — it is a *completion*. The run's publication gate
    passes; passed gates are preserved; only the publication-evidence gate is
    updated. The run must NOT restart from zero (item 6).

    Returns exactly one ``next_single_action``.
    """
    base = verify_resume(manifest, current_head=current_head,
                         current_branch=current_branch,
                         artifact_paths_present=artifact_paths_present)

    # The merged-PR case: the run's objective was to ship the PR; it shipped.
    if (manifest.open_pr_number and current_pr_state == "MERGED"):
        return ResumeCheck(
            can_resume=True,
            drift_detected=base.drift_detected,  # HEAD on main is expected post-merge
            invalidated_gates=[],
            reason=(f"PR #{manifest.open_pr_number} merged during interruption — "
                    "publication gate satisfied; passed gates preserved"),
            next_single_action=(
                "mark the publication gate PASSED (PR merged) and complete the run"))

    # If the PR was closed WITHOUT merge (e.g. force-rejected), surface it.
    if manifest.open_pr_number and current_pr_state == "CLOSED":
        return ResumeCheck(
            can_resume=False, drift_detected=True,
            reason=f"PR #{manifest.open_pr_number} closed without merge",
            next_single_action=(
                "investigate why the PR was closed; do not auto-recreate it"))

    return base


__all__ = [
    "RUNS_ROOT", "run_dir", "RunManifest", "RunStore", "ResumeCheck",
    "verify_resume", "verify_resume_with_pr",
]
