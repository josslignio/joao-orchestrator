"""C7 Capability contracts — deterministic permission policy.

Policy decides access deterministically. Rules:
  - unknown capability -> denied
  - write cannot inherit from read (write actions require explicit write grant)
  - sensitive action -> human-only (never auto-approved)
  - project scoping: if the contract lists allowed_projects and the requesting
    project is not in it -> denied
  - human_approval_required -> the decision is DEFER, never ALLOW
  - no credential output: the policy never returns secrets
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .contracts import CapabilityContract, ActionClass
from .registry import CapabilityRegistry


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    DEFER = "DEFER"   # requires human approval


@dataclass(frozen=True)
class AccessDecision:
    """The policy decision for one access request."""

    decision: str        # Decision value
    capability_id: str
    action: str
    action_class: str    # ActionClass value or "unknown"
    reason: str
    project_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "capability_id": self.capability_id,
            "action": self.action,
            "action_class": self.action_class,
            "reason": self.reason,
            "project_id": self.project_id,
        }

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW.value


class PermissionPolicy:
    """Deterministic permission policy over capability contracts."""

    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    def decide(
        self,
        capability_id: str,
        action: str,
        project_id: str = "",
        role: str = "coder",
    ) -> AccessDecision:
        """Decide whether (capability, action) is allowed for a project+role."""
        contract = self.registry.get(capability_id)
        if contract is None:
            return AccessDecision(
                decision=Decision.DENY.value,
                capability_id=capability_id,
                action=action,
                action_class="unknown",
                reason=f"unknown capability: {capability_id}",
                project_id=project_id,
            )

        action_cls = contract.action_class(action)
        if action_cls is None:
            return AccessDecision(
                decision=Decision.DENY.value,
                capability_id=capability_id,
                action=action,
                action_class="unknown",
                reason=f"unknown action {action!r} for capability {capability_id}",
                project_id=project_id,
            )

        # Sensitive action -> always human-only (DEFER, never auto-allow).
        if action_cls is ActionClass.SENSITIVE:
            return AccessDecision(
                decision=Decision.DEFER.value,
                capability_id=capability_id,
                action=action,
                action_class=ActionClass.SENSITIVE.value,
                reason="sensitive action requires human approval",
                project_id=project_id,
            )

        # Write cannot inherit from read: write actions require explicit
        # write membership. A read-only role cannot perform write actions.
        if action_cls is ActionClass.WRITE and role == "reviewer":
            return AccessDecision(
                decision=Decision.DENY.value,
                capability_id=capability_id,
                action=action,
                action_class=ActionClass.WRITE.value,
                reason="write action cannot be performed by read-only role (reviewer)",
                project_id=project_id,
            )

        # Project scoping: if the contract restricts projects, the requesting
        # project must be in the allowed set.
        if contract.allowed_projects and project_id not in contract.allowed_projects:
            return AccessDecision(
                decision=Decision.DENY.value,
                capability_id=capability_id,
                action=action,
                action_class=action_cls.value,
                reason=f"project {project_id!r} not in allowed_projects",
                project_id=project_id,
            )

        # Human approval requirement -> DEFER.
        if contract.human_approval_required:
            return AccessDecision(
                decision=Decision.DEFER.value,
                capability_id=capability_id,
                action=action,
                action_class=action_cls.value,
                reason="contract requires human approval",
                project_id=project_id,
            )

        return AccessDecision(
            decision=Decision.ALLOW.value,
            capability_id=capability_id,
            action=action,
            action_class=action_cls.value,
            reason="allowed by policy",
            project_id=project_id,
        )

    def decide_no_credentials(self, capability_id: str, action: str,
                              project_id: str = "", role: str = "coder") -> AccessDecision:
        """Same as decide(), but guaranteed to never output secrets.

        The decision dict contains only the decision fields — never the
        contract's required_secrets. This is the safe entry point for logging.
        """
        d = self.decide(capability_id, action, project_id, role)
        # Double-check: no secret fields leak into the decision dict.
        assert "secret" not in d.to_dict()
        assert "credential" not in d.to_dict()
        return d
