"""Focused pytest tests for CP1 checkpoint engine."""

import json
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Mapping
from datetime import datetime, timezone

import pytest

from joao_orchestrator.control_plane import (
    TaskState,
    validate_transition,
    RunEvent,
    BudgetConstraints,
    BudgetUsage,
    PathPolicy,
    CheckpointContract,
    TaskNode,
    ProjectState,
    ProjectRegistration,
    CP1CheckpointEngine,
    CP1_SCHEMA_VERSION,
)
from joao_orchestrator.v2 import state as v2_state


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_state_root(tmp_path):
    """Provide isolated state root for tests."""
    old_root = v2_state.JOSS_ROOT
    v2_state.JOSS_ROOT = tmp_path
    yield tmp_path
    v2_state.JOSS_ROOT = old_root


@pytest.fixture
def engine(temp_state_root):
    """Create a CP1 checkpoint engine instance."""
    return CP1CheckpointEngine("test_project", lock_timeout=1.0)


@pytest.fixture
def sample_budget():
    """Sample budget constraints."""
    return BudgetConstraints(
        max_files=100,
        max_lines=5000,
        max_network_calls=5,
        max_cost_usd=0.50,
        max_model_calls=50,
        max_tokens=50000,
    )


@pytest.fixture
def sample_path_policy():
    """Sample path policy."""
    return PathPolicy(
        allowed_roots=["/tmp/test", "/workspace"],
        denied_patterns=["*.secret", "config/*.json"],
    )


# ---------------------------------------------------------------------------
# TaskState and validation tests
# ---------------------------------------------------------------------------

def test_task_state_enum_values():
    """TaskState enum has correct values."""
    assert TaskState.QUEUED.value == "QUEUED"
    assert TaskState.PENDING.value == "PENDING"
    assert TaskState.RUNNING.value == "RUNNING"
    assert TaskState.PAUSED.value == "PAUSED"
    assert TaskState.COMPLETED.value == "COMPLETED"
    assert TaskState.FAILED.value == "FAILED"
    assert TaskState.BLOCKED.value == "BLOCKED"


def test_validate_valid_transitions():
    """Valid transitions are accepted."""
    validate_transition(TaskState.QUEUED, TaskState.PENDING)
    validate_transition(TaskState.PENDING, TaskState.RUNNING)
    validate_transition(TaskState.RUNNING, TaskState.PAUSED)
    validate_transition(TaskState.RUNNING, TaskState.COMPLETED)
    validate_transition(TaskState.RUNNING, TaskState.FAILED)
    validate_transition(TaskState.PAUSED, TaskState.RUNNING)
    validate_transition(TaskState.PAUSED, TaskState.FAILED)
    validate_transition(TaskState.FAILED, TaskState.PENDING)
    validate_transition(TaskState.BLOCKED, TaskState.PENDING)


def test_validate_invalid_transitions():
    """Invalid transitions raise ValueError."""
    with pytest.raises(ValueError, match="Invalid transition"):
        validate_transition(TaskState.COMPLETED, TaskState.RUNNING)

    with pytest.raises(ValueError, match="Invalid transition"):
        validate_transition(TaskState.COMPLETED, TaskState.FAILED)

    with pytest.raises(ValueError, match="Invalid transition"):
        validate_transition(TaskState.QUEUED, TaskState.COMPLETED)


# ---------------------------------------------------------------------------
# RunEvent tests
# ---------------------------------------------------------------------------

def test_run_event_creation():
    """RunEvent creates and serializes correctly."""
    event = RunEvent(
        task_id="task1",
        from_state="QUEUED",
        to_state="RUNNING",
        timestamp="2024-01-01T12:00:00Z",
        reason="test",
        metadata={"key": "value"},
    )

    assert event.task_id == "task1"
    assert event.from_state == "QUEUED"
    assert event.to_state == "RUNNING"
    assert event.metadata == {"key": "value"}

    d = event.to_dict()
    assert d["task_id"] == "task1"
    assert d["from_state"] == "QUEUED"
    assert d["to_state"] == "RUNNING"


