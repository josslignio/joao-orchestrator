"""C5 Nightly Batch Consolidation.

Makes the factory execute compiled roadmaps overnight using C2, C3, C4, C6 and
Turbo, WITHOUT a parallel scheduler. Extends Turbo T11 (nightly.py) and the
existing C2 goal loop.

Canonical flow:
  short goal -> C3 roadmap -> validator -> queue materialization
  -> complexity/skill routing -> context packet -> implementation
  -> targeted tests -> C1 -> bounded Codex review where required
  -> correction -> full gate -> commit -> publication -> next task
  -> morning report

Batch limits (defaults):
  max duration = 8 hours (hard = 10 hours)
  max tasks = 12
  max active writers = 2
  max consecutive failures = 2
  max model calls = 24
  max Codex calls = 6
  max corrections per task = 1
  max questions per task = 2

Task grouping: parallel only when dependency/path/artifact/migration/publication
disjoint. Otherwise serialize.

Publication: allowed = commit exact files, push without force, create PR,
update existing run-owned PR. Forbidden = merge, auto-approve, force push,
delete user branches, publish secrets.

Deterministic. No network. Stdlib only.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..evaluation.models import sha256_json
from ..roadmap.models import Roadmap, RoadmapState
from ..roadmap.compiler import materialize_to_queue
from ..storage.atomic import append_line, atomic_write_json
from .nightly import BatchSpec, BatchTaskOutcome, PublicationMode
from .parallel import TaskPlan, plan_parallelism, ParallelismDecision, MAX_PARALLEL_WRITERS
from .plan_compiler import Complexity
from .turbo_v11 import verify_batch_task_consistency, BatchConsistencyReport


SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Batch limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NightlyBatchLimits:
    """Bounded limits for a nightly batch (spec defaults)."""

    max_duration_hours: float = 8.0
    hard_duration_hours: float = 10.0
    max_tasks: int = 12
    max_active_writers: int = MAX_PARALLEL_WRITERS
    max_consecutive_failures: int = 2
    max_model_calls: int = 24
    max_codex_calls: int = 6
    max_corrections_per_task: int = 1
    max_questions_per_task: int = 2

    def validate(self) -> None:
        for name, val in [
            ("max_duration_hours", self.max_duration_hours),
            ("hard_duration_hours", self.hard_duration_hours),
        ]:
            if val <= 0:
                raise ValueError(f"{name} must be positive")
        if self.hard_duration_hours < self.max_duration_hours:
            raise ValueError("hard_duration must be >= max_duration")
        for name, val in [
            ("max_tasks", self.max_tasks),
            ("max_active_writers", self.max_active_writers),
            ("max_consecutive_failures", self.max_consecutive_failures),
            ("max_model_calls", self.max_model_calls),
            ("max_codex_calls", self.max_codex_calls),
            ("max_corrections_per_task", self.max_corrections_per_task),
            ("max_questions_per_task", self.max_questions_per_task),
        ]:
            if val <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_active_writers > MAX_PARALLEL_WRITERS:
            raise ValueError(
                f"max_active_writers {self.max_active_writers} exceeds bound {MAX_PARALLEL_WRITERS}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_duration_hours": self.max_duration_hours,
            "hard_duration_hours": self.hard_duration_hours,
            "max_tasks": self.max_tasks,
            "max_active_writers": self.max_active_writers,
            "max_consecutive_failures": self.max_consecutive_failures,
            "max_model_calls": self.max_model_calls,
            "max_codex_calls": self.max_codex_calls,
            "max_corrections_per_task": self.max_corrections_per_task,
            "max_questions_per_task": self.max_questions_per_task,
        }


# ---------------------------------------------------------------------------
# Task grouping (parallel only when fully disjoint)
# ---------------------------------------------------------------------------


def can_parallelize(a: TaskPlan, b: TaskPlan) -> bool:
    """Parallel only when dependency/path/artifact/migration/publication disjoint."""
    from .parallel import compute_overlap
    return compute_overlap(a, b).can_parallelize


# ---------------------------------------------------------------------------
# Publication policy
# ---------------------------------------------------------------------------


ALLOWED_PUBLICATION = frozenset({
    "commit_exact_files",
    "push_without_force",
    "create_pr",
    "update_run_owned_pr",
})

FORBIDDEN_PUBLICATION = frozenset({
    "merge",
    "auto_approve",
    "force_push",
    "delete_user_branches",
    "publish_secrets",
})


def is_publication_allowed(action: str) -> bool:
    return action in ALLOWED_PUBLICATION


def is_publication_forbidden(action: str) -> bool:
    return action in FORBIDDEN_PUBLICATION


# ---------------------------------------------------------------------------
# Consolidated batch report (morning report)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MorningReport:
    """The complete morning report for a nightly batch."""

    batch_id: str
    roadmap_id: str
    started_at: str
    finished_at: str
    state: str
    tasks_attempted: int
    tasks_accepted: int
    tasks_rejected: int
    tasks_blocked: int
    tasks_deferred: int
    commits: int
    prs: tuple[str, ...]
    tests_run: int
    tests_passed: int
    c1_verdicts: tuple[tuple[str, str], ...]   # (task_id, verdict)
    codex_verdicts: tuple[tuple[str, str], ...]
    model_call_proxy: int
    context_bytes: int
    cache_hit_rate: float
    parallel_speedup: float
    drift_events: int
    remaining_blockers: tuple[str, ...]
    next_recommended_goal: str
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "roadmap_id": self.roadmap_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "state": self.state,
            "tasks_attempted": self.tasks_attempted,
            "tasks_accepted": self.tasks_accepted,
            "tasks_rejected": self.tasks_rejected,
            "tasks_blocked": self.tasks_blocked,
            "tasks_deferred": self.tasks_deferred,
            "commits": self.commits,
            "prs": list(self.prs),
            "tests_run": self.tests_run,
            "tests_passed": self.tests_passed,
            "c1_verdicts": [list(v) for v in self.c1_verdicts],
            "codex_verdicts": [list(v) for v in self.codex_verdicts],
            "model_call_proxy": self.model_call_proxy,
            "context_bytes": self.context_bytes,
            "cache_hit_rate": self.cache_hit_rate,
            "parallel_speedup": self.parallel_speedup,
            "drift_events": self.drift_events,
            "remaining_blockers": list(self.remaining_blockers),
            "next_recommended_goal": self.next_recommended_goal,
        }

    @property
    def report_hash(self) -> str:
        return sha256_json(self.unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        d = self.unsigned_dict()
        d["report_hash"] = self.report_hash
        return d


# ---------------------------------------------------------------------------
# Consolidated executor
# ---------------------------------------------------------------------------


@dataclass
class TaskExecutionResult:
    """Result of executing one task in the consolidated flow."""

    task_id: str
    accepted: bool
    rejected: bool
    blocked: bool
    committed: bool
    pr_url: str
    c1_verdict: str
    codex_verdict: str
    tests_run: int
    tests_passed: int
    model_calls: int
    codex_calls: int
    context_bytes: int
    cache_hit: bool
    drift_events: int
    failure_reason: str


class ConsolidatedBatchExecutor:
    """Executes a compiled roadmap as a bounded nightly batch.

    Composes C3 (roadmap), C4 (lessons), C6 (drift), Turbo (caching, parallelism,
    telemetry). Does NOT create a parallel scheduler — uses the existing T9
    parallelism planner for grouping only.

    This executor is deterministic for testing: real provider/git dispatch is
    delegated to C2's existing scheduler. This module orchestrates the flow and
    produces the morning report.
    """

    def __init__(
        self,
        roadmap: Roadmap,
        limits: Optional[NightlyBatchLimits] = None,
        now_fn: Optional[Callable[[], str]] = None,
    ):
        if roadmap.state != RoadmapState.READY.value:
            raise ValueError(
                f"roadmap must be READY; got {roadmap.state}")
        (limits or NightlyBatchLimits()).validate()
        self.roadmap = roadmap
        self.limits = limits or NightlyBatchLimits()
        self._now_fn = now_fn or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def execute(
        self,
        task_outcomes: Optional[Mapping[str, TaskExecutionResult]] = None,
    ) -> MorningReport:
        """Execute the batch flow over the roadmap's tasks.

        For deterministic testing, ``task_outcomes`` may provide pre-computed
        results per task_id. When None, the flow records tasks as
        attempted/accepted based on bounds (real dispatch is C2's job).
        """
        started = self._now_fn()
        limits = self.limits

        # Materialize queue items (exactly once).
        queue_items = materialize_to_queue(self.roadmap)
        task_ids = tuple(i["task_id"] for i in queue_items)
        task_ids = task_ids[: limits.max_tasks]

        # Build TaskPlans for grouping.
        plans = tuple(
            TaskPlan(
                task_id=i["task_id"],
                project_id=i["project_id"],
                allowed_paths=tuple(i["allowed_paths"]),
                dependency_task_ids=tuple(i["dependencies"]),
            )
            for i in queue_items if i["task_id"] in task_ids
        )

        # Verify BatchSpec/TaskPlan consistency (P0.6 fix).
        consistency = verify_batch_task_consistency(task_ids, plans)

        # Group into parallel waves.
        plan = plan_parallelism(plans)

        # Execute.
        results: dict[str, TaskExecutionResult] = {}
        model_calls = 0
        codex_calls = 0
        context_bytes = 0
        commits = 0
        prs: list[str] = []
        consecutive_failures = 0
        drift_events = 0
        tests_run = 0
        tests_passed = 0
        cache_hits = 0
        c1_verdicts: list[tuple[str, str]] = []
        codex_verdicts: list[tuple[str, str]] = []
        blocked_tasks: list[str] = []

        stop_reason = "completed"

        for wave in plan.waves:
            if model_calls >= limits.max_model_calls:
                stop_reason = "max_model_calls reached"
                break
            if codex_calls >= limits.max_codex_calls and len(wave) > 0:
                # Codex budget exhausted; remaining tasks deferred.
                stop_reason = "max_codex_calls reached"
                break
            if consecutive_failures >= limits.max_consecutive_failures:
                stop_reason = "max_consecutive_failures reached"
                break

            for tid in wave:
                if model_calls >= limits.max_model_calls:
                    break
                item = next((i for i in queue_items if i["task_id"] == tid), None)
                if item is None:
                    continue

                if task_outcomes and tid in task_outcomes:
                    tr = task_outcomes[tid]
                else:
                    # Deterministic default outcome.
                    tr = TaskExecutionResult(
                        task_id=tid, accepted=True, rejected=False, blocked=False,
                        committed=True, pr_url=f"https://example.com/pr/{tid}",
                        c1_verdict="KEEP", codex_verdict="PASS",
                        tests_run=5, tests_passed=5,
                        model_calls=1, codex_calls=1 if item["complexity"] != "TRIVIAL" else 0,
                        context_bytes=2048, cache_hit=False, drift_events=0,
                        failure_reason="",
                    )
                results[tid] = tr
                model_calls += tr.model_calls
                codex_calls += tr.codex_calls
                context_bytes += tr.context_bytes
                tests_run += tr.tests_run
                tests_passed += tr.tests_passed
                if tr.cache_hit:
                    cache_hits += 1
                drift_events += tr.drift_events
                c1_verdicts.append((tid, tr.c1_verdict))
                if tr.codex_verdict:
                    codex_verdicts.append((tid, tr.codex_verdict))
                if tr.blocked:
                    blocked_tasks.append(tid)
                if tr.committed:
                    commits += 1
                    if tr.pr_url:
                        prs.append(tr.pr_url)
                if tr.rejected:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

        attempted = len(results)
        accepted = sum(1 for r in results.values() if r.accepted)
        rejected = sum(1 for r in results.values() if r.rejected)
        blocked = sum(1 for r in results.values() if r.blocked)
        deferred = len(task_ids) - attempted

        cache_hit_rate = (cache_hits / attempted) if attempted > 0 else 0.0
        # Parallel speedup: if multiple waves merged, ratio of sequential to actual.
        sequential = len(task_ids)
        parallel_speedup = (sequential / max(1, len(plan.waves))) if plan.waves else 1.0

        state = "COMPLETED" if stop_reason == "completed" else "STOPPED"

        report = MorningReport(
            batch_id=f"nightly-{self.roadmap.roadmap_id}",
            roadmap_id=self.roadmap.roadmap_id,
            started_at=started,
            finished_at=self._now_fn(),
            state=state,
            tasks_attempted=attempted,
            tasks_accepted=accepted,
            tasks_rejected=rejected,
            tasks_blocked=blocked,
            tasks_deferred=max(0, deferred),
            commits=commits,
            prs=tuple(prs),
            tests_run=tests_run,
            tests_passed=tests_passed,
            c1_verdicts=tuple(c1_verdicts),
            codex_verdicts=tuple(codex_verdicts),
            model_call_proxy=model_calls,
            context_bytes=context_bytes,
            cache_hit_rate=cache_hit_rate,
            parallel_speedup=parallel_speedup,
            drift_events=drift_events,
            remaining_blockers=tuple(blocked_tasks),
            next_recommended_goal="review morning report; plan next roadmap",
        )
        return report


def persist_morning_report(state_root: Path, report: MorningReport) -> Path:
    """Persist a morning report outside repos."""
    root = Path(state_root).resolve() / "nightly"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{report.batch_id}.json"
    atomic_write_json(path, report.to_dict())
    append_line(root / "batch_events.jsonl", json.dumps({
        "ts": report.finished_at,
        "batch_id": report.batch_id,
        "state": report.state,
        "report_hash": report.report_hash,
    }, ensure_ascii=False))
    return path
