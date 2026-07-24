"""Focused pytest tests for modular CP1 control plane."""

import json
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from joao_orchestrator.control_plane import (
    Budget,
    CheckpointContract,
    CheckpointStore,
    EventStore,
    EventType,
    RunEvent,
    ProjectRecord,
    ProjectRegistry,
    State,
    StateMachine,
    Status,
    TaskGraph,
    TaskNode,
    TaskStatus,
    DriftDetector,
    DriftReport,
    ControlPlane,
    ControlPlaneConfig,
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
def sample_budget():
    """Sample budget constraints."""
    return Budget(
        max_files=100,
        max_lines=5000,
        max_network_calls=5,
        max_cost_usd=0.50,
        max_model_calls=50,
        max_tokens=50000,
    )


@pytest.fixture
def control_plane_config(temp_state_root):
    """Sample control plane config."""
    return ControlPlaneConfig(
        state_root=temp_state_root / "control_plane",
        project_id="test_project",
        budget=Budget(
            max_files=1000,
            max_lines=50000,
            max_network_calls=10,
            max_cost_usd=1.0,
            max_model_calls=100,
            max_tokens=100000,
        ),
    )


@pytest.fixture
def control_plane(control_plane_config):
    """Create a control plane instance."""
    return ControlPlane(control_plane_config)


# ---------------------------------------------------------------------------
# Budget tests
# ---------------------------------------------------------------------------

def test_budget_creation(sample_budget):
    """Budget creates with correct constraints."""
    assert sample_budget.max_files == 100
    assert sample_budget.max_lines == 5000
    assert sample_budget.max_network_calls == 5
    assert sample_budget.max_cost_usd == 0.50
    assert sample_budget.max_model_calls == 50
    assert sample_budget.max_tokens == 50000


def test_budget_defaults():
    """Budget has sensible defaults."""
    budget = Budget()
    assert budget.max_files == 1000
    assert budget.max_lines == 50000
    assert budget.max_network_calls == 10
    assert budget.max_cost_usd == 1.0
    assert budget.max_model_calls == 100
    assert budget.max_tokens == 100000


def test_budget_serialization(sample_budget):
    """Budget serializes correctly."""
    d = sample_budget.to_dict()
    assert d["max_files"] == 100
    assert d["max_lines"] == 5000

    recovered = Budget.from_dict(d)
    assert recovered.max_files == 100
    assert recovered.max_lines == 5000


# ---------------------------------------------------------------------------
# CheckpointContract tests
# ---------------------------------------------------------------------------

def test_checkpoint_contract_creation(sample_budget):
    """CheckpointContract creates with immutable hash."""
    contract = CheckpointContract(
        contract_id="cp_test",
        starting_sha="abc123def456",
        budget=sample_budget,
        allowed_paths=["/tmp/test"],
    )

    assert contract.contract_id == "cp_test"
    assert contract.starting_sha == "abc123def456"
    assert len(contract.contract_hash) == 64  # SHA256


def test_checkpoint_contract_hash_computation(sample_budget):
    """CheckpointContract computes consistent hash."""
    contract = CheckpointContract(
        contract_id="cp_test",
        starting_sha="abc123def456",
        budget=sample_budget,
        allowed_paths=["/tmp/test"],
    )

    # Compute hash
    hash1 = contract.compute_contract_hash()
    hash2 = contract.compute_contract_hash()

    assert hash1 == hash2
    assert len(hash1) == 64


def test_checkpoint_contract_validation(sample_budget):
    """CheckpointContract validates hash integrity."""
    contract = CheckpointContract(
        contract_id="cp_test",
        starting_sha="abc123def456",
        budget=sample_budget,
        allowed_paths=["/tmp/test"],
    )

    # Compute and validate
    contract.contract_hash = contract.compute_contract_hash()
    contract.validate_hash()  # Should not raise

    # Tamper with data
    contract.starting_sha = "corrupted"

    # Should raise validation error
    with pytest.raises(ValueError, match="hash mismatch"):
        contract.validate_hash()


# ---------------------------------------------------------------------------
# EventStore tests
# ---------------------------------------------------------------------------

def test_event_store_append(temp_state_root):
    """EventStore appends events atomically."""
    store = EventStore(temp_state_root / "events.jsonl")

    event = RunEvent(
        task_id="task1",
        event_type=EventType.STATE_CHANGE,
        from_state="QUEUED",
        to_state="RUNNING",
        timestamp="2024-01-01T12:00:00Z",
        metadata={"key": "value"},
    )

    store.append(event)

    events = store.get_events()
    assert len(events) == 1
    assert events[0].task_id == "task1"
    assert events[0].to_state == "RUNNING"


def test_event_store_filtering(temp_state_root):
    """EventStore can filter events by task_id."""
    store = EventStore(temp_state_root / "events.jsonl")

    store.append(RunEvent(
        task_id="task1",
        event_type=EventType.STATE_CHANGE,
        from_state="QUEUED",
        to_state="RUNNING",
        timestamp="2024-01-01T12:00:00Z",
    ))

    store.append(RunEvent(
        task_id="task2",
        event_type=EventType.STATE_CHANGE,
        from_state="QUEUED",
        to_state="RUNNING",
        timestamp="2024-01-01T12:01:00Z",
    ))

    store.append(RunEvent(
        task_id="task1",
        event_type=EventType.STATE_CHANGE,
        from_state="RUNNING",
        to_state="COMPLETED",
        timestamp="2024-01-01T12:02:00Z",
    ))

    # Filter by task_id
    task1_events = store.get_events(task_id="task1")
    assert len(task1_events) == 2
    assert all(e.task_id == "task1" for e in task1_events)


def test_event_store_empty(temp_state_root):
    """EventStore handles empty state correctly."""
    store = EventStore(temp_state_root / "events.jsonl")

    events = store.get_events()
    assert len(events) == 0


# ---------------------------------------------------------------------------
# CheckpointStore tests
# ---------------------------------------------------------------------------

def test_checkpoint_store_create_and_load(temp_state_root, sample_budget):
    """CheckpointStore creates and loads contracts."""
    store = CheckpointStore(temp_state_root / "checkpoints")

    contract = CheckpointContract(
        contract_id="cp_test",
        starting_sha="abc123def456",
        budget=sample_budget,
        allowed_paths=["/tmp/test"],
    )
    contract.contract_hash = contract.compute_contract_hash()

    # Save contract
    store.save(contract)

    # Load contract
    loaded = store.load("cp_test")
    assert loaded is not None
    assert loaded.contract_id == "cp_test"
    assert loaded.starting_sha == "abc123def456"


def test_checkpoint_store_load_nonexistent(temp_state_root):
    """CheckpointStore returns None for nonexistent contracts."""
    store = CheckpointStore(temp_state_root / "checkpoints")

    loaded = store.load("nonexistent")
    assert loaded is None


# ---------------------------------------------------------------------------
# ProjectRegistry tests
# ---------------------------------------------------------------------------

def test_project_registry_register_and_load(temp_state_root):
    """ProjectRegistry can register and load projects."""
    registry = ProjectRegistry(temp_state_root / "registry.json")

    record = ProjectRecord(
        project_id="test_project",
        repository_path="/path/to/repo",
        registered_at="2024-01-01T12:00:00Z",
    )

    # Register project
    registry.register(record)

    # Load project
    loaded = registry.load("test_project")
    assert loaded is not None
    assert loaded.project_id == "test_project"
    assert loaded.repository_path == "/path/to/repo"


def test_project_registry_duplicate(temp_state_root):
    """ProjectRegistry rejects duplicate registrations."""
    registry = ProjectRegistry(temp_state_root / "registry.json")

    record = ProjectRecord(
        project_id="test_project",
        repository_path="/path/to/repo",
        registered_at="2024-01-01T12:00:00Z",
    )

    registry.register(record)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(record)


def test_project_registry_unregister(temp_state_root):
    """ProjectRegistry can unregister projects."""
    registry = ProjectRegistry(temp_state_root / "registry.json")

    record = ProjectRecord(
        project_id="test_project",
        repository_path="/path/to/repo",
        registered_at="2024-01-01T12:00:00Z",
    )

    registry.register(record)
    assert registry.load("test_project") is not None

    registry.unregister("test_project")
    assert registry.load("test_project") is None


# ---------------------------------------------------------------------------
# StateMachine tests
# ---------------------------------------------------------------------------

def test_state_machine_valid_transitions():
    """StateMachine allows valid transitions."""
    machine = StateMachine()

    # Valid transitions
    machine.transition(State.QUEUED, State.PENDING)
    machine.transition(State.PENDING, State.RUNNING)
    machine.transition(State.RUNNING, State.PAUSED)
    machine.transition(State.PAUSED, State.RUNNING)
    machine.transition(State.RUNNING, State.COMPLETED)


def test_state_machine_invalid_transitions():
    """StateMachine rejects invalid transitions."""
    machine = StateMachine()

    with pytest.raises(ValueError, match="Invalid transition"):
        machine.transition(State.COMPLETED, State.RUNNING)

    with pytest.raises(ValueError, match="Invalid transition"):
        machine.transition(State.QUEUED, State.COMPLETED)


def test_state_machine_status():
    """StateMachine provides status correctly."""
    machine = StateMachine()

    assert machine.get_status(State.QUEUED) == Status.PENDING
    assert machine.get_status(State.RUNNING) == Status.ACTIVE
    assert machine.get_status(State.PAUSED) == Status.PAUSED
    assert machine.get_status(State.COMPLETED) == Status.TERMINATED
    assert machine.get_status(State.FAILED) == Status.TERMINATED


# ---------------------------------------------------------------------------
# TaskGraph tests
# ---------------------------------------------------------------------------

def test_task_graph_add_and_get():
    """TaskGraph can add and retrieve tasks."""
    graph = TaskGraph()

    task = TaskNode(
        task_id="task1",
        status=TaskStatus.QUEUED,
        dependencies=[],
    )

    graph.add_task(task)

    retrieved = graph.get_task("task1")
    assert retrieved is not None
    assert retrieved.task_id == "task1"
    assert retrieved.status == TaskStatus.QUEUED


def test_task_graph_dependencies():
    """TaskGraph tracks task dependencies."""
    graph = TaskGraph()

    task0 = TaskNode(task_id="task0", status=TaskStatus.COMPLETED)
    task1 = TaskNode(task_id="task1", status=TaskStatus.QUEUED, dependencies=["task0"])

    graph.add_task(task0)
    graph.add_task(task1)

    # Check dependencies
    assert "task0" in graph.get_dependencies("task1")
    assert graph.get_dependents("task0") == {"task1"}


def test_task_graph_ready_tasks():
    """TaskGraph identifies ready tasks (no pending dependencies)."""
    graph = TaskGraph()

    task0 = TaskNode(task_id="task0", status=TaskStatus.QUEUED)
    task1 = TaskNode(task_id="task1", status=TaskStatus.QUEUED, dependencies=["task0"])

    graph.add_task(task0)
    graph.add_task(task1)

    # Initially only task0 is ready
    assert graph.get_ready_tasks() == {"task0"}

    # Complete task0
    graph.update_task_status("task0", TaskStatus.COMPLETED)

    # Now task1 is also ready
    assert graph.get_ready_tasks() == {"task1"}


def test_task_graph_update_status():
    """TaskGraph can update task status."""
    graph = TaskGraph()

    task = TaskNode(task_id="task1", status=TaskStatus.QUEUED)
    graph.add_task(task)

    graph.update_task_status("task1", TaskStatus.RUNNING)

    updated = graph.get_task("task1")
    assert updated.status == TaskStatus.RUNNING


# ---------------------------------------------------------------------------
# DriftDetector tests
# ---------------------------------------------------------------------------

def test_drift_detection_no_drift():
    """DriftDetector detects no drift when states match."""
    detector = DriftDetector()

    graph1 = TaskGraph()
    graph1.add_task(TaskNode(task_id="task1", status=TaskStatus.RUNNING))

    graph2 = TaskGraph()
    graph2.add_task(TaskNode(task_id="task1", status=TaskStatus.RUNNING))

    report = detector.detect(graph1, graph2)

    assert report.has_drift is False
    assert len(report.drifted_tasks) == 0


def test_drift_detection_state_mismatch():
    """DriftDetector detects task state mismatches."""
    detector = DriftDetector()

    graph1 = TaskGraph()
    graph1.add_task(TaskNode(task_id="task1", status=TaskStatus.RUNNING))

    graph2 = TaskGraph()
    graph2.add_task(TaskNode(task_id="task1", status=TaskStatus.PAUSED))

    report = detector.detect(graph1, graph2)

    assert report.has_drift is True
    assert len(report.drifted_tasks) == 1
    assert "task1" in report.drifted_tasks


def test_drift_detection_missing_task():
    """DriftDetector detects missing tasks."""
    detector = DriftDetector()

    graph1 = TaskGraph()
    graph1.add_task(TaskNode(task_id="task1", status=TaskStatus.RUNNING))

    graph2 = TaskGraph()

    report = detector.detect(graph1, graph2)

    assert report.has_drift is True
    assert len(report.drifted_tasks) == 1
    assert "task1" in report.drifted_tasks


# ---------------------------------------------------------------------------
# ControlPlane integration tests
# ---------------------------------------------------------------------------

def test_control_plane_initialization(control_plane):
    """ControlPlane initializes correctly."""
    assert control_plane.config.project_id == "test_project"
    assert control_plane.task_graph is not None
    assert control_plane.event_store is not None
    assert control_plane.checkpoint_store is not None
    assert control_plane.registry is not None


def test_control_plane_add_task(control_plane):
    """ControlPlane can add tasks."""
    control_plane.add_task("task1", dependencies=[])

    task = control_plane.task_graph.get_task("task1")
    assert task is not None
    assert task.task_id == "task1"
    assert task.status == TaskStatus.QUEUED


def test_control_plane_transition_task(control_plane):
    """ControlPlane can transition tasks through state machine."""
    control_plane.add_task("task1", dependencies=[])

    control_plane.transition_task("task1", State.RUNNING)

    task = control_plane.task_graph.get_task("task1")
    assert task.status == TaskStatus.RUNNING

    # Verify event was recorded
    events = control_plane.event_store.get_events(task_id="task1")
    assert len(events) >= 2  # QUEUED -> RUNNING


def test_control_plane_invalid_transition(control_plane):
    """ControlPlane rejects invalid transitions."""
    control_plane.add_task("task1", dependencies=[])
    control_plane.transition_task("task1", State.RUNNING)

    with pytest.raises(ValueError, match="Invalid transition"):
        control_plane.transition_task("task1", State.QUEUED)


def test_control_plane_pause_resume(control_plane):
    """ControlPlane can pause and resume tasks."""
    control_plane.add_task("task1", dependencies=[])
    control_plane.transition_task("task1", State.RUNNING)

    # Pause task
    control_plane.pause_task("task1")
    task = control_plane.task_graph.get_task("task1")
    assert task.status == TaskStatus.PAUSED

    # Resume task
    control_plane.resume_task("task1")
    task = control_plane.task_graph.get_task("task1")
    assert task.status == TaskStatus.RUNNING


def test_control_plane_create_checkpoint(control_plane, sample_budget):
    """ControlPlane can create checkpoints."""
    checkpoint = control_plane.create_checkpoint(
        starting_sha="abc123def456",
        budget=sample_budget,
        allowed_paths=["/tmp/test"],
    )

    assert checkpoint.contract_id.startswith("cp_")
    assert checkpoint.starting_sha == "abc123def456"

    # Verify checkpoint can be loaded
    loaded = control_plane.checkpoint_store.load(checkpoint.contract_id)
    assert loaded is not None


def test_control_plane_drift_detection(control_plane):
    """ControlPlane can detect drift."""
    control_plane.add_task("task1", dependencies=[])
    control_plane.transition_task("task1", State.RUNNING)

    # Get baseline
    baseline_graph = control_plane.task_graph

    # Modify state
    control_plane.transition_task("task1", State.PAUSED)

    # Detect drift
    report = control_plane.detect_drift(baseline_graph)

    assert report.has_drift is True
    assert "task1" in report.drifted_tasks


def test_control_plane_project_registration(control_plane):
    """ControlPlane can register projects."""
    control_plane.register_project("/path/to/repo")

    record = control_plane.registry.load("test_project")
    assert record is not None
    assert record.repository_path == "/path/to/repo"


def test_control_plane_save_and_load_state(control_plane):
    """ControlPlane can save and load state."""
    control_plane.add_task("task1", dependencies=[])
    control_plane.transition_task("task1", State.RUNNING)

    # Save state
    control_plane.save_state()

    # Create new control plane instance
    new_plane = ControlPlane(control_plane.config)

    # Load state
    new_plane.load_state()

    # Verify task exists
    task = new_plane.task_graph.get_task("task1")
    assert task is not None
    assert task.status == TaskStatus.RUNNING


def test_control_plane_crash_recovery(control_plane):
    """ControlPlane can recover from crashes."""
    control_plane.add_task("task1", dependencies=[])
    control_plane.transition_task("task1", State.RUNNING)
    control_plane.save_state()

    # Simulate crash by creating new instance
    new_plane = ControlPlane(control_plane.config)
    new_plane.recover()

    # Verify state recovered
    task = new_plane.task_graph.get_task("task1")
    assert task is not None


def test_control_plane_budget_tracking(control_plane):
    """ControlPlane tracks resource usage."""
    control_plane.add_task("task1", dependencies=[])

    control_plane.transition_task(
        "task1",
        State.RUNNING,
        budget_delta={
            "files_touched": 10,
            "lines_processed": 100,
            "network_calls": 1,
            "cost_usd": 0.05,
        },
    )

    usage = control_plane.get_budget_usage()
    assert usage.files_touched == 10
    assert usage.lines_processed == 100
    assert usage.network_calls == 1
    assert usage.cost_usd == 0.05


def test_control_pause_all(control_plane):
    """ControlPlane can pause all running tasks."""
    control_plane.add_task("task1", dependencies=[])
    control_plane.add_task("task2", dependencies=[])
    control_plane.add_task("task3", dependencies=[])

    # Start all tasks
    for task_id in ["task1", "task2", "task3"]:
        control_plane.transition_task(task_id, State.RUNNING)

    # Pause all
    paused = control_plane.pause_all()
    assert set(paused) == {"task1", "task2", "task3"}

    # Verify all paused
    for task_id in ["task1", "task2", "task3"]:
        task = control_plane.task_graph.get_task(task_id)
        assert task.status == TaskStatus.PAUSED


def test_control_resume_all(control_plane):
    """ControlPlane can resume all paused tasks."""
    control_plane.add_task("task1", dependencies=[])
    control_plane.add_task("task2", dependencies=[])

    # Start and pause tasks
    for task_id in ["task1", "task2"]:
        control_plane.transition_task(task_id, State.RUNNING)
        control_plane.pause_task(task_id)

    # Resume all
    resumed = control_plane.resume_all()
    assert set(resumed) == {"task1", "task2"}

    # Verify all running
    for task_id in ["task1", "task2"]:
        task = control_plane.task_graph.get_task(task_id)
        assert task.status == TaskStatus.RUNNING


def test_control_plane_get_last_valid_state(control_plane):
    """ControlPlane preserves last valid state."""
    control_plane.add_task("task1", dependencies=[])
    control_plane.transition_task("task1", State.RUNNING)
    control_plane.save_state()

    last_valid = control_plane.get_last_valid_state()
    assert last_valid is not None
    assert "task1" in last_valid.tasks


if __name__ == "__main__":
    pytest.main([__file__, "-v"])