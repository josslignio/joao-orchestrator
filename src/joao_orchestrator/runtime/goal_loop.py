"""Bounded goal execution loop (C2).

A deterministic controller that pursues a bounded objective through an ordered
chain of tasks while reusing existing JOSS primitives for persistence and
integrity. The loop is provider- and network-free: the actual task execution
and C1 evaluation are supplied as injected callables, so the controller itself
is fully deterministic and unit-testable with fakes.

Required behavior (see runbook CHECKPOINT A):

* deterministic dependency order;
* only one writer per worktree;
* no task starts before its dependency is accepted;
* a rejected C1 comparison blocks the dependent task;
* repeated failure pauses the goal;
* elapsed-time budget is enforced;
* every transition is persisted;
* restart resumes from durable state;
* no completed task is replayed;
* sensitive changes always pause for human review;
* no self-approval;
* no auto-merge.

Standard library only. Reuses ``atomic_write_json``/``append_line`` (storage),
``now_iso`` (domain events) and ``sha256_json``/``canonical_json_bytes``
(evaluation models) — no duplication of those primitives.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..domain.events import now_iso
from ..evaluation.models import (
    KeepDecision,
    KeepVerdict,
    canonical_json_bytes,
    sha256_json,
)
from ..storage.atomic import append_line, atomic_write_json


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

GOAL_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Goal + task state machines
# ---------------------------------------------------------------------------

class GoalState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    COMPLETED = "COMPLETED"


class TaskAttemptState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    ACCEPTED = "ACCEPTED"      # C1 verdict KEEP (or valid tie rule)
    REJECTED = "REJECTED"      # C1 verdict REJECT
    PAUSED_SENSITIVE = "PAUSED_SENSITIVE"   # sensitive change → human review
    FAILED = "FAILED"          # execution failure (not a C1 reject)

    @classmethod
    def terminal(cls) -> set:
        return {cls.ACCEPTED, cls.REJECTED, cls.PAUSED_SENSITIVE, cls.FAILED}


# ---------------------------------------------------------------------------
# Spec types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskRef:
    """A reference to a task within a goal.

    ``spec_payload`` is the task's own specification (arbitrary JSON-able dict).
    Its canonical hash MUST equal ``source_sha256`` at runtime; a mismatch fails
    closed (the goal pauses) — this guards against silent task-spec drift.
    """

    task_id: str
    source_sha256: str
    spec_payload: Dict[str, Any] = field(default_factory=dict)
    dependencies: Tuple[str, ...] = ()
    allowed_paths: Tuple[str, ...] = ()
    forbidden_paths: Tuple[str, ...] = ()
    sensitive_paths: Tuple[str, ...] = ()

    def computed_sha256(self) -> str:
        return sha256_json(self.spec_payload)

    def validate(self) -> None:
        if not self.task_id or not isinstance(self.task_id, str):
            raise ValueError("task_id must be a non-empty string")
        if self.computed_sha256() != self.source_sha256:
            raise ValueError(
                f"task {self.task_id}: source_sha256 mismatch "
                f"(declared={self.source_sha256}, "
                f"computed={self.computed_sha256()})"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source_sha256": self.source_sha256,
            "spec_payload": self.spec_payload,
            "dependencies": list(self.dependencies),
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "sensitive_paths": list(self.sensitive_paths),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskRef":
        return cls(
            task_id=d["task_id"],
            source_sha256=d["source_sha256"],
            spec_payload=d.get("spec_payload", {}),
            dependencies=tuple(d.get("dependencies", [])),
            allowed_paths=tuple(d.get("allowed_paths", [])),
            forbidden_paths=tuple(d.get("forbidden_paths", [])),
            sensitive_paths=tuple(d.get("sensitive_paths", [])),
        )


@dataclass(frozen=True)
class GoalSpec:
    schema_version: int
    goal_id: str
    project_id: str
    task_refs: Tuple[TaskRef, ...]
    dependency_order: Tuple[str, ...]
    max_tasks: int
    max_minutes: int
    max_questions_per_task: int
    max_corrections_per_task: int
    max_consecutive_failures: int
    allowed_paths: Tuple[str, ...] = ()
    forbidden_paths: Tuple[str, ...] = ()
    required_gates: Tuple[str, ...] = ()
    evaluation_spec: Dict[str, Any] = field(default_factory=dict)
    publication_mode: str = "none"  # "none" | "pr"
    created_at: str = ""

    def validate(self) -> None:
        if self.schema_version != GOAL_SCHEMA_VERSION:
            raise ValueError(f"unsupported goal schema_version: {self.schema_version}")
        if not self.goal_id or not isinstance(self.goal_id, str):
            raise ValueError("goal_id must be a non-empty string")
        if not self.project_id or not isinstance(self.project_id, str):
            raise ValueError("project_id must be a non-empty string")
        if self.max_tasks <= 0:
            raise ValueError("max_tasks must be > 0")
        if self.max_minutes <= 0:
            raise ValueError("max_minutes must be > 0")
        if self.max_questions_per_task < 0:
            raise ValueError("max_questions_per_task must be >= 0")
        if self.max_corrections_per_task < 0:
            raise ValueError("max_corrections_per_task must be >= 0")
        if self.max_consecutive_failures <= 0:
            raise ValueError("max_consecutive_failures must be > 0")
        ids = [t.task_id for t in self.task_refs]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate task_id in task_refs")
        ref_map = {t.task_id: t for t in self.task_refs}
        if list(self.dependency_order) != ids and set(self.dependency_order) != set(ids):
            # dependency_order must cover exactly the task ids (order may differ
            # from declaration order; we sort/validate below).
            if set(self.dependency_order) != set(ids):
                raise ValueError(
                    "dependency_order must contain exactly the task ids"
                )
        for tid in self.dependency_order:
            if tid not in ref_map:
                raise ValueError(f"dependency_order references unknown task: {tid}")
        # Every dependency must be a known task.
        for t in self.task_refs:
            for dep in t.dependencies:
                if dep not in ref_map:
                    raise ValueError(
                        f"task {t.task_id} depends on unknown task: {dep}"
                    )
        if _has_cycle(self.task_refs):
            raise ValueError("task dependency cycle detected")
        # dependency_order must be topological: every task must appear after all
        # of its dependencies. Otherwise the loop would pause on a pending
        # dependency before ever reaching the prerequisite.
        order_index = {tid: i for i, tid in enumerate(self.dependency_order)}
        for t in self.task_refs:
            for dep in t.dependencies:
                if order_index[dep] > order_index[t.task_id]:
                    raise ValueError(
                        f"dependency_order is not topological: task {t.task_id} "
                        f"appears before its dependency {dep}"
                    )
        # Each task's own hash must be internally consistent.
        for t in self.task_refs:
            t.validate()
        if self.publication_mode not in ("none", "pr"):
            raise ValueError(f"invalid publication_mode: {self.publication_mode}")

    @property
    def sha256(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal_id": self.goal_id,
            "project_id": self.project_id,
            "task_refs": [t.to_dict() for t in self.task_refs],
            "dependency_order": list(self.dependency_order),
            "max_tasks": self.max_tasks,
            "max_minutes": self.max_minutes,
            "max_questions_per_task": self.max_questions_per_task,
            "max_corrections_per_task": self.max_corrections_per_task,
            "max_consecutive_failures": self.max_consecutive_failures,
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "required_gates": list(self.required_gates),
            "evaluation_spec": self.evaluation_spec,
            "publication_mode": self.publication_mode,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GoalSpec":
        return cls(
            schema_version=d.get("schema_version", GOAL_SCHEMA_VERSION),
            goal_id=d["goal_id"],
            project_id=d["project_id"],
            task_refs=tuple(TaskRef.from_dict(t) for t in d.get("task_refs", [])),
            dependency_order=tuple(d.get("dependency_order", [])),
            max_tasks=d["max_tasks"],
            max_minutes=d["max_minutes"],
            max_questions_per_task=d["max_questions_per_task"],
            max_corrections_per_task=d["max_corrections_per_task"],
            max_consecutive_failures=d["max_consecutive_failures"],
            allowed_paths=tuple(d.get("allowed_paths", [])),
            forbidden_paths=tuple(d.get("forbidden_paths", [])),
            required_gates=tuple(d.get("required_gates", [])),
            evaluation_spec=d.get("evaluation_spec", {}),
            publication_mode=d.get("publication_mode", "none"),
            created_at=d.get("created_at", ""),
        )


def _has_cycle(task_refs: Tuple[TaskRef, ...]) -> bool:
    graph = {t.task_id: list(t.dependencies) for t in task_refs}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in graph}

    def visit(node: str) -> bool:
        color[node] = GRAY
        for nxt in graph.get(node, []):
            if color.get(nxt, WHITE) == GRAY:
                return True
            if color.get(nxt, WHITE) == WHITE and visit(nxt):
                return True
        color[node] = BLACK
        return False

    return any(color[n] == WHITE and visit(n) for n in graph)


# ---------------------------------------------------------------------------
# Injected collaborator contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoalExecResult:
    """Outcome of executing one task in a worktree."""

    success: bool
    changed_paths: Tuple[str, ...] = ()
    touched_sensitive: bool = False
    error: str = ""


@dataclass(frozen=True)
class GoalEvalResult:
    """Outcome of the C1 baseline/candidate comparison for one task.

    ``verdict`` is one of ``KEEP``/``REJECT``/``TIE``. A ``TIE`` is accepted
    only when ``tie_acceptable`` is True (used for pure compatibility changes
    per the runbook's explicit tie rule).
    """

    verdict: str  # "KEEP" | "REJECT" | "TIE"
    baseline_report_sha256: str = ""
    candidate_report_sha256: str = ""
    spec_sha256: str = ""
    reasons: Tuple[str, ...] = ()
    tie_acceptable: bool = False


# Executor: (task_ref, worktree_path, correction_count) -> GoalExecResult
GoalExecutor = Callable[[TaskRef, Path, int], GoalExecResult]
# Evaluator: (task_ref, worktree_path) -> GoalEvalResult
GoalEvaluator = Callable[[TaskRef, Path], GoalEvalResult]


# ---------------------------------------------------------------------------
# Per-task runtime state (persisted)
# ---------------------------------------------------------------------------

@dataclass
class TaskAttempt:
    task_id: str
    state: str = TaskAttemptState.PENDING.value
    attempts: int = 0
    corrections: int = 0
    questions: int = 0
    consecutive_failures: int = 0
    last_verdict: str = ""
    last_error: str = ""
    started_at: str = ""
    finished_at: str = ""
    exec_changed_paths: List[str] = field(default_factory=list)
    eval_baseline_sha: str = ""
    eval_candidate_sha: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "attempts": self.attempts,
            "corrections": self.corrections,
            "questions": self.questions,
            "consecutive_failures": self.consecutive_failures,
            "last_verdict": self.last_verdict,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exec_changed_paths": list(self.exec_changed_paths),
            "eval_baseline_sha": self.eval_baseline_sha,
            "eval_candidate_sha": self.eval_candidate_sha,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskAttempt":
        return cls(
            task_id=d["task_id"],
            state=d.get("state", TaskAttemptState.PENDING.value),
            attempts=d.get("attempts", 0),
            corrections=d.get("corrections", 0),
            questions=d.get("questions", 0),
            consecutive_failures=d.get("consecutive_failures", 0),
            last_verdict=d.get("last_verdict", ""),
            last_error=d.get("last_error", ""),
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
            exec_changed_paths=list(d.get("exec_changed_paths", [])),
            eval_baseline_sha=d.get("eval_baseline_sha", ""),
            eval_candidate_sha=d.get("eval_candidate_sha", ""),
        )


# ---------------------------------------------------------------------------
# Persisted goal state document
# ---------------------------------------------------------------------------

@dataclass
class GoalRuntimeState:
    goal_id: str
    state: str = GoalState.PENDING.value
    tasks: Dict[str, TaskAttempt] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    stop_reason: str = ""
    processed_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "state": self.state,
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stop_reason": self.stop_reason,
            "processed_count": self.processed_count,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GoalRuntimeState":
        return cls(
            goal_id=d["goal_id"],
            state=d.get("state", GoalState.PENDING.value),
            tasks={
                tid: TaskAttempt.from_dict(td)
                for tid, td in d.get("tasks", {}).items()
            },
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
            stop_reason=d.get("stop_reason", ""),
            processed_count=d.get("processed_count", 0),
        )


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------

@dataclass
class GoalReport:
    goal_id: str
    state: str
    stop_reason: str
    processed_count: int
    task_outcomes: List[Dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    spec_sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "state": self.state,
            "stop_reason": self.stop_reason,
            "processed_count": self.processed_count,
            "task_outcomes": list(self.task_outcomes),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "spec_sha256": self.spec_sha256,
        }


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

class GoalLoop:
    """Deterministic, bounded, resumable goal execution loop.

    The loop never approves its own implementation, never merges, and never
    touches the network. Execution and evaluation are injected. All state is
    persisted under ``state_dir`` so a restart resumes without replaying
    completed tasks.
    """

    def __init__(
        self,
        state_dir: Path,
        spec: GoalSpec,
        executor: GoalExecutor,
        evaluator: GoalEvaluator,
        *,
        worktree_path: Path,
        now_fn: Optional[Callable[[], str]] = None,
        monotonic_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        spec.validate()
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.spec = spec
        self.executor = executor
        self.evaluator = evaluator
        self.worktree_path = Path(worktree_path).resolve()
        self.now_fn = now_fn or now_iso
        self.monotonic_fn = monotonic_fn or time.monotonic
        self._task_by_id = {t.task_id: t for t in spec.task_refs}
        self.state = self._load_state()

    # -- artifact paths ---------------------------------------------------- #

    @property
    def spec_path(self) -> Path:
        return self.state_dir / "goal_spec.json"

    @property
    def state_path(self) -> Path:
        return self.state_dir / "goal_state.json"

    @property
    def events_path(self) -> Path:
        return self.state_dir / "goal_events.jsonl"

    @property
    def attempts_path(self) -> Path:
        return self.state_dir / "task_attempts.jsonl"

    @property
    def eval_links_path(self) -> Path:
        return self.state_dir / "evaluation_links.json"

    @property
    def publication_links_path(self) -> Path:
        return self.state_dir / "publication_links.json"

    @property
    def final_report_path(self) -> Path:
        return self.state_dir / "final_goal_report.json"

    # -- persistence ------------------------------------------------------- #

    def _load_state(self) -> GoalRuntimeState:
        if self.state_path.exists():
            import json
            with open(self.state_path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            st = GoalRuntimeState.from_dict(doc)
        else:
            st = GoalRuntimeState(goal_id=self.spec.goal_id)
        # Ensure every declared task has an attempt record.
        for tid in self._task_by_id:
            if tid not in st.tasks:
                st.tasks[tid] = TaskAttempt(task_id=tid)
        # Persist the spec once (content-addressed; immutable).
        if not self.spec_path.exists():
            atomic_write_json(self.spec_path, self.spec.to_dict())
        if not self.eval_links_path.exists():
            atomic_write_json(self.eval_links_path, {"links": {}})
        if not self.publication_links_path.exists():
            atomic_write_json(self.publication_links_path, {"links": {}})
        return st

    def _save_state(self) -> None:
        atomic_write_json(self.state_path, self.state.to_dict())

    def _emit_event(self, **fields: Any) -> None:
        record = {"ts": self.now_fn(), **fields}
        append_line(self.events_path, _canonical_line(record))

    def _append_attempt(self, attempt: TaskAttempt) -> None:
        append_line(
            self.attempts_path,
            _canonical_line({"ts": self.now_fn(), **attempt.to_dict()}),
        )

    def _record_eval_link(self, task_id: str, ev: GoalEvalResult) -> None:
        import json
        with open(self.eval_links_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        doc.setdefault("links", {})[task_id] = {
            "verdict": ev.verdict,
            "baseline_report_sha256": ev.baseline_report_sha256,
            "candidate_report_sha256": ev.candidate_report_sha256,
            "spec_sha256": ev.spec_sha256,
            "tie_acceptable": ev.tie_acceptable,
            "reasons": list(ev.reasons),
        }
        atomic_write_json(self.eval_links_path, doc)

    # -- main entry -------------------------------------------------------- #

    def run(self) -> GoalReport:
        """Run the goal loop to completion, a limit, or a blocker.

        Idempotent and resumable: already-ACCEPTED tasks are never replayed.
        """
        spec_sha = self.spec.sha256
        # If the goal was already terminal, return its prior report if present.
        if self.state.state in {
            GoalState.COMPLETED.value,
            GoalState.BUDGET_EXHAUSTED.value,
            GoalState.BLOCKED.value,
        } and self.final_report_path.exists():
            import json
            with open(self.final_report_path, "r", encoding="utf-8") as fh:
                return GoalReport(**json.load(fh))

        # Fail-closed hash check: every task's spec must still hash to its
        # declared source_sha256. A mismatch pauses the goal.
        for tid in self.spec.dependency_order:
            tref = self._task_by_id[tid]
            try:
                tref.validate()
            except ValueError as exc:
                self._pause_for(str(exc), reason="task_hash_mismatch")
                return self._finalize(stop_reason="task_hash_mismatch")

        if self.state.state == GoalState.PENDING.value:
            self.state.state = GoalState.RUNNING.value
            self.state.started_at = self.now_fn()
            self._emit_event(event="goal_started", goal_id=self.spec.goal_id)

        start_monotonic = self.monotonic_fn()
        # Budget is measured across the whole goal wall-clock; on resume we
        # approximate remaining budget from processed_count vs max_tasks and
        # a per-resume fresh time allowance capped at max_minutes.
        budget_seconds = self.spec.max_minutes * 60

        stop_reason = ""
        for tid in self.spec.dependency_order:
            # Budget enforcement.
            elapsed = self.monotonic_fn() - start_monotonic
            if elapsed >= budget_seconds:
                self.state.state = GoalState.BUDGET_EXHAUSTED.value
                stop_reason = "budget_exhausted_max_minutes"
                self._emit_event(event="budget_exhausted", elapsed_s=elapsed)
                break
            # max_tasks bound.
            if self.state.processed_count >= self.spec.max_tasks:
                self.state.state = GoalState.COMPLETED.value \
                    if self._all_accepted() else GoalState.PAUSED.value
                stop_reason = "max_tasks_reached"
                self._emit_event(event="max_tasks_reached")
                break

            tref = self._task_by_id[tid]
            attempt = self.state.tasks[tid]

            # Skip already-accepted tasks (no replay).
            if attempt.state == TaskAttemptState.ACCEPTED.value:
                continue

            # Dependency gate: a dependency that is not ACCEPTED blocks/pauses.
            blocked_dep = self._blocking_dependency(tref)
            if blocked_dep is not None:
                dep_state = self.state.tasks[blocked_dep].state
                if dep_state == TaskAttemptState.REJECTED.value:
                    # C1 REJECT blocks the dependent → goal blocked.
                    self.state.state = GoalState.BLOCKED.value
                    attempt.state = TaskAttemptState.REJECTED.value
                    attempt.last_error = f"dependency {blocked_dep} rejected"
                    self._emit_event(
                        event="task_blocked_by_rejected_dependency",
                        task_id=tid, dependency=blocked_dep,
                    )
                    self._append_attempt(attempt)
                    stop_reason = "dependency_rejected"
                    break
                # Dependency pending/failed → pause for later resume.
                self.state.state = GoalState.PAUSED.value
                stop_reason = f"dependency_{blocked_dep}_not_accepted"
                self._emit_event(
                    event="task_waiting_on_dependency",
                    task_id=tid, dependency=blocked_dep,
                )
                self._save_state()
                return self._finalize(stop_reason=stop_reason)

            # Process the task.
            self._process_task(tref, attempt)
            self._save_state()

            if attempt.state == TaskAttemptState.ACCEPTED.value:
                self.state.processed_count += 1
                continue
            if attempt.state == TaskAttemptState.REJECTED.value:
                # A C1-rejected task still consumed processing (exec + eval),
                # so it counts toward the max_tasks bound.
                self.state.processed_count += 1
                # C1 REJECT: continue so dependents hit the dependency gate
                # and are marked blocked-by-rejected.
                self.state.state = GoalState.BLOCKED.value
                stop_reason = f"task_{tid}_c1_rejected"
                self._emit_event(event="goal_blocked_c1_reject", task_id=tid)
                continue
            if attempt.state == TaskAttemptState.PAUSED_SENSITIVE.value:
                self.state.state = GoalState.PAUSED.value
                stop_reason = "sensitive_change_requires_human_review"
                self._emit_event(event="goal_paused_sensitive", task_id=tid)
                self._save_state()
                return self._finalize(stop_reason=stop_reason)
            if attempt.state == TaskAttemptState.FAILED.value:
                # Consecutive-failure policy.
                if attempt.consecutive_failures >= self.spec.max_consecutive_failures:
                    self.state.state = GoalState.PAUSED.value
                    stop_reason = "max_consecutive_failures"
                    self._emit_event(
                        event="goal_paused_consecutive_failures",
                        task_id=tid,
                        consecutive_failures=attempt.consecutive_failures,
                    )
                    self._save_state()
                    return self._finalize(stop_reason=stop_reason)
                # Otherwise the failure does not advance; the task stays FAILED
                # and the loop stops this run (a correction was already tried).
                self.state.state = GoalState.PAUSED.value
                stop_reason = f"task_{tid}_failed"
                break

        # Loop finished: did we accept everything in order?
        if not stop_reason:
            if self._all_accepted():
                self.state.state = GoalState.COMPLETED.value
                stop_reason = "all_tasks_accepted"
            else:
                self.state.state = GoalState.PAUSED.value
                stop_reason = "incomplete"

        return self._finalize(stop_reason=stop_reason, spec_sha=spec_sha)

    # -- per-task processing ----------------------------------------------- #

    def _process_task(self, tref: TaskRef, attempt: TaskAttempt) -> None:
        attempt.state = TaskAttemptState.RUNNING.value
        attempt.attempts += 1
        attempt.started_at = self.now_fn()
        self._emit_event(event="task_started", task_id=tref.task_id,
                         attempt=attempt.attempts)
        self._append_attempt(attempt)

        # Execute (with up to max_corrections retries on execution failure).
        exec_result = self._execute_with_correction(tref, attempt)
        attempt.exec_changed_paths = list(exec_result.changed_paths)

        if not exec_result.success:
            attempt.state = TaskAttemptState.FAILED.value
            attempt.last_error = exec_result.error or "execution_failed"
            attempt.consecutive_failures += 1
            attempt.finished_at = self.now_fn()
            self._emit_event(
                event="task_failed", task_id=tref.task_id,
                error=attempt.last_error,
                consecutive_failures=attempt.consecutive_failures,
            )
            self._append_attempt(attempt)
            return
        # Reset consecutive failures on a successful execution.
        attempt.consecutive_failures = 0

        # Sensitive-path gate: any touched sensitive path pauses for humans.
        # The controller owns this check (it must not rely solely on the
        # executor's self-reported flag).
        if (exec_result.touched_sensitive
                or self._writes_forbidden(tref, exec_result)
                or self._touches_sensitive(tref, exec_result)):
            attempt.state = TaskAttemptState.PAUSED_SENSITIVE.value
            attempt.last_error = "sensitive_or_forbidden_path_touched"
            attempt.finished_at = self.now_fn()
            self._emit_event(
                event="task_paused_sensitive", task_id=tref.task_id,
                changed_paths=list(exec_result.changed_paths),
            )
            self._append_attempt(attempt)
            return

        # C1 baseline/candidate evaluation.
        ev = self.evaluator(tref, self.worktree_path)
        attempt.last_verdict = ev.verdict
        attempt.eval_baseline_sha = ev.baseline_report_sha256
        attempt.eval_candidate_sha = ev.candidate_report_sha256
        self._record_eval_link(tref.task_id, ev)

        accepted = ev.verdict == KeepVerdict.KEEP.value or (
            ev.verdict == KeepVerdict.TIE.value and ev.tie_acceptable
        )
        if accepted:
            attempt.state = TaskAttemptState.ACCEPTED.value
            attempt.finished_at = self.now_fn()
            self._emit_event(
                event="task_accepted", task_id=tref.task_id, verdict=ev.verdict,
            )
        else:
            attempt.state = TaskAttemptState.REJECTED.value
            attempt.last_error = f"c1_{ev.verdict.lower()}"
            attempt.finished_at = self.now_fn()
            self._emit_event(
                event="task_rejected", task_id=tref.task_id, verdict=ev.verdict,
                reasons=list(ev.reasons),
            )
        self._append_attempt(attempt)

    def _execute_with_correction(self, tref: TaskRef, attempt: TaskAttempt) -> GoalExecResult:
        """Execute, allowing up to ``max_corrections`` retries on failure."""
        result = self.executor(tref, self.worktree_path, attempt.corrections)
        while not result.success and attempt.corrections < self.spec.max_corrections_per_task:
            attempt.corrections += 1
            self._emit_event(
                event="task_correction", task_id=tref.task_id,
                correction=attempt.corrections,
            )
            result = self.executor(tref, self.worktree_path, attempt.corrections)
        return result

    # -- helpers ----------------------------------------------------------- #

    def _blocking_dependency(self, tref: TaskRef) -> Optional[str]:
        """Return the first dependency that is not ACCEPTED, else None."""
        for dep in tref.dependencies:
            dep_attempt = self.state.tasks.get(dep)
            if dep_attempt is None or dep_attempt.state != TaskAttemptState.ACCEPTED.value:
                return dep
        return None

    def _writes_forbidden(self, tref: TaskRef, result: GoalExecResult) -> bool:
        forbidden = set(self.spec.forbidden_paths) | set(tref.forbidden_paths)
        for p in result.changed_paths:
            for f in forbidden:
                if p == f or p.startswith(f.rstrip("/") + "/"):
                    return True
        return False

    def _touches_sensitive(self, tref: TaskRef, result: GoalExecResult) -> bool:
        sensitive = set(tref.sensitive_paths)
        for p in result.changed_paths:
            for s in sensitive:
                if p == s or p.startswith(s.rstrip("/") + "/"):
                    return True
        return False

    def _all_accepted(self) -> bool:
        return all(
            self.state.tasks[tid].state == TaskAttemptState.ACCEPTED.value
            for tid in self.spec.dependency_order
        )

    def _pause_for(self, message: str, reason: str = "paused") -> None:
        self.state.state = GoalState.PAUSED.value
        self.state.stop_reason = reason
        self._emit_event(event="goal_paused", reason=reason, message=message)
        self._save_state()

    def _finalize(self, stop_reason: str, spec_sha: str = "") -> GoalReport:
        self.state.stop_reason = stop_reason
        self.state.finished_at = self.now_fn()
        self._save_state()
        outcomes = []
        for tid in self.spec.dependency_order:
            a = self.state.tasks[tid]
            outcomes.append({
                "task_id": tid,
                "state": a.state,
                "attempts": a.attempts,
                "corrections": a.corrections,
                "last_verdict": a.last_verdict,
            })
        report = GoalReport(
            goal_id=self.spec.goal_id,
            state=self.state.state,
            stop_reason=stop_reason,
            processed_count=self.state.processed_count,
            task_outcomes=outcomes,
            started_at=self.state.started_at,
            finished_at=self.state.finished_at,
            spec_sha256=spec_sha or self.spec.sha256,
        )
        atomic_write_json(self.final_report_path, report.to_dict())
        self._emit_event(
            event="goal_finalized", state=self.state.state, stop_reason=stop_reason,
        )
        return report


def _canonical_line(record: Dict[str, Any]) -> str:
    """Serialize an event/attempt line deterministically for JSONL."""
    import json
    return json.dumps(record, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
