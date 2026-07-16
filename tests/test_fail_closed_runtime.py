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
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
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
# Mock storage for integration tests
# ---------------------------------------------------------------------------

class MockTaskStore:
    """Minimal TaskStore mock for pipeline integration tests."""

    def __init__(self):
        self.transitions = []
        self.artifacts = {}
        self.states = {}

    def write_artifact_json(self, project_id, task_id, name, data):
        key = (project_id, task_id, name)
        self.artifacts[key] = ("json", json.dumps(data))

    def write_artifact_text(self, project_id, task_id, name, content):
        key = (project_id, task_id, name)
        self.artifacts[key] = ("text", content)

    def get_artifact_text(self, project_id, task_id, name):
        key = (project_id, task_id, name)
        if key in self.artifacts:
            return self.artifacts[key][1]
        raise FileNotFoundError(f"Artifact not found: {name}")

    def transition(self, project_id, task_id, state, reason=""):
        self.states[(project_id, task_id)] = state
        self.transitions.append({
            "project_id": project_id,
            "task_id": task_id,
            "state": state,
            "reason": reason
        })

    def load(self, project_id, task_id):
        # Return minimal task metadata
        return Mock(
            task_id=task_id,
            project_id=project_id,
            state=self.states.get((project_id, task_id), "QUEUED"),
            branch="main",
            title="Mock Task"
        )

    def artifact_path(self, project_id, task_id, name):
        return f"/mock/artifacts/{project_id}/{task_id}/{name}"


class MockBudgetStore:
    """Minimal BudgetStore mock for pipeline integration tests."""

    def __init__(self):
        self.blocks = []

    def check_task_budget(self, project_id, task_id):
        return {"ok": True, "remaining_minutes": 999}

    def record_task_usage(self, project_id, task_id, minutes_used):
        pass


# ---------------------------------------------------------------------------
# Test: Real reviewer missing cannot PASS
# ---------------------------------------------------------------------------

def test_real_reviewer_missing_returns_blocked_not_pass(tmp_path):
    """An unavailable or unimplemented real reviewer must return BLOCKED/ERROR, never PASS."""
    routing = {"selected_provider": "codex-subscription"}  # Real provider, no executable
    config = ConvergenceConfig(review_executable=None)  # No fake executable configured
    context = {"task_id": "test-123"}
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    profile = Mock()

    result = _invoke_reviewer(routing, config, context, worktree, profile)

    # Must fail closed: unavailable real reviewer returns BLOCKED, not PASS
    assert result.verdict == "BLOCKED", f"Expected BLOCKED but got {result.verdict}"
    assert result.reason_code == "no_reviewer_implementation"
    assert "unavailable or unimplemented" in result.summary.lower()


def test_fake_reviewer_explicitly_allowed_remains_pass(tmp_path):
    """Fake reviewer remains available only when explicitly configured for tests."""
    routing = {"selected_provider": "fake"}
    config = ConvergenceConfig(review_executable=None)  # No executable, fake engine
    context = {"task_id": "test-123"}
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    profile = Mock()

    result = _invoke_reviewer(routing, config, context, worktree, profile)

    # Fake behavior is allowed when explicitly configured
    assert result.verdict == "PASS"
    assert result.reason_code == "fake_reviewer_default"


# ---------------------------------------------------------------------------
# Test: Real fixer missing cannot succeed
# ---------------------------------------------------------------------------

def test_real_fixer_missing_returns_non_zero_failure(tmp_path):
    """An unavailable or unimplemented real fixer must return non-zero failure, never success."""
    routing = {"selected_provider": "opencode-zai"}  # Real provider, no executable
    config = ConvergenceConfig(fix_executable=None)  # No fake executable configured
    fix_request = {"task_id": "test-123"}
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    profile = Mock()

    stdout, returncode, stderr = _invoke_fixer(routing, config, fix_request, worktree, profile)

    # Must fail closed: unavailable real fixer returns non-zero
    assert returncode != 0, f"Expected non-zero returncode but got {returncode}"
    assert "unavailable or unimplemented" in stdout.lower()


def test_fake_fixer_explicitly_allowed_succeeds(tmp_path):
    """Fake fixer remains available only when explicitly configured for tests."""
    routing = {"selected_provider": "fake"}
    config = ConvergenceConfig(fix_executable=None)  # No executable, fake engine
    fix_request = {"task_id": "test-123"}
    worktree = tmp_path / "worktree"
    worktree.mkdir()
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



