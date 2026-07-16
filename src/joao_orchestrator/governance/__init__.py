"""
JOÃO governance package.

This package contains fail-closed governance utilities for enforcing
immutable security contracts.
"""

from joao_orchestrator.governance.contract_guard import (
    ContractViolation,
    FrozenContractGuard,
    PolicyVerdict,
)

__all__ = [
    "FrozenContractGuard",
    "PolicyVerdict",
    "ContractViolation",
]
