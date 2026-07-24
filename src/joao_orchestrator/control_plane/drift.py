"""Drift detection for CP1 state validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class DriftReport:
    """Report of drift detection results."""

    has_drift: bool
    drift_details: list[str]
    expected_hash: str
    actual_hash: str
    drifted_tasks: list[str] = None
    budget_drift: dict[str, Any] = None

    def to_dict(self) -> dict:
        return {
            "has_drift": self.has_drift,
            "drift_details": self.drift_details,
            "expected_hash": self.expected_hash,
            "actual_hash": self.actual_hash,
            "drifted_tasks": self.drifted_tasks or [],
            "budget_drift": self.budget_drift or {},
        }


class DriftDetector:
    """Detect drift between expected and actual state."""

    def __init__(self, allowed_paths: tuple[str, ...] = ()):
        self.allowed_paths = allowed_paths

    def detect_state_drift(self, expected_state: dict[str, Any],
                          actual_state: dict[str, Any]) -> DriftReport:
        """Detect drift between expected and actual state."""
        expected_hash = self._hash_state(expected_state)
        actual_hash = self._hash_state(actual_state)

        if expected_hash == actual_hash:
            return DriftReport(
                has_drift=False,
                drift_details=[],
                expected_hash=expected_hash,
                actual_hash=actual_hash,
            )

        details = self._compare_states(expected_state, actual_state)
        return DriftReport(
            has_drift=True,
            drift_details=details,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
        )

    def detect(self, expected, actual):
        """Compare task graphs while keeping the report deterministic."""
        ids = set(expected.tasks) | set(actual.tasks)
        drifted = sorted(x for x in ids if getattr(expected.tasks.get(x), "status", None) != getattr(actual.tasks.get(x), "status", None))
        return DriftReport(bool(drifted), [f"Task drift: {x}" for x in drifted], "", "", drifted, {})

    def detect_filesystem_drift(self, repository_path: str,
                               expected_snapshot: dict[str, str]) -> DriftReport:
        """Detect drift in repository filesystem."""
        repo_path = Path(repository_path)
        if not repo_path.exists():
            return DriftReport(
                has_drift=True,
                drift_details=[f"Repository path does not exist: {repository_path}"],
                expected_hash="",
                actual_hash="",
            )

        actual_snapshot = self._create_filesystem_snapshot(repo_path)
        expected_hash = self._hash_snapshot(expected_snapshot)
        actual_hash = self._hash_snapshot(actual_snapshot)

        if expected_hash == actual_hash:
            return DriftReport(
                has_drift=False,
                drift_details=[],
                expected_hash=expected_hash,
                actual_hash=actual_hash,
            )

        details = self._compare_snapshots(expected_snapshot, actual_snapshot)
        return DriftReport(
            has_drift=True,
            drift_details=details,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
        )

    def _hash_state(self, state: dict[str, Any]) -> str:
        normalized = json.dumps(state, sort_keys=True)
        return hashlib.sha256(normalized.encode()).hexdigest()

    def _hash_snapshot(self, snapshot: dict[str, str]) -> str:
        normalized = json.dumps(snapshot, sort_keys=True)
        return hashlib.sha256(normalized.encode()).hexdigest()

    def _compare_states(self, expected: dict[str, Any],
                       actual: dict[str, Any]) -> list[str]:
        details = []
        all_keys = set(expected.keys()) | set(actual.keys())

        for key in sorted(all_keys):
            if key not in expected:
                details.append(f"Unexpected key in actual: {key}")
            elif key not in actual:
                details.append(f"Missing key in actual: {key}")
            elif expected[key] != actual[key]:
                details.append(f"Value mismatch for {key}: expected={expected[key]!r}, actual={actual[key]!r}")

        return details

    def _compare_snapshots(self, expected: dict[str, str],
                          actual: dict[str, str]) -> list[str]:
        details = []
        all_paths = set(expected.keys()) | set(actual.keys())

        for path in sorted(all_paths):
            if path not in expected:
                details.append(f"Unexpected file: {path}")
            elif path not in actual:
                details.append(f"Missing file: {path}")
            elif expected[path] != actual[path]:
                details.append(f"Content mismatch: {path}")

        return details

    def _create_filesystem_snapshot(self, repo_path: Path) -> dict[str, str]:
        snapshot = {}

        for allowed_path in self.allowed_paths:
            full_path = repo_path / allowed_path
            if not full_path.exists():
                continue

            if full_path.is_file():
                rel_path = allowed_path
                snapshot[rel_path] = self._hash_file(full_path)
            elif full_path.is_dir():
                for file_path in full_path.rglob("*"):
                    if file_path.is_file():
                        rel_path = file_path.relative_to(repo_path).as_posix()
                        if self._is_allowed(rel_path):
                            snapshot[rel_path] = self._hash_file(file_path)

        return snapshot

    def _hash_file(self, file_path: Path) -> str:
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _is_allowed(self, rel_path: str) -> bool:
        for allowed in self.allowed_paths:
            if rel_path == allowed or rel_path.startswith(allowed + "/"):
                return True
        return False
