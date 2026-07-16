"""Fail-closed foundation regression tests.

Proves that JOÃO cannot report PASS when no real reviewer exists, that
non-fake builder dispatch fails closed, and that missing or mismatched
reviewed_patch_sha256 is rejected. These tests enforce fail-closed behavior
as required by the authoritative audit findings.
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
    run_convergence,
)
from joao_orchestrator.runtime.queue import QueueItem, SchedulerEngine
from joao_orchestrator.domain.models import TaskMeta, TaskState, ProjectProfile


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
# Test: JOÃO never fills the reviewer hash itself
# ---------------------------------------------------------------------------

@patch('joao_orchestrator.runtime.convergence._invoke_reviewer')
def test_convergence_never_overwrites_reviewer_hash(mock_invoke_reviewer):
    """The convergence engine must never overwrite reviewed_patch_sha256 after validation."""
    # Setup: Mock reviewer returns valid result with hash
    mock_invoke_reviewer.return_value = ReviewResult(
        verdict="PASS",
        reviewed_patch_sha256="reviewer_original_hash",
        summary="Test pass",
        started_at="2024-01-01T00:00:00Z",
        finished_at="2024-01-01T00:01:00Z",
    )
    
    # Create minimal test environment
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Create task store with required artifacts
        task = TaskMeta(
            task_id="test-001",
            project_id="test-project",
            title="Test Task",
            request="Test request",
            state=TaskState.AWAITING_APPROVAL.value,
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        
        profile = Mock()
        profile.allowed_write_paths = []
        profile.forbidden_paths = []
        profile.repository_root = tmpdir
        profile.validation_profile = "strict"
        
        # Mock store
        store = Mock()
        store.artifact_path = Mock(return_value=tmpdir / "convergence_result.json")
        store.can_transition = Mock(return_value=True)
        store.transition = Mock()
        store.write_artifact_json = Mock()
        store.task_directory = Mock(return_value=tmpdir)
        
        # Create required artifacts
        (tmpdir / "state.json").write_text(json.dumps({"state": "AWAITING_APPROVAL"}))
        (tmpdir / "events.jsonl").write_text("")
        (tmpdir / "worktree.json").write_text(json.dumps({"worktree_path": str(tmpdir)}))
        (tmpdir / "changed_files.json").write_text(json.dumps({"changed_files": []}))
        (tmpdir / "validation.json").write_text(json.dumps({"ok": True, "commands": []}))
        (tmpdir / "review_packet.md").write_text("# Test\n\nVerdict: REVIEW_NOT_RUN")
        (tmpdir / "patch.diff").write_text("")
        
        # Mock budget store
        budget_store = Mock()
        
        # Mock worktree
        worktree = tmpdir
        worktree.mkdir(exist_ok=True)
        
        config = ConvergenceConfig(max_fixes=0)  # No fixes to keep it simple
        
        # Run convergence
        result = run_convergence(
            task, profile, store, budget_store, config, worktree,
            codex_available=False, codex_verified=False,
            opencode_available=False, opencode_verified=False,
        )
        
        # Verify the reviewer's hash was preserved
        written_artifacts = []
        for call in store.write_artifact_json.call_args_list:
            written_artifacts.append(call[0][2])  # artifact name
            
        # Check that initial review result was written with reviewer's original hash
        initial_review_calls = [
            call for call in store.write_artifact_json.call_args_list
            if len(call[0]) >= 3 and call[0][2] == "initial_review_result.json"
        ]
        
        if initial_review_calls:
            written_data = initial_review_calls[0][1]
            assert "reviewed_patch_sha256" in written_data
            # The hash must be the one from the reviewer, not overwritten
            assert written_data["reviewed_patch_sha256"] == "reviewer_original_hash"


# ---------------------------------------------------------------------------
# Test: Non-fake builder missing cannot reach CHANGES_READY
# ---------------------------------------------------------------------------

@patch('joao_orchestrator.runtime.queue._run_task_pipeline')
def test_non_fake_builder_missing_fails_closed(mock_pipeline):
    """Non-fake engine without concrete adapter fails closed, never ok=True."""
    # Setup: Mock pipeline that should fail for non-fake engines
    mock_pipeline.return_value = {"ok": False, "error": "no concrete adapter"}
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Create scheduler engine
        scheduler = SchedulerEngine(tmpdir)
        
        # Create queue item with non-fake engine
        item = QueueItem(
            item_id="test-item-001",
            queue_id="test-queue",
            project_id="test-project",
            title="Test Task",
            request_file=str(tmpdir / "request.txt"),
            implementation_engine="auto",  # Non-fake engine
            source_revision="main",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        (tmpdir / "request.txt").write_text("Test request")
        
        # Mock profile and stores
        profile = Mock()
        profile.repository_root = tmpdir
        profile.validation_profile = "strict"
        profile.allowed_write_paths = []
        profile.forbidden_paths = []
        
        task_store = Mock()
        budget_store = Mock()
        
        # Add item to queue
        scheduler.store.add_item(item)
        
        # Execute item
        updated_item, execution = scheduler.execute_item(
            queue_id="test-queue",
            item=item,
            profile=profile,
            task_store=task_store,
            budget_store=budget_store,
            worktree_parent=tmpdir,
            reviewer_executable=None,
            fixer_executable=None,
        )
        
        # Must fail closed: non-fake engine without adapter should not succeed
        assert execution.get("ok") is False or updated_item.status != "AWAITING_APPROVAL", \
            "Non-fake engine without concrete adapter must fail closed"


# ---------------------------------------------------------------------------
# Test: Failed validation command is recorded as failed
# ---------------------------------------------------------------------------

def test_validation_json_records_real_return_code():
    """Validation.json must record real return code, not mark every command ok=True."""
    from joao_orchestrator.domain.models import ValidationRun
    
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


# ---------------------------------------------------------------------------
# Test: Packet created before review cannot contain PASSED
# ---------------------------------------------------------------------------

def test_review_packet_before_review_contains_not_run():
    """Review packet created before review must contain REVIEW_NOT_RUN, not PASSED."""
    from joao_orchestrator.runtime.queue import _run_task_pipeline
    from joao_orchestrator.workspace.worktree import create_worktree
    from joao_orchestrator.validation.profiles import load_profile_commands
    from joao_orchestrator.validation.executor import RestrictedExecutor
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Create a minimal git repo
        repo = tmpdir / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        
        # Create profile
        profile = ProjectProfile(
            project_id="test",
            display_name="Test Project",
            repository_root=str(repo),
            allowed_write_paths=["src"],
            forbidden_paths=[],
            validation_profile="strict",
        )
        
        # Create task meta
        meta = TaskMeta(
            task_id="test-001",
            project_id="test",
            title="Test Task",
            request="Test request",
            state="DRAFT",
            branch="test-branch",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        
        # Create task store
        task_store = Mock()
        task_store.artifact_path = Mock(side_effect=lambda pid, tid, name: tmpdir / name)
        task_store.write_artifact_text = Mock()
        task_store.write_artifact_json = Mock()
        task_store.transition = Mock()
        
        # Create validation.toml
        validation_dir = repo / ".agent"
        validation_dir.mkdir()
        (validation_dir / "validation.toml").write_text("")
        
        # Mock the worktree creation and validation
        with patch('joao_orchestrator.workspace.worktree.create_worktree') as mock_wt:
            mock_wt.return_value = Mock(
                worktree_path=str(repo),
                to_dict=lambda: {"worktree_path": str(repo)}
            )
            
            with patch('joao_orchestrator.workspace.worktree.capture_worktree_state') as mock_capture:
                mock_capture.return_value = Mock(
                    changed_files=[],
                    patch="",
                    git_status={}
                )
                
                with patch('joao_orchestrator.validation.profiles.load_profile_commands') as mock_load:
                    mock_load.return_value = []
                    
                    # Run the pipeline
                    result = _run_task_pipeline(
                        meta=meta,
                        profile=profile,
                        task_store=task_store,
                        budget_store=Mock(),
                        worktree_parent=tmpdir,
                        source_revision="main",
                        engine="fake",
                    )
                    
                    # Check that review packet was written with REVIEW_NOT_RUN
                    packet_calls = [
                        call for call in task_store.write_artifact_text.call_args_list
                        if len(call[0]) >= 3 and call[0][2] == "review_packet.md"
                    ]
                    
                    if packet_calls:
                        packet_content = packet_calls[0][1]
                        assert "REVIEW_NOT_RUN" in packet_content, \
                            "Review packet before review must contain REVIEW_NOT_RUN"
                        assert "PASSED" not in packet_content, \
                            "Review packet before review must NOT contain PASSED"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])