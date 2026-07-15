"""Provider budget model and persistence.

Tracks per-provider budget state (remaining percent, run counts, status)
outside managed repositories. All state files live under the orchestrator
state directory. Atomic writes for snapshots; append-only for usage logs.

Budget schema version 1. Never claims exact token usage unless a verified
CLI explicitly returned it. Tracks only dispatch count, duration, result,
and available measured fields.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import List, Optional

from ..domain.events import now_iso


class BudgetStatus(str, Enum):
    AVAILABLE = "available"
    LIMITED = "limited"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class BudgetThresholds:
    """Threshold boundaries for budget status determination."""
    normal_min: float = 50.0        # >50%: normal
    reduced_min: float = 25.0       # 25-50%: reduced context
    priority_min: float = 10.0      # 10-25%: priority and small tasks only
    # <10%: reject new medium or large work

    DEFAULT = None  # set after class body


BudgetThresholds.DEFAULT = BudgetThresholds()


@dataclass
class ProviderBudget:
    """Versioned provider budget record.

    Schema version 1. All budget and routing state remains outside
    managed repositories.
    """
    schema_version: int = 1
    provider: str = ""
    source: str = "manual"  # manual | measured | inferred
    remaining_percent: Optional[float] = None
    reset_at: Optional[str] = None
    captured_at: str = ""
    expires_at: Optional[str] = None
    daily_runs: int = 0
    weekly_runs: int = 0
    status: str = BudgetStatus.UNKNOWN.value
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderBudget":
        return cls(
            schema_version=int(d.get("schema_version", 1)),
            provider=d.get("provider", ""),
            source=d.get("source", "manual"),
            remaining_percent=d.get("remaining_percent"),
            reset_at=d.get("reset_at"),
            captured_at=d.get("captured_at", ""),
            expires_at=d.get("expires_at"),
            daily_runs=int(d.get("daily_runs", 0)),
            weekly_runs=int(d.get("weekly_runs", 0)),
            status=d.get("status", BudgetStatus.UNKNOWN.value),
            notes=d.get("notes", ""),
        )

    def status_enum(self) -> BudgetStatus:
        try:
            return BudgetStatus(self.status)
        except ValueError:
            return BudgetStatus.UNKNOWN

    def is_fresh(self, max_age_hours: int = 24) -> bool:
        """True if captured_at is within max_age_hours of now."""
        if not self.captured_at:
            return False
        try:
            captured = datetime.fromisoformat(
                self.captured_at.replace("Z", "+00:00")
            )
            age = datetime.now(timezone.utc) - captured
            return age.total_seconds() < max_age_hours * 3600
        except (ValueError, OSError):
            return False


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        suffix=".tmp", prefix=path.stem, dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp).rename(path)
    except BaseException:
        os.unlink(tmp)
        raise


def _append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON line to an append-only log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")


class BudgetStore:
    """Persist and retrieve provider budgets outside repositories."""

    def __init__(self, state_root: Path):
        self.state_root = Path(state_root).resolve()
        self.providers_dir = self.state_root / "providers"
        self.providers_dir.mkdir(parents=True, exist_ok=True)

    def _budget_path(self, provider: str) -> Path:
        return self.providers_dir / provider / "provider_budget.json"

    def _usage_log_path(self, provider: str) -> Path:
        return self.providers_dir / provider / "provider_usage.jsonl"

    def _routing_log_path(self) -> Path:
        return self.providers_dir / "routing_decisions.jsonl"

    def save_budget(self, budget: ProviderBudget) -> None:
        """Atomically write the current budget snapshot."""
        _atomic_write_json(
            self._budget_path(budget.provider), budget.to_dict()
        )

    def load_budget(self, provider: str) -> Optional[ProviderBudget]:
        """Load the current budget for a provider. Returns None if missing."""
        path = self._budget_path(provider)
        if not path.is_file():
            return None
        try:
            return ProviderBudget.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None

    def load_all_budgets(self) -> List[ProviderBudget]:
        """Load budgets for all providers that have a budget file."""
        budgets = []
        if not self.providers_dir.is_dir():
            return budgets
        for provider_dir in self.providers_dir.iterdir():
            if not provider_dir.is_dir():
                continue
            budget_file = provider_dir / "provider_budget.json"
            if not budget_file.is_file():
                continue
            budget = self.load_budget(provider_dir.name)
            if budget is not None:
                budgets.append(budget)
        return budgets

    def set_budget(
        self,
        provider: str,
        remaining_percent: Optional[float],
        source: str = "manual",
        expires_at: Optional[str] = None,
        notes: str = "",
    ) -> ProviderBudget:
        """Set or update a provider budget. Returns the new budget."""
        existing = self.load_budget(provider)
        status = _classify_status(remaining_percent)
        budget = ProviderBudget(
            provider=provider,
            source=source,
            remaining_percent=remaining_percent,
            captured_at=now_iso(),
            expires_at=expires_at,
            daily_runs=existing.daily_runs if existing else 0,
            weekly_runs=existing.weekly_runs if existing else 0,
            status=status.value,
            notes=notes,
        )
        self.save_budget(budget)
        return budget

    def record_usage(
        self,
        provider: str,
        task_id: str,
        duration_seconds: float,
        result: str,  # ok | failed | timeout
        extra: Optional[dict] = None,
    ) -> None:
        """Append a usage record to the provider's append-only log."""
        record = {
            "ts": now_iso(),
            "provider": provider,
            "task_id": task_id,
            "duration_seconds": duration_seconds,
            "result": result,
        }
        if extra:
            record["extra"] = extra
        _append_jsonl(self._usage_log_path(provider), record)

    def record_routing_decision(self, decision: dict) -> None:
        """Append a routing decision to the global append-only log."""
        decision["ts"] = now_iso()
        _append_jsonl(self._routing_log_path(), decision)

    def snapshot_path(self, provider: str) -> Path:
        """Path to the budget snapshot file (for reference)."""
        return self._budget_path(provider)


def _classify_status(remaining: Optional[float]) -> BudgetStatus:
    """Classify budget status from remaining percent."""
    if remaining is None:
        return BudgetStatus.UNKNOWN
    if remaining <= 0:
        return BudgetStatus.EXHAUSTED
    t = BudgetThresholds.DEFAULT
    if remaining > t.normal_min:
        return BudgetStatus.AVAILABLE
    if remaining > t.reduced_min:
        return BudgetStatus.LIMITED
    if remaining > t.priority_min:
        return BudgetStatus.LIMITED
    return BudgetStatus.EXHAUSTED


def status_for_task(
    budget: ProviderBudget,
    task_size: str,
) -> BudgetStatus:
    """Determine effective budget status for a given task size.

    Returns EXHAUSTED for medium/large tasks when budget <10%.
    """
    base = budget.status_enum()
    remaining = budget.remaining_percent
    if remaining is None:
        return base
    t = BudgetThresholds.DEFAULT
    if task_size in ("medium", "large") and remaining < t.priority_min:
        return BudgetStatus.EXHAUSTED
    return base
