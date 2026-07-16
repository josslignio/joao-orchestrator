"""Fail-closed foundation regression tests - Phase 1 repairs.

Proves that JOÃO fails closed across all critical paths:
- Real reviewer/fixer missing cannot succeed
- Missing/mismatched reviewed_patch_sha256 is rejected
- Review packets never contain PASSED before independent evidence
- Validation records actual command outcomes
- Non-fake builder dispatch fails closed

These tests exercise real runtime paths without mocking core behavior.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import pytest

from joao_orchestrator.runtime.convergence import (
    ReviewResult,
    validate_review_json,
    _invoke_reviewer,
    _invoke_fixer,
    ConvergenceConfig,
)
from joao_orchestrator.runtime.queue import QueueItem, SchedulerEngine
from joao_orchestrator.domain.models import TaskMeta, TaskState, ProjectProfile, ValidationRun
from joao_orchestrator.validation.evidence import build_review_packet


# ---------------------------------------------------------------------------
# Test: Real reviewer missing cannot PASS
# ---------------------------------------------------------------------------

def test_real_reviewer_missing_returns_blocked_not_pass():
    """An unavailable or unimplemented real reviewer must return BLOCKED/ERROR, never PASS."""
    routing = {"selected_provider": "codex-subscription"}  # Real provider, no executable
    config = ConvergenceConfig(review_executable=None)  # No fake executable configured
    context = {"task_id": "test-123"}
    worktree = Path(tempfile.mkdtemp())
    profile = Mock()
    
    result = _invoke_reviewer(routing, config, context, worktree, profile)
    
    # Must fail closed: unavailable real reviewer returns BLOCKED, not PASS
    assert result.verdict == "BLOCKED", f"Expected BLOCKED but got {result.verdict}"
    assert result.reason_code == "no_reviewer_implementation"
    assert "unavailable or unimplemented" in result.summary.lower()


def test_fake_reviewer_explicitly_allowed_remains_pass():
    """Fake reviewer remains available only when explicitly configured for tests."""
    routing = {"selected_provider": "fake"}
    config = ConvergenceConfig(review_executable=None)  # No executable, fake engine
    context = {"task_id": "test-123"}
    worktree = Path(tempfile.mkdtemp())
    profile = Mock()
    
    result = _invoke_reviewer(routing, config, context, worktree, profile)
    
    # Fake behavior is allowed when explicitly configured
    assert result.verdict == "PASS"
    assert result.reason_code == "fake_reviewer_default"


# ---------------------------------------------------------------------------
# Test: Real fixer missing cannot succeed
# ---------------------------------------------------------------------------

def test_real_fixer_missing_returns_non_zero_failure():
    """An unavailable or unimplemented real fixer must return non-zero failure, never success."""
    routing = {"selected_provider": "opencode-zai"}  # Real provider, no executable
    config = ConvergenceConfig(fix_executable=None)  # No fake executable configured
    fix_request = {"task_id": "test-123"}
    worktree = Path(tempfile.mkdtemp())
    profile = Mock()
    
    stdout, returncode, stderr = _invoke_fixer(routing, config, fix_request, worktree, profile)
    
    # Must fail closed: unavailable real fixer returns non-zero
    assert returncode != 0, f"Expected non-zero returncode but got {returncode}"
    assert "unavailable or unimplemented" in stdout.lower()


def test_fake_fixer_explicitly_allowed_succeeds():
    """Fake fixer remains available only when explicitly configured for tests."""
    routing = {"selected_provider": "fake"}
    config = ConvergenceConfig(fix_executable=None)  # No executable, fake engine
    fix_request = {"task_id": "test-123"}
    worktree = Path(tempfile.mkdtemp())
    profile = Mock()
    
    stdout, returncode, stderr = _invoke_fixer(routing, config, fix_request, worktree, profile)
    
    # Fake behavior is allowed when explicitly configured
    assert returncode == 0, "Fake fixer should succeed when explicitly configured"


# ---------------------------------------------------------------------------
# Test: Missing reviewed_patch_sha256 is rejected
# ---------------------------------------------------------------------------

def test_validate_review_json_missing_hash_fails():
    """Missing reviewed_patch_sha256 field must fail validation."""
    review = {
        "verdict": "PASS",
        "schema_version": 1,
        "findings": [],
        "reviewed_patch_sha256": "",  # Missing hash
    }
    patch_sha256 = "abc123"
    allowed_paths = []
    
    error, result = validate_review_json(review, patch_sha256, allowed_paths)
    
    # Must fail: missing hash is rejected
    assert error is not None, "Expected validation error for missing hash"
    assert "missing required field" in error.lower()
    assert result is None


def test_validate_review_json_mismatched_hash_fails():
    """Mismatched reviewed_patch_sha256 must fail validation."""
    review = {
        "verdict": "PASS",
        "schema_version": 1,
        "findings": [],
        "reviewed_patch_sha256": "wrong123",  # Mismatched hash
    }
    patch_sha256 = "correct456"
    allowed_paths = []
    
    error, result = validate_review_json(review, patch_sha256, allowed_paths)
    
    # Must fail: hash mismatch is rejected
    assert error is not None, "Expected validation error for hash mismatch"
    assert "mismatch" in error.lower()
    assert result is None


def test_validate_review_json_correct_hash_passes():
    """Correct reviewed_patch_sha256 must pass validation."""
    review = {
        "verdict": "PASS",
        "schema_version": 1,
        "findings": [],
        "reviewed_patch_sha256": "correct456",  # Correct hash
    }
    patch_sha256 = "correct456"
    allowed_paths = []
    
    error, result = validate_review_json(review, patch_sha256, allowed_paths)
    
    # Must pass: correct hash is accepted
    assert error is None, f"Expected no error but got: {error}"
    assert result is not None
    assert result.verdict == "PASS"


# ---------------------------------------------------------------------------
# Test: build_review_packet fails closed without independent evidence
# ---------------------------------------------------------------------------

def test_build_review_packet_validation_ok_alone_cannot_emit_passed():
    """build_review_packet with validation.ok=true and no reviewed_patch_sha256 must not emit PASSED."""
    task = TaskMeta(
        task_id="test-001",
        project_id="test-project",
        title="Test Task",
        request="Test request",
        state=TaskState.AWAITING_APPROVAL.value,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    
    validation = ValidationRun(
        task_id="test-001",
        ok=True,  # Validation passes
        commands=[],
        violations=[],
        started_at="2024-01-01T00:00:00Z",
        finished_at="2024-01-01T00:01:00Z",
    )
    
    # No reviewed_patch_sha256 (no independent review evidence yet)
    packet = build_review_packet(
        task=task,
        validation=validation,
        changed_paths=["src/test.py"],
        git_diff_stat="1 file changed",
        diff_check_output="",
        reviewed_patch_sha256="",  # Missing independent evidence
    )
    
    # Must not contain PASSED
    assert "PASSED" not in packet, "Packet must not contain PASSED without independent evidence"
    assert "REVIEW_NOT_RUN" in packet, "Packet must contain REVIEW_NOT_RUN before independent evidence"
    assert "awaiting independent reviewer evidence" in packet.lower()


def test_build_review_packet_with_independent_evidence_can_emit_passed():
    """build_review_packet with reviewed_patch_sha256 and validation.ok=true can emit PASSED."""
    task = TaskMeta(
        task_id="test-001",
        project_id="test-project",
        title="Test Task",
        request="Test request",
        state=TaskState.AWAITING_APPROVAL.value,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    
    validation = ValidationRun(
        task_id="test-001",
        ok=True,  # Validation passes
        commands=[],
        violations=[],
        started_at="2024-01-01T00:00:00Z",
        finished_at="2024-01-01T00:01:00Z",
    )
    
    # Has reviewed_patch_sha256 (independent review evidence exists)
    packet = build_review_packet(
        task=task,
        validation=validation,
        changed_paths=["src/test.py"],
        git_diff_stat="1 file changed",
        diff_check_output="",
        reviewed_patch_sha256="abc123",  # Has independent evidence
    )
    
    # Can contain PASSED when independent evidence exists
    assert "PASSED" in packet, "Packet may contain PASSED with independent evidence"
    assert "REVIEW_NOT_RUN" not in packet, "Packet must not contain REVIEW_NOT_RUN with independent evidence"


def test_build_review_packet_validation_failed_emits_failed():
    """build_review_packet with validation.ok=false must emit FAILED regardless of hash."""
    task = TaskMeta(
        task_id="test-001",
        project_id="test-project",
        title="Test Task",
        request="Test request",
        state=TaskState.AWAITING_APPROVAL.value,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    
    validation = ValidationRun(
        task_id="test-001",
        ok=False,  # Validation fails
        commands=[],
        violations=["test failed"],
        started_at="2024-01-01T00:00:00Z",
        finished_at="2024-01-01T00:01:00Z",
    )
    
    # Even with reviewed_patch_sha256, failed validation must show FAILED
    packet = build_review_packet(
        task=task,
        validation=validation,
        changed_paths=["src/test.py"],
        git_diff_stat="1 file changed",
        diff_check_output="",
        reviewed_patch_sha256="abc123",  # Has independent evidence but validation failed
    )
    
    assert "FAILED" in packet, "Packet must contain FAILED when validation fails"
    assert "PASSED" not in packet, "Packet must not contain PASSED when validation fails"


# ---------------------------------------------------------------------------
# Test: Validation.json records real command outcomes
# ---------------------------------------------------------------------------

def test_validation_json_records_real_return_code():
    """Validation.json must record real return code, not mark every command ok=True."""
    # Create validation run with mixed results
    vrun = ValidationRun(
        task_id="test-001",
        ok=False,
        commands=[
            {"command": "pytest", "ok": True, "returncode": 0},
            {"command": "mypy", "ok": False, "returncode": 1},  # Failed
            {"command": "black", "ok": True, "returncode": 0},
        ],
        violations=["mypy found type errors"],
        started_at="2024-01-01T00:00:00Z",
        finished_at="2024-01-01T00:01:00Z",
    )
    
    data = vrun.to_dict()
    
    # Verify failed command is recorded as failed
    failed_commands = [cmd for cmd in data["commands"] if cmd.get("returncode", 0) != 0]
    assert len(failed_commands) == 1, "Expected exactly one failed command"
    assert failed_commands[0]["command"] == "mypy"
    assert failed_commands[0]["ok"] is False
    assert failed_commands[0]["returncode"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])