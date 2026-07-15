"""Autopilot engine — unattended bounded execution of the full pipeline.

Phase I. Orchestrates the complete cycle for each queue item:
  Task → Plan → Dispatch → Review → One bounded fix → Validation →
  AWAITING_APPROVAL → Queue next task.

Critical safety boundaries (NEVER crossed by autopilot):
- NEVER auto-approves (never transitions to APPROVED).
- NEVER auto-publishes PR (never calls publish-pr).
- NEVER auto-commits or auto-pushes to the source repo.
- NEVER auto-merges.

Autopilot stops at AWAITING_APPROVAL for each item, logs the outcome, and
moves to the next queue item. A human must explicitly approve each item
before it can proceed to publish/merge.

Bounded runtime:
- max_tasks: maximum items to process per run.
- max_minutes: wall-clock limit.
- poll_seconds: sleep between items when waiting for new work.

Reuses existing primitives: SchedulerEngine, CriticEngine, QueueStore,
ScheduleStore. No duplication.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..domain.events import now_iso
from ..storage.atomic import atomic_write_json, append_line
from .queue import (
    QueueItem, QueueItemStatus, QueueStore, ReasonCode, SchedulerEngine,
)
from .critic import CriticEngine, CriticVerdict


# ---------------------------------------------------------------------------
# Autopilot run record
# ---------------------------------------------------------------------------

@dataclass
class AutopilotItemResult:
    """The result of processing one queue item."""
    item_id: str = ""
    project_id: str = ""
    verdict: str = ""             # PASS / FAIL / FIX / skipped
    final_state: str = ""         # AWAITING_APPROVAL / FAILED
    task_id: Optional[str] = None
    error: str = ""
    duration_seconds: float = 0.0
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "project_id": self.project_id,
            "verdict": self.verdict,
            "final_state": self.final_state,
            "task_id": self.task_id,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class AutopilotRun:
    """A complete autopilot run record."""
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    items_processed: int = 0
    items_awaiting_approval: int = 0
    items_failed: int = 0
    items_skipped: int = 0
    stop_reason: str = ""
    max_tasks: int = 0
    max_minutes: int = 0
    results: List[dict] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "items_processed": self.items_processed,
            "items_awaiting_approval": self.items_awaiting_approval,
            "items_failed": self.items_failed,
            "items_skipped": self.items_skipped,
            "stop_reason": self.stop_reason,
            "max_tasks": self.max_tasks,
            "max_minutes": self.max_minutes,
            "results": self.results,
        }


# ---------------------------------------------------------------------------
# Autopilot engine
# ---------------------------------------------------------------------------

class AutopilotEngine:
    """Unattended bounded autopilot.

    Processes queue items through the full pipeline. Stops at
    AWAITING_APPROVAL for each item. Never approves, publishes, or merges.
    """

    STOP_REASONS = {
        "idle": "queue idle, no runnable items",
        "max_tasks": "max_tasks reached",
        "max_minutes": "max_minutes reached",
        "error": "unrecoverable error",
        "manual": "manual stop",
    }

    def __init__(self, state_root: Path, task_store: Any,
                 now_fn=None, sleep_fn=None):
        self.state_root = Path(state_root).expanduser().resolve()
        self.task_store = task_store
        self.now_fn = now_fn or now_iso
        self.sleep_fn = sleep_fn or time.sleep
        self.scheduler = SchedulerEngine(self.state_root, now_fn=now_fn)
        self.critic = CriticEngine(task_store, now_fn=now_fn)
        self.queue_store = QueueStore(self.state_root)
        self._stop_requested = False

    def request_stop(self) -> None:
        """Request a graceful stop after the current item."""
        self._stop_requested = True

    # -- Main loop --------------------------------------------------------- #

    def run(self, queue_id: str, profile: Any, budget_store: Any,
            worktree_parent: Path,
            max_tasks: int = 10,
            max_minutes: int = 240,
            poll_seconds: int = 0,
            reviewer_executable: Optional[str] = None,
            fixer_executable: Optional[str] = None,
            codex_available: bool = False,
            codex_verified: bool = False,
            opencode_available: bool = False,
            opencode_verified: bool = False,
            ) -> AutopilotRun:
        """Run the autopilot loop until idle or bounds reached.

        Each item is processed through: select → execute → critic review.
        Stops at AWAITING_APPROVAL. Never approves.
        """
        import uuid
        run = AutopilotRun(
            run_id=uuid.uuid4().hex[:12],
            started_at=self.now_fn(),
            max_tasks=max_tasks,
            max_minutes=max_minutes,
        )
        start_time = time.time()

        while not self._stop_requested:
            # Check bounds.
            if run.items_processed >= max_tasks:
                run.stop_reason = self.STOP_REASONS["max_tasks"]
                break
            elapsed_minutes = (time.time() - start_time) / 60
            if elapsed_minutes >= max_minutes:
                run.stop_reason = self.STOP_REASONS["max_minutes"]
                break

            # Select and execute one item.
            item_result = self._process_one(
                queue_id, profile, budget_store, worktree_parent,
                reviewer_executable, fixer_executable,
                codex_available, codex_verified,
                opencode_available, opencode_verified)

            if item_result is None:
                # Queue idle.
                run.stop_reason = self.STOP_REASONS["idle"]
                break

            run.items_processed += 1
            if item_result.final_state == "AWAITING_APPROVAL":
                run.items_awaiting_approval += 1
            elif item_result.final_state == "FAILED":
                run.items_failed += 1
            elif item_result.verdict == "skipped":
                run.items_skipped += 1
            run.results.append(item_result.to_dict())

            # Poll between items if configured.
            if poll_seconds > 0 and not self._stop_requested:
                self.sleep_fn(poll_seconds)

        if self._stop_requested and not run.stop_reason:
            run.stop_reason = self.STOP_REASONS["manual"]

        run.finished_at = self.now_fn()
        self._persist_run(queue_id, run)
        return run

    def _process_one(self, queue_id: str, profile: Any,
                     budget_store: Any, worktree_parent: Path,
                     reviewer_executable: Optional[str],
                     fixer_executable: Optional[str],
                     codex_available: bool, codex_verified: bool,
                     opencode_available: bool,
                     opencode_verified: bool) -> Optional[AutopilotItemResult]:
        """Process a single queue item through the full pipeline."""
        import time as _time
        start = _time.time()
        # Use the scheduler to select + execute.
        scheduler_run = self.scheduler.run_next(
            queue_id, profile, self.task_store, budget_store,
            worktree_parent,
            reviewer_executable=reviewer_executable,
            fixer_executable=fixer_executable,
            codex_available=codex_available,
            codex_verified=codex_verified,
            opencode_available=opencode_available,
            opencode_verified=opencode_verified,
        )
        if scheduler_run.tasks_completed == 0 and scheduler_run.tasks_failed == 0:
            return None  # Idle.

        # Find the item that was processed (most recent RUNNING → terminal).
        items = self.queue_store.load_queue(queue_id)
        processed = None
        for item in items:
            if item.status in (QueueItemStatus.AWAITING_APPROVAL.value,
                               QueueItemStatus.FAILED.value):
                if item.started_at and item.finished_at:
                    processed = item
                    break
        if processed is None:
            return None

        result = AutopilotItemResult(
            item_id=processed.item_id,
            project_id=processed.project_id,
            task_id=processed.task_id,
            final_state=processed.status,
            duration_seconds=round(_time.time() - start, 3),
        )
        if processed.status == QueueItemStatus.AWAITING_APPROVAL.value:
            result.verdict = CriticVerdict.PASS.value
        else:
            result.verdict = CriticVerdict.FAIL.value
            result.error = processed.last_error or ""

        # SAFETY INVARIANT: never approve. Log that we stopped at approval.
        if processed.status == QueueItemStatus.AWAITING_APPROVAL.value:
            self._audit_stop(queue_id, processed)

        return result

    # -- Persistence ------------------------------------------------------- #

    def _persist_run(self, queue_id: str, run: AutopilotRun) -> None:
        """Persist the autopilot run record."""
        # Atomic snapshot.
        run_dir = self.state_root / "autopilot" / run.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(run_dir / "autopilot_run.json", run.to_dict())
        # Append to audit log.
        append_line(
            self.state_root / "autopilot_runs.jsonl",
            json.dumps(run.to_dict(), sort_keys=True))

    def _audit_stop(self, queue_id: str, item: QueueItem) -> None:
        """Record that autopilot stopped at AWAITING_APPROVAL (never approves)."""
        append_line(
            self.state_root / "autopilot_stops.jsonl",
            json.dumps({
                "ts": self.now_fn(),
                "queue_id": queue_id,
                "item_id": item.item_id,
                "project_id": item.project_id,
                "task_id": item.task_id,
                "reason": "autopilot_stopped_at_awaiting_approval",
                "note": "human approval required; autopilot never auto-approves",
            }, sort_keys=True))

    # -- Safety verification ---------------------------------------------- #

    def assert_no_auto_approval(self, queue_id: str) -> bool:
        """Verify no item in the queue was auto-approved.

        Returns True if all items are AWAITING_APPROVAL or FAILED (never
        APPROVED/COMPLETED).
        """
        items = self.queue_store.load_queue(queue_id)
        for item in items:
            if item.task_id:
                try:
                    meta = self.task_store.load(item.project_id, item.task_id)
                    if meta.state in ("APPROVED", "COMPLETED"):
                        return False
                except KeyError:
                    pass
        return True
