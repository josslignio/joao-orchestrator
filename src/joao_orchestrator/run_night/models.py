"""Typed contracts for authenticated, bounded Run Night operation."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Any

from ..evaluation.models import sha256_json, validate_safe_id

SCHEMA_VERSION = 2


class RunNightMode(str, Enum):
    REHEARSAL = "rehearsal"
    READ_ONLY = "read_only"


class RunNightState(str, Enum):
    PREFLIGHT_PASSED = "preflight_passed"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    BLOCKED = "blocked"


class NightTaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


class ArtifactKind(str, Enum):
    REHEARSAL_RECEIPT = "rehearsal_receipt"
    ARCHITECTURE_PACKET = "architecture_packet"
    TEST_PLAN = "test_plan"
    IMPLEMENTATION_PACKET = "implementation_packet"


@dataclass(frozen=True)
class RunNightLimits:
    max_duration_minutes: int = 240
    hard_duration_minutes: int = 300
    max_tasks: int = 4
    max_provider_calls: int = 12
    max_codex_calls: int = 3
    max_consecutive_failures: int = 1
    max_context_bytes: int = 2 * 1024 * 1024
    max_artifact_bytes: int = 256 * 1024
    max_concurrency: int = 1

    def validate(self) -> None:
        values = self.to_dict()
        for name, value in values.items():
            if int(value) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.hard_duration_minutes < self.max_duration_minutes:
            raise ValueError("hard_duration_minutes must be >= max_duration_minutes")
        if self.max_concurrency != 1:
            raise ValueError("Run Night Master V2 requires sequential execution")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_duration_minutes": self.max_duration_minutes,
            "hard_duration_minutes": self.hard_duration_minutes,
            "max_tasks": self.max_tasks,
            "max_provider_calls": self.max_provider_calls,
            "max_codex_calls": self.max_codex_calls,
            "max_consecutive_failures": self.max_consecutive_failures,
            "max_context_bytes": self.max_context_bytes,
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_concurrency": self.max_concurrency,
        }


@dataclass(frozen=True)
class NightTask:
    task_id: str
    project_id: str
    objective: str
    execution_root: str
    artifact_kind: str
    supervisor_mode: str = "builder_reviewer"
    preferred_provider: str | None = None
    provider_names: tuple[str, ...] = ()
    judge_provider: str | None = None
    max_provider_calls: int = 4
    require_independent_review: bool = True
    sensitive: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        validate_safe_id(self.task_id, "task_id")
        validate_safe_id(self.project_id, "project_id")
        ArtifactKind(self.artifact_kind)
        if not self.objective.strip():
            raise ValueError("task objective is required")
        if not self.execution_root.strip():
            raise ValueError("execution_root is required")
        if self.max_provider_calls <= 0:
            raise ValueError("task max_provider_calls must be positive")
        if self.supervisor_mode not in {
            "direct", "auto", "challenge", "council", "builder_reviewer"
        }:
            raise ValueError("unsupported supervisor mode")
        if self.require_independent_review and self.supervisor_mode != "builder_reviewer":
            raise ValueError("independent review requires builder_reviewer mode")

    @property
    def objective_sha256(self) -> str:
        return sha256(self.objective.encode("utf-8")).hexdigest()

    def persisted_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "objective_sha256": self.objective_sha256,
            "objective_chars": len(self.objective),
            "execution_root": self.execution_root,
            "artifact_kind": self.artifact_kind,
            "supervisor_mode": self.supervisor_mode,
            "preferred_provider": self.preferred_provider,
            "provider_names": list(self.provider_names),
            "judge_provider": self.judge_provider,
            "max_provider_calls": self.max_provider_calls,
            "require_independent_review": self.require_independent_review,
            "sensitive": self.sensitive,
            "metadata_sha256": sha256_json(self.metadata),
            "metadata_keys": sorted(str(key) for key in self.metadata),
        }


@dataclass(frozen=True)
class RunNightSpec:
    run_id: str
    authorized_sha: str
    mode: str
    state_root: str
    repo_root: str
    tranche3_evidence_path: str
    tranche3_closure_path: str
    runnight_evidence_path: str
    runnight_closure_path: str
    tasks: tuple[NightTask, ...]
    limits: RunNightLimits = field(default_factory=RunNightLimits)
    publication_mode: str = "none"
    stop_on_sensitive: bool = True
    require_clean_repo: bool = True
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        validate_safe_id(self.run_id, "run_id")
        if len(self.authorized_sha) != 40 or any(
            char not in "0123456789abcdef" for char in self.authorized_sha
        ):
            raise ValueError("authorized_sha must be a lowercase 40-character git SHA")
        RunNightMode(self.mode)
        if not self.state_root.strip() or not self.repo_root.strip():
            raise ValueError("state_root and repo_root are required")
        for name in (
            "tranche3_evidence_path", "tranche3_closure_path",
            "runnight_evidence_path", "runnight_closure_path",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.publication_mode != "none":
            raise ValueError("Run Night Master forbids publication")
        self.limits.validate()
        if not self.tasks:
            raise ValueError("at least one task is required")
        if len(self.tasks) > self.limits.max_tasks:
            raise ValueError("task count exceeds max_tasks")
        seen: set[str] = set()
        for task in self.tasks:
            task.validate()
            if task.task_id in seen:
                raise ValueError(f"duplicate task_id: {task.task_id}")
            seen.add(task.task_id)

    def persisted_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "authorized_sha": self.authorized_sha,
            "mode": self.mode,
            "state_root": self.state_root,
            "repo_root": self.repo_root,
            "tranche3_evidence_sha256": sha256(
                self.tranche3_evidence_path.encode()
            ).hexdigest(),
            "tranche3_closure_sha256": sha256(
                self.tranche3_closure_path.encode()
            ).hexdigest(),
            "runnight_evidence_sha256": sha256(
                self.runnight_evidence_path.encode()
            ).hexdigest(),
            "runnight_closure_sha256": sha256(
                self.runnight_closure_path.encode()
            ).hexdigest(),
            "tasks": [task.persisted_dict() for task in self.tasks],
            "limits": self.limits.to_dict(),
            "publication_mode": self.publication_mode,
            "stop_on_sensitive": self.stop_on_sensitive,
            "require_clean_repo": self.require_clean_repo,
        }

    @property
    def spec_sha256(self) -> str:
        return sha256_json(self.persisted_dict())


@dataclass(frozen=True)
class NightTaskExecution:
    ok: bool
    output: str = ""
    verdict: str = ""
    provider_calls: int = 0
    codex_calls: int = 0
    context_bytes: int = 0
    provider_families: tuple[str, ...] = ()
    selected_provider: str | None = None
    needs_human: bool = False
    error_code: str = ""


@dataclass(frozen=True)
class NightTaskResult:
    task_id: str
    project_id: str
    state: str
    ok: bool
    verdict: str
    artifact_kind: str
    artifact_sha256: str
    artifact_path: str
    provider_calls: int
    codex_calls: int
    context_bytes: int
    provider_families: tuple[str, ...]
    selected_provider: str | None
    needs_human: bool
    error_code: str
    mutation_detected: bool
    started_at: str
    finished_at: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "state": self.state,
            "ok": self.ok,
            "verdict": self.verdict,
            "artifact_kind": self.artifact_kind,
            "artifact_sha256": self.artifact_sha256,
            "artifact_path": self.artifact_path,
            "provider_calls": self.provider_calls,
            "codex_calls": self.codex_calls,
            "context_bytes": self.context_bytes,
            "provider_families": list(self.provider_families),
            "selected_provider": self.selected_provider,
            "needs_human": self.needs_human,
            "error_code": self.error_code,
            "mutation_detected": self.mutation_detected,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class RunNightReport:
    run_id: str
    spec_sha256: str
    activation_id: str
    authorized_sha: str
    mode: str
    state: str
    stop_reason: str
    started_at: str
    finished_at: str
    tasks_total: int
    tasks_attempted: int
    tasks_awaiting_approval: int
    tasks_blocked: int
    tasks_deferred: int
    provider_calls: int
    codex_calls: int
    context_bytes: int
    results: tuple[NightTaskResult, ...]
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "spec_sha256": self.spec_sha256,
            "activation_id": self.activation_id,
            "authorized_sha": self.authorized_sha,
            "mode": self.mode,
            "state": self.state,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "tasks_total": self.tasks_total,
            "tasks_attempted": self.tasks_attempted,
            "tasks_awaiting_approval": self.tasks_awaiting_approval,
            "tasks_blocked": self.tasks_blocked,
            "tasks_deferred": self.tasks_deferred,
            "provider_calls": self.provider_calls,
            "codex_calls": self.codex_calls,
            "context_bytes": self.context_bytes,
            "results": [result.to_dict() for result in self.results],
        }

    @property
    def report_sha256(self) -> str:
        return sha256_json(self.unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "report_sha256": self.report_sha256}


def spec_from_dict(data: dict[str, Any]) -> RunNightSpec:
    limits = RunNightLimits(**dict(data.get("limits", {})))
    tasks = tuple(
        NightTask(
            task_id=str(item["task_id"]),
            project_id=str(item["project_id"]),
            objective=str(item["objective"]),
            execution_root=str(item["execution_root"]),
            artifact_kind=str(item["artifact_kind"]),
            supervisor_mode=str(item.get("supervisor_mode", "builder_reviewer")),
            preferred_provider=item.get("preferred_provider"),
            provider_names=tuple(item.get("provider_names", [])),
            judge_provider=item.get("judge_provider"),
            max_provider_calls=int(item.get("max_provider_calls", 4)),
            require_independent_review=bool(item.get("require_independent_review", True)),
            sensitive=bool(item.get("sensitive", False)),
            metadata=dict(item.get("metadata", {})),
        )
        for item in data.get("tasks", [])
    )
    return RunNightSpec(
        run_id=str(data["run_id"]),
        authorized_sha=str(data["authorized_sha"]),
        mode=str(data["mode"]),
        state_root=str(data["state_root"]),
        repo_root=str(data["repo_root"]),
        tranche3_evidence_path=str(data["tranche3_evidence_path"]),
        tranche3_closure_path=str(data["tranche3_closure_path"]),
        runnight_evidence_path=str(data["runnight_evidence_path"]),
        runnight_closure_path=str(data["runnight_closure_path"]),
        tasks=tasks,
        limits=limits,
        publication_mode=str(data.get("publication_mode", "none")),
        stop_on_sensitive=bool(data.get("stop_on_sensitive", True)),
        require_clean_repo=bool(data.get("require_clean_repo", True)),
    )
