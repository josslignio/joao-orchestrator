"""C7 Capability contracts — registry.

A registry of declared capability contracts. Unknown capabilities are denied.
Contracts are immutable once registered.
"""

from __future__ import annotations

from typing import Optional

from .contracts import CapabilityContract, BUILTIN_CONTRACTS


class CapabilityRegistry:
    """Registry of declared capability contracts."""

    def __init__(self):
        self._contracts: dict[str, CapabilityContract] = {}
        for c in BUILTIN_CONTRACTS:
            self._contracts[c.capability_id] = c

    def register(self, contract: CapabilityContract) -> None:
        if contract.capability_id in self._contracts:
            raise ValueError(
                f"capability already registered: {contract.capability_id}")
        self._contracts[contract.capability_id] = contract

    def get(self, capability_id: str) -> Optional[CapabilityContract]:
        return self._contracts.get(capability_id)

    def is_known(self, capability_id: str) -> bool:
        return capability_id in self._contracts

    def all(self) -> tuple[CapabilityContract, ...]:
        return tuple(self._contracts.values())

    def count(self) -> int:
        return len(self._contracts)


def default_registry() -> CapabilityRegistry:
    """Return a registry pre-loaded with all built-in contracts."""
    return CapabilityRegistry()