def test_run_event_from_dict():
    """RunEvent deserializes from dict correctly."""
    d = {
        "task_id": "task1",
        "from_state": "QUEUED",
        "to_state": "RUNNING",
        "timestamp": "2024-01-01T12:00:00Z",
        "reason": "test",
        "metadata": {"key": "value"},
    }

    event = RunEvent.from_dict(d)
    assert event.task_id == "task1"
    assert event.from_state == "QUEUED"
    assert event.to_state == "RUNNING"
    assert event.metadata == {"key": "value"}


# ---------------------------------------------------------------------------
# BudgetConstraints tests
# ---------------------------------------------------------------------------

def test_budget_constraints_serialization(sample_budget):
    """BudgetConstraints serializes and deserializes correctly."""
    d = sample_budget.to_dict()
    assert d["max_files"] == 100
    assert d["max_lines"] == 5000

    recovered = BudgetConstraints.from_dict(d)
    assert recovered.max_files == 100
    assert recovered.max_lines == 5000


def test_budget_constraints_defaults():
    """BudgetConstraints has sensible defaults."""
    budget = BudgetConstraints()
    assert budget.max_files == 1000
    assert budget.max_lines == 50000
    assert budget.max_network_calls == 10
    assert budget.max_cost_usd == 1.0
    assert budget.max_model_calls == 100
    assert budget.max_tokens == 100000


# ---------------------------------------------------------------------------
# BudgetUsage tests
# ---------------------------------------------------------------------------

def test_budget_usage_serialization():
    """BudgetUsage serializes and deserializes correctly."""
    usage = BudgetUsage(
        files_touched=10,
        lines_processed=100,
        network_calls=1,
        cost_usd=0.1,
        model_calls=5,
        tokens_used=1000,
    )

    d = usage.to_dict()
    assert d["files_touched"] == 10
    assert d["lines_processed"] == 100

    recovered = BudgetUsage.from_dict(d)
    assert recovered.files_touched == 10
    assert recovered.lines_processed == 100


def test_budget_usage_defaults():
    """BudgetUsage defaults to zero."""
    usage = BudgetUsage()
    assert usage.files_touched == 0
    assert usage.lines_processed == 0
    assert usage.network_calls == 0
    assert usage.cost_usd == 0.0
    assert usage.model_calls == 0
    assert usage.tokens_used == 0


# ---------------------------------------------------------------------------
# PathPolicy tests
# ---------------------------------------------------------------------------

def test_path_policy_serialization(sample_path_policy):
    """PathPolicy serializes and deserializes correctly."""
    d = sample_path_policy.to_dict()
    assert "/tmp/test" in d["allowed_roots"]
    assert "*.secret" in d["denied_patterns"]

    recovered = PathPolicy.from_dict(d)
    assert "/tmp/test" in recovered.allowed_roots
    assert "*.secret" in recovered.denied_patterns


# ---------------------------------------------------------------------------
# CheckpointContract tests
# ---------------------------------------------------------------------------

def test_checkpoint_contract_hash_computation(sample_budget, sample_path_policy):
    """CheckpointContract computes and validates hash correctly."""
    contract = CheckpointContract(
        contract_id="test_contract",
        starting_sha="abc123def456",
        contract_hash="",  # Will compute
        budget_constraints=sample_budget,
        path_policy=sample_path_policy,
        created_at="2024-01-01T12:00:00Z",
    )

    # Compute hash
    contract.contract_hash = contract.compute_contract_hash()
    assert len(contract.contract_hash) == 64  # SHA256 hex

    # Validate passes
    contract.validate_hash()

    # Tamper with budget
    contract.budget_constraints.max_files = 999
    tampered_hash = contract.compute_contract_hash()
    assert tampered_hash != contract.contract_hash

    # Validate fails with tampered data
    with pytest.raises(ValueError, match="Contract hash mismatch"):
        contract.validate_hash()


def test_checkpoint_contract_serialization(sample_budget, sample_path_policy):
    """CheckpointContract serializes and deserializes correctly."""
    contract = CheckpointContract(
        contract_id="test_contract",
        starting_sha="abc123def456",
        contract_hash="hash123",
        budget_constraints=sample_budget,
        path_policy=sample_path_policy,
        created_at="2024-01-01T12:00:00Z",
    )

    d = contract.to_dict()
    assert d["contract_id"] == "test_contract"
    assert d["starting_sha"] == "abc123def456"

    recovered = CheckpointContract.from_dict(d)
    assert recovered.contract_id == "test_contract"
    assert recovered.starting_sha == "abc123def456"


# ---------------------------------------------------------------------------
# TaskNode tests
# ---------------------------------------------------------------------------

def test_task_node_serialization():
    """TaskNode serializes and deserializes correctly."""
    task = TaskNode(
        task_id="task1",
        state=TaskState.RUNNING,
        dependencies=["task0"],
        checkpoint_contract="cp_abc123",
        metadata={"key": "value"},
    )

    d = task.to_dict()
    assert d["task_id"] == "task1"
    assert d["state"] == "RUNNING"
    assert d["dependencies"] == ["task0"]

    recovered = TaskNode.from_dict(d)
    assert recovered.task_id == "task1"
    assert recovered.state == TaskState.RUNNING
    assert recovered.dependencies == ["task0"]


# ---------------------------------------------------------------------------
# ProjectState tests
# ---------------------------------------------------------------------------

def test_project_state_serialization():
    """ProjectState serializes and deserializes correctly."""
    state = ProjectState(
        project_id="test_project",
        current_head="abc123def456",
        tasks={
            "task1": TaskNode(task_id="task1", state=TaskState.RUNNING),
        },
        budget_usage=BudgetUsage(),
        last_valid_state={},
        paused_tasks=set(),
        checkpoint_active=False,
        checkpoint_id=None,
    )

    d = state.to_dict()
    assert d["project_id"] == "test_project"
    assert d["current_head"] == "abc123def456"
    assert "task1" in d["tasks"]

    recovered = ProjectState.from_dict(d)
    assert recovered.project_id == "test_project"
    assert recovered.current_head == "abc123def456"
    assert "task1" in recovered.tasks


# ---------------------------------------------------------------------------
# ProjectRegistration tests
# ---------------------------------------------------------------------------

def test_project_registration_serialization():
    """ProjectRegistration serializes and deserializes correctly."""
    registration = ProjectRegistration(
        project_id="test_project",
        repository_path="/path/to/repo",
        registered_at="2024-01-01T12:00:00Z",
        active=True,
        metadata={"key": "value"},
    )

    d = registration.to_dict()
    assert d["project_id"] == "test_project"
    assert d["repository_path"] == "/path/to/repo"
    assert d["active"] is True

    recovered = ProjectRegistration.from_dict(d)
    assert recovered.project_id == "test_project"
    assert recovered.repository_path == "/path/to/repo"
    assert recovered.active is True


# ---------------------------------------------------------------------------
# CP1CheckpointEngine tests
# ---------------------------------------------------------------------------

def test_engine_initialization(engine, temp_state_root):
    """Engine initializes correctly with proper paths."""
    assert engine.project_id == "test_project"
    assert engine.project_root == temp_state_root / "projects" / "test_project" / "cp1"
    assert engine.project_root.exists()
    assert engine.events_path == engine.project_root / "events.jsonl"
    assert engine.state_path == engine.project_root / "state.json"
    assert engine.previous_state_path == engine.project_root / "state.previous.json"
    assert engine.registry_path == engine.project_root / "registry.json"
    assert engine.contracts_path.exists()


def test_append_only_events(engine):
    """Events are appended atomically and can be retrieved."""
    event1 = RunEvent(
        task_id="task1",
        from_state="QUEUED",
        to_state="RUNNING",
        timestamp="2024-01-01T12:00:00Z",
        reason="test",
    )

    event2 = RunEvent(
        task_id="task1",
        from_state="RUNNING",
        to_state="COMPLETED",
        timestamp="2024-01-01T12:01:00Z",
        reason="completed",
    )

    engine._append_event(event1)
    engine._append_event(event2)

    events = engine.get_events()
    assert len(events) == 2
    assert events[0].task_id == "task1"
    assert events[0].to_state == "RUNNING"
    assert events[1].to_state == "COMPLETED"

    # Test filtering by task_id
    task1_events = engine.get_events(task_id="task1")
    assert len(task1_events) == 2


