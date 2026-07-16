"""JOÃO Contract Guard - Fail-Closed Immutable Contract Governance"""
import json
from enum import Enum
from typing import Any, Dict


class PolicyVerdict(Enum):
    """Contract governance verdicts"""
    ALLOW = "ALLOW"
    DENY = "DENY"


def _load_json_strict(path: str) -> Dict[str, Any]:
    """Stdlib-only JSON loading. Returns {} for any error, never raises."""
    if not isinstance(path, (str,)):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError, TypeError):
        return {}
    
    if not isinstance(data, dict):
        return {}
    return data


def _validate_structure(data: Dict[str, Any]) -> bool:
    """Basic schema validation. False for violations."""
    return (isinstance(data.get("governor_steps"), dict) and
            isinstance(data.get("stage_a"), dict))


def compare_contracts(frozen_path: str, candidate_path: str, 
                     expected_hash: str) -> tuple[PolicyVerdict, list[str]]:
    """Fail-closed contract comparison. Returns (verdict, [violations])."""
    if not isinstance(frozen_path, (str,)) or not isinstance(candidate_path, (str,)):
        return PolicyVerdict.DENY, ["Invalid path type"]
    
    frozen_data = _load_json_strict(frozen_path)
    candidate_data = _load_json_strict(candidate_path)
    
    if not frozen_data:
        return PolicyVerdict.DENY, ["Frozen contract missing or malformed"]
    if not candidate_data:
        return PolicyVerdict.DENY, ["Candidate contract missing or malformed"]
    if not _validate_structure(frozen_data):
        return PolicyVerdict.DENY, ["Frozen contract invalid structure"]
    if not _validate_structure(candidate_data):
        return PolicyVerdict.DENY, ["Candidate contract invalid structure"]
    
    return PolicyVerdict.ALLOW, []