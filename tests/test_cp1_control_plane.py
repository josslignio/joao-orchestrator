"""Focused tests for CP1 control plane modules."""

import json
import secrets
import tempfile
from pathlib import Path

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


@pytest.fixture
def temp_dir():
    """Temporary directory for test storage."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def budget():
    """Standard budget for testing."""
    return Budget(
        file_budget=1000,
        line_budget=10000,
        network_allowed=False,
        cost_limit_usd=1.0,
        model_budget={"gpt-4": 10},
        token_budget=100000,
    )


@pytest.fixture
def checkpoint_contract(budget):
    """Checkpoint contract for testing."""
    return CheckpointContract(
        contract_id="test_contract",
        starting_sha="abc123def456",
        allowed_paths=("src/", "tests/"),
        budget=budget,
        task_graph_hash="hash789",
    )


class TestCheckpointContract:
    """Test checkpoint contract immutability and hashing."""

    def test_contract_creation(self, checkpoint_contract):
        """Contract creates with correct fields."""
        assert checkpoint_contract.contract_id == "test_contract"
        assert checkpoint_contract.starting_sha == "abc123def456"
        assert checkpoint_contract.allowed_paths == ("src/", "tests/")
        assert len(checkpoint_contract.contract_hash) == 64

    def test_contract_hash_deterministic(self, budget):
        """Contract hash is deterministic."""
        contract1 = CheckpointContract(
            contract_id="test",
            starting_sha="abc123",
            allowed_paths=("src/",),
            budget=budget,
            task_graph_hash="hash",
        )
        contract2 = CheckpointContract(
            contract_id="test",
            starting_sha="abc123",
            allowed_paths=("src/",),
            budget=budget,
            task_graph_hash="hash",
        )
        assert contract1.contract_hash == contract2.contract_hash

    def test_contract_serialization(self, checkpoint_contract):
        """Contract serializes correctly."""
        data = checkpoint_contract.to_dict()
        assert "contract_hash" in data
        assert data["contract_id"] == "test_contract"

        restored = CheckpointContract.from_dict(data)
        assert restored.contract_hash == checkpoint_contract.contract_hash
        assert restored.starting_sha == checkpoint_contract.starting_sha


class TestCheckpointStore:
    """Test checkpoint storage with atomic writes."""

    def test_store_creation(self, temp_dir):
        """Store creates directory structure."""
        store = CheckpointStore(temp_dir, "test_cp")
        assert store.contract_dir.exists()
        assert store.contract_dir.name == "test_cp"

    def test_contract_persistence(self, temp_dir, checkpoint_contract):
        """Contract persists and loads correctly."""
        store = CheckpointStore(temp_dir, "test_cp")
        store.save_contract(checkpoint_contract)

        loaded = store.load_contract()
        assert loaded is not None
        assert loaded.contract_hash == checkpoint_contract.contract_hash
        assert loaded.starting_sha == checkpoint_contract.starting_sha

    def test_state_atomic_write(self, temp_dir):
        """State writes are atomic with backup."""
        store = CheckpointStore(temp_dir, "test_cp")

        state1 = {"version": 1, "data": "first"}
        store.save_state(state1)

        assert store.load_state() == state1
        assert store.load_previous_state() is None

        state2 = {"version": 2, "data": "second"}
        store.save_state(state2)

        assert store.load_state() == state2
        assert store.load_previous_state() == state1

    def test_last_valid_state_recovery(self, temp_dir):
        """Recovers last valid state when current is corrupt."""
        store = CheckpointStore(temp_dir, "test_cp")

        valid_state = {"version": 1, "data": "valid"}
        store.save_state(valid_state)

        # Write a second state to ensure backup exists
        valid_state2 = {"version": 2, "data": "valid2"}
        store.save_state(valid_state2)

        # Corrupt current state (simulating corruption after write)
        store.state_path.write_text("{invalid json")

        # Should recover from previous state (state1)
        recovered = store.load_last_valid_state()
        assert recovered == valid_state


class TestEventStore:
    """Test append-only event storage and projection."""

    def test_event_append(self, temp_dir):
        """Events append correctly."""
        store = EventStore(temp_dir, "test_cp")

        event = RunEvent(
            event_id="evt1",
            event_type=EventType.RUN_STARTED,
            checkpoint_id="cp1",
            timestamp="",
            data={"test": "data"},
        )
        store.append(event)

        events = store.load_all()
        assert len(events) == 1
        assert events[0].event_id == "evt1"

    def test_deterministic_projection(self, temp_dir):
        """Projection is deterministic from events."""
        store = EventStore(temp_dir, "test_cp")

        # Add events in sequence
        store.append(RunEvent(
            event_id="evt1",
            event_type=EventType.RUN_STARTED,
            checkpoint_id="cp1",
            timestamp="",
            data={},
        ))
        store.append(RunEvent(
            event_id="evt2",
            event_type=EventType.CHECKPOINT_CREATED,
            checkpoint_id="cp1",
            timestamp="",
            data={},
        ))
        store.append(RunEvent(
            event_id="evt3",
            event_type=EventType.RUN_COMPLETED,
            checkpoint_id="cp1",
            timestamp="",
            data={},
        ))

        state = store.project_state()
        assert state["run_status"] == "completed"
        assert state["events_count"] == 3
        assert state["checkpoints_created"] == 1


class TestProjectRegistry:
    """Test project registration and tracking."""

    def test_project_registration(self, temp_dir):
        """Projects register correctly."""
        registry = ProjectRegistry(temp_dir)

        record = registry.register(
            repository_path="/path/to/repo",
            starting_sha="abc123",
            contract_hash="hash456",
        )

        assert record.project_id is not None
        assert len(record.project_id) == 16  # token_hex(8)
        assert record.repository_path == "/path/to/repo"
        assert record.starting_sha == "abc123"

    def test_project_retrieval(self, temp_dir):
        """Projects retrieve by ID."""
        registry = ProjectRegistry(temp_dir)

        record = registry.register(
            repository_path="/path/to/repo",
            starting_sha="abc123",
            contract_hash="hash456",
        )

        loaded = registry.get(record.project_id)
        assert loaded is not None
        assert loaded.project_id == record.project_id

    def test_project_update(self, temp_dir):
        """Project records update correctly."""
        registry = ProjectRegistry(temp_dir)

        record = registry.register(
            repository_path="/path/to/repo",
            starting_sha="abc123",
            contract_hash="hash456",
        )

        updated = registry.update(
            record.project_id,
            status="running",
            last_checkpoint_id="cp123",
        )

        assert updated is not None
        assert updated.status == "running"
        assert updated.last_checkpoint_id == "cp123"


class TestStateMachine:
    """Test deterministic state machine transitions."""

    def test_valid_transitions(self):
        """Valid transitions succeed."""
        machine = StateMachine(State(
            checkpoint_id="cp1",
            status=Status.NOT_STARTED,
            current_task_id="",
            completed_tasks=(),
            failed_tasks=(),
            progress=0.0,
        ))

        # NOT_STARTED -> RUNNING
        state = machine.transition(Status.RUNNING)
        assert state.status == Status.RUNNING

        # RUNNING -> PAUSED
        state = machine.transition(Status.PAUSED)
        assert state.status == Status.PAUSED

        # PAUSED -> RUNNING
        state = machine.transition(Status.RUNNING)
        assert state.status == Status.RUNNING

    def test_invalid_transitions(self):
        """Invalid transitions raise error."""
        machine = StateMachine(State(
            checkpoint_id="cp1",
            status=Status.NOT_STARTED,
            current_task_id="",
            completed_tasks=(),
            failed_tasks=(),
            progress=0.0,
        ))

        with pytest.raises(ValueError, match="Invalid transition"):
            machine.transition(Status.COMPLETED)

    def test_task_completion(self):
        """Task completion updates state."""
        machine = StateMachine(State(
            checkpoint_id="cp1",
            status=Status.RUNNING,
            current_task_id="task1",
            completed_tasks=(),
            failed_tasks=(),
            progress=0.0,
        ))

        state = machine.complete_task("task1")
        assert "task1" in state.completed_tasks
        assert state.progress == 1.0

    def test_task_failure(self):
        """Task failure updates state."""
        machine = StateMachine(State(
            checkpoint_id="cp1",
            status=Status.RUNNING,
            current_task_id="task1",
            completed_tasks=(),
            failed_tasks=(),
            progress=0.0,
        ))

        state = machine.fail_task("task1")
        assert "task1" in state.failed_tasks


class TestTaskGraph:
    """Test task graph operations."""

    def test_task_graph_creation(self):
        """Task graph creates with tasks."""
        graph = TaskGraph()

        task1 = TaskNode(
            task_id="task1",
            task_type="test",
            dependencies=(),
            parameters={"key": "value"},
            estimated_cost_usd=0.1,
        )

        graph.add_task(task1)
        graph.add_root_task("task1")

        assert graph.get_task("task1") == task1
        assert graph.root_tasks == ("task1",)

    def test_dependencies(self):
        """Task dependencies resolve correctly."""
        graph = TaskGraph()

        task1 = TaskNode(
            task_id="task1",
            task_type="test",
            dependencies=(),
            parameters={},
        )
        task2 = TaskNode(
            task_id="task2",
            task_type="test",
            dependencies=("task1",),
            parameters={},
        )

        graph.add_task(task1)
        graph.add_task(task2)

        deps = graph.get_dependencies("task2")
        assert len(deps) == 1
        assert deps[0].task_id == "task1"

    def test_ready_tasks(self):
        """Ready tasks identified correctly."""
        graph = TaskGraph()

        task1 = TaskNode(
            task_id="task1",
            task_type="test",
            dependencies=(),
            parameters={},
        )
        task2 = TaskNode(
            task_id="task2",
            task_type="test",
            dependencies=("task1",),
            parameters={},
        )

        graph.add_task(task1)
        graph.add_task(task2)
        graph.add_root_task("task1")
        graph.add_root_task("task2")

        ready = graph.get_ready_tasks(set())
        assert len(ready) == 1
        assert ready[0].task_id == "task1"

        ready = graph.get_ready_tasks({"task1"})
        assert len(ready) == 1
        assert ready[0].task_id == "task2"


class TestDriftDetector:
    """Test drift detection."""

    def test_no_state_drift(self):
        """No drift when states match."""
        detector = DriftDetector(allowed_paths=("src/",))

        state = {"key": "value", "number": 42}
        report = detector.detect_state_drift(state, state)

        assert not report.has_drift
        assert len(report.drift_details) == 0

    def test_state_drift_detected(self):
        """Drift detected when states differ."""
        detector = DriftDetector(allowed_paths=("src/",))

        expected = {"key": "value", "number": 42}
        actual = {"key": "different", "number": 42}

        report = detector.detect_state_drift(expected, actual)

        assert report.has_drift
        assert len(report.drift_details) > 0
        assert "Value mismatch for key" in report.drift_details[0]

    def test_filesystem_drift(self, temp_dir):
        """Filesystem drift detected."""
        detector = DriftDetector(allowed_paths=("src/",))

        # Create test files
        src_dir = temp_dir / "src"
        src_dir.mkdir()
        (src_dir / "test.py").write_text("print('hello')")

        snapshot = {"src/test.py": "abc123"}

        report = detector.detect_filesystem_drift(str(temp_dir), snapshot)

        # Should have drift since hash won't match
        assert report.has_drift


class TestControlPlane:
    """Test main control plane orchestration."""

    def test_control_plane_creation(self, temp_dir):
        """Control plane creates with config."""
        config = ControlPlaneConfig(
            storage_root=temp_dir,
            allowed_paths=("src/", "tests/"),
            file_budget=1000,
        )

        cp = ControlPlane(config)
        assert cp.config.storage_root == temp_dir
        assert cp.config.allowed_paths == ("src/", "tests/")

    def test_project_registration(self, temp_dir):
        """Project registers through control plane."""
        config = ControlPlaneConfig(storage_root=temp_dir)
        cp = ControlPlane(config)

        record = cp.register_project(
            repository_path="/path/to/repo",
            starting_sha="abc123",
        )

        assert record.project_id is not None
        assert record.starting_sha == "abc123"

    def test_run_lifecycle(self, temp_dir):
        """Full run lifecycle works correctly."""
        config = ControlPlaneConfig(storage_root=temp_dir)
        cp = ControlPlane(config)

        # Register project
        record = cp.register_project(
            repository_path="/path/to/repo",
            starting_sha="abc123",
        )

        # Create task graph
        task1 = TaskNode(
            task_id="task1",
            task_type="test",
            dependencies=(),
            parameters={},
        )
        cp.create_task_graph([task1], ["task1"])

        # Load project
        cp.load_project(record.project_id)

        # Start run
        checkpoint_id = cp.start_run("/path/to/repo", "abc123")
        assert len(checkpoint_id) > 0

        # Pause and resume
        cp.pause_run()
        state = cp.get_projected_state()
        assert state["run_status"] == "paused"

        cp.resume_run()
        state = cp.get_projected_state()
        assert state["run_status"] == "running"

        # Complete run
        cp.complete_run()
        state = cp.get_projected_state()
        assert state["run_status"] == "completed"

    def test_crash_recovery(self, temp_dir):
        """Crash recovery restores last valid state."""
        config = ControlPlaneConfig(storage_root=temp_dir)
        cp = ControlPlane(config)

        # Register and start project
        record = cp.register_project(
            repository_path="/path/to/repo",
            starting_sha="abc123",
        )

        task1 = TaskNode(
            task_id="task1",
            task_type="test",
            dependencies=(),
            parameters={},
        )
        cp.create_task_graph([task1], ["task1"])
        cp.load_project(record.project_id)

        checkpoint_id = cp.start_run("/path/to/repo", "abc123")

        # Simulate crash recovery
        recovered = cp.crash_recovery()
        assert recovered is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])