def test_record_transition(engine):
    """Transitions are recorded as events."""
    engine.record_transition(
        task_id="task1",
        from_state="QUEUED",
        to_state="RUNNING",
        reason="test",
    )

    events = engine.get_events()
    assert len(events) == 1
    assert events[0].task_id == "task1"
    assert events[0].from_state == "QUEUED"
    assert events[0].to_state == "RUNNING"
    assert events[0].reason == "test"


def test_deterministic_projection(engine):
    """State projection is deterministic from events."""
    # Record multiple events
    engine.record_transition("task1", None, "QUEUED", "task_added")
    engine.record_transition("task1", "QUEUED", "RUNNING", "started")
    engine.record_transition(
        "task1", "RUNNING", "COMPLETED", "finished",
        metadata={"budget_delta": {"files_touched": 5, "lines_processed": 100}}
    )

    # Compute projection
    state = engine.compute_projection()

    assert state.project_id == "test_project"
    assert "task1" in state.tasks
    assert state.tasks["task1"].state == TaskState.COMPLETED
    assert state.budget_usage.files_touched == 5
    assert state.budget_usage.lines_processed == 100


def test_checkpoint_contract_creation_and_loading(engine, sample_budget, sample_path_policy):
    """Checkpoint contracts can be created, persisted, and loaded."""
    contract = engine.create_checkpoint_contract(
        starting_sha="abc123def456",
        budget_constraints=sample_budget,
        path_policy=sample_path_policy,
        metadata={"key": "value"},
    )

    assert contract.contract_id.startswith("cp_")
    assert contract.starting_sha == "abc123def456"
    assert len(contract.contract_hash) == 64

    # Load the contract
    loaded = engine.load_checkpoint_contract(contract.contract_id)
    assert loaded is not None
    assert loaded.contract_id == contract.contract_id
    assert loaded.starting_sha == contract.starting_sha
    assert loaded.contract_hash == contract.contract_hash

    # Verify hash validation
    loaded.validate_hash()


def test_checkpoint_contract_invalid_hash(engine, sample_budget, sample_path_policy):
    """Invalid checkpoint contracts are rejected."""
    # Create contract
    contract = engine.create_checkpoint_contract(
        starting_sha="abc123def456",
        budget_constraints=sample_budget,
        path_policy=sample_path_policy,
    )

    # Manually corrupt the file
    contract_path = engine.contracts_path / f"{contract.contract_id}.json"
    data = json.loads(contract_path.read_text())
    data["starting_sha"] = "corrupted"
    contract_path.write_text(json.dumps(data))

    # Loading should fail validation
    loaded = engine.load_checkpoint_contract(contract.contract_id)
    assert loaded is None


def test_add_task(engine):
    """Tasks can be added to the graph."""
    engine.add_task(
        task_id="task1",
        dependencies=["task0"],
        checkpoint_contract="cp_abc123",
        metadata={"key": "value"},
    )

    events = engine.get_events()
    assert len(events) == 1
    assert events[0].task_id == "task1"
    assert events[0].to_state == "QUEUED"
    assert events[0].reason == "task_added"

    # Compute projection
    state = engine.compute_projection()
    assert "task1" in state.tasks
    assert state.tasks["task1"].state == TaskState.QUEUED
    assert state.tasks["task1"].dependencies == ["task0"]
    assert state.tasks["task1"].checkpoint_contract == "cp_abc123"


def test_add_duplicate_task(engine):
    """Duplicate tasks are rejected."""
    engine.add_task(task_id="task1")

    with pytest.raises(ValueError, match="already exists"):
        engine.add_task(task_id="task1")


def test_transition_task(engine):
    """Task transitions through state machine."""
    # Add task
    engine.add_task(task_id="task1")

    # Transition to RUNNING
    engine.transition_task(
        task_id="task1",
        to_state=TaskState.RUNNING,
        reason="started",
    )

    state = engine.compute_projection()
    assert state.tasks["task1"].state == TaskState.RUNNING

    # Transition to COMPLETED
    engine.transition_task(
        task_id="task1",
        to_state=TaskState.COMPLETED,
        reason="finished",
    )

    state = engine.compute_projection()
    assert state.tasks["task1"].state == TaskState.COMPLETED


def test_transition_invalid_state(engine):
    """Invalid transitions are rejected."""
    engine.add_task(task_id="task1")
    engine.transition_task(task_id="task1", to_state=TaskState.RUNNING)

    # Try invalid transition
    with pytest.raises(ValueError, match="Invalid transition"):
        engine.transition_task(task_id="task1", to_state=TaskState.QUEUED)


def test_transition_missing_task(engine):
    """Transitioning missing task raises error."""
    with pytest.raises(ValueError, match="not found"):
        engine.transition_task(task_id="nonexistent", to_state=TaskState.RUNNING)


def test_budget_tracking(engine):
    """Budget is tracked through transitions."""
    engine.add_task(task_id="task1")

    engine.transition_task(
        task_id="task1",
        to_state=TaskState.RUNNING,
        budget_delta={
            "files_touched": 10,
            "lines_processed": 200,
            "network_calls": 1,
            "cost_usd": 0.05,
            "model_calls": 3,
            "tokens_used": 500,
        },
    )

    state = engine.compute_projection()
    assert state.budget_usage.files_touched == 10
    assert state.budget_usage.lines_processed == 200
    assert state.budget_usage.network_calls == 1
    assert state.budget_usage.cost_usd == 0.05
    assert state.budget_usage.model_calls == 3
    assert state.budget_usage.tokens_used == 500


def test_pause_resume_task(engine):
    """Tasks can be paused and resumed."""
    engine.add_task(task_id="task1")
    engine.transition_task(task_id="task1", to_state=TaskState.RUNNING)

    # Pause task
    engine.pause_task(task_id="task1")
    state = engine.compute_projection()
    assert state.tasks["task1"].state == TaskState.PAUSED
    assert "task1" in state.paused_tasks

    # Resume task
    engine.resume_task(task_id="task1")
    state = engine.compute_projection()
    assert state.tasks["task1"].state == TaskState.RUNNING
    assert "task1" not in state.paused_tasks


def test_pause_resume_non_paused_task(engine):
    """Resuming non-paused task raises error."""
    engine.add_task(task_id="task1")
    engine.transition_task(task_id="task1", to_state=TaskState.RUNNING)

    with pytest.raises(ValueError, match="is not paused"):
        engine.resume_task(task_id="task1")


def test_pause_resume_all(engine):
    """All tasks can be paused and resumed."""
    engine.add_task(task_id="task1")
    engine.add_task(task_id="task2")
    engine.add_task(task_id="task3")

    # Start all tasks
    for task_id in ["task1", "task2", "task3"]:
        engine.transition_task(task_id, to_state=TaskState.RUNNING)

    # Pause task2 only
    engine.pause_task("task2")

    # Pause all running
    paused = engine.pause_all()
    assert set(paused) == {"task1", "task3"}

    state = engine.compute_projection()
    assert state.tasks["task1"].state == TaskState.PAUSED
    assert state.tasks["task2"].state == TaskState.PAUSED
    assert state.tasks["task3"].state == TaskState.PAUSED

    # Resume all
    resumed = engine.resume_all()
    assert set(resumed) == {"task1", "task2", "task3"}

    state = engine.compute_projection()
    assert state.tasks["task1"].state == TaskState.RUNNING
    assert state.tasks["task2"].state == TaskState.RUNNING
    assert state.tasks["task3"].state == TaskState.RUNNING


def test_crash_recovery(engine):
    """Engine can recover from crash using last valid state."""
    # Add some tasks and transitions
    engine.add_task(task_id="task1")
    engine.add_task(task_id="task2")
    engine.transition_task(task_id="task1", to_state=TaskState.RUNNING)
    engine.transition_task(task_id="task2", to_state=TaskState.RUNNING)

    # Save state
    state_before = engine.compute_projection()
    engine.save_state(state_before)

    # Verify previous state exists
    assert engine.previous_state_path.exists()

    # Simulate crash by corrupting current state
    engine.state_path.write_text("corrupted")

    # Recover
    state_after = engine.recover_from_crash()

    # State should be recovered
    assert state_after.project_id == state_before.project_id
    assert "task1" in state_after.tasks
    assert "task2" in state_after.tasks


def test_crash_recovery_no_previous_state(engine):
    """Recovery works even without previous state."""
    # No previous state exists
    state = engine.recover_from_crash()

    # Should compute from events (empty)
    assert state.project_id == "test_project"
    assert len(state.tasks) == 0


def test_drift_detection(engine):
    """Drift between expected and actual state is detected."""
    # Add task and transition
    engine.add_task(task_id="task1")
    engine.transition_task(task_id="task1", to_state=TaskState.RUNNING)

    # Compute expected state
    expected = engine.compute_projection()

    # Modify actual state
    engine.transition_task(task_id="task1", to_state=TaskState.PAUSED)
    engine.transition_task(
        task_id="task1",
        to_state=TaskState.RUNNING,
        budget_delta={"files_touched": 10},
    )

    # Detect drift
    drift = engine.detect_drift(expected)

    assert drift["has_drift"] is True
    assert len(drift["drifted_tasks"]) > 0
    assert drift["budget_drift"]["files_touched"] == 10


def test_drift_detection_no_drift(engine):
    """No drift is detected when states match."""
    # Add task
    engine.add_task(task_id="task1")

    # Compute expected state
    expected = engine.compute_projection()

    # Compute actual state (same)
    actual = engine.compute_projection()

    # Detect drift
    drift = engine.detect_drift(expected)

    assert drift["has_drift"] is False
    assert len(drift["drifted_tasks"]) == 0


def test_last_valid_state(engine):
    """Last valid state is preserved and retrievable."""
    # Create some state
    engine.add_task(task_id="task1")
    engine.transition_task(task_id="task1", to_state=TaskState.RUNNING)

    # Save state
    state = engine.compute_projection()
    engine.save_state(state)

    # Get last valid state
    last_valid = engine.get_last_valid_state()

    assert last_valid is not None
    assert last_valid.project_id == "test_project"
    assert "task1" in last_valid.tasks


def test_save_load_state(engine):
    """State can be saved and loaded."""
    # Create some state
    engine.add_task(task_id="task1")
    engine.add_task(task_id="task2")
    engine.transition_task(task_id="task1", to_state=TaskState.RUNNING)

    # Save state
    saved_state = engine.compute_projection()
    engine.save_state(saved_state)

    # Load state
    loaded_state = engine.load_state()

    assert loaded_state.project_id == saved_state.project_id
    assert loaded_state.current_head == saved_state.current_head
    assert set(loaded_state.tasks.keys()) == set(saved_state.tasks.keys())
    assert loaded_state.tasks["task1"].state == saved_state.tasks["task1"].state


def test_load_corrupt_state(engine):
    """Corrupt state file falls back to projection."""
    # Create some state
    engine.add_task(task_id="task1")
    engine.transition_task(task_id="task1", to_state=TaskState.RUNNING)

    # Save state
    engine.save_state()

    # Corrupt state file
    engine.state_path.write_text("corrupted")

    # Load should fall back to projection
    loaded_state = engine.load_state()

    assert loaded_state.project_id == "test_project"
    assert "task1" in loaded_state.tasks


def test_project_registry(engine):
    """Projects can be registered and retrieved."""
    registration = engine.register_project(
        repository_path="/path/to/repo",
        metadata={"key": "value"},
    )

    assert registration.project_id == "test_project"
    assert registration.repository_path == "/path/to/repo"
    assert registration.active is True

    # Retrieve registration
    retrieved = engine.get_registration()
    assert retrieved is not None
    assert retrieved.project_id == "test_project"
    assert retrieved.repository_path == "/path/to/repo"


def test_duplicate_registration(engine):
    """Duplicate project registration is rejected."""
    engine.register_project(repository_path="/path/to/repo")

    with pytest.raises(ValueError, match="already registered"):
        engine.register_project(repository_path="/path/to/repo")


def test_unregister_project(engine):
    """Projects can be unregistered."""
    engine.register_project(repository_path="/path/to/repo")
    assert engine.get_registration() is not None

    engine.unregister_project()
    assert engine.get_registration() is None


