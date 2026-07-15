"""Recurring scheduler — cron-driven task scheduling for the durable queue.

Phase B. A minimal launchd/cron-style recurring scheduler that:
- Parses 5-field cron expressions (minute hour day-of-month month day-of-week).
- Computes the next fire time deterministically.
- Enqueues recurring items into the durable QueueStore with deduplication.
- Applies exponential backoff for retry scheduling.
- Supports batch enqueue (multiple schedules at once).
- Bounded runtime (max schedules per invocation).
- Startup recovery (resume schedules that missed their window).

Design principles (per project invariants):
- SQLite is the runtime index only; JSON + JSONL remain the audit trail.
- No external dependencies. Pure stdlib cron field math.
- Deterministic and offline-testable.
- Fails closed.

The cron parser implements the standard 5-field algorithm (POSIX crontab),
supporting wildcards (*), ranges (1-5), lists (1,3,5), and step values (*/15).
This is a well-known battle-tested algorithm; no third-party code is copied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Tuple

from ..domain.events import now_iso
from ..storage.atomic import atomic_write_json, append_line
from .queue import QueueItem, QueueItemStatus, QueueStore


# ---------------------------------------------------------------------------
# Cron expression parser (5-field)
# ---------------------------------------------------------------------------

# Field ranges per POSIX crontab.
_FIELD_RANGES = [
    (0, 59),    # minute
    (0, 23),    # hour
    (1, 31),    # day of month
    (1, 12),    # month
    (0, 6),     # day of week (0=Sun .. 6=Sat; 7 also accepted as Sun)
]


class CronParseError(ValueError):
    """Raised when a cron expression cannot be parsed."""


def _parse_field(expr: str, lo: int, hi: int) -> set:
    """Parse one cron field into a set of integer values.

    Supports: * (wildcard), a-b (range), a,b,c (list), */n (step),
    a-b/n (stepped range), and single values. Names are not supported
    (use numeric values).
    """
    if not expr:
        raise CronParseError("empty cron field")
    values: set = set()
    for part in expr.split(","):
        part = part.strip()
        if part == "*":
            step = 1
            r_lo, r_hi = lo, hi
        elif "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                raise CronParseError(f"invalid step {step_s!r}")
            if step <= 0:
                raise CronParseError(f"step must be positive: {step}")
            if base == "*" or base == "":
                r_lo, r_hi = lo, hi
            elif "-" in base:
                a, _, b = base.partition("-")
                r_lo, r_hi = int(a), int(b)
            else:
                r_lo = int(base)
                r_hi = hi
        elif "-" in part:
            a, _, b = part.partition("-")
            r_lo, r_hi = int(a), int(b)
            step = 1
        else:
            try:
                v = int(part)
            except ValueError:
                raise CronParseError(f"invalid value {part!r}")
            values.add(v)
            continue
        if r_lo < lo or r_hi > hi + (1 if hi == 6 else 0):
            # Allow 7 in dow as alias for 0 (Sunday).
            if not (lo == 0 and hi == 6 and r_hi == 7):
                raise CronParseError(
                    f"value out of range [{lo},{hi}]: {part}")
        if r_hi < r_lo:
            raise CronParseError(f"invalid range {r_lo}-{r_hi}")
        values.update(range(r_lo, r_hi + 1, step))
    # Normalize dow 7 → 0.
    if lo == 0 and hi == 6 and 7 in values:
        values.discard(7)
        values.add(0)
    # Validate.
    for v in values:
        if v < lo or v > hi:
            if lo == 0 and hi == 6 and v == 0:
                continue
            raise CronParseError(f"value {v} out of range [{lo},{hi}]")
    return values


@dataclass
class CronSchedule:
    """A parsed 5-field cron expression."""
    minute: set
    hour: set
    dom: set
    month: set
    dow: set

    @classmethod
    def parse(cls, expr: str) -> "CronSchedule":
        """Parse a cron expression string into a CronSchedule.

        Five whitespace-separated fields:
        minute hour day-of-month month day-of-week
        """
        parts = str(expr).split()
        if len(parts) != 5:
            raise CronParseError(
                f"expected 5 fields, got {len(parts)}: {expr!r}")
        fields = []
        for i, part in enumerate(parts):
            lo, hi = _FIELD_RANGES[i]
            fields.append(_parse_field(part, lo, hi))
        return cls(*fields)

    def matches(self, dt: datetime) -> bool:
        """Return True if this schedule fires at the given datetime (minute precision)."""
        return (dt.minute in self.minute
                and dt.hour in self.hour
                and dt.day in self.dom
                and dt.month in self.month
                and (dt.weekday() + 1) % 7 in self.dow)

    def next_fire(self, after: datetime) -> datetime:
        """Compute the next fire time strictly after `after`.

        Scans minute by minute up to 366 days (worst case Feb-29 schedule).
        Returns a tz-aware UTC datetime.
        """
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        # Start from the next minute, second=0.
        candidate = (after.replace(second=0, microsecond=0)
                     + timedelta(minutes=1))
        limit = after + timedelta(days=366)
        while candidate <= limit:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise CronParseError(
            "no fire time within 366 days; schedule may be invalid")


# ---------------------------------------------------------------------------
# Recurring schedule definitions
# ---------------------------------------------------------------------------

class ScheduleStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


@dataclass
class RecurringSchedule:
    """A recurring schedule that enqueues items into the durable queue."""
    schema_version: int = 1
    schedule_id: str = ""
    queue_id: str = ""
    project_id: str = ""
    cron: str = ""
    item_template: dict = field(default_factory=dict)
    # item_template contains: title, request_file, done_criteria, category,
    #   size, priority, dependencies, implementation_engine, review_engine,
    #   fix_engine, max_fixes, source_revision
    status: str = ScheduleStatus.ACTIVE.value
    last_fired_at: Optional[str] = None
    next_fire_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    # Dedup window: do not enqueue if an item was enqueued for this schedule
    # within the last N seconds.
    dedup_seconds: int = 0
    # Backoff: exponential retry scheduling for failed items.
    backoff_base_seconds: int = 60
    backoff_max_seconds: int = 3600
    backoff_multiplier: float = 2.0
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "schedule_id": self.schedule_id,
            "queue_id": self.queue_id,
            "project_id": self.project_id,
            "cron": self.cron,
            "item_template": self.item_template,
            "status": self.status,
            "last_fired_at": self.last_fired_at,
            "next_fire_at": self.next_fire_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dedup_seconds": self.dedup_seconds,
            "backoff_base_seconds": self.backoff_base_seconds,
            "backoff_max_seconds": self.backoff_max_seconds,
            "backoff_multiplier": self.backoff_multiplier,
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RecurringSchedule":
        return cls(
            schema_version=d.get("schema_version", 1),
            schedule_id=d.get("schedule_id", ""),
            queue_id=d.get("queue_id", ""),
            project_id=d.get("project_id", ""),
            cron=d.get("cron", ""),
            item_template=d.get("item_template", {}),
            status=d.get("status", ScheduleStatus.ACTIVE.value),
            last_fired_at=d.get("last_fired_at"),
            next_fire_at=d.get("next_fire_at"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            dedup_seconds=d.get("dedup_seconds", 0),
            backoff_base_seconds=d.get("backoff_base_seconds", 60),
            backoff_max_seconds=d.get("backoff_max_seconds", 3600),
            backoff_multiplier=d.get("backoff_multiplier", 2.0),
            consecutive_failures=d.get("consecutive_failures", 0),
        )


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------

def compute_backoff(attempt: int, base: int = 60, max_seconds: int = 3600,
                    multiplier: float = 2.0) -> int:
    """Compute exponential backoff in seconds for a given attempt number.

    attempt is 0-indexed (attempt=0 → base). Capped at max_seconds.
    """
    if attempt < 0:
        attempt = 0
    delay = base * (multiplier ** attempt)
    delay = int(delay)
    return min(delay, max_seconds)


# ---------------------------------------------------------------------------
# Schedule store — durable persistence of recurring schedules
# ---------------------------------------------------------------------------

class ScheduleStore:
    """Durable storage for recurring schedules.

    Schedules persisted as JSON snapshot + JSONL audit (same pattern as
    QueueStore). The schedule_id is unique per store.
    """

    def __init__(self, state_root: Path):
        self.state_root = Path(state_root).expanduser().resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.schedules_dir = self.state_root / "schedules"
        self.schedules_dir.mkdir(parents=True, exist_ok=True)
        self._lock = __import__("threading").Lock()

    def _snapshot_path(self) -> Path:
        return self.schedules_dir / "schedules.json"

    def _events_path(self) -> Path:
        return self.schedules_dir / "schedule_events.jsonl"

    def _write_snapshot(self, schedules: List[RecurringSchedule]) -> None:
        atomic_write_json(
            self._snapshot_path(),
            {"schema_version": 1,
             "schedules": [s.to_dict() for s in schedules]})

    def _append_event(self, event: dict) -> None:
        event["ts"] = now_iso()
        append_line(self._events_path(), json.dumps(event, sort_keys=True))

    def add_schedule(self, schedule: RecurringSchedule) -> RecurringSchedule:
        """Add a recurring schedule. Validates cron and uniqueness."""
        if not schedule.schedule_id:
            raise ValueError("schedule_id is required")
        # Validate cron expression.
        CronSchedule.parse(schedule.cron)
        with self._lock:
            existing = self.load_schedules()
            for s in existing:
                if s.schedule_id == schedule.schedule_id:
                    raise ValueError(
                        f"duplicate schedule_id {schedule.schedule_id!r}")
            ts = now_iso()
            if not schedule.created_at:
                schedule.created_at = ts
            schedule.updated_at = ts
            existing.append(schedule)
            self._write_snapshot(existing)
            self._append_event({
                "event": "schedule_added",
                "schedule_id": schedule.schedule_id,
            })
        return schedule

    def load_schedules(self,
                       status: Optional[str] = None) -> List[RecurringSchedule]:
        path = self._snapshot_path()
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        schedules = [RecurringSchedule.from_dict(d)
                     for d in data.get("schedules", [])]
        if status:
            schedules = [s for s in schedules if s.status == status]
        return schedules

    def load_schedule(self, schedule_id: str) -> RecurringSchedule:
        for s in self.load_schedules():
            if s.schedule_id == schedule_id:
                return s
        raise KeyError(f"schedule {schedule_id!r} not found")

    def update_schedule(self, schedule_id: str,
                        **fields) -> RecurringSchedule:
        with self._lock:
            schedules = self.load_schedules()
            found = None
            for i, s in enumerate(schedules):
                if s.schedule_id == schedule_id:
                    found = i
                    break
            if found is None:
                raise KeyError(f"schedule {schedule_id!r} not found")
            schedule = schedules[found]
            for k, v in fields.items():
                setattr(schedule, k, v)
            schedule.updated_at = now_iso()
            schedules[found] = schedule
            self._write_snapshot(schedules)
        return schedule

    def remove_schedule(self, schedule_id: str) -> bool:
        with self._lock:
            schedules = self.load_schedules()
            new = [s for s in schedules if s.schedule_id != schedule_id]
            if len(new) == len(schedules):
                return False
            self._write_snapshot(new)
            self._append_event({
                "event": "schedule_removed",
                "schedule_id": schedule_id,
            })
        return True

    def pause_schedule(self, schedule_id: str) -> RecurringSchedule:
        self._append_event({
            "event": "schedule_paused",
            "schedule_id": schedule_id,
        })
        return self.update_schedule(schedule_id,
                                    status=ScheduleStatus.PAUSED.value)

    def resume_schedule(self, schedule_id: str) -> RecurringSchedule:
        self._append_event({
            "event": "schedule_resumed",
            "schedule_id": schedule_id,
        })
        return self.update_schedule(schedule_id,
                                    status=ScheduleStatus.ACTIVE.value)


# ---------------------------------------------------------------------------
# Recurring scheduler engine
# ---------------------------------------------------------------------------

class RecurringSchedulerEngine:
    """Evaluates recurring schedules and enqueues due items.

    Bounded: processes at most `max_schedules` schedules per invocation.
    Deduplicates within `dedup_seconds`. Applies backoff on consecutive
    failures by setting not_before on the next enqueue.
    """

    def __init__(self, state_root: Path, now_fn=None):
        self.state_root = Path(state_root).expanduser().resolve()
        self.schedule_store = ScheduleStore(self.state_root)
        self.queue_store = QueueStore(self.state_root)
        self.now_fn = now_fn or now_iso

    def _parse_iso(self, ts: str) -> Optional[datetime]:
        if not ts:
            return None
        try:
            # Normalize Z suffix.
            ts_clean = ts.replace("Z", "+00:00")
            return datetime.fromisoformat(ts_clean)
        except ValueError:
            return None

    def _now_dt(self) -> datetime:
        return self._parse_iso(self.now_fn()) or datetime.now(timezone.utc)

    def _dedup_active(self, schedule: RecurringSchedule,
                      now_dt: datetime) -> bool:
        """Return True if a recent item exists (dedup window active)."""
        if schedule.dedup_seconds <= 0:
            return False
        if not schedule.last_fired_at:
            return False
        last = self._parse_iso(schedule.last_fired_at)
        if last is None:
            return False
        age = (now_dt - last).total_seconds()
        return age < schedule.dedup_seconds

    def evaluate(self, max_schedules: int = 100,
                 queue_store: Optional[QueueStore] = None) -> List[dict]:
        """Evaluate all ACTIVE schedules and enqueue due items.

        Returns a list of records: {schedule_id, item_id, action, reason}.
        Actions: "enqueued", "skipped_dedup", "skipped_paused",
                 "skipped_not_due", "error".
        """
        qstore = queue_store or self.queue_store
        now_dt = self._now_dt()
        now_str = self.now_fn()
        records: List[dict] = []
        schedules = self.schedule_store.load_schedules(
            status=ScheduleStatus.ACTIVE.value)
        count = 0
        for schedule in schedules:
            if count >= max_schedules:
                records.append({
                    "schedule_id": schedule.schedule_id,
                    "action": "skipped_max_reached",
                    "reason": "max_schedules reached",
                })
                continue
            count += 1
            try:
                record = self._evaluate_one(
                    schedule, qstore, now_dt, now_str)
            except Exception as exc:
                record = {
                    "schedule_id": schedule.schedule_id,
                    "action": "error",
                    "reason": str(exc),
                }
            records.append(record)
        return records

    def _evaluate_one(self, schedule: RecurringSchedule,
                      qstore: QueueStore, now_dt: datetime,
                      now_str: str) -> dict:
        """Evaluate a single schedule."""
        cron = CronSchedule.parse(schedule.cron)
        # Check if due: next_fire_at <= now, OR last_fired_at's next fire <= now.
        due = self._is_due(schedule, cron, now_dt)
        if not due:
            # Update next_fire_at for observability.
            nxt = cron.next_fire(now_dt)
            self.schedule_store.update_schedule(
                schedule.schedule_id, next_fire_at=nxt.isoformat())
            return {"schedule_id": schedule.schedule_id,
                    "action": "skipped_not_due",
                    "reason": "not due"}

        # Dedup check.
        if self._dedup_active(schedule, now_dt):
            return {"schedule_id": schedule.schedule_id,
                    "action": "skipped_dedup",
                    "reason": "dedup window active"}

        # Backoff check: if consecutive failures, apply not_before.
        not_before = None
        if schedule.consecutive_failures > 0:
            backoff = compute_backoff(
                schedule.consecutive_failures - 1,
                base=schedule.backoff_base_seconds,
                max_seconds=schedule.backoff_max_seconds,
                multiplier=schedule.backoff_multiplier)
            nb_dt = now_dt + timedelta(seconds=backoff)
            not_before = nb_dt.isoformat()

        # Enqueue item.
        item_id = self._make_item_id(schedule, now_dt)
        tpl = schedule.item_template
        item = QueueItem(
            queue_id=schedule.queue_id,
            item_id=item_id,
            project_id=schedule.project_id,
            title=tpl.get("title", schedule.schedule_id),
            request_file=tpl.get("request_file", ""),
            done_criteria=tpl.get("done_criteria", []),
            source_revision=tpl.get("source_revision", "HEAD"),
            category=tpl.get("category", "implementation"),
            size=tpl.get("size", "small"),
            priority=tpl.get("priority", 50),
            dependencies=tpl.get("dependencies", []),
            implementation_engine=tpl.get("implementation_engine", "auto"),
            review_engine=tpl.get("review_engine", "auto"),
            fix_engine=tpl.get("fix_engine", "auto"),
            max_fixes=tpl.get("max_fixes", 1),
            not_before=not_before,
        )
        try:
            qstore.add_item(item)
        except ValueError as exc:
            # Duplicate item_id — dedup by existence.
            return {"schedule_id": schedule.schedule_id,
                    "action": "skipped_dedup",
                    "reason": f"item exists: {exc}",
                    "item_id": item_id}

        # Update schedule state.
        nxt = cron.next_fire(now_dt)
        self.schedule_store.update_schedule(
            schedule.schedule_id,
            last_fired_at=now_str,
            next_fire_at=nxt.isoformat())
        self.schedule_store._append_event({
            "event": "schedule_fired",
            "schedule_id": schedule.schedule_id,
            "item_id": item_id,
        })
        return {"schedule_id": schedule.schedule_id,
                "action": "enqueued",
                "item_id": item_id,
                "not_before": not_before}

    def _is_due(self, schedule: RecurringSchedule,
                cron: CronSchedule, now_dt: datetime) -> bool:
        """Determine if a schedule is due to fire now."""
        # If never fired, due if next_fire_at (planned) <= now OR if no
        # next_fire_at, fire immediately (first run).
        if not schedule.last_fired_at:
            if schedule.next_fire_at:
                planned = self._parse_iso(schedule.next_fire_at)
                if planned and planned > now_dt:
                    return False
            return True
        # Compute next fire after last fire; if that's <= now, due.
        last = self._parse_iso(schedule.last_fired_at)
        if last is None:
            return True
        expected = cron.next_fire(last)
        return expected <= now_dt

    def _make_item_id(self, schedule: RecurringSchedule,
                      now_dt: datetime) -> str:
        """Generate a deterministic, unique item_id for a scheduled fire."""
        stamp = now_dt.strftime("%Y%m%d%H%M")
        return f"{schedule.schedule_id}_{stamp}"

    # -- Backoff feedback -------------------------------------------------- #

    def record_item_outcome(self, schedule_id: str, success: bool) -> None:
        """Feed an item outcome back to the schedule for backoff tracking.

        On success, resets consecutive_failures to 0. On failure, increments.
        """
        try:
            schedule = self.schedule_store.load_schedule(schedule_id)
        except KeyError:
            return
        if success:
            new_failures = 0
        else:
            new_failures = schedule.consecutive_failures + 1
        self.schedule_store.update_schedule(
            schedule_id, consecutive_failures=new_failures)

    # -- Startup recovery -------------------------------------------------- #

    def recover(self) -> List[dict]:
        """Startup recovery: evaluate all schedules that missed their window.

        On startup, call this to enqueue any items that should have fired
        while the scheduler was down. This is a bounded evaluate() with a
        high max_schedules limit.
        """
        # Mark all schedules as needing re-evaluation by recomputing
        # next_fire_at. The evaluate() will enqueue due items.
        return self.evaluate(max_schedules=10000)
