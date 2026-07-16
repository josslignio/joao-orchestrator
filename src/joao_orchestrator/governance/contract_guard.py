"""
Frozen contract guard implementation.

This module provides fail-closed enforcement of immutable security contracts
using stable requirement IDs. It is designed to use only the Python standard
library and to default to DENY for any malformed or suspicious input.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping
from pathlib import Path


@dataclass(frozen=True)
class ContractViolation:
    """A specific policy violation with structured details."""

    requirement_id: str
    reason: str
    severity: str = "ERROR"
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert violation to dictionary for serialization."""
        return {
            "requirement_id": self.requirement_id,
            "reason": self.reason,
            "severity": self.severity,
            "details": self.details,
        }


@dataclass(frozen=True)
class PolicyVerdict:
    """
    Result of comparing a candidate contract against a frozen contract.

    The verdict is FAIL-CLOSED: any missing data, malformed input, or
    detected violation results in DENY. Only when the candidate is proven
    monotonic (equal or strictly stricter) does the guard return ALLOW.
    """

    allowed: bool
    violations: tuple[ContractViolation, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Convert verdict to dictionary for serialization."""
        return {
            "allowed": self.allowed,
            "violations": [v.to_dict() for v in self.violations],
            "verdict": "ALLOW" if self.allowed else "DENY",
        }


class FrozenContractGuard:
    """
    Immutable contract enforcer using stable requirement ID comparison.

    This guard enforces the following invariants:
    - Existing requirement IDs cannot be removed or renamed
    - Existing requirement text and values cannot be changed
    - Numeric safety limits cannot increase
    - Required test sets cannot shrink
    - Forbidden operation sets cannot shrink
    - Allowed file paths cannot expand (only stay equal or become narrower)
    - Wildcards are never permitted in allowed paths
    - Only additions of stricter requirements are permitted
    """

    def __init__(self, frozen_contract: Mapping[str, Any]):
        """
        Initialize the guard with a frozen contract.

        Args:
            frozen_contract: The authoritative frozen contract mapping.
                            Must not be modified after initialization.

        Raises:
            ValueError: If the frozen contract is malformed or missing
                       required fields.
        """
        if not frozen_contract:
            raise ValueError("Frozen contract cannot be empty or None")

        self._frozen = dict(frozen_contract)  # Defensive copy
        self._frozen_ids = self._extract_requirement_ids(self._frozen)

    def _extract_requirement_ids(self, contract: Mapping[str, Any]) -> set[str]:
        """Extract all requirement IDs from a contract."""
        ids = set()

        try:
            # Extract from governor_steps requirements
            governor_steps = contract.get("governor_steps", {})
            if not isinstance(governor_steps, dict):
                return ids

            for step_id, step_data in governor_steps.items():
                if not isinstance(step_data, dict):
                    continue
                for req_id in step_data.get("required_behavior", []):
                    if isinstance(req_id, str):
                        ids.add(f"G-{step_id}-{req_id}")

            # Extract from stage_a requirements
            stage_a = contract.get("stage_a", {})
            if isinstance(stage_a, dict):
                for req_id in stage_a.get("requirements", {}):
                    if isinstance(req_id, str):
                        ids.add(req_id)

            # Extract global invariants
            global_invariants = contract.get("global_invariants", {})
            if isinstance(global_invariants, dict):
                for inv_id in global_invariants:
                    if isinstance(inv_id, str):
                        ids.add(inv_id)
        except (AttributeError, TypeError):
            # Malformed contract structure
            return set()

        return ids

    def _contains_wildcard(self, path_pattern: str) -> bool:
        """Check if a path pattern contains wildcards."""
        if not isinstance(path_pattern, str):
            return True  # Treat non-strings as suspicious

        wildcards = {"*", "?", "[", "]"}
        return any(char in path_pattern for char in wildcards)

    def _compare_paths_strictness(self, frozen_path: str, candidate_path: str) -> bool:
        """
        Check if candidate path is equally or more restrictive than frozen path.

        Returns True if candidate_path is allowed (equal or stricter),
        False if it's more permissive.
        """
        if not isinstance(frozen_path, str) or not isinstance(candidate_path, str):
            return False

        # Identical paths are always allowed
        if frozen_path == candidate_path:
            return True

        # Candidate must not contain wildcards
        if self._contains_wildcard(candidate_path):
            return False

        # Frozen must not have contained wildcards
        if self._contains_wildcard(frozen_path):
            return False

        # Candidate is stricter if it's more specific (e.g., adds subdirectories)
        # For now, require exact match to avoid path manipulation
        return frozen_path == candidate_path

    def _compare_numeric_limits(self, frozen_val: Any, candidate_val: Any) -> bool:
        """
        Check if numeric limit hasn't increased.

        Returns True if candidate_val <= frozen_val (allowed),
        False if candidate_val > frozen_val (increased limit).
        """
        try:
            frozen_num = float(frozen_val)
            candidate_num = float(candidate_val)
            return candidate_num <= frozen_num
        except (ValueError, TypeError):
            return False

    def _compare_sets(self, frozen_set: set, candidate_set: set, context: str) -> bool:
        """
        Compare sets based on context.

        For required_tests and forbidden_paths: candidate must be superset or equal.
        For allowed_paths: candidate must be subset or equal.
        """
        if context in {"required_tests", "forbidden_paths"}:
            return candidate_set.issuperset(frozen_set)
        elif context == "allowed_paths":
            return candidate_set.issubset(frozen_set)
        else:
            return candidate_set == frozen_set

    def verify_hash(
        self,
        frozen_path: str | Path,
        expected_hash: str
    ) -> PolicyVerdict:
        """
        Verify the frozen contract file hash matches the expected value.

        The hash verification follows the JOÃO protocol:
        1. Load the JSON contract
        2. Remove the frozen_contract_sha256 field
        3. Canonicalize using JSON sorted keys and indent=2
        4. Compute SHA-256 of the canonicalized bytes
        5. Compare with expected hash

        Args:
            frozen_path: Path to the frozen contract JSON file.
            expected_hash: Expected SHA-256 hash of the canonicalized contract.

        Returns:
            PolicyVerdict with ALLOW if hash matches, DENY otherwise.
        """
        try:
            frozen_path = Path(frozen_path)
            if not frozen_path.exists():
                return PolicyVerdict(
                    allowed=False,
                    violations=(
                        ContractViolation(
                            requirement_id="HASH-001",
                            reason="Frozen contract file does not exist",
                            details=f"Path: {frozen_path}"
                        ),
                    )
                )

            # Load and parse JSON
            content = frozen_path.read_text(encoding="utf-8")
            try:
                contract_dict = json.loads(content)
            except json.JSONDecodeError as e:
                return PolicyVerdict(
                    allowed=False,
                    violations=(
                        ContractViolation(
                            requirement_id="HASH-002",
                            reason="Frozen contract is not valid JSON",
                            details=str(e)
                        ),
                    )
                )

            # Remove frozen_contract_sha256 field as per protocol
            if "frozen_contract_sha256" in contract_dict:
                del contract_dict["frozen_contract_sha256"]

            # Canonicalize using JSON sorted keys and indent=2
            canonicalized = json.dumps(contract_dict, sort_keys=True, indent=2)
            actual_hash = hashlib.sha256(canonicalized.encode("utf-8")).hexdigest()

            if actual_hash != expected_hash:
                return PolicyVerdict(
                    allowed=False,
                    violations=(
                        ContractViolation(
                            requirement_id="HASH-003",
                            reason="Frozen contract hash mismatch",
                            details=f"Expected: {expected_hash}, Actual: {actual_hash}"
                        ),
                    )
                )

            return PolicyVerdict(allowed=True)

        except Exception as e:
            return PolicyVerdict(
                allowed=False,
                violations=(
                    ContractViolation(
                        requirement_id="HASH-004",
                        reason="Failed to verify frozen contract hash",
                        details=str(e)
                    ),
                )
            )

    def compare(
        self,
        candidate_contract: Mapping[str, Any]
    ) -> PolicyVerdict:
        """
        Compare a candidate contract against the frozen contract.

        Args:
            candidate_contract: The candidate contract mapping to validate.

        Returns:
            PolicyVerdict with ALLOW if candidate is monotonic,
            DENY with structured violations otherwise.
        """
        violations = []

        if not candidate_contract:
            return PolicyVerdict(
                allowed=False,
                violations=(
                    ContractViolation(
                        requirement_id="INV-001",
                        reason="Candidate contract is empty or None"
                    ),
                )
            )

        try:
            candidate = dict(candidate_contract)
        except Exception as e:
            return PolicyVerdict(
                allowed=False,
                violations=(
                    ContractViolation(
                        requirement_id="INV-002",
                        reason="Candidate contract cannot be converted to dict",
                        details=str(e)
                    ),
                )
            )

        # Check for removed requirement IDs
        candidate_ids = self._extract_requirement_ids(candidate)
        missing_ids = self._frozen_ids - candidate_ids

        if missing_ids:
            violations.append(
                ContractViolation(
                    requirement_id="INV-005",
                    reason="Existing requirement IDs were removed",
                    details=f"Missing IDs: {sorted(missing_ids)}"
                )
            )

        # Check for changed authority field
        frozen_authority = self._frozen.get("authority")
        candidate_authority = candidate.get("authority")
        if frozen_authority and candidate_authority != frozen_authority:
            violations.append(
                ContractViolation(
                    requirement_id="AUTH-001",
                    reason="Authority field cannot be changed",
                    details=f"Frozen: {frozen_authority}, Candidate: {candidate_authority}"
                )
            )

        # Check for changed required starting SHA
        frozen_sha = self._frozen.get("required_start_sha")
        candidate_sha = candidate.get("required_start_sha")
        if frozen_sha and candidate_sha != frozen_sha:
            violations.append(
                ContractViolation(
                    requirement_id="SHA-001",
                    reason="Required starting SHA cannot be changed",
                    details=f"Frozen: {frozen_sha}, Candidate: {candidate_sha}"
                )
            )

        # Check governor_steps for policy violations
        frozen_steps = self._frozen.get("governor_steps", {})
        candidate_steps = candidate.get("governor_steps", {})

        # Validate structure first
        if not isinstance(frozen_steps, dict) or not isinstance(candidate_steps, dict):
            violations.append(
                ContractViolation(
                    requirement_id="STEP-STRUCT",
                    reason="Governor steps must be dictionaries",
                    details=f"Frozen type: {type(frozen_steps)}, Candidate type: {type(candidate_steps)}"
                )
            )
        else:
            for step_id in frozen_steps:
                if step_id not in candidate_steps:
                    violations.append(
                        ContractViolation(
                            requirement_id=f"STEP-{step_id}",
                            reason="Governor step was removed",
                            details=f"Step ID: {step_id}"
                        )
                    )
                    continue

                frozen_step = frozen_steps[step_id]
                candidate_step = candidate_steps[step_id]

                # Validate step structure
                if not isinstance(frozen_step, dict) or not isinstance(candidate_step, dict):
                    violations.append(
                        ContractViolation(
                            requirement_id=f"STEP-{step_id}-STRUCT",
                            reason="Step data must be dictionaries",
                            details=f"Step ID: {step_id}"
                        )
                    )
                    continue

                # Check for expanded allowed files
                frozen_files = set(frozen_step.get("allowed_files", []))
                candidate_files = set(candidate_step.get("allowed_files", []))

                if not candidate_files.issubset(frozen_files):
                    violations.append(
                        ContractViolation(
                            requirement_id=f"STEP-{step_id}-FILES",
                            reason="Allowed files expanded beyond frozen contract",
                            details=f"Frozen: {frozen_files}, Candidate: {candidate_files}"
                        )
                    )

                # Check for wildcards in allowed files
                for file_path in candidate_files:
                    if self._contains_wildcard(file_path):
                        violations.append(
                            ContractViolation(
                                requirement_id=f"STEP-{step_id}-WILDCARD",
                                reason="Wildcard detected in allowed files",
                                details=f"Path: {file_path}"
                            )
                        )

                # Check for reduced required_tests
                frozen_tests = set(frozen_step.get("required_tests", []))
                candidate_tests = set(candidate_step.get("required_tests", []))
                if not candidate_tests.issuperset(frozen_tests):
                    violations.append(
                        ContractViolation(
                            requirement_id=f"STEP-{step_id}-TESTS",
                            reason="Required tests were reduced",
                            details=f"Missing tests: {frozen_tests - candidate_tests}"
                        )
                    )

                # Check for reduced forbidden_paths
                frozen_forbidden_paths = set(frozen_step.get("forbidden_paths", []))
                candidate_forbidden_paths = set(candidate_step.get("forbidden_paths", []))
                if not candidate_forbidden_paths.issuperset(frozen_forbidden_paths):
                    violations.append(
                        ContractViolation(
                            requirement_id=f"STEP-{step_id}-FPATH",
                            reason="Forbidden paths were reduced",
                            details=f"Removed forbidden paths: {frozen_forbidden_paths - candidate_forbidden_paths}"
                        )
                    )

                # Check for reduced forbidden_operations
                frozen_forbidden_ops = set(frozen_step.get("forbidden_operations", []))
                candidate_forbidden_ops = set(candidate_step.get("forbidden_operations", []))
                if not candidate_forbidden_ops.issuperset(frozen_forbidden_ops):
                    violations.append(
                        ContractViolation(
                            requirement_id=f"STEP-{step_id}-FOPS",
                            reason="Forbidden operations were reduced",
                            details=f"Removed operations: {frozen_forbidden_ops - candidate_forbidden_ops}"
                        )
                    )

        # Check diff policy numeric limits
        frozen_diff = self._frozen.get("diff_policy", {})
        candidate_diff = candidate.get("diff_policy", {})

        # Validate structure
        if not isinstance(frozen_diff, dict) or not isinstance(candidate_diff, dict):
            violations.append(
                ContractViolation(
                    requirement_id="DIFF-STRUCT",
                    reason="Diff policy must be dictionaries",
                    details=f"Frozen type: {type(frozen_diff)}, Candidate type: {type(candidate_diff)}"
                )
            )
        else:
            for limit_key in ["maximum_changed_lines_per_step", "maximum_files_per_step"]:
                frozen_limit = frozen_diff.get(limit_key)
                candidate_limit = candidate_diff.get(limit_key)

                # If frozen has the limit, candidate must also have it
                if frozen_limit is not None:
                    if candidate_limit is None:
                        violations.append(
                            ContractViolation(
                                requirement_id="DIFF-002",
                                reason=f"Required numeric limit missing: {limit_key}",
                                details=f"Frozen has {limit_key}={frozen_limit}, candidate omitted it"
                            )
                        )
                    elif not self._compare_numeric_limits(frozen_limit, candidate_limit):
                        violations.append(
                            ContractViolation(
                                requirement_id="DIFF-003",
                                reason=f"Diff limit increased: {limit_key}",
                                details=f"Frozen: {frozen_limit}, Candidate: {candidate_limit}"
                            )
                        )

        # Check stage_a requirements
        frozen_stage = self._frozen.get("stage_a", {})
        candidate_stage = candidate.get("stage_a", {})

        if frozen_stage and candidate_stage:
            frozen_reqs = frozen_stage.get("requirements", {})
            candidate_reqs = candidate_stage.get("requirements", {})

            for req_id in frozen_reqs:
                if req_id not in candidate_reqs:
                    violations.append(
                        ContractViolation(
                            requirement_id="STAGE-A-001",
                            reason="Stage A requirement was removed",
                            details=f"Requirement ID: {req_id}"
                        )
                    )
                    continue

                frozen_text = frozen_reqs[req_id]
                candidate_text = candidate_reqs[req_id]

                if frozen_text != candidate_text:
                    violations.append(
                        ContractViolation(
                            requirement_id="STAGE-A-002",
                            reason="Stage A requirement text was changed",
                            details=f"Requirement {req_id}: Frozen='{frozen_text}', Candidate='{candidate_text}'"
                        )
                    )

        # Check global invariants preservation
        frozen_invs = self._frozen.get("global_invariants", {})
        candidate_invs = candidate.get("global_invariants", {})

        for inv_id in frozen_invs:
            if inv_id not in candidate_invs:
                violations.append(
                    ContractViolation(
                        requirement_id="INV-003",
                        reason="Global invariant was removed",
                        details=f"Invariant ID: {inv_id}"
                    )
                )
                continue

            frozen_inv_text = frozen_invs[inv_id]
            candidate_inv_text = candidate_invs[inv_id]

            if frozen_inv_text != candidate_inv_text:
                violations.append(
                    ContractViolation(
                        requirement_id="INV-004",
                        reason="Global invariant text was changed",
                        details=f"Invariant {inv_id}: Frozen='{frozen_inv_text}', Candidate='{candidate_inv_text}'"
                    )
                )

        # Return verdict
        if violations:
            return PolicyVerdict(allowed=False, violations=tuple(violations))

        # If we got here with no violations, the candidate is monotonic
        return PolicyVerdict(allowed=True)


def load_frozen_contract(frozen_path: str | Path) -> dict[str, Any]:
    """
    Load a frozen contract from a JSON file.

    Args:
        frozen_path: Path to the frozen contract JSON file.

    Returns:
        The loaded contract as a dictionary.

    Raises:
        ValueError: If the file cannot be read or contains invalid JSON.
    """
    frozen_path = Path(frozen_path)

    if not frozen_path.exists():
        raise ValueError(f"Frozen contract file does not exist: {frozen_path}")

    if not frozen_path.is_file():
        raise ValueError(f"Frozen contract path is not a file: {frozen_path}")

    try:
        content = frozen_path.read_text(encoding="utf-8")
        contract = json.loads(content)
    except Exception as e:
        raise ValueError(f"Failed to load frozen contract: {e}") from e

    if not isinstance(contract, dict):
        raise ValueError("Frozen contract must be a JSON object")

    if not contract:
        raise ValueError("Frozen contract cannot be empty")

    return contract


def compare_contracts(
    frozen_path: str | Path,
    candidate_path: str | Path,
    expected_frozen_hash: str | None = None
) -> PolicyVerdict:
    """
    High-level function to compare two contract files.

    Args:
        frozen_path: Path to the frozen contract JSON file.
        candidate_path: Path to the candidate contract JSON file.
        expected_frozen_hash: Optional SHA-256 hash to verify the frozen contract.

    Returns:
        PolicyVerdict indicating whether the candidate is allowed.
    """
    try:
        frozen = load_frozen_contract(frozen_path)
        candidate = load_frozen_contract(candidate_path)
    except ValueError as e:
        return PolicyVerdict(
            allowed=False,
            violations=(
                ContractViolation(
                    requirement_id="LOAD-001",
                    reason="Failed to load contract files",
                    details=str(e)
                ),
            )
        )

    guard = FrozenContractGuard(frozen)

    # Verify hash if provided
    if expected_frozen_hash:
        hash_result = guard.verify_hash(frozen_path, expected_frozen_hash)
        if not hash_result.allowed:
            return hash_result

    return guard.compare(candidate)
