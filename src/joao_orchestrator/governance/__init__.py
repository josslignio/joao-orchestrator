"""
JOÃO Governance - Contract Guard Module

Fail-closed contract governance for JOÃO orchestration.
This module provides immutable contract validation with strict enforcement.
"""

from .contract_guard import (
    PolicyViolation,
    PolicyVerdict,
    compare_contracts,
)

__all__ = [
    "PolicyViolation",
    "PolicyVerdict", 
    "compare_contracts",
]