# ---------------------------------------------------------------------------
# Test: Queue pipeline fail-closed integration tests
# ---------------------------------------------------------------------------

def test_queue_pipeline_non_fake_engine_fails_closed(tmp_path):
    """_run_task_pipeline with engine != 'fake' must fail closed, never return synthetic success."""
    from joao_orchestrator.runtime.queue import _run_task_pipeline

    # Create minimal task metadata
    meta = Mock()
    meta.task_id = "test-task-001"
    meta.project_id = "test-project"
    meta.branch = "test-branch"

    # Create minimal profile
    profile = Mock()
    profile.repository_root = str(tmp_path / "repo")
    profile.allowed_write_paths = ["*"]
    profile.forbidden_paths = []
    profile.validation_profile = "default"

    # Create minimal repository
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    (repo_path / "test.txt").write_text("test")

    # Initialize git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path, capture_output=True)

    # Create mock stores
    task_store = MockTaskStore()
    budget_store = MockBudgetStore()

    # Test with non-fake engine (e.g., "manual" - no concrete adapter)
    result = _run_task_pipeline(
        meta=meta,
        profile=profile,
        task_store=task_store,
        budget_store=budget_store,
        worktree_parent=tmp_path / "worktrees",
        source_revision="HEAD",
        engine="manual"  # Non-fake engine with no adapter
    )

    # Must fail closed: non-fake engine should not return synthetic success
    assert result["ok"] is False, f"Expected ok=False for non-fake engine, but got {result}"
    assert "error" in result, "Result must contain error field"
    assert "no concrete adapter" in result["error"].lower() or "engine" in result["error"].lower()


def test_queue_pipeline_review_packet_status_is_review_not_run(tmp_path):
    """Queue-generated review_packet.md must use REVIEW_NOT_RUN status, not PASSED."""
    from joao_orchestrator.runtime.queue import _run_task_pipeline
    from joao_orchestrator.providers.fake_provider import FakeProvider
    from joao_orchestrator.policy.capabilities import CapabilitySet

    # Create minimal task metadata
    meta = Mock()
    meta.task_id = "test-task-002"
    meta.project_id = "test-project"
    meta.branch = "test-branch"

    # Create minimal profile with validation commands that will succeed
    profile = Mock()
    profile.repository_root = str(tmp_path / "repo")
    profile.allowed_write_paths = ["*"]
    profile.forbidden_paths = []
    profile.validation_profile = "default"
    profile.python_strategy = "python3"
    profile.command_timeout_seconds = 30
    profile.max_output_bytes = 1024
    profile.environment_allowlist = ["PATH", "HOME"]

    # Create minimal repository
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    (repo_path / "test.txt").write_text("test")

    # Initialize git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path, capture_output=True)

    # Create mock stores
    task_store = MockTaskStore()
    budget_store = MockBudgetStore()

    # Create validation.toml that uses allowed executables (python and git only)
    validation_dir = repo_path / ".agent"
    validation_dir.mkdir(exist_ok=True)
    validation_toml = validation_dir / "validation.toml"
    validation_toml.write_text("""
[[commands]]
executable = "git"
args = ["status"]
timeout = 30

[profiles.default]
commands = ["git status"]
""")

    # Run pipeline with fake engine (this should succeed and generate review packet)
    result = _run_task_pipeline(
        meta=meta,
        profile=profile,
        task_store=task_store,
        budget_store=budget_store,
        worktree_parent=tmp_path / "worktrees",
        source_revision="HEAD",
        engine="fake"
    )

    # Pipeline should succeed with fake engine
    assert result["ok"] is True, f"Expected pipeline to succeed with fake engine: {result}"

    # Check that review_packet.md was written with REVIEW_NOT_RUN status
    review_packet_content = task_store.get_artifact_text(meta.project_id, meta.task_id, "review_packet.md")

    # Must contain REVIEW_NOT_RUN, not PASSED (before independent evidence)
    assert "REVIEW_NOT_RUN" in review_packet_content, "Review packet must contain REVIEW_NOT_RUN status"
    assert "PASSED" not in review_packet_content, "Review packet must not contain PASSED before independent evidence"
    assert "## Verdict" in review_packet_content, "Review packet must have verdict section"


