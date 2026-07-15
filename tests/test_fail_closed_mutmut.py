#!/usr/bin/env python3
"""
Fail-closed gate tests for mutmut - simplified import handling.
Tests critical fail-closed behavior without complex imports.
"""

import sys
from pathlib import Path

# Handle both normal and mutmut execution
src_path = Path(__file__).parent.parent / "src"
if src_path.exists():
    sys.path.insert(0, str(src_path))

from joao_orchestrator.v2.pr_gates import (
    PRBudgetGate, ScopeGate, EvidenceProvenanceGate,
    BudgetConfig, ScopeContract, EvidenceClaim, ChangedFile
)


def test_budget_gate_blocks_on_lines_exceeded():
    """Budget gate fails closed when changed lines exceed limit"""
    gate = PRBudgetGate(BudgetConfig(max_non_generated_changed_lines=100))
    
    files = [
        ChangedFile(path="src/feature.py", insertions=80, deletions=30, is_generated=False)
    ]
    
    result = gate.evaluate(files, declared_concerns=["feature-add"])
    
    # Must fail closed immediately
    assert result.passed is False, "Budget gate must fail closed on lines exceeded"
    assert result.detail.get("blocked_at") == "budget_lines"


def test_budget_gate_blocks_on_too_many_files():
    """Budget gate fails closed when behavioral files exceed limit"""
    gate = PRBudgetGate(BudgetConfig(max_behavioral_files=3))
    
    files = [
        ChangedFile(path=f"src/file{i}.py", insertions=10, deletions=0, is_generated=False)
        for i in range(5)
    ]
    
    result = gate.evaluate(files, declared_concerns=["feature-add"])
    
    # Must fail closed immediately
    assert result.passed is False, "Budget gate must fail closed on too many files"
    assert result.detail.get("blocked_at") == "budget_files"


def test_scope_gate_blocks_on_forbidden_path():
    """Scope gate fails closed when forbidden path is touched"""
    gate = ScopeGate()
    contract = ScopeContract(
        allowed_paths=("src/",),
        forbidden_paths=("src/deprecated/",),
        expected_concern="refactor"
    )
    
    files = [ChangedFile(path="src/deprecated/old_module.py", insertions=5, deletions=0, is_generated=False)]
    
    result = gate.evaluate(files, contract)
    
    # Must fail closed immediately
    assert result.passed is False, "Scope gate must fail closed on forbidden path"
    assert result.detail.get("blocked_at") == "forbidden_path"


def test_evidence_gate_blocks_on_malformed_claim():
    """Evidence gate fails closed when claim has missing required fields"""
    gate = EvidenceProvenanceGate()
    
    malformed_claim = EvidenceClaim(
        label="test-suite",
        command="",  # Missing
        expected_exit_code=0,
        artifact_path="results/test.json",
        expected_artifact_hash="",
        producing_commit="",  # Missing
        clean_worktree=True,
        timestamp=""  # Missing
    )
    
    result = gate.evaluate([malformed_claim])
    
    # Must fail closed immediately
    assert result.passed is False, "Evidence gate must fail closed on malformed claim"
    assert result.detail.get("blocked_at") == "claim_validation"


def test_evidence_gate_blocks_on_forbidden_command():
    """Evidence gate fails closed when command template doesn't match"""
    gate = EvidenceProvenanceGate()
    
    bad_claim = EvidenceClaim(
        label="external-fetch",
        command="curl https://example.com/data.json",
        expected_exit_code=0,
        artifact_path="data/fetched.json",
        expected_artifact_hash="abc123",
        producing_commit="def456",
        clean_worktree=True,
        timestamp="2026-07-15T00:00:00Z"
    )
    
    result = gate.evaluate([bad_claim])
    
    # Must fail closed immediately
    assert result.passed is False, "Evidence gate must fail closed on forbidden command"
    assert result.detail.get("blocked_at") == "command_template_match"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))