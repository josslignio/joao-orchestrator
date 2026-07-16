"""
JOÃO Contract Guard - Fail-Closed Immutable Contract Governance

Provides stdlib-only JSON loading and structured contract validation.
Malformed or missing frozen/candidate data always returns DENY.
No public exception for malformed contract input.
"""

import json
from dataclasses import dataclass
from typing import Any, Dict
from enum import Enum


class PolicyVerdict(Enum):
    """Contract governance verdicts"""
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True)
class PolicyViolation:
    """Structured policy violation record"""
    reason: str
    field: str
    expected: Any
    actual: Any


def _load_json_strict(path: str) -> Dict[str, Any]:
    """
    Stdlib-only JSON loading with fail-closed semantics.
    Returns empty dict for any error (missing file, invalid JSON, etc).
    Never raises for user-controlled malformed input.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return {}
    
    # Require object root
    if not isinstance(data, dict):
        return {}
    
    return data


def _validate_contract_structure(data: Dict[str, Any]) -> bool:
    """
    Basic schema validation for contract structure.
    Returns False for any structural violations.
    """
    # Must have governor_steps as object
    governor_steps = data.get("governor_steps")
    if not isinstance(governor_steps, dict):
        return False
    
    # Must have stage_a as object  
    stage_a = data.get("stage_a")
    if not isinstance(stage_a, dict):
        return False
    
    return True


def compare_contracts(
    frozen_path: str,
    candidate_path: str,
    expected_hash: str
) -> tuple[PolicyVerdict, list[PolicyViolation]]:
    """
    Fail-closed contract comparison.
    Malformed or missing frozen/candidate data always returns DENY.
    No comparison logic beyond parsing and schema validation in GOV-1.
    
    Args:
        frozen_path: Path to frozen contract file
        candidate_path: Path to candidate contract file  
        expected_hash: Expected hash of frozen contract
        
    Returns:
        (PolicyVerdict, list[PolicyViolation]) - Always returns tuple, never raises
    """
    violations: list[PolicyViolation] = []
    
    # Load both contracts with fail-closed semantics
    frozen_data = _load_json_strict(frozen_path)
    candidate_data = _load_json_strict(candidate_path)
    
    # Empty result indicates loading failure
    if not frozen_data:
        return PolicyVerdict.DENY, [
            PolicyViolation(
                reason="Frozen contract missing or malformed",
                field="frozen_contract",
                expected="<valid JSON object>",
                actual="<missing or invalid>"
            )
        ]
    
    if not candidate_data:
        return PolicyVerdict.DENY, [
            PolicyViolation(
                reason="Candidate contract missing or malformed",
                field="candidate_contract", 
                expected="<valid JSON object>",
                actual="<missing or invalid>"
            )
        ]
    
    # Validate basic structure (no comparison yet, that's GOV-2)
    if not _validate_contract_structure(frozen_data):
        return PolicyVerdict.DENY, [
            PolicyViolation(
                reason="Frozen contract has invalid structure",
                field="contract_structure",
                expected="governor_steps: object, stage_a: object",
                actual="<malformed structure>"
            )
        ]
    
    if not _validate_contract_structure(candidate_data):
        return PolicyVerdict.DENY, [
            PolicyViolation(
                reason="Candidate contract has invalid structure",
                field="contract_structure",
                expected="governor_steps: object, stage_a: object", 
                actual="<malformed structure>"
            )
        ]
    
    # GOV-1 only validates parsing - always ALLOW if structure valid
    # Further comparison will be implemented in GOV-2
    return PolicyVerdict.ALLOW, violations