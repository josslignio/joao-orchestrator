"""Typed contracts for JOÃO's multi-provider supervisor.

The supervisor is deliberately read/control-plane first.  It can ask provider
adapters for plans, reviews and candidate text, but it never approves,
promotes, merges, pushes or enables the write tier.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..policy.capabilities import CapabilitySet


class SupervisorMode(str, Enum):
    DIRECT = "direct"
    AUTO = "auto"
    CHALLENGE = "challenge"
    COUNCIL = "council"
    BUILDER_REVIEWER = "builder_reviewer"


class SupervisorStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True)
class SupervisorRequest:
    task_id: str
    project_id: str
    prompt: str
    mode: str = SupervisorMode.AUTO.value
    role: str = "planner"
    worktree_path: Optional[str] = None
    preferred_provider: Optional[str] = None
    provider_names: tuple[str, ...] = ()
    judge_provider: Optional[str] = None
    max_provider_calls: int = 4
    timeout_seconds: int = 300
    capability_grant: Optional[CapabilitySet] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1


@dataclass
class SupervisorCall:
    provider: str
    family: str
    model: str
    role: str
    ok: bool
    content: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    sequence: int = 0
    response_sha256: str = ""
    schema_version: int = 1

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "family": self.family,
            "model": self.model,
            "role": self.role,
            "ok": self.ok,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "sequence": self.sequence,
            "response_sha256": self.response_sha256,
        }
        if include_content:
            data["content"] = self.content
        else:
            data["content_chars"] = len(self.content)
        return data


@dataclass
class SupervisorResult:
    run_id: str
    task_id: str
    project_id: str
    mode: str
    status: str
    selected_provider: Optional[str] = None
    final_content: str = ""
    verdict: str = ""
    reason: str = ""
    calls: list[SupervisorCall] = field(default_factory=list)
    routing: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    prompt_sha256: str = ""
    needs_human: bool = False
    schema_version: int = 1

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "mode": self.mode,
            "status": self.status,
            "selected_provider": self.selected_provider,
            "verdict": self.verdict,
            "reason": self.reason,
            "calls": [c.to_dict(include_content=include_content) for c in self.calls],
            "routing": self.routing,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "prompt_sha256": self.prompt_sha256,
            "needs_human": self.needs_human,
        }
        if include_content:
            data["final_content"] = self.final_content
        else:
            data["final_content_chars"] = len(self.final_content)
        return data
