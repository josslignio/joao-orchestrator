"""Quota and drift circuit breakers + resume (T10).

Monitors a task run for drift and quota exhaustion. On trigger: persist
evidence, stop the dependent task, continue independent safe tasks only, emit
an exact resume command. Never retries blindly.

States (spec):
  RUNNING, PAUSED, BLOCKED, BUDGET_EXHAUSTED, NO_PROGRESS, NEEDS_HUMAN, COMPLETED

Trigger on:
  - same failing test twice with unchanged diff
  - same file reopened beyond threshold
  - repeated question
  - repeated review finding after correction
  - context growth without scope change
  - tool/model/context/time budget exceeded
  - cache miss storm
  - unexpected path expansion
  - two consecutive task failures
  - C1 regression

On trigger:
  - persist evidence;
  - stop dependent task;
  - continue independent safe tasks only;
  - never retry blindly;
  - emit exact resume command.

Deterministic. No network. Stdlib only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Optional

from ..evaluation.models import sha256_json
from ..storage.atomic import append_line, atomic_write_json


SCHEMA_VERSION = 1


class RunState(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NO_PROGRESS = "NO_PROGRESS"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    COMPLETED = "COMPLETED"


class BreakerReason(str, Enum):
    SAME_FAILING_TEST_UNCHANGED_DIFF = "same_failing_test_unchanged_diff"
    FILE_REOPENED_BEYOND_THRESHOLD = "file_reopened_beyond_threshold"
    REPEATED_QUESTION = "repeated_question"
    REPEATED_REVIEW_FINDING = "repeated_review_finding"
    CONTEXT_GROWTH_NO_SCOPE_CHANGE = "context_growth_no_scope_change"
    BUDGET_EXCEEDED = "budget_exceeded"
    CACHE_MISS_STORM = "cache_miss_storm"
    PATH_EXPANSION = "path_expansion"
    TWO_CONSECUTIVE_FAILURES = "two_consecutive_failures"
    C1_REGRESSION = "c1_regression"


# Thresholds (deterministic, from spec defaults).
DEFAULT_FILE_REOPEN_THRESHOLD = 3
DEFAULT_CACHE_MISS_STORM = 10
DEFAULT_CONSECUTIVE_FAILURES = 2
DEFAULT_CONTEXT_GROWTH_RATIO = 2.0  # context doubled without scope change


@dataclass(frozen=True)
class BreakerEvent:
    """One circuit-breaker trigger event."""
    reason: BreakerReason
    task_id: str
    evidence: str
    timestamp: str
    new_state: RunState
    resume_command: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reason": self.reason.value,
            "task_id": self.task_id,
            "evidence": self.evidence,
            "timestamp": self.timestamp,
            "new_state": self.new_state.value,
            "resume_command": self.resume_command,
        }


class CircuitBreaker:
    """Tracks drift signals for one task run and trips on threshold breach.

    Stateless across runs except for the signals the caller records. The
    breaker is deterministic: same signal history -> same trip decision.
    """

    def __init__(
        self,
        task_id: str,
        resume_command: str = "",
        file_reopen_threshold: int = DEFAULT_FILE_REOPEN_THRESHOLD,
        cache_miss_storm: int = DEFAULT_CACHE_MISS_STORM,
        consecutive_failures: int = DEFAULT_CONSECUTIVE_FAILURES,
        context_growth_ratio: float = DEFAULT_CONTEXT_GROWTH_RATIO,
        now_fn=None,
    ):
        self.task_id = task_id
        self._resume_command = resume_command or f"# resume task {task_id}"
        self._file_reopen_threshold = file_reopen_threshold
        self._cache_miss_storm = cache_miss_storm
        self._consecutive_failures = consecutive_failures
        self._context_growth_ratio = context_growth_ratio
        self._now_fn = now_fn or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.state = RunState.RUNNING
        self.events: list[BreakerEvent] = []
        # Signal accumulators.
        self._file_reopen_counts: dict[str, int] = {}
        self._failing_test_diffs: dict[str, str] = {}  # test_name -> last diff hash
        self._question_hashes: list[str] = []
        self._review_finding_hashes: list[str] = []
        self._consecutive_failures_count = 0
        self._cache_misses = 0
        self._context_bytes_initial: Optional[int] = None
        self._context_bytes_last: Optional[int] = None
        self._scope_paths: Optional[set[str]] = None

    def _trip(self, reason: BreakerReason, evidence: str, new_state: RunState) -> BreakerEvent:
        ev = BreakerEvent(
            reason=reason, task_id=self.task_id, evidence=evidence,
            timestamp=self._now_fn(), new_state=new_state,
            resume_command=self._resume_command,
        )
        self.events.append(ev)
        self.state = new_state
        return ev

    def record_file_reopen(self, path: str) -> Optional[BreakerEvent]:
        """Record that a file was reopened. Trip if beyond threshold."""
        if self.state != RunState.RUNNING:
            return None
        self._file_reopen_counts[path] = self._file_reopen_counts.get(path, 0) + 1
        if self._file_reopen_counts[path] > self._file_reopen_threshold:
            return self._trip(
                BreakerReason.FILE_REOPENED_BEYOND_THRESHOLD,
                f"{path} reopened {self._file_reopen_counts[path]} times",
                RunState.NO_PROGRESS,
            )
        return None

    def record_failing_test(self, test_name: str, diff_hash: str) -> Optional[BreakerEvent]:
        """Record a failing test with the current diff hash. Trip if same test
        fails twice with an unchanged diff (no progress)."""
        if self.state != RunState.RUNNING:
            return None
        prev = self._failing_test_diffs.get(test_name)
        if prev is not None and prev == diff_hash:
            return self._trip(
                BreakerReason.SAME_FAILING_TEST_UNCHANGED_DIFF,
                f"{test_name} failed twice with unchanged diff {diff_hash[:8]}",
                RunState.NO_PROGRESS,
            )
        self._failing_test_diffs[test_name] = diff_hash
        return None

    def record_question(self, question_hash: str) -> Optional[BreakerEvent]:
        """Record a question. Trip if the same question is repeated."""
        if self.state != RunState.RUNNING:
            return None
        if question_hash in self._question_hashes:
            return self._trip(
                BreakerReason.REPEATED_QUESTION,
                f"question {question_hash[:8]} repeated",
                RunState.NO_PROGRESS,
            )
        self._question_hashes.append(question_hash)
        return None

    def record_review_finding(self, finding_hash: str, after_correction: bool) -> Optional[BreakerEvent]:
        """Record a review finding. Trip if the same finding recurs after a correction."""
        if self.state != RunState.RUNNING:
            return None
        if after_correction and finding_hash in self._review_finding_hashes:
            return self._trip(
                BreakerReason.REPEATED_REVIEW_FINDING,
                f"finding {finding_hash[:8]} recurred after correction",
                RunState.NEEDS_HUMAN,
            )
        self._review_finding_hashes.append(finding_hash)
        return None

    def record_context_growth(self, context_bytes: int, scope_paths: set[str]) -> Optional[BreakerEvent]:
        """Record context size. Trip if it grew beyond ratio without scope change."""
        if self.state != RunState.RUNNING:
            return None
        if self._context_bytes_initial is None:
            self._context_bytes_initial = context_bytes
            self._scope_paths = set(scope_paths)
            self._context_bytes_last = context_bytes
            return None
        scope_changed = scope_paths != self._scope_paths
        if not scope_changed and context_bytes >= self._context_bytes_initial * self._context_growth_ratio:
            return self._trip(
                BreakerReason.CONTEXT_GROWTH_NO_SCOPE_CHANGE,
                f"context grew {context_bytes} from {self._context_bytes_initial} without scope change",
                RunState.NO_PROGRESS,
            )
        self._context_bytes_last = context_bytes
        if scope_changed:
            self._scope_paths = set(scope_paths)
        return None

    def record_cache_miss(self) -> Optional[BreakerEvent]:
        """Record a cache miss. Trip if a storm (many misses) occurs."""
        if self.state != RunState.RUNNING:
            return None
        self._cache_misses += 1
        if self._cache_misses >= self._cache_miss_storm:
            return self._trip(
                BreakerReason.CACHE_MISS_STORM,
                f"{self._cache_misses} consecutive cache misses",
                RunState.NO_PROGRESS,
            )
        return None

    def record_path_expansion(self, allowed_paths: set[str], touched_paths: set[str]) -> Optional[BreakerEvent]:
        """Trip if the task touched paths outside its allowed set."""
        if self.state != RunState.RUNNING:
            return None
        outside = touched_paths - allowed_paths
        if outside:
            return self._trip(
                BreakerReason.PATH_EXPANSION,
                f"touched paths outside allowed: {sorted(outside)[:5]}",
                RunState.BLOCKED,
            )
        return None

    def record_task_failure(self) -> Optional[BreakerEvent]:
        """Record a task failure. Trip after N consecutive failures."""
        if self.state != RunState.RUNNING:
            return None
        self._consecutive_failures_count += 1
        if self._consecutive_failures_count >= self._consecutive_failures:
            return self._trip(
                BreakerReason.TWO_CONSECUTIVE_FAILURES,
                f"{self._consecutive_failures_count} consecutive task failures",
                RunState.NEEDS_HUMAN,
            )
        return None

    def record_task_success(self) -> None:
        """Reset the consecutive-failure counter on success."""
        self._consecutive_failures_count = 0

    def record_budget_exceeded(self, detail: str) -> BreakerEvent:
        """Trip on budget exhaustion (tool/model/context/time)."""
        return self._trip(
            BreakerReason.BUDGET_EXCEEDED, detail, RunState.BUDGET_EXHAUSTED,
        )

    def record_c1_regression(self, detail: str) -> BreakerEvent:
        """Trip on a C1 blocking regression."""
        return self._trip(
            BreakerReason.C1_REGRESSION, detail, RunState.BLOCKED,
        )

    def complete(self) -> None:
        """Mark the run completed."""
        self.state = RunState.COMPLETED

    def is_terminal(self) -> bool:
        return self.state in (RunState.COMPLETED, RunState.BLOCKED,
                              RunState.BUDGET_EXHAUSTED, RunState.NEEDS_HUMAN)

    def resume_command(self) -> str:
        """The exact command to resume from the current state."""
        if self.state == RunState.RUNNING:
            return f"# task {self.task_id} still running"
        if self.state == RunState.COMPLETED:
            return f"# task {self.task_id} completed; nothing to resume"
        return self._resume_command


class BreakerStore:
    """Persists breaker events outside repos (append-only audit)."""

    def __init__(self, state_root):
        from pathlib import Path
        self.dir = Path(state_root).resolve() / "circuit_breakers"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"

    def record(self, event: BreakerEvent) -> None:
        append_line(self.events_path, __import__("json").dumps(event.to_dict(), ensure_ascii=False))

    def load_for_task(self, task_id: str) -> list[BreakerEvent]:
        if not self.events_path.is_file():
            return []
        import json
        out = []
        for line in self.events_path.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("task_id") == task_id:
                out.append(BreakerEvent(
                    reason=BreakerReason(d["reason"]), task_id=d["task_id"],
                    evidence=d.get("evidence", ""), timestamp=d.get("timestamp", ""),
                    new_state=RunState(d.get("new_state", "NO_PROGRESS")),
                    resume_command=d.get("resume_command", ""),
                ))
        return out
