"""Canonical domain models: task states, lifecycle enums, and core dataclasses.

The 17-state task lifecycle is a superset of the V1.4.0 10-state machine. The
compat shim maps old state names onto this graph (see runtime.transitions).

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional


# --------------------------------------------------------------------------- #
# Task lifecycle (17 canonical states)
# --------------------------------------------------------------------------- #

class TaskState(str, Enum):
    DRAFT = "DRAFT"
    PLANNED = "PLANNED"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    WORKSPACE_CREATING = "WORKSPACE_CREATING"
    WORKSPACE_READY = "WORKSPACE_READY"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    CHANGES_READY = "CHANGES_READY"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    REVIEWING = "REVIEWING"
    FIXING = "FIXING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"

    # Legacy names remain available from the canonical enum so the
    # compatibility package can be a pure re-export layer.
    CREATED = "CREATED"
    PROMPT_READY = "PROMPT_READY"
    WAITING_FOR_ZCODE = "WAITING_FOR_ZCODE"
    CHANGES_DETECTED = "CHANGES_DETECTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


# Compat aliases: V1.4.0 names map onto V1.4.1 states.
STATE_COMPAT_ALIASES: Dict[str, str] = {
    "CREATED": TaskState.DRAFT.value,
    "PROMPT_READY": TaskState.PLANNED.value,
    "WAITING_FOR_ZCODE": TaskState.WAITING_FOR_INPUT.value,
    "CHANGES_DETECTED": TaskState.CHANGES_READY.value,
    "REVIEW_REQUIRED": TaskState.AWAITING_APPROVAL.value,
}


def canonical_state(value: str) -> str:
    """Map any (possibly old) state name to its canonical V1.4.1 name."""
    if value in STATE_COMPAT_ALIASES:
        return STATE_COMPAT_ALIASES[value]
    return value


# --------------------------------------------------------------------------- #
# Provider roles
# --------------------------------------------------------------------------- #

class ProviderRole(str, Enum):
    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    RESEARCHER = "researcher"
    TESTER = "tester"
    SKILL_WORKER = "skill_worker"


# --------------------------------------------------------------------------- #
# Core dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class TaskMeta:
    """A task's persisted metadata."""
    task_id: str
    project_id: str
    title: str
    request: str
    state: str
    created_at: str
    updated_at: str
    branch: Optional[str] = None
    schema_version: int = 1
    done_criteria: Optional[str] = None
    size_class: Optional[str] = None        # TRIVIAL/SMALL/MEDIUM/LARGE/HIGH_RISK
    attempt: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskMeta":
        return cls(
            task_id=d["task_id"],
            project_id=d.get("project_id", ""),
            title=d.get("title", ""),
            request=d.get("request", ""),
            state=canonical_state(d.get("state", TaskState.DRAFT.value)),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            branch=d.get("branch"),
            schema_version=int(d.get("schema_version", 1)),
            done_criteria=d.get("done_criteria"),
            size_class=d.get("size_class"),
            attempt=int(d.get("attempt", 0)),
        )


@dataclass
class CommandResult:
    """Result of one allowlisted command execution."""
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    ok: bool
    reason: str = ""
    timed_out: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationRun:
    """The outcome of a full validation profile against a task."""
    task_id: str
    ok: bool
    commands: List[dict] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProjectProfile:
    """A project's configuration (loaded from .agent/project.toml)."""
    project_id: str
    display_name: str
    repository_root: str
    default_branch: str = "main"
    runtime: str = "python"                 # python | node | generic_git
    python_strategy: str = ".venv/bin/python"
    allowed_write_paths: List[str] = field(default_factory=list)
    forbidden_paths: List[str] = field(default_factory=list)
    generated_paths: List[str] = field(default_factory=list)
    validation_profile: str = "default"
    workspace_strategy: str = "none"        # none | worktree (V0.2)
    command_timeout_seconds: int = 60
    max_output_bytes: int = 65536
    concurrency_limit: int = 1
    environment_allowlist: List[str] = field(default_factory=list)
    approval_required: bool = True
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)
