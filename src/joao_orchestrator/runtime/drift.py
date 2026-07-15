"""C6 Complete Drift Control — allows long autonomous runs without silent loops,
quota waste, or scope drift.

Extends Turbo T10 (circuit_breaker.py). Does NOT create a second circuit-breaker
subsystem. This module adds:

  - ProgressFingerprint-based no-progress detection (runtime/progress.py)
  - Additional drift signals: scope drift, budget exhaustion, eval decline,
    provider circuit, no-accepted-artifact interval, review-finding repeat
  - Recovery policy: bounded automatic recovery (rebuild one stale context,
    switch to cached evidence, retry one flaky command, continue independent
    tasks, resume after provider cooldown). NOT allowed: blind retry, scope
    expansion, budget/permission increase, test weakening, sensitive continuation.
  - Resume: persists exact last-accepted commit, current task, remaining tasks,
    failure signature, required human decision, next safe command.

States (superset of T10):
  RUNNING, PAUSED, BLOCKED, NO_PROGRESS, BUDGET_EXHAUSTED, NEEDS_HUMAN,
  RECOVERING, COMPLETED

Deterministic. No network. Stdlib only. Persistence outside repos.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..evaluation.models import sha256_json
from ..storage.atomic import append_line, atomic_write_json
from .progress import ProgressFingerprint


SCHEMA_VERSION = 1


def _norm_rel(path: str) -> str:
    """Normalize a relative path for prefix comparison (forward slashes, stripped)."""
    return path.strip().replace("\\", "/")


def _path_allowed_by_plan(path: str, allowed: frozenset[str]) -> bool:
    """Prefix-aware containment used by ``record_touched_paths``.

    A path is allowed if it equals an allowed entry, or lives beneath an
    allowed directory. ``allowed = {"src/"}`` covers ``src/app.py``; an allowed
    entry without a trailing slash covers either an exact file match or, when
    it is a directory, anything beneath it (``src`` matches ``src/app.py``).
    """
    p = _norm_rel(path)
    if not p:
        return False
    for entry in allowed:
        e = _norm_rel(entry)
        if not e:
            continue
        if p == e:
            return True
        e_dir = e if e.endswith("/") else e + "/"
        if p.startswith(e_dir):
            return True
    return False


class DriftState(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    NO_PROGRESS = "NO_PROGRESS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"


class DriftSignal(str, Enum):
    SAME_FAILURE_UNCHANGED_DIFF = "same_failure_unchanged_diff"
    SAME_TEST_RERUN_UNCHANGED_HASHES = "same_test_rerun_unchanged_hashes"
    FILE_REOPENED_ABOVE_THRESHOLD = "file_reopened_above_threshold"
    SAME_CONTEXT_PACKET_REGENERATED = "same_context_packet_regenerated"
    CONTEXT_GROWS_WITHOUT_SCOPE_CHANGE = "context_grows_without_scope_change"
    SCOPE_EXPANDS_OUTSIDE_PLAN = "scope_expands_outside_plan"
    MODEL_CALLS_EXCEED_BUDGET = "model_calls_exceed_budget"
    TOOL_CALLS_EXCEED_BUDGET = "tool_calls_exceed_budget"
    WALL_CLOCK_EXCEEDS_BUDGET = "wall_clock_exceeds_budget"
    TWO_CONSECUTIVE_TASK_FAILURES = "two_consecutive_task_failures"
    EVALUATION_SCORE_DECLINES = "evaluation_score_declines"
    PROVIDER_CIRCUIT_OPENS = "provider_circuit_opens"
    NO_ACCEPTED_ARTIFACT_AFTER_INTERVAL = "no_accepted_artifact_after_interval"
    REVIEW_FINDING_REPEATS_AFTER_CORRECTION = "review_finding_repeats_after_correction"


class RecoveryAction(str, Enum):
    REBUILD_STALE_CONTEXT = "rebuild_stale_context"
    SWITCH_TO_CACHED_EVIDENCE = "switch_to_cached_evidence"
    RETRY_ONE_FLAKY_COMMAND = "retry_one_flaky_command"
    CONTINUE_INDEPENDENT_TASKS = "continue_independent_tasks"
    RESUME_AFTER_PROVIDER_COOLDOWN = "resume_after_provider_cooldown"


# Recovery actions that are allowed automatically.
ALLOWED_AUTO_RECOVERY = frozenset({
    RecoveryAction.REBUILD_STALE_CONTEXT,
    RecoveryAction.SWITCH_TO_CACHED_EVIDENCE,
    RecoveryAction.RETRY_ONE_FLAKY_COMMAND,
    RecoveryAction.CONTINUE_INDEPENDENT_TASKS,
    RecoveryAction.RESUME_AFTER_PROVIDER_COOLDOWN,
})

# Actions that are NOT allowed automatically.
FORBIDDEN_RECOVERY = frozenset({
    "blind_model_retry",
    "scope_expansion",
    "budget_increase",
    "permission_increase",
    "test_weakening",
    "sensitive_path_continuation",
})


@dataclass(frozen=True)
class DriftEvent:
    """One drift-control event."""

    signal: str
    task_id: str
    from_state: str
    to_state: str
    evidence: str
    recovery_action: str   # "" if none, or an allowed RecoveryAction value
    timestamp: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signal": self.signal,
            "task_id": self.task_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "evidence": self.evidence,
            "recovery_action": self.recovery_action,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class DriftBudget:
    """Budgets for drift detection."""

    max_model_calls: int = 24
    max_tool_calls: int = 100
    max_wall_clock_seconds: int = 3600
    max_file_reopens: int = 3
    max_consecutive_failures: int = 2
    max_context_growth_ratio: float = 2.0
    no_artifact_interval_seconds: int = 1800
    max_eval_decline: float = 0.1

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_model_calls": self.max_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_wall_clock_seconds": self.max_wall_clock_seconds,
            "max_file_reopens": self.max_file_reopens,
            "max_consecutive_failures": self.max_consecutive_failures,
            "max_context_growth_ratio": self.max_context_growth_ratio,
            "no_artifact_interval_seconds": self.no_artifact_interval_seconds,
            "max_eval_decline": self.max_eval_decline,
        }


@dataclass(frozen=True)
class ResumePoint:
    """Exact resume state for a paused/blocked run."""

    last_accepted_commit: str
    current_task: str
    remaining_tasks: tuple[str, ...]
    failure_signature: str
    required_human_decision: str
    next_safe_command: str
    state: str
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "last_accepted_commit": self.last_accepted_commit,
            "current_task": self.current_task,
            "remaining_tasks": list(self.remaining_tasks),
            "failure_signature": self.failure_signature,
            "required_human_decision": self.required_human_decision,
            "next_safe_command": self.next_safe_command,
            "state": self.state,
        }

    @property
    def resume_hash(self) -> str:
        return sha256_json(self.unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        d = self.unsigned_dict()
        d["resume_hash"] = self.resume_hash
        return d


class DriftController:
    """Tracks drift signals for a task run and controls state transitions.

    Extends the circuit-breaker concept with fingerprint-based no-progress
    detection, budget guards, and a bounded recovery policy. Deterministic:
    same signal history -> same decisions.
    """

    def __init__(
        self,
        task_id: str,
        budget: Optional[DriftBudget] = None,
        allowed_plan_paths: Optional[tuple[str, ...]] = None,
        now_fn=None,
    ):
        self.task_id = task_id
        self.budget = budget or DriftBudget()
        self.allowed_plan_paths = set(allowed_plan_paths or ())
        self._now_fn = now_fn or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.state = DriftState.RUNNING
        self.events: list[DriftEvent] = []

        # Fingerprint tracking.
        self._last_fingerprint: Optional[ProgressFingerprint] = None
        self._fingerprint_unchanged_count = 0

        # Signal accumulators.
        self._file_reopen_counts: dict[str, int] = {}
        self._failure_signatures: dict[str, str] = {}  # sig -> last diff hash
        self._test_rerun_hashes: dict[str, str] = {}   # test -> last hashes
        self._context_hashes: list[str] = []
        self._context_bytes_initial: Optional[int] = None
        self._context_scope: Optional[frozenset[str]] = None
        self._consecutive_failures = 0
        self._review_finding_hashes: list[str] = []
        self._model_calls = 0
        self._tool_calls = 0
        self._eval_scores: list[float] = []
        self._last_accepted_artifact_ts: Optional[str] = None
        self._recovery_attempts: dict[str, int] = {}  # action -> count

    def is_terminal(self) -> bool:
        return self.state in (
            DriftState.COMPLETED, DriftState.BLOCKED,
            DriftState.BUDGET_EXHAUSTED, DriftState.NEEDS_HUMAN,
        )

    def _emit(self, signal: DriftSignal, evidence: str, to_state: DriftState,
              recovery: str = "") -> DriftEvent:
        ev = DriftEvent(
            signal=signal.value, task_id=self.task_id,
            from_state=self.state.value, to_state=to_state.value,
            evidence=evidence, recovery_action=recovery,
            timestamp=self._now_fn(),
        )
        self.events.append(ev)
        self.state = to_state
        return ev

    # -- fingerprint-based no-progress ----------------------------------- #

    def observe_fingerprint(self, fp: ProgressFingerprint) -> Optional[DriftEvent]:
        """Record a progress fingerprint. Trip NO_PROGRESS if unchanged twice."""
        if self.is_terminal():
            return None
        if self._last_fingerprint is not None and not fp.changed_from(self._last_fingerprint):
            self._fingerprint_unchanged_count += 1
            if self._fingerprint_unchanged_count >= 1 and fp.failure_signature:
                return self._emit(
                    DriftSignal.SAME_FAILURE_UNCHANGED_DIFF,
                    f"fingerprint unchanged; failure={fp.failure_signature}",
                    DriftState.NO_PROGRESS,
                )
        else:
            self._fingerprint_unchanged_count = 0
        self._last_fingerprint = fp
        return None

    # -- individual signals ---------------------------------------------- #

    def record_file_reopen(self, path: str) -> Optional[DriftEvent]:
        if self.is_terminal():
            return None
        self._file_reopen_counts[path] = self._file_reopen_counts.get(path, 0) + 1
        if self._file_reopen_counts[path] > self.budget.max_file_reopens:
            return self._emit(
                DriftSignal.FILE_REOPENED_ABOVE_THRESHOLD,
                f"{path} reopened {self._file_reopen_counts[path]} times",
                DriftState.NO_PROGRESS,
            )
        return None

    def record_test_rerun(self, test_name: str, source_hash: str, test_hash: str) -> Optional[DriftEvent]:
        if self.is_terminal():
            return None
        combined = f"{source_hash}:{test_hash}"
        prev = self._test_rerun_hashes.get(test_name)
        if prev is not None and prev == combined:
            return self._emit(
                DriftSignal.SAME_TEST_RERUN_UNCHANGED_HASHES,
                f"{test_name} rerun with unchanged hashes",
                DriftState.NO_PROGRESS,
            )
        self._test_rerun_hashes[test_name] = combined
        return None

    def record_context_packet(self, packet_hash: str) -> Optional[DriftEvent]:
        if self.is_terminal():
            return None
        if packet_hash in self._context_hashes:
            return self._emit(
                DriftSignal.SAME_CONTEXT_PACKET_REGENERATED,
                f"context packet {packet_hash[:8]} regenerated identically",
                DriftState.NO_PROGRESS,
            )
        self._context_hashes.append(packet_hash)
        return None

    def record_context_growth(self, context_bytes: int, scope_paths: frozenset[str]) -> Optional[DriftEvent]:
        if self.is_terminal():
            return None
        if self._context_bytes_initial is None:
            self._context_bytes_initial = context_bytes
            self._context_scope = frozenset(scope_paths)
            return None
        scope_changed = scope_paths != self._context_scope
        if (not scope_changed
                and context_bytes >= self._context_bytes_initial * self.budget.max_context_growth_ratio):
            return self._emit(
                DriftSignal.CONTEXT_GROWS_WITHOUT_SCOPE_CHANGE,
                f"context {context_bytes} >= {self._context_bytes_initial} * {self.budget.max_context_growth_ratio}",
                DriftState.NO_PROGRESS,
            )
        if scope_changed:
            self._context_scope = frozenset(scope_paths)
        return None

    def record_touched_paths(self, touched: frozenset[str]) -> Optional[DriftEvent]:
        """Trip if the task touched paths outside the plan's allowed set.

        Matching is prefix-aware: an allowed entry ``"src/"`` (or ``"src"``)
        covers any path beneath it (e.g. ``src/app.py``), matching how the
        path policy treats directory allow-lists. A touched path is inside the
        plan if it equals an allowed entry or lives under an allowed directory.
        """
        if self.is_terminal():
            return None
        if not self.allowed_plan_paths:
            return None
        outside: list[str] = []
        for path in touched:
            if _path_allowed_by_plan(path, self.allowed_plan_paths):
                continue
            outside.append(path)
        if outside:
            return self._emit(
                DriftSignal.SCOPE_EXPANDS_OUTSIDE_PLAN,
                f"touched outside plan: {sorted(outside)[:5]}",
                DriftState.BLOCKED,
            )
        return None

    def record_model_call(self) -> Optional[DriftEvent]:
        if self.is_terminal():
            return None
        self._model_calls += 1
        if self._model_calls >= self.budget.max_model_calls:
            return self._emit(
                DriftSignal.MODEL_CALLS_EXCEED_BUDGET,
                f"{self._model_calls} >= {self.budget.max_model_calls}",
                DriftState.BUDGET_EXHAUSTED,
            )
        return None

    def record_tool_call(self) -> Optional[DriftEvent]:
        if self.is_terminal():
            return None
        self._tool_calls += 1
        if self._tool_calls >= self.budget.max_tool_calls:
            return self._emit(
                DriftSignal.TOOL_CALLS_EXCEED_BUDGET,
                f"{self._tool_calls} >= {self.budget.max_tool_calls}",
                DriftState.BUDGET_EXHAUSTED,
            )
        return None

    def record_task_failure(self, failure_signature: str = "") -> Optional[DriftEvent]:
        if self.is_terminal():
            return None
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.budget.max_consecutive_failures:
            return self._emit(
                DriftSignal.TWO_CONSECUTIVE_TASK_FAILURES,
                f"{self._consecutive_failures} consecutive failures; sig={failure_signature}",
                DriftState.NEEDS_HUMAN,
            )
        return None

    def record_task_success(self) -> None:
        self._consecutive_failures = 0

    def record_evaluation(self, score: float) -> Optional[DriftEvent]:
        if self.is_terminal():
            return None
        self._eval_scores.append(score)
        if len(self._eval_scores) >= 2:
            decline = self._eval_scores[-2] - self._eval_scores[-1]
            if decline >= self.budget.max_eval_decline:
                return self._emit(
                    DriftSignal.EVALUATION_SCORE_DECLINES,
                    f"eval declined {decline:.3f} ({self._eval_scores[-2]} -> {self._eval_scores[-1]})",
                    DriftState.NO_PROGRESS,
                )
        return None

    def record_provider_circuit_open(self) -> DriftEvent:
        return self._emit(
            DriftSignal.PROVIDER_CIRCUIT_OPENS,
            "provider circuit opened",
            DriftState.PAUSED,
            recovery=RecoveryAction.RESUME_AFTER_PROVIDER_COOLDOWN.value,
        )

    def record_review_finding(self, finding_hash: str, after_correction: bool) -> Optional[DriftEvent]:
        if self.is_terminal():
            return None
        if after_correction and finding_hash in self._review_finding_hashes:
            return self._emit(
                DriftSignal.REVIEW_FINDING_REPEATS_AFTER_CORRECTION,
                f"finding {finding_hash[:8]} repeated after correction",
                DriftState.NEEDS_HUMAN,
            )
        self._review_finding_hashes.append(finding_hash)
        return None

    def record_accepted_artifact(self) -> None:
        self._last_accepted_artifact_ts = self._now_fn()
        self._last_accepted_artifact_mono = time.monotonic()

    def check_no_artifact_interval(self, current_ts: Optional[str] = None) -> Optional[DriftEvent]:
        """Trip if no accepted artifact within the configured interval.

        Uses the monotonic clock for accurate elapsed-time measurement. The
        interval starts from the last accepted artifact (or controller creation
        if none accepted yet). Caller may pass current_ts for audit logging.
        """
        if self.is_terminal():
            return None
        mono_now = time.monotonic()
        last_mono = getattr(self, "_last_accepted_artifact_mono", None)
        if last_mono is None:
            last_mono = getattr(self, "_start_mono", None)
        if last_mono is None:
            self._start_mono = mono_now
            return None
        elapsed = mono_now - last_mono
        if elapsed >= self.budget.no_artifact_interval_seconds:
            return self._emit(
                DriftSignal.NO_ACCEPTED_ARTIFACT_AFTER_INTERVAL,
                f"no accepted artifact for {elapsed:.0f}s "
                f"(>= {self.budget.no_artifact_interval_seconds}s)",
                DriftState.NO_PROGRESS,
            )
        return None

    # -- recovery -------------------------------------------------------- #

    def attempt_recovery(self, action: RecoveryAction) -> tuple[bool, str]:
        """Attempt a bounded automatic recovery action.

        Returns (allowed, reason). Each allowed action may be attempted a
        bounded number of times. Forbidden actions are always rejected.
        """
        if action not in ALLOWED_AUTO_RECOVERY:
            return False, f"recovery action {action.value} is not allowed automatically"
        count = self._recovery_attempts.get(action.value, 0)
        if count >= 1:
            return False, f"recovery action {action.value} already attempted once (bound=1)"
        self._recovery_attempts[action.value] = count + 1
        # Transition to RECOVERING.
        if self.state in (DriftState.NO_PROGRESS, DriftState.PAUSED):
            self.state = DriftState.RECOVERING
        return True, f"recovery {action.value} allowed (attempt {count + 1})"

    def is_forbidden_recovery(self, action_name: str) -> bool:
        return action_name in FORBIDDEN_RECOVERY

    # -- resume ---------------------------------------------------------- #

    def resume_point(
        self,
        last_accepted_commit: str,
        current_task: str,
        remaining_tasks: tuple[str, ...],
        next_safe_command: str = "",
        required_human_decision: str = "",
    ) -> ResumePoint:
        failure_sig = ""
        if self._last_fingerprint is not None:
            failure_sig = self._last_fingerprint.failure_signature
        return ResumePoint(
            last_accepted_commit=last_accepted_commit,
            current_task=current_task,
            remaining_tasks=remaining_tasks,
            failure_signature=failure_sig,
            required_human_decision=required_human_decision,
            next_safe_command=next_safe_command,
            state=self.state.value,
        )

    def complete(self) -> None:
        self.state = DriftState.COMPLETED


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class DriftStore:
    """Persists drift events + resume points outside repos."""

    def __init__(self, state_root):
        self.dir = Path(state_root).resolve() / "drift_control"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"

    def record_event(self, event: DriftEvent) -> None:
        append_line(self.events_path, json.dumps(event.to_dict(), ensure_ascii=False))

    def persist_resume(self, resume: ResumePoint) -> Path:
        path = self.dir / f"resume_{resume.current_task}.json"
        atomic_write_json(path, resume.to_dict())
        return path

    def load_events_for_task(self, task_id: str) -> list[DriftEvent]:
        if not self.events_path.is_file():
            return []
        out = []
        for line in self.events_path.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("task_id") == task_id:
                out.append(DriftEvent(
                    signal=d["signal"], task_id=d["task_id"],
                    from_state=d.get("from_state", ""), to_state=d.get("to_state", ""),
                    evidence=d.get("evidence", ""),
                    recovery_action=d.get("recovery_action", ""),
                    timestamp=d.get("timestamp", ""),
                ))
        return out
