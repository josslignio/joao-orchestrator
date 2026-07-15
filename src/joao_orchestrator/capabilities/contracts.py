"""C7 Capability contracts.

Makes future tools easy to add without giving agents unrestricted power. Each
capability is a declared contract with explicit read/write/sensitive actions,
required secrets, network requirement, cost class, rate limit, allowed projects,
and human-approval requirement.

Policy decides access deterministically. No real new connector in this run —
built-in contracts for the existing factory surface only.

Contracts module:
  contracts.py    — CapabilityContract data model + built-in contracts
  registry.py     — registry of declared contracts
  permissions.py  — deterministic policy engine for contract access

Deterministic. No network. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

from ..evaluation.models import sha256_json


SCHEMA_VERSION = 1


class ActionClass(str, Enum):
    READ = "read"
    WRITE = "write"
    SENSITIVE = "sensitive"


class CostClass(str, Enum):
    FREE = "free"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class CapabilityContract:
    """A declared capability contract.

    A capability is a tool/provider the factory may invoke. Its contract
    declares exactly what actions it can take and under what constraints.
    """

    capability_id: str
    provider_tool: str
    read_actions: tuple[str, ...]
    write_actions: tuple[str, ...]
    sensitive_actions: tuple[str, ...]
    required_secrets: tuple[str, ...]
    network_required: bool
    cost_class: str          # CostClass value
    rate_limit_per_hour: int
    allowed_projects: tuple[str, ...]   # empty = all
    human_approval_required: bool
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "provider_tool": self.provider_tool,
            "read_actions": list(self.read_actions),
            "write_actions": list(self.write_actions),
            "sensitive_actions": list(self.sensitive_actions),
            "required_secrets": list(self.required_secrets),
            "network_required": self.network_required,
            "cost_class": self.cost_class,
            "rate_limit_per_hour": self.rate_limit_per_hour,
            "allowed_projects": list(self.allowed_projects),
            "human_approval_required": self.human_approval_required,
        }

    @property
    def contract_hash(self) -> str:
        return sha256_json(self.unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        d = self.unsigned_dict()
        d["contract_hash"] = self.contract_hash
        return d

    def all_actions(self) -> tuple[str, ...]:
        return self.read_actions + self.write_actions + self.sensitive_actions

    def action_class(self, action: str) -> Optional[ActionClass]:
        if action in self.sensitive_actions:
            return ActionClass.SENSITIVE
        if action in self.write_actions:
            return ActionClass.WRITE
        if action in self.read_actions:
            return ActionClass.READ
        return None

    def is_action_known(self, action: str) -> bool:
        return self.action_class(action) is not None


# ---------------------------------------------------------------------------
# Built-in contracts
# ---------------------------------------------------------------------------


FILESYSTEM_READ = CapabilityContract(
    capability_id="filesystem.read",
    provider_tool="local_fs",
    read_actions=("read_file", "list_dir", "stat_file"),
    write_actions=(),
    sensitive_actions=(),
    required_secrets=(),
    network_required=False,
    cost_class=CostClass.FREE.value,
    rate_limit_per_hour=10000,
    allowed_projects=(),
    human_approval_required=False,
)

FILESYSTEM_BOUNDED_WRITE = CapabilityContract(
    capability_id="filesystem.bounded_write",
    provider_tool="local_fs",
    read_actions=(),
    write_actions=("write_file", "create_file", "edit_file"),
    sensitive_actions=(),
    required_secrets=(),
    network_required=False,
    cost_class=CostClass.FREE.value,
    rate_limit_per_hour=1000,
    allowed_projects=(),
    human_approval_required=False,
)

GIT_READ = CapabilityContract(
    capability_id="git.read",
    provider_tool="git",
    read_actions=("status", "log", "diff", "show", "branch_list"),
    write_actions=(),
    sensitive_actions=(),
    required_secrets=(),
    network_required=False,
    cost_class=CostClass.FREE.value,
    rate_limit_per_hour=5000,
    allowed_projects=(),
    human_approval_required=False,
)

GIT_COMMIT = CapabilityContract(
    capability_id="git.commit",
    provider_tool="git",
    read_actions=(),
    write_actions=("add", "commit"),
    sensitive_actions=(),
    required_secrets=(),
    network_required=False,
    cost_class=CostClass.LOW.value,
    rate_limit_per_hour=100,
    allowed_projects=(),
    human_approval_required=False,
)

GIT_PUSH = CapabilityContract(
    capability_id="git.push",
    provider_tool="git",
    read_actions=(),
    write_actions=("push",),
    sensitive_actions=(),
    required_secrets=("GIT_CREDENTIALS",),
    network_required=True,
    cost_class=CostClass.MEDIUM.value,
    rate_limit_per_hour=50,
    allowed_projects=(),
    human_approval_required=False,
)

GITHUB_PR_CREATE = CapabilityContract(
    capability_id="github.pr_create",
    provider_tool="gh_cli",
    read_actions=(),
    write_actions=("create_pr", "update_pr"),
    sensitive_actions=("merge_pr",),
    required_secrets=("GITHUB_TOKEN",),
    network_required=True,
    cost_class=CostClass.MEDIUM.value,
    rate_limit_per_hour=30,
    allowed_projects=(),
    human_approval_required=False,
)

CODEX_REVIEW = CapabilityContract(
    capability_id="codex.review",
    provider_tool="codex_cli",
    read_actions=("review_diff", "review_plan"),
    write_actions=(),
    sensitive_actions=(),
    required_secrets=(),
    network_required=False,
    cost_class=CostClass.HIGH.value,
    rate_limit_per_hour=20,
    allowed_projects=(),
    human_approval_required=False,
)

ZCODE_IMPLEMENTATION = CapabilityContract(
    capability_id="zcode.implementation",
    provider_tool="zcode_cli",
    read_actions=(),
    write_actions=("implement_task", "correct_task"),
    sensitive_actions=(),
    required_secrets=(),
    network_required=False,
    cost_class=CostClass.HIGH.value,
    rate_limit_per_hour=24,
    allowed_projects=(),
    human_approval_required=False,
)

LOCAL_TESTS = CapabilityContract(
    capability_id="local.tests",
    provider_tool="python_venv",
    read_actions=("run_test", "run_suite"),
    write_actions=(),
    sensitive_actions=(),
    required_secrets=(),
    network_required=False,
    cost_class=CostClass.FREE.value,
    rate_limit_per_hour=2000,
    allowed_projects=(),
    human_approval_required=False,
)

LOCAL_CACHE = CapabilityContract(
    capability_id="local.cache",
    provider_tool="content_cache",
    read_actions=("get", "stats"),
    write_actions=("put", "evict"),
    sensitive_actions=("clear",),
    required_secrets=(),
    network_required=False,
    cost_class=CostClass.FREE.value,
    rate_limit_per_hour=10000,
    allowed_projects=(),
    human_approval_required=False,
)


BUILTIN_CONTRACTS: tuple[CapabilityContract, ...] = (
    FILESYSTEM_READ,
    FILESYSTEM_BOUNDED_WRITE,
    GIT_READ,
    GIT_COMMIT,
    GIT_PUSH,
    GITHUB_PR_CREATE,
    CODEX_REVIEW,
    ZCODE_IMPLEMENTATION,
    LOCAL_TESTS,
    LOCAL_CACHE,
)
