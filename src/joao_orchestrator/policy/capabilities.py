"""Capability tokens (deny-by-default).

V1.4.1 defines the capability model and grant sets. Enabled adapters that
perform side effects must explicitly check the required grant. Broader runtime
capability routing remains out of scope until V0.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Set


# Canonical capability tokens. Anything not granted is denied.
ALL_CAPABILITIES = (
    "repo.read",
    "workspace.read",
    "workspace.write",
    "validation.run",
    "diff.inspect",
    "artifact.write",
    "network.request",
    "dependency.install",
    "git.commit",
    "git.push",
    "git.merge",
    "deployment.run",
    "secret.read",
    "task.approve",
)

# The conservative default grant for a coding task. Mutating git ops, network,
# installs, deployments, and secret access are NEVER in the default.
DEFAULT_CODER_GRANT = frozenset({
    "workspace.read",
    "workspace.write",
    "diff.inspect",
    "artifact.write",
})

DEFAULT_REVIEWER_GRANT = frozenset({
    "repo.read",
    "workspace.read",
    "diff.inspect",
    "validation.run",
})

DEFAULT_VALIDATOR_GRANT = frozenset({
    "repo.read",
    "workspace.read",
    "validation.run",
    "diff.inspect",
})


@dataclass(frozen=True)
class CapabilitySet:
    """An immutable set of granted capabilities."""
    granted: frozenset = field(default_factory=frozenset)

    @classmethod
    def for_role(cls, role: str) -> "CapabilitySet":
        return {
            "coder": cls(DEFAULT_CODER_GRANT),
            "reviewer": cls(DEFAULT_REVIEWER_GRANT),
            "validator": cls(DEFAULT_VALIDATOR_GRANT),
        }.get(role, cls())  # deny-by-default for unknown roles

    def has(self, capability: str) -> bool:
        return capability in self.granted

    def check(self, capability: str) -> tuple:
        """Return (allowed, reason)."""
        if self.has(capability):
            return True, "ok"
        return False, f"capability not granted: {capability}"

    def union(self, other: "CapabilitySet") -> "CapabilitySet":
        return CapabilitySet(self.granted | other.granted)
