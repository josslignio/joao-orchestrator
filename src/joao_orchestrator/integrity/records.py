"""Domain-separated integrity records for the candidate lifecycle.

The records protect against stale, crossed, accidentally mutated, or forged
controller artifacts within the honest same-macOS-user trust boundary. They do
not claim to defend against a malicious process that can read the same user's
0600 key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, TypeVar, Type

from ..storage.persistence_errors import CheckpointCorruptionError

PURPOSE_IDENTITY = "joao:candidate-identity:v2"
PURPOSE_REVIEWER = "joao:reviewer-record:v2"
PURPOSE_APPROVAL = "joao:approval-record:v2"
PURPOSE_CHECKPOINT = "joao:checkpoint-record:v2"


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sign(key: bytes, purpose: str, payload: str) -> str:
    if not isinstance(key, (bytes, bytearray)) or len(key) < 32:
        raise ValueError("integrity key must contain at least 32 bytes")
    message = purpose.encode("utf-8") + b"\0" + payload.encode("utf-8")
    return hmac.new(bytes(key), message, hashlib.sha256).hexdigest()


def sha256_hex(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


T = TypeVar("T")


class _SignedRecord:
    signature_field = "signature"
    purpose = ""

    def _unsigned_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop(self.signature_field, None)
        return result

    def to_canonical_json(self) -> str:
        return _canonical_json(self._unsigned_dict())

    def digest(self) -> str:
        return sha256_hex(self.to_canonical_json())

    def compute_signature(self, hmac_key: bytes) -> str:
        return _sign(hmac_key, self.purpose, self.to_canonical_json())

    def verify_signature(self, signature: str, hmac_key: bytes) -> bool:
        if not isinstance(signature, str):
            return False
        return hmac.compare_digest(self.compute_signature(hmac_key), signature)

    def signed_dict(self, hmac_key: bytes) -> dict[str, Any]:
        result = asdict(self)
        result[self.signature_field] = self.compute_signature(hmac_key)
        return result

    @classmethod
    def from_mapping(cls: Type[T], value: Mapping[str, Any]) -> T:
        if not isinstance(value, Mapping):
            raise ValueError(f"{cls.__name__} must be an object")
        allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        extra = sorted(set(value) - allowed)
        if extra:
            raise ValueError(f"{cls.__name__} unexpected keys: {extra}")
        return cls(**dict(value))  # type: ignore[arg-type]


@dataclass
class CandidateIdentityV2(_SignedRecord):
    schema_version: int = 2
    run_id: str = ""
    attempt: int = 1
    base_commit: str = ""
    parent_commit: str = ""
    candidate_commit: str = ""
    candidate_tree: str = ""
    canonical_diff_sha256: str = ""
    changed_path_manifest: str = ""
    manifest_sha256: str = ""
    source_worktree_identity: str = ""
    creation_timestamp: float = 0.0
    controller_signature: Optional[str] = None

    signature_field = "controller_signature"
    purpose = PURPOSE_IDENTITY

    def validate(self) -> None:
        if self.schema_version != 2:
            raise ValueError("candidate identity schema_version must be 2")
        if not self.run_id or self.attempt < 1:
            raise ValueError("candidate identity missing run/attempt")
        for field_name in (
            "base_commit", "parent_commit", "candidate_commit", "candidate_tree",
            "canonical_diff_sha256", "manifest_sha256", "source_worktree_identity",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"candidate identity missing {field_name}")
        if self.changed_path_manifest and sha256_hex(self.changed_path_manifest) != self.manifest_sha256:
            raise ValueError("candidate changed-path manifest digest mismatch")


@dataclass
class ReviewerRecordV2(_SignedRecord):
    schema_version: int = 2
    identity_digest: str = ""
    identity_signature: str = ""
    base_commit: str = ""
    parent_commit: str = ""
    candidate_commit: str = ""
    candidate_tree: str = ""
    canonical_diff_sha256: str = ""
    manifest_sha256: str = ""
    reviewer_provider: str = ""
    reviewer_model: str = ""
    reviewer_return_code: int = 0
    verdict: str = ""
    findings: list[str] = field(default_factory=list)
    timestamp: float = 0.0
    signature: Optional[str] = None

    purpose = PURPOSE_REVIEWER

    def validate(self) -> None:
        if self.schema_version != 2:
            raise ValueError("reviewer record schema_version must be 2")
        if self.verdict not in {"ACCEPT", "P1", "BLOCK"}:
            raise ValueError("reviewer record has invalid verdict")
        if not isinstance(self.reviewer_return_code, int):
            raise ValueError("reviewer return code must be an integer")
        if not all(isinstance(item, str) for item in self.findings):
            raise ValueError("reviewer findings must be strings")
        for name in (
            "identity_digest", "identity_signature", "base_commit", "parent_commit",
            "candidate_commit", "candidate_tree", "canonical_diff_sha256",
            "manifest_sha256", "reviewer_provider", "reviewer_model",
        ):
            if not getattr(self, name):
                raise ValueError(f"reviewer record missing {name}")


@dataclass
class ApprovalRecord(_SignedRecord):
    schema_version: int = 2
    identity_digest: str = ""
    identity_signature: str = ""
    review_signature: str = ""
    approver_run_id: str = ""
    approval_timestamp: float = 0.0
    approved_by: str = "human"
    signature: Optional[str] = None

    purpose = PURPOSE_APPROVAL

    def validate(self) -> None:
        if self.schema_version != 2:
            raise ValueError("approval record schema_version must be 2")
        for name in (
            "identity_digest", "identity_signature", "review_signature",
            "approver_run_id", "approved_by",
        ):
            if not getattr(self, name):
                raise ValueError(f"approval record missing {name}")


@dataclass
class CheckpointRecord(_SignedRecord):
    schema_version: int = 2
    purpose_name: str = ""
    payload_sha256: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0
    signature: Optional[str] = None
    # Backward-compatible constructor alias used by the initial M5 tests.
    purpose: str = ""

    signature_field = "signature"

    def __post_init__(self) -> None:
        if self.purpose and not self.purpose_name:
            self.purpose_name = self.purpose
        if self.purpose_name and not self.purpose:
            self.purpose = self.purpose_name

    @property
    def domain_purpose(self) -> str:
        return PURPOSE_CHECKPOINT + ":" + self.purpose_name

    def compute_signature(self, hmac_key: bytes) -> str:
        return _sign(hmac_key, self.domain_purpose, self.to_canonical_json())

    def validate(self, expected_purpose: str | None = None) -> None:
        if self.schema_version != 2:
            raise CheckpointCorruptionError("checkpoint schema_version must be 2")
        if not self.purpose_name:
            raise CheckpointCorruptionError("checkpoint purpose missing")
        if expected_purpose is not None and self.purpose_name != expected_purpose:
            raise CheckpointCorruptionError("checkpoint purpose mismatch")
        if not self.payload_sha256:
            raise CheckpointCorruptionError("checkpoint payload digest missing")


class IntegrityKeyManager:
    """Create/read a dedicated 0600 HMAC key below a 0700 state directory."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_dir.is_symlink():
            raise PermissionError("integrity state directory must not be a symlink")
        os.chmod(self.state_dir, 0o700)
        if stat.S_IMODE(self.state_dir.stat().st_mode) != 0o700:
            raise PermissionError("integrity state directory must be mode 0700")
        self._key_path = self.state_dir / "integrity-hmac.key"
        self._ensure_key()

    @property
    def key_path(self) -> Path:
        return self._key_path

    def _ensure_key(self) -> None:
        if self._key_path.is_symlink():
            raise PermissionError("integrity key must not be a symlink")
        if not self._key_path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(self._key_path, flags, 0o600)
            try:
                os.write(fd, secrets.token_bytes(32))
                os.fsync(fd)
            finally:
                os.close(fd)
        os.chmod(self._key_path, 0o600)
        if stat.S_IMODE(self._key_path.stat().st_mode) != 0o600:
            raise PermissionError("integrity key must be mode 0600")
        if self._key_path.stat().st_size != 32:
            raise PermissionError("integrity key must be exactly 32 bytes")

    def get_key(self) -> bytes:
        self._ensure_key()
        return self._key_path.read_bytes()

    def rotate_key(self) -> bytes:
        old = self.get_key()
        fd, temp_name = tempfile.mkstemp(prefix=".integrity-key-", dir=self.state_dir)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, secrets.token_bytes(32))
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temp_name, self._key_path)
            os.chmod(self._key_path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return old


def verify_checkpoint_record(
    record: CheckpointRecord, hmac_key: bytes,
    *, expected_purpose: str | None = None,
) -> bool:
    record.validate(expected_purpose)
    if not record.signature:
        raise CheckpointCorruptionError("checkpoint record missing signature")
    if not record.verify_signature(record.signature, hmac_key):
        raise CheckpointCorruptionError("checkpoint record signature verification failed")
    return True


def signed_checkpoint_for_payload(
    payload: Mapping[str, Any], *, purpose: str, key: bytes,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], CheckpointRecord]:
    payload_text = _canonical_json(dict(payload))
    record = CheckpointRecord(
        purpose_name=purpose,
        payload_sha256=sha256_hex(payload_text),
        metadata=dict(metadata or {}),
        timestamp=time.time(),
    )
    record.signature = record.compute_signature(key)
    return {"payload": dict(payload), "integrity": asdict(record)}, record


def verify_signed_checkpoint_envelope(
    envelope: Mapping[str, Any], *, purpose: str, key: bytes,
) -> dict[str, Any]:
    if not isinstance(envelope, Mapping):
        raise CheckpointCorruptionError("checkpoint envelope must be an object")
    payload = envelope.get("payload")
    integrity = envelope.get("integrity")
    if not isinstance(payload, Mapping) or not isinstance(integrity, Mapping):
        raise CheckpointCorruptionError("checkpoint envelope missing payload/integrity")
    try:
        record = CheckpointRecord.from_mapping(integrity)
    except (TypeError, ValueError) as exc:
        raise CheckpointCorruptionError(f"invalid checkpoint integrity record: {exc}") from exc
    verify_checkpoint_record(record, key, expected_purpose=purpose)
    if sha256_hex(_canonical_json(dict(payload))) != record.payload_sha256:
        raise CheckpointCorruptionError("checkpoint payload digest mismatch")
    return dict(payload)
