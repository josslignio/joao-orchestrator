"""Adapter from Run Night tasks to the existing SupervisorCore."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

from ..supervisor.core import SupervisorCore
from ..supervisor.models import SupervisorRequest, SupervisorStatus
from .models import NightTask, NightTaskExecution


class NightTaskRunner(Protocol):
    def execute(self, task: NightTask, *, max_provider_calls: int) -> NightTaskExecution:
        ...


@dataclass
class SupervisorNightRunner:
    supervisor: SupervisorCore

    def execute(self, task: NightTask, *, max_provider_calls: int) -> NightTaskExecution:
        limit = min(max_provider_calls, task.max_provider_calls)
        result = self.supervisor.execute(SupervisorRequest(
            task_id=task.task_id,
            project_id=task.project_id,
            prompt=task.objective,
            mode=task.supervisor_mode,
            role="planner",
            worktree_path=task.execution_root,
            preferred_provider=task.preferred_provider,
            provider_names=task.provider_names,
            judge_provider=task.judge_provider,
            max_provider_calls=limit,
            metadata={
                **task.metadata,
                "run_night": True,
                "read_only": True,
                "artifact_kind": task.artifact_kind,
                "strict_json_artifact": True,
            },
        ))
        families = tuple(call.family for call in result.calls)
        codex_calls = sum(
            1 for call in result.calls
            if call.provider in {"codex-review", "codex-subscription"}
        )
        ok = (
            result.status == SupervisorStatus.COMPLETED.value
            and result.verdict != "BLOCK"
        )
        return NightTaskExecution(
            ok=ok,
            output=result.final_content,
            verdict=result.verdict,
            provider_calls=len(result.calls),
            codex_calls=codex_calls,
            context_bytes=(
                len(task.objective.encode("utf-8"))
                + len(result.final_content.encode("utf-8"))
            ),
            provider_families=families,
            selected_provider=result.selected_provider,
            needs_human=result.needs_human,
            error_code="" if ok else "SUPERVISOR_BLOCKED",
        )
