#!/usr/bin/env python3
"""
Boundary condition tests - catch behavioral mutations like > vs >=

These tests ensure exact boundary conditions are tested to kill mutations
that change comparison operators (e.g., > to >=, < to <=).
"""

import sys
from pathlib import Path
sys.path.insert(0, 'src')

from joao_orchestrator.v2.pr_gates import (
    PRBudgetGate, ScopeGate, EvidenceProvenanceGate,
    BudgetConfig, ScopeContract, EvidenceClaim, ChangedFile
)


def test_budget_gate_exact_boundary_at_limit():
    """Budget gate: exactly at limit should PASS"""
    gate = PRBudgetGate(BudgetConfig(max_non_generated_changed_lines=100))
    
    # Exactly at limit (100 lines) should PASS
    files = [ChangedFile(path="src/feature.py", insertions=100, deletions=0, is_generated=False)]
    result = gate.evaluate(files, declared_concerns=["feature-add"])
    
    assert result.passed is True, f"Exactly 100 lines should pass, got passed={result.passed}"


def test_budget_gate_boundary_plus_one():
    """Budget gate: limit + 1 should FAIL"""
    gate = PRBudgetGate(BudgetConfig(max_non_generated_changed_lines=100))
    
    # Limit + 1 (101 lines) should FAIL
    files = [ChangedFile(path="src/feature.py", insertions=101, deletions=0, is_generated=False)]
    result = gate.evaluate(files, declared_concerns=["feature-add"])
    
    assert result.passed is False, f"101 lines should fail, got passed={result.passed}"
    assert result.detail.get("blocked_at") == "budget_lines"


def test_budget_gate_zero_files():
    """Budget gate: zero files edge case"""
    gate = PRBudgetGate(BudgetConfig(max_behavioral_files=3))
    
    # Empty file list should PASS
    result = gate.evaluate([], declared_concerns=["test"])
    
    assert result.passed is True, "Zero files should pass"


def test_budget_gate_exact_file_limit():
    """Budget gate: exactly at file limit should PASS"""
    gate = PRBudgetGate(BudgetConfig(max_behavioral_files=3))
    
    # Exactly at limit (3 files) should PASS
    files = [
        ChangedFile(path=f"src/file{i}.py", insertions=10, deletions=0, is_generated=False)
        for i in range(3)
    ]
    result = gate.evaluate(files, declared_concerns=["test"])
    
    assert result.passed is True, f"Exactly 3 files should pass, got passed={result.passed}"


def test_budget_gate_file_limit_plus_one():
    """Budget gate: limit + 1 file should FAIL"""
    gate = PRBudgetGate(BudgetConfig(max_behavioral_files=3))
    
    # Limit + 1 (4 files) should FAIL
    files = [
        ChangedFile(path=f"src/file{i}.py", insertions=10, deletions=0, is_generated=False)
        for i in range(4)
    ]
    result = gate.evaluate(files, declared_concerns=["test"])
    
    assert result.passed is False, f"4 files should fail, got passed={result.passed}"
    assert result.detail.get("blocked_at") == "budget_files"


def test_scope_gate_empty_forbidden_list():
    """Scope gate: empty forbidden list should allow anything in scope"""
    gate = ScopeGate()
    contract = ScopeContract(
        allowed_paths=("src/",),
        forbidden_paths=(),  # Empty forbidden list
        expected_concern="test"
    )
    
    # File in allowed path should PASS
    files = [ChangedFile(path="src/module.py", insertions=10, deletions=0, is_generated=False)]
    result = gate.evaluate(files, contract)
    
    assert result.passed is True, "File in allowed path should pass"


def test_scope_gate_exact_forbidden_match():
    """Scope gate: exact forbidden path match should FAIL"""
    gate = ScopeGate()
    contract = ScopeContract(
        allowed_paths=("src/",),
        forbidden_paths=("src/deprecated/",),
        expected_concern="test"
    )
    
    # Exact match should FAIL
    files = [ChangedFile(path="src/deprecated/old.py", insertions=10, deletions=0, is_generated=False)]
    result = gate.evaluate(files, contract)
    
    assert result.passed is False, "Forbidden path should fail"
    assert result.detail.get("blocked_at") == "forbidden_path"


def test_scope_gate_boundary_path():
    """Scope gate: path at boundary (deprecated/ subdir) should FAIL"""
    gate = ScopeGate()
    contract = ScopeContract(
        allowed_paths=("src/",),
        forbidden_paths=("src/deprecated/",),
        expected_concern="test"
    )
    
    # Subdirectory of forbidden path should FAIL
    files = [ChangedFile(path="src/deprecated/v1/old.py", insertions=10, deletions=0, is_generated=False)]
    result = gate.evaluate(files, contract)
    
    assert result.passed is False, "Subdirectory of forbidden path should fail"


def test_evidence_gate_empty_claim_list():
    """Evidence gate: empty claim list should PASS"""
    gate = EvidenceProvenanceGate()
    
    # No claims should PASS (nothing to validate)
    result = gate.evaluate([])
    
    assert result.passed is True, "Empty claim list should pass"


def test_evidence_gate_exact_exit_code_match():
    """Evidence gate: exact exit code match should PASS"""
    gate = EvidenceProvenanceGate()
    
    # Use network-safe echo command (no Python to avoid network isolation blocking)
    claim = EvidenceClaim(
        label="test-echo",
        command="echo 'test'",
        expected_exit_code=0,
        artifact_path="",
        expected_artifact_hash="",
        producing_commit="abc123",
        clean_worktree=True,
        timestamp="2026-07-15T00:00:00Z"
    )
    
    result = gate.evaluate([claim])
    
    assert result.passed is True, f"Matching exit code should pass, got {result.findings}"


def test_evidence_gate_exit_code_boundary():
    """Evidence gate: exit code + 1 should FAIL"""
    gate = EvidenceProvenanceGate()
    
    # Use network-safe false command (no Python to avoid network isolation blocking)
    # false command exits with code 1
    claim = EvidenceClaim(
        label="failing-command",
        command="false",  # Always exits with code 1
        expected_exit_code=0,  # Expects 0 but will get 1
        artifact_path="",
        expected_artifact_hash="",
        producing_commit="abc123",
        clean_worktree=True,
        timestamp="2026-07-15T00:00:00Z"
    )
    
    result = gate.evaluate([claim])
    
    assert result.passed is False, "Exit code mismatch should fail"
    assert result.detail.get("blocked_at") == "exit_code_mismatch"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))