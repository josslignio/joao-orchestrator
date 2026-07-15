"""Provider role interfaces.

Typed, provider-neutral contracts for: planner, coder, reviewer, researcher,
tester, skill_worker. Concrete adapters implement these. The task engine
depends only on these interfaces, never on a specific vendor.

Future adapters (V0.2+):
- OpenAI: Agents SDK, handoffs, sessions, tracing, tool guardrails
- Claude: Agent SDK, hooks, permission requests, subagents, MCP
- Codex: app-server, stdio JSON-RPC, event streaming, approval requests
- Z.AI: OpenAI-compatible API, configurable base URL
- Hermes: research, memory, skill worker (no policy/approval authority)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from ..domain.models import ProviderRole
from ..policy.capabilities import CapabilitySet


@dataclass
class ProviderRequest:
    """A typed request to a provider."""
    role: str
    task_id: str
    project_id: str
    prompt: str
    worktree_path: Optional[str] = None
    capability_grant: Optional[CapabilitySet] = None
    schema_version: int = 1


@dataclass
class ProviderResponse:
    """A typed response from a provider."""
    role: str
    task_id: str
    ok: bool
    content: str = ""
    error: str = ""
    changed_paths: List[str] = field(default_factory=list)
    schema_version: int = 1
    provider_name: str = ""


class ProviderAdapter(ABC):
    """Base contract for all provider adapters."""

    name: str = "base"
    supported_roles: tuple = ()
    network_required: bool = False

    @abstractmethod
    def is_enabled(self) -> bool:
        """Whether this adapter is available (disabled adapters return False)."""

    @abstractmethod
    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """Execute a request. Must respect the capability grant."""

    def supports_role(self, role: str) -> bool:
        return role in self.supported_roles




class ProviderCapabilityError(PermissionError):
    """Raised when an enabled provider lacks a required capability."""


def require_capability(request: ProviderRequest, capability: str) -> None:
    grant = request.capability_grant or CapabilitySet()
    allowed, reason = grant.check(capability)
    if not allowed:
        raise ProviderCapabilityError(reason)


class DisabledProviderError(RuntimeError):
    """Raised when a disabled provider is invoked."""

    def __init__(self, provider_name: str, reason: str, what_is_missing: str):
        super().__init__(
            f"{provider_name} is disabled: {reason}. Missing: {what_is_missing}")
        self.provider_name = provider_name
        self.reason = reason
        self.what_is_missing = what_is_missing


class DisabledProvider(ProviderAdapter):
    """Base for all disabled providers. Returns a structured explanation
    instead of making a network call."""

    is_disabled = True
    network_required = False
    what_is_missing = "not implemented in this version"

    def is_enabled(self) -> bool:
        return False

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        raise DisabledProviderError(self.name, "disabled in V1.4.1",
                                    self.what_is_missing)

    def describe(self) -> dict:
        """Structured explanation of what would be needed to enable this provider."""
        return {
            "name": self.name,
            "enabled": False,
            "network_required": self.network_required,
            "supported_roles": list(self.supported_roles),
            "what_is_missing": self.what_is_missing,
        }