def test_queue_pipeline_validation_json_records_real_return_codes(tmp_path):
    """Pipeline must record actual return codes in validation.json, not synthetic ok=True.

    This test proves that when validation commands run through the actual pipeline,
    their real return codes and ok values are recorded in validation.json.
    The test includes a failing git command to ensure failures are not converted to success.
    """
    from joao_orchestrator.runtime.queue import _run_task_pipeline

    # Create minimal task metadata
    meta = Mock()
    meta.task_id = "test-task-003"
    meta.project_id = "test-project"
    meta.branch = "test-branch"

    # Create minimal profile
    profile = Mock()
    profile.repository_root = str(tmp_path / "repo")
    profile.allowed_write_paths = ["*"]
    profile.forbidden_paths = []
    profile.validation_profile = "default"  # Profile with commands
    profile.python_strategy = "python3"
    profile.command_timeout_seconds = 30
    profile.max_output_bytes = 1024
    profile.environment_allowlist = ["PATH", "HOME"]

    # Create minimal repository
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    (repo_path / "test.txt").write_text("test")

    # Initialize git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path, capture_output=True)

    # Create mock stores
    task_store = MockTaskStore()
    budget_store = MockBudgetStore()

    # Create validation.toml with passing commands AND one real failing Git
    # command. The hostile ref is supplied through args_extra so the regression
    # proves that evidence does not silently drop permitted trailing arguments.
    validation_dir = repo_path / ".agent"
    validation_dir.mkdir(exist_ok=True)
    validation_toml = validation_dir / "validation.toml"
    validation_toml.write_text("""
[[commands]]
executable = "git"
args = ["status"]
timeout = 30

[[commands]]
executable = "git"
args = ["ls-files"]
timeout = 30

[[commands]]
executable = "git"
args = ["rev-parse", "--verify"]
extra_args_allowed = ["refs/heads/__joao_intentionally_missing_ref_7f98c7__"]
args_extra = ["refs/heads/__joao_intentionally_missing_ref_7f98c7__"]
timeout = 30

[profiles.default]
commands = ["git status", "git ls-files", "git rev-parse --verify refs/heads/__joao_intentionally_missing_ref_7f98c7__"]
""")

    # Run pipeline with fake engine
    result = _run_task_pipeline(
        meta=meta,
        profile=profile,
        task_store=task_store,
        budget_store=budget_store,
        worktree_parent=tmp_path / "worktrees",
        source_revision="HEAD",
        engine="fake"
    )

    # PROVE: pipeline result ok is false (because validation fails)
    assert result["ok"] is False, f"Expected ok=False due to failing git command, but got: {result}"

    # PROVE: validation.json exists
    validation_json_text = task_store.get_artifact_text(meta.project_id, meta.task_id, "validation.json")
    assert validation_json_text is not None, "validation.json must exist"

    validation_json = json.loads(validation_json_text)

    # PROVE: validation overall ok is false
    assert validation_json["ok"] is False, "Overall validation ok must be false when any command fails"

    # PROVE: exactly one evidence entry contains the complete, resolved argv.
    commands = validation_json.get("commands", [])
    assert len(commands) == 3, f"Expected 3 commands, got {len(commands)}"
    import shlex
    hostile_argv = [
        str(shutil.which("git")), "rev-parse", "--verify",
        "refs/heads/__joao_intentionally_missing_ref_7f98c7__",
    ]
    matches = [cmd for cmd in commands if cmd.get("argv") == hostile_argv]
    assert len(matches) == 1, (
        "validation.json must contain exactly one complete hostile argv entry; "
        f"got {matches}"
    )
    failing_command = matches[0]
    assert failing_command["command"] == shlex.join(hostile_argv)

    # PROVE: command returncode is non-zero
    assert failing_command["returncode"] != 0, f"Failing command must have non-zero returncode, got: {failing_command['returncode']}"

    # PROVE: command ok is false
    assert failing_command["ok"] is False, "Failing command must have ok=False"

    # PROVE: the failure was not converted into success
    # (verified by checking that the command's ok field correctly reflects the failure)
    assert failing_command["ok"] is False, "Failure must not be converted into success"

    # Additional validation: passing commands should still be recorded as passing
    passing_commands = [cmd for cmd in commands if cmd.get("returncode", -1) == 0]
    assert len(passing_commands) == 2, f"Expected 2 passing commands (git status, git ls-files), got {len(passing_commands)}"
    for cmd in passing_commands:
        assert cmd["ok"] is True, f"Passing command should have ok=True: {cmd}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
