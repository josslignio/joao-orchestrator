"""M4 SQLite-authoritative lease authority tests.

Tests verify exact ownership validation, token/generation enforcement,
and fail-closed behavior for all lease operations.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from joao_orchestrator.runtime.lease_authority import LeaseAuthority, QueueClaim, ProjectLease
from joao_orchestrator.storage.persistence_errors import (
    StaleLeaseError,
    SQLiteConstraintError,
    SQLiteInitializationError,
    SQLiteLockTimeoutError,
)


def test_lease_authority_initialization():
    """Verify lease authority initializes with proper schema."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"
        authority = LeaseAuthority(db_path)

        # Check database exists
        assert db_path.exists(), "Database file should be created"

        # Check schema version
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute("SELECT value FROM metadata WHERE key = 'lease_authority_schema_version'")
        row = cur.fetchone()
        conn.close()

        assert row is not None, "Schema version should be stored"
        assert row[0] == "2", f"Expected schema version 2, got {row[0]}"

        authority.close()


def test_claim_queue_item_atomic():
    """Verify atomic claim transaction with token and generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"
        authority = LeaseAuthority(db_path)

        claim = authority.claim_queue_item(
            queue_id="test_queue",
            item_id="test_item",
            run_id="test_run",
            owner_pid=12345,
            ttl_seconds=60.0,
        )

        # Verify claim structure
        assert isinstance(claim, QueueClaim), "Should return QueueClaim object"
        assert claim.queue_id == "test_queue", "Queue ID should match"
        assert claim.item_id == "test_item", "Item ID should match"
        assert claim.run_id == "test_run", "Run ID should match"
        assert claim.owner_pid == 12345, "Owner PID should match"
        assert claim.generation == 1, "First claim should have generation=1"
        assert len(claim.lease_token) == 32, "Token should be 16 hex bytes (32 chars)"
        assert claim.expires_at > claim.claimed_at, "Expiry should be after claim time"

        authority.close()


def test_duplicate_claim_raises_error():
    """Verify duplicate claim raises SQLiteConstraintError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"
        authority = LeaseAuthority(db_path)

        # First claim should succeed
        authority.claim_queue_item(
            queue_id="test_queue",
            item_id="test_item",
            run_id="test_run1",
            owner_pid=12345,
            ttl_seconds=60.0,
        )

        # Second claim should fail
        with pytest.raises(SQLiteConstraintError, match="already claimed"):
            authority.claim_queue_item(
                queue_id="test_queue",
                item_id="test_item",
                run_id="test_run2",
                owner_pid=12346,
                ttl_seconds=60.0,
            )

        authority.close()


def test_renew_lease_with_valid_token():
    """Verify lease renewal works with valid token and generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"
        authority = LeaseAuthority(db_path)

        # Create initial claim
        claim = authority.claim_queue_item(
            queue_id="test_queue",
            item_id="test_item",
            run_id="test_run",
            owner_pid=12345,
            ttl_seconds=60.0,
        )

        time.sleep(0.1)  # Small delay to test expiry update

        # Renew with valid token and generation
        renewed = authority.renew_lease(
            claim_id=claim.claim_id,
            lease_token=claim.lease_token,
            generation=claim.generation,
            ttl_seconds=120.0,
        )

        assert renewed.expires_at > claim.expires_at, "Renewed expiry should be later"
        assert renewed.generation == claim.generation, "Generation should not change on renew"
        assert renewed.heartbeat_at > claim.heartbeat_at, "Heartbeat should be updated"

        authority.close()


def test_stale_owner_cannot_renew():
    """Verify stale owner (wrong generation) cannot renew lease."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"
        authority = LeaseAuthority(db_path)

        # Create initial claim
        claim = authority.claim_queue_item(
            queue_id="test_queue",
            item_id="test_item",
            run_id="test_run",
            owner_pid=12345,
            ttl_seconds=60.0,
        )

        # Try to renew with wrong generation (stale owner)
        with pytest.raises(StaleLeaseError, match="stale owner"):
            authority.renew_lease(
                claim_id=claim.claim_id,
                lease_token=claim.lease_token,
                generation=999,  # Wrong generation
                ttl_seconds=120.0,
            )

        authority.close()


def test_wrong_token_cannot_renew():
    """Verify wrong token cannot renew lease."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"
        authority = LeaseAuthority(db_path)

        # Create initial claim
        claim = authority.claim_queue_item(
            queue_id="test_queue",
            item_id="test_item",
            run_id="test_run",
            owner_pid=12345,
            ttl_seconds=60.0,
        )

        # Try to renew with wrong token
        with pytest.raises(StaleLeaseError, match="stale owner"):
            authority.renew_lease(
                claim_id=claim.claim_id,
                lease_token="wrong_token_123456789012345678",
                generation=claim.generation,
                ttl_seconds=120.0,
            )

        authority.close()


def test_ack_lease_releases_claim():
    """Verify ack releases claim and allows new claim."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"
        authority = LeaseAuthority(db_path)

        # Create initial claim
        claim = authority.claim_queue_item(
            queue_id="test_queue",
            item_id="test_item",
            run_id="test_run",
            owner_pid=12345,
            ttl_seconds=60.0,
        )

        # Ack the claim
        result = authority.ack_lease(
            claim_id=claim.claim_id,
            lease_token=claim.lease_token,
            generation=claim.generation,
            final_status="COMPLETE",
        )

        assert result is True, "Ack should succeed"

        # New claim should now be possible
        new_claim = authority.claim_queue_item(
            queue_id="test_queue",
            item_id="test_item",
            run_id="test_run2",
            owner_pid=12346,
            ttl_seconds=60.0,
        )

        assert new_claim.run_id == "test_run2", "New claim should succeed"

        authority.close()


def test_project_lease_exclusivity():
    """Verify project lease exclusivity across processes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"
        authority = LeaseAuthority(db_path)

        # First project lease should succeed
        lease1 = authority.claim_project_lease(
            project_id="test_project",
            run_id="run1",
            ttl_seconds=60.0,
        )

        assert lease1.project_id == "test_project"
        assert lease1.owner_run_id == "run1"
        assert lease1.generation == 1

        # Second lease on same project should fail
        with pytest.raises(SQLiteConstraintError, match="already leased"):
            authority.claim_project_lease(
                project_id="test_project",
                run_id="run2",
                ttl_seconds=60.0,
            )

        authority.close()


def test_project_lease_expiry_allows_renewal():
    """Verify expired project lease can be claimed by new run."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"
        authority = LeaseAuthority(db_path)

        # First lease with very short TTL
        lease1 = authority.claim_project_lease(
            project_id="test_project",
            run_id="run1",
            ttl_seconds=0.1,  # Very short expiry
        )

        time.sleep(0.2)  # Wait for expiry

        # New lease should succeed after expiry
        lease2 = authority.claim_project_lease(
            project_id="test_project",
            run_id="run2",
            ttl_seconds=60.0,
        )

        assert lease2.owner_run_id == "run2", "New lease should succeed after expiry"

        authority.close()


def test_lease_authority_thread_safe():
    """Verify lease authority is thread-safe via thread-local connections."""
    import threading

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"
        authority = LeaseAuthority(db_path)

        results = []
        errors = []

        def claim_in_thread(run_id):
            try:
                claim = authority.claim_queue_item(
                    queue_id="test_queue",
                    item_id=f"item_{run_id}",
                    run_id=run_id,
                    owner_pid=12345,
                    ttl_seconds=60.0,
                )
                results.append(claim.run_id)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=claim_in_thread, args=(f"run{i}",))
            for i in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Thread-safe operations failed: {errors}"
        assert len(results) == 5, "All claims should succeed"

        authority.close()


def test_lease_authority_fail_closed_on_corruption():
    """Verify fail-closed behavior on database corruption."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lease.db"

        # Create valid database
        authority = LeaseAuthority(db_path)
        authority.close()

        # Corrupt the database
        with open(db_path, "wb") as f:
            f.write(b"corrupted data")

        # Should raise error on initialization
        with pytest.raises(SQLiteInitializationError):
            authority = LeaseAuthority(db_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
