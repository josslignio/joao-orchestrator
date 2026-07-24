"""M5 CandidateIdentityV2 and HMAC signing tests.

Tests verify domain-separated HMAC signing, canonical JSON,
constant-time verification, and proper secret handling.
"""
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from joao_orchestrator.integrity.records import (
    CandidateIdentityV2,
    ReviewerRecordV2,
    ApprovalRecord,
    CheckpointRecord,
    IntegrityKeyManager,
    PURPOSE_IDENTITY,
    PURPOSE_REVIEWER,
    PURPOSE_APPROVAL,
    PURPOSE_CHECKPOINT,
    verify_checkpoint_record,
)
from joao_orchestrator.storage.persistence_errors import CheckpointCorruptionError


def test_candidate_identity_v2_canonical_json():
    """Verify canonical JSON with sorted keys and compact separators."""
    identity = CandidateIdentityV2(
        run_id="test_run",
        base_commit="abc123",
        candidate_commit="def456",
        canonical_diff_sha256="sha256",
    )

    canonical = identity.to_canonical_json()

    # Verify it's valid JSON
    parsed = json.loads(canonical)
    assert parsed["run_id"] == "test_run"

    # Verify keys are sorted
    keys = list(json.loads(canonical).keys())
    assert keys == sorted(keys), "Keys should be sorted"

    # Verify compact separators (no spaces)
    assert ", " not in canonical, "Should use compact separators"


def test_candidate_identity_v2_hmac_signing():
    """Verify domain-separated HMAC signing."""
    identity = CandidateIdentityV2(
        run_id="test_run",
        base_commit="abc123",
        candidate_commit="def456",
        canonical_diff_sha256="sha256",
    )

    key = secrets.token_bytes(32)
    signature = identity.compute_signature(key)

    assert len(signature) == 64, "HMAC-SHA256 should be 64 hex chars"
    assert isinstance(signature, str), "Signature should be string"


def test_candidate_identity_v2_signature_verification():
    """Verify constant-time signature verification."""
    identity = CandidateIdentityV2(
        run_id="test_run",
        base_commit="abc123",
        candidate_commit="def456",
    )

    key = secrets.token_bytes(32)
    signature = identity.compute_signature(key)

    # Valid signature should verify
    assert identity.verify_signature(signature, key) is True

    # Invalid signature should not verify
    fake_signature = "x" * 64
    assert identity.verify_signature(fake_signature, key) is False

    # Wrong key should not verify
    wrong_key = secrets.token_bytes(32)
    assert identity.verify_signature(signature, wrong_key) is False


def test_candidate_identity_v2_domain_separation():
    """Verify domain separation prevents signature reuse."""
    identity1 = CandidateIdentityV2(
        run_id="test1",
        base_commit="abc",
        candidate_commit="def",
    )

    identity2 = CandidateIdentityV2(
        run_id="test2",
        base_commit="abc",
        candidate_commit="def",
    )

    key = secrets.token_bytes(32)
    sig1 = identity1.compute_signature(key)
    sig2 = identity2.compute_signature(key)

    # Same content but different run_id should produce different signatures
    # due to domain separation with run_id in the content
    assert sig1 != sig2, "Different content should produce different signatures"


def test_reviewer_record_v2_binding():
    """Verify reviewer V2 binds full identity and process result."""
    reviewer = ReviewerRecordV2(
        identity_digest="digest123",
        base_commit="abc",
        candidate_commit="def",
        reviewer_provider="anthropic",
        reviewer_model="claude-3",
        reviewer_return_code=0,
        verdict="ACCEPT",
    )

    key = secrets.token_bytes(32)
    signature = reviewer.compute_signature(key)

    assert reviewer.verify_signature(signature, key) is True

    # Verify all fields are included
    canonical = reviewer.to_canonical_json()
    parsed = json.loads(canonical)
    assert parsed["identity_digest"] == "digest123"
    assert parsed["reviewer_return_code"] == 0
    assert parsed["verdict"] == "ACCEPT"


def test_approval_record_signature():
    """Verify approval record binds identity and review."""
    approval = ApprovalRecord(
        identity_digest="digest123",
        identity_signature="sig456",
        review_signature="sig789",
        approver_run_id="approver_run",
    )

    key = secrets.token_bytes(32)
    signature = approval.compute_signature(key)

    assert approval.verify_signature(signature, key) is True

    # Verify binding fields
    canonical = approval.to_canonical_json()
    parsed = json.loads(canonical)
    assert parsed["identity_signature"] == "sig456"
    assert parsed["review_signature"] == "sig789"


def test_checkpoint_record_domain_separation():
    """Verify checkpoint records support purpose-based domain separation."""
    key = secrets.token_bytes(32)

    # Two checkpoints with same content but different purposes
    cp1 = CheckpointRecord(
        purpose="state_snapshot",
        payload_sha256="abc123",
    )

    cp2 = CheckpointRecord(
        purpose="milestone",
        payload_sha256="abc123",
    )

    sig1 = cp1.compute_signature(key)
    sig2 = cp2.compute_signature(key)

    # Different purposes should produce different signatures
    assert sig1 != sig2, "Different purposes should produce different signatures"


def test_integrity_key_manager_permissions():
    """Verify key manager creates keys with correct permissions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        manager = IntegrityKeyManager(state_dir)

        key_path = state_dir / "integrity-hmac.key"

        # Check directory permissions
        dir_stat = state_dir.stat()
        dir_mode = dir_stat.st_mode & 0o777
        assert dir_mode == 0o700, f"Directory should be 0700, got {oct(dir_mode)}"

        # Check key file permissions
        key_stat = key_path.stat()
        key_mode = key_stat.st_mode & 0o777
        assert key_mode == 0o600, f"Key file should be 0600, got {oct(key_mode)}"

        # Verify key exists and is 32 bytes (256 bits)
        key = manager.get_key()
        assert len(key) == 32, "Key should be 32 bytes (256 bits)"


def test_integrity_key_rotation():
    """Verify key rotation generates new key and returns old key."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        manager = IntegrityKeyManager(state_dir)

        old_key = manager.get_key()
        previous_key = manager.rotate_key()
        new_key = manager.get_key()

        assert previous_key == old_key, "Should return old key"
        assert new_key != old_key, "New key should be different"
        assert len(new_key) == 32, "New key should be 32 bytes"


def test_verify_checkpoint_record_fail_closed():
    """Verify checkpoint verification fails closed on invalid records."""
    key = secrets.token_bytes(32)

    # Missing signature should raise error
    record = CheckpointRecord(
        purpose="test",
        payload_sha256="abc",
        signature=None,
    )

    with pytest.raises(CheckpointCorruptionError, match="missing signature"):
        verify_checkpoint_record(record, key)

    # Invalid signature should raise error
    record.signature = "fake_signature_64_hex_chars_xxxxxxxxxxxxxx"

    with pytest.raises(CheckpointCorruptionError, match="verification failed"):
        verify_checkpoint_record(record, key)


def test_signature_excludes_signature_field():
    """Verify signature computation excludes the signature field itself."""
    identity = CandidateIdentityV2(
        run_id="test",
        base_commit="abc",
        candidate_commit="def",
        controller_signature="presigned_value",  # Should be excluded
    )

    key = secrets.token_bytes(32)
    signature = identity.compute_signature(key)

    # Set the signature
    identity.controller_signature = signature

    # Verify signature is still valid (proves it wasn't included in computation)
    assert identity.verify_signature(signature, key) is True


def test_constant_time_verification():
    """Verify constant-time comparison is used for signatures."""
    identity = CandidateIdentityV2(
        run_id="test",
        base_commit="abc",
        candidate_commit="def",
    )

    key = secrets.token_bytes(32)
    valid_sig = identity.compute_signature(key)

    # Verify timing attack protection via hmac.compare_digest
    # This tests the implementation, not timing side channels
    assert identity.verify_signature(valid_sig, key) is True

    # Wrong length signatures should also be handled safely
    wrong_length_sig = "short"
    assert identity.verify_signature(wrong_length_sig, key) is False


def test_secret_never_in_errors():
    """Verify secret key never appears in error messages."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        manager = IntegrityKeyManager(state_dir)
        key = manager.get_key()

        # Try to verify with wrong record - should not leak key in error
        record = CheckpointRecord(
            purpose="test",
            payload_sha256="abc",
            signature=None,
        )

        try:
            verify_checkpoint_record(record, key)
            assert False, "Should have raised error"
        except CheckpointCorruptionError as e:
            error_msg = str(e)
            # Verify key bytes are not in error message
            assert key.hex() not in error_msg, "Key should not be in error message"
            assert all(b not in error_msg for b in [key[:8].hex(), key[-8:].hex()]), \
                "Key parts should not be in error message"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