def test_checkpoint_tracking(engine, sample_budget, sample_path_policy):
    """Checkpoint status is tracked in state."""
    # Create checkpoint contract
    contract = engine.create_checkpoint_contract(
        starting_sha="abc123",
        budget_constraints=sample_budget,
        path_policy=sample_path_policy,
    )

    # Record transition with checkpoint
    engine.record_transition(
        task_id="task1",
        from_state=None,
        to_state="QUEUED",
        reason="test",
        metadata={"checkpoint_id": contract.contract_id},
    )

    # Compute projection
    state = engine.compute_projection()

    assert state.checkpoint_active is True
    assert state.checkpoint_id == contract.contract_id


def test_current_head_tracking(engine):
    """Current HEAD is tracked in state."""
    # Record transition with current_head
    engine.record_transition(
        task_id="task1",
        from_state=None,
        to_state="QUEUED",
        reason="test",
        metadata={"current_head": "abc123def456"},
    )

    # Compute projection
    state = engine.compute_projection()

    assert state.current_head == "abc123def456"


def test_atomic_writes(engine):
    """State writes are atomic with backup."""
    # Create initial state
    engine.add_task(task_id="task1")
    engine.save_state()

    # Modify state
    engine.transition_task(task_id="task1", to_state=TaskState.RUNNING)
    engine.save_state()

    # Verify both files exist
    assert engine.state_path.exists()
    assert engine.previous_state_path.exists()

    # Verify they're different
    current_data = json.loads(engine.state_path.read_text())
    previous_data = json.loads(engine.previous_state_path.read_text())

    # Current state should have RUNNING task
    current_task_state = current_data["tasks"]["task1"]["state"]
    previous_task_state = previous_data["tasks"]["task1"]["state"]

    assert current_task_state == "RUNNING"
    assert previous_task_state == "QUEUED"


def test_concurrent_writes(engine):
    """Concurrent writes are serialized by lock."""
    import threading

    results = []

    def add_task(task_id):
        try:
            engine.add_task(task_id)
            results.append(task_id)
        except Exception as e:
            results.append(f"error: {e}")

    # Create multiple threads adding tasks
    threads = [
        threading.Thread(target=add_task, args=(f"task{i}",))
        for i in range(5)
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    # All tasks should be added without errors
    assert len(results) == 5
    assert all("error" not in r for r in results)

    # Verify all tasks in state
    state = engine.compute_projection()
    assert len(state.tasks) == 5


def test_empty_events(engine):
    """Engine handles empty events correctly."""
    events = engine.get_events()
    assert len(events) == 0

    state = engine.compute_projection()
    assert len(state.tasks) == 0
    assert state.project_id == "test_project"


def test_schema_version():
    """Schema version is defined."""
    assert CP1_SCHEMA_VERSION == "1.0"


def test_task_dependencies(engine):
    """Task dependencies are tracked."""
    engine.add_task(task_id="task0")
    engine.add_task(task_id="task1", dependencies=["task0"])
    engine.add_task(task_id="task2", dependencies=["task0", "task1"])

    state = engine.compute_projection()

    assert state.tasks["task0"].dependencies == []
    assert state.tasks["task1"].dependencies == ["task0"]
    assert state.tasks["task2"].dependencies == ["task0", "task1"]


def test_checkpoint_metadata(engine, sample_budget, sample_path_policy):
    """Checkpoint metadata is preserved."""
    metadata = {
        "author": "test",
        "description": "test checkpoint",
        "tags": ["test", "example"],
    }

    contract = engine.create_checkpoint_contract(
        starting_sha="abc123",
        budget_constraints=sample_budget,
        path_policy=sample_path_policy,
        metadata=metadata,
    )

    assert contract.metadata == metadata

    # Load and verify
    loaded = engine.load_checkpoint_contract(contract.contract_id)
    assert loaded is not None
    assert loaded.metadata == metadata


def test_budget_accumulation(engine):
    """Budget accumulates across multiple transitions."""
    engine.add_task(task_id="task1")

    # First transition
    engine.transition_task(
        task_id="task1",
        to_state=TaskState.RUNNING,
        budget_delta={"files_touched": 5, "cost_usd": 0.01},
    )

    # Second transition
    engine.transition_task(
        task_id="task1",
        to_state=TaskState.COMPLETED,
        budget_delta={"files_touched": 3, "cost_usd": 0.02},
    )

    state = engine.compute_projection()
    assert state.budget_usage.files_touched == 8
    assert state.budget_usage.cost_usd == 0.03