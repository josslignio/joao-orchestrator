"""Task state-transition graph (17-state FSM).

A superset of the V1.4.0 10-state machine. Compat aliases (see domain.models)
fold old state names into this graph so existing tasks/tests keep working.

Standard library only.
"""

from __future__ import annotations

from typing import Dict, Set

from ..domain.models import TaskState


def _s(*states: TaskState) -> Set[str]:
    return {s.value for s in states}


# Allowed transitions: from-state -> set of permitted to-states.
ALLOWED_TRANSITIONS: Dict[str, Set[str]] = {
    TaskState.DRAFT.value: _s(TaskState.PLANNED, TaskState.WAITING_FOR_INPUT,
                              TaskState.CANCELLED, TaskState.FAILED),
    TaskState.PLANNED.value: _s(TaskState.WAITING_FOR_INPUT,
                                TaskState.WORKSPACE_CREATING,
                                TaskState.CANCELLED, TaskState.FAILED),
    TaskState.WAITING_FOR_INPUT.value: _s(TaskState.WORKSPACE_CREATING,
                                          TaskState.VALIDATING,
                                          TaskState.CANCELLED,
                                          TaskState.FAILED),
    TaskState.WORKSPACE_CREATING.value: _s(TaskState.WORKSPACE_READY,
                                           TaskState.FAILED,
                                           TaskState.CANCELLED),
    TaskState.WORKSPACE_READY.value: _s(TaskState.DISPATCHED,
                                        TaskState.CANCELLED, TaskState.FAILED),
    TaskState.DISPATCHED.value: _s(TaskState.RUNNING, TaskState.FAILED,
                                   TaskState.CANCELLED),
    TaskState.RUNNING.value: _s(TaskState.CHANGES_READY, TaskState.VALIDATING,
                                TaskState.FAILED, TaskState.CANCELLED),
    TaskState.CHANGES_READY.value: _s(TaskState.VALIDATING, TaskState.RUNNING,
                                      TaskState.FAILED, TaskState.CANCELLED),
    TaskState.VALIDATING.value: _s(TaskState.VALIDATED, TaskState.REVIEWING,
                                   TaskState.RUNNING, TaskState.CHANGES_READY,
                                   TaskState.FAILED, TaskState.CANCELLED),
    TaskState.VALIDATED.value: _s(TaskState.REVIEWING, TaskState.AWAITING_APPROVAL,
                                  TaskState.FAILED),
    TaskState.REVIEWING.value: _s(TaskState.AWAITING_APPROVAL,
                                  TaskState.FIXING, TaskState.CHANGES_READY,
                                  TaskState.REJECTED,
                                  TaskState.FAILED, TaskState.CANCELLED),
    TaskState.FIXING.value: _s(TaskState.VALIDATING, TaskState.CHANGES_READY,
                               TaskState.FAILED, TaskState.CANCELLED),
    TaskState.AWAITING_APPROVAL.value: _s(TaskState.APPROVED,
                                          TaskState.REJECTED,
                                          TaskState.REVIEWING,
                                          TaskState.FAILED,
                                          TaskState.CANCELLED),
    TaskState.APPROVED.value: _s(TaskState.COMPLETED, TaskState.FAILED),
    TaskState.REJECTED.value: set(),
    TaskState.FAILED.value: set(),
    TaskState.CANCELLED.value: set(),
    TaskState.COMPLETED.value: set(),
}

TERMINAL_STATES = _s(TaskState.REJECTED, TaskState.FAILED,
                     TaskState.CANCELLED, TaskState.COMPLETED)


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, set())


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES
