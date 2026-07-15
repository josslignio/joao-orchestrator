"""C6 Drift Control — progress fingerprint (extends Turbo T10).

A progress fingerprint hashes the observable state of a task run:
  task state, diff hash, test-result hash, evaluation hash, context packet
  hash, files opened, failure signature.

If the fingerprint does not change between two observations while the task is
not complete, the task has made NO PROGRESS. This is the core drift signal.

Extends the circuit_breaker.py (T10) — does not replace it. The breaker trips
on specific patterns; this module provides the fingerprint abstraction that
makes "no progress" detection precise and deterministic.

Deterministic. No network. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional

from ..evaluation.models import sha256_json


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProgressFingerprint:
    """Content-addressed snapshot of a task's observable progress."""

    task_id: str
    task_state: str
    diff_hash: str
    test_result_hash: str
    evaluation_hash: str
    context_packet_hash: str
    files_opened: tuple[str, ...]
    failure_signature: str
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_state": self.task_state,
            "diff_hash": self.diff_hash,
            "test_result_hash": self.test_result_hash,
            "evaluation_hash": self.evaluation_hash,
            "context_packet_hash": self.context_packet_hash,
            "files_opened": sorted(self.files_opened),
            "failure_signature": self.failure_signature,
        }

    @property
    def digest(self) -> str:
        """The content-addressed fingerprint digest."""
        return sha256_json(self.unsigned_dict())

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, ProgressFingerprint):
            return NotImplemented
        return self.digest == other.digest

    def __hash__(self) -> int:
        return hash(self.digest)

    def changed_from(self, previous: Optional["ProgressFingerprint"]) -> bool:
        """Whether this fingerprint differs from a previous one.

        Two fingerprints are 'the same' (no progress) if their digests match.
        """
        if previous is None:
            return True
        return self.digest != previous.digest

    def diff_fields(self, previous: Optional["ProgressFingerprint"]) -> dict[str, bool]:
        """Per-field change report vs a previous fingerprint (for diagnostics)."""
        if previous is None:
            return {
                "task_state": True, "diff_hash": True, "test_result_hash": True,
                "evaluation_hash": True, "context_packet_hash": True,
                "files_opened": True, "failure_signature": True,
            }
        return {
            "task_state": self.task_state != previous.task_state,
            "diff_hash": self.diff_hash != previous.diff_hash,
            "test_result_hash": self.test_result_hash != previous.test_result_hash,
            "evaluation_hash": self.evaluation_hash != previous.evaluation_hash,
            "context_packet_hash": self.context_packet_hash != previous.context_packet_hash,
            "files_opened": set(self.files_opened) != set(previous.files_opened),
            "failure_signature": self.failure_signature != previous.failure_signature,
        }


def make_fingerprint(
    task_id: str,
    task_state: str,
    diff_hash: str = "",
    test_result_hash: str = "",
    evaluation_hash: str = "",
    context_packet_hash: str = "",
    files_opened: tuple[str, ...] = (),
    failure_signature: str = "",
) -> ProgressFingerprint:
    """Construct a progress fingerprint."""
    return ProgressFingerprint(
        task_id=task_id,
        task_state=task_state,
        diff_hash=diff_hash,
        test_result_hash=test_result_hash,
        evaluation_hash=evaluation_hash,
        context_packet_hash=context_packet_hash,
        files_opened=tuple(sorted(files_opened)),
        failure_signature=failure_signature,
    )
