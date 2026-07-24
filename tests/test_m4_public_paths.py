from __future__ import annotations

import json
import os
import sqlite3
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from joao_orchestrator.runtime.queue import QueueItem, QueueItemStatus, QueueStore, SchedulerEngine
from joao_orchestrator.storage.persistence_errors import SQLiteInitializationError, StaleLeaseError


def _item(queue: str, item: str, project: str) -> QueueItem:
    return QueueItem(
        queue_id=queue, item_id=item, project_id=project,
        title=item, request_file=__file__, source_revision="a" * 40,
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
    )


def test_queue_store_claims_item_and_project_atomically(tmp_path: Path):
    store = QueueStore(tmp_path / "state")
    store.add_item(_item("q", "one", "project"))
    store.add_item(_item("q", "two", "project"))

    first = store.claim_item(
        queue_id="q", item_id="one", project_id="project",
        run_id="run-one", owner_pid=os.getpid(), ttl_seconds=30,
    )
    assert first is not None
    second = store.claim_item(
        queue_id="q", item_id="two", project_id="project",
        run_id="run-two", owner_pid=os.getpid(), ttl_seconds=30,
    )
    assert second is None
    assert store.load_item("q", "one").status == QueueItemStatus.RUNNING.value
    assert store.load_item("q", "two").status == QueueItemStatus.QUEUED.value
    claims = store.lease_authority.get_active_claims("q")
    projects = store.lease_authority.get_active_project_leases()
    assert [(c.item_id, c.run_id) for c in claims] == [("one", "run-one")]
    assert [(p.project_id, p.owner_run_id) for p in projects] == [("project", "run-one")]


def test_expired_claim_recovery_and_successor_generation(tmp_path: Path):
    store = QueueStore(tmp_path / "state")
    store.add_item(_item("q", "one", "project"))
    first = store.claim_item(
        queue_id="q", item_id="one", project_id="project",
        run_id="run-one", owner_pid=os.getpid(), ttl_seconds=0.05,
    )
    assert first is not None
    time.sleep(0.08)
    recovered = store.recover_stale_leases()
    assert recovered and store.load_item("q", "one").status == QueueItemStatus.QUEUED.value
    second = store.claim_item(
        queue_id="q", item_id="one", project_id="project",
        run_id="run-two", owner_pid=os.getpid(), ttl_seconds=30,
    )
    assert second is not None
    assert second["generation"] > first["generation"]
    assert second["project_generation"] > first["project_generation"]
    with pytest.raises(StaleLeaseError):
        store.update_claimed_item(first, last_error="stale write")


def test_json_snapshot_is_never_runtime_authority_after_migration(tmp_path: Path):
    root = tmp_path / "state"
    qdir = root / "queues" / "q"
    qdir.mkdir(parents=True)
    snapshot = {
        "schema_version": 1, "queue_id": "q",
        "items": [_item("q", "one", "project").to_dict()],
    }
    (qdir / "queue.json").write_text(json.dumps(snapshot), encoding="utf-8")
    store = QueueStore(root)
    assert [i.item_id for i in store.load_queue("q")] == ["one"]

    # Mutating the derived JSON display after migration cannot alter runtime.
    snapshot["items"] = [_item("q", "attacker", "project").to_dict()]
    (qdir / "queue.json").write_text(json.dumps(snapshot), encoding="utf-8")
    reopened = QueueStore(root)
    assert [i.item_id for i in reopened.load_queue("q")] == ["one"]


def test_corrupt_sqlite_blocks_queue_store_initialization(tmp_path: Path):
    root = tmp_path / "state"
    root.mkdir()
    (root / "lease_authority.db").write_bytes(b"not a sqlite database")
    with pytest.raises(SQLiteInitializationError):
        QueueStore(root)


def test_complete_claim_releases_both_authorities_atomically(tmp_path: Path):
    store = QueueStore(tmp_path / "state")
    store.add_item(_item("q", "one", "project"))
    claim = store.claim_item(
        queue_id="q", item_id="one", project_id="project",
        run_id="run", owner_pid=os.getpid(), ttl_seconds=30,
    )
    assert claim is not None
    completed = store.complete_claim(
        claim, QueueItemStatus.AWAITING_APPROVAL.value,
        finished_at="2026-01-01T00:00:10Z", last_error=None,
    )
    assert completed.status == QueueItemStatus.AWAITING_APPROVAL.value
    assert store.lease_authority.get_active_claims("q") == []
    assert store.lease_authority.get_active_project_leases() == []

def test_active_claim_blocks_unscoped_queue_write(tmp_path: Path):
    store = QueueStore(tmp_path / "state")
    store.add_item(_item("q", "one", "project"))
    claim = store.claim_item(
        queue_id="q", item_id="one", project_id="project",
        run_id="run-one", owner_pid=os.getpid(), ttl_seconds=30,
    )
    assert claim is not None
    with pytest.raises(StaleLeaseError, match="ownership-bound update required"):
        store.update_item("q", "one", status=QueueItemStatus.FAILED.value)
    assert store.load_item("q", "one").status == QueueItemStatus.RUNNING.value


def test_reconcile_active_claim_is_noop(tmp_path: Path):
    root = tmp_path / "state"
    store = QueueStore(root)
    store.add_item(_item("q", "one", "project"))
    claim = store.claim_item(
        queue_id="q", item_id="one", project_id="project",
        run_id="run-one", owner_pid=os.getpid(), ttl_seconds=30,
    )
    assert claim is not None
    store.update_claimed_item(claim, task_id="task-one")
    engine = SchedulerEngine(root)

    class Tasks:
        def load(self, project_id, task_id):
            return SimpleNamespace(state="AWAITING_APPROVAL")

    result = engine._reconcile_one("q", engine.store.load_item("q", "one"), Tasks())
    assert result["action"] == "NOOP"
    assert result["reason"] == "ACTIVE_LEASE"
    assert engine.store.load_item("q", "one").status == QueueItemStatus.RUNNING.value
    assert len(engine.store.lease_authority.get_active_claims("q")) == 1
    assert len(engine.store.lease_authority.get_active_project_leases()) == 1


def test_reconcile_expired_claim_is_atomic_and_releases_pair(tmp_path: Path):
    root = tmp_path / "state"
    store = QueueStore(root)
    store.add_item(_item("q", "one", "project"))
    claim = store.claim_item(
        queue_id="q", item_id="one", project_id="project",
        run_id="run-one", owner_pid=os.getpid(), ttl_seconds=0.05,
    )
    assert claim is not None
    store.update_claimed_item(claim, task_id="task-one")
    time.sleep(0.08)
    engine = SchedulerEngine(root)

    class Tasks:
        def load(self, project_id, task_id):
            return SimpleNamespace(state="AWAITING_APPROVAL")

    result = engine._reconcile_one("q", engine.store.load_item("q", "one"), Tasks())
    assert result["action"] == QueueItemStatus.AWAITING_APPROVAL.value
    assert engine.store.load_item("q", "one").status == QueueItemStatus.AWAITING_APPROVAL.value
    assert engine.store.lease_authority.get_active_claims("q") == []
    assert engine.store.lease_authority.get_active_project_leases() == []
