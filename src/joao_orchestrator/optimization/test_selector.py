"""Deterministic impact-based test selector + tiered gates (T5).

Uses the T4 impact graph to select the minimal test set for a change, and
defines tiered gates (T0-T4) that bound how much testing runs at each phase.

Tiers (spec):
  T0 syntax/import   — compileall + import check
  T1 exact unit tests — tests directly targeting changed files
  T2 dependent-module — tests for modules that import changed files
  T3 smoke tests      — CLI/entry smoke checks
  T4 complete suite   — full test run, once per checkpoint

Rules:
  - editing:        T0-T2
  - before review:  T0-T3
  - before commit:  T4 once
  - sensitive/core/unknown impact: full suite
  - never reuse a green result when source/test/env hashes changed
  - audit every selection

Deterministic. No network. Stdlib only.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from ..evaluation.models import sha256_json
from ..storage.atomic import atomic_write_json
from .repo_index import ImpactGraph, RepoIndex, relevant_tests, relevant_files


SCHEMA_VERSION = 1


class GateTier(str, Enum):
    T0_SYNTAX_IMPORT = "t0_syntax_import"
    T1_EXACT_UNIT = "t1_unit"
    T2_DEPENDENT = "t2_dependent"
    T3_SMOKE = "t3_smoke"
    T4_FULL = "t4_full"


# Tier -> the set of tiers included at that phase (inclusive).
PHASE_TIERS = {
    "editing": (GateTier.T0_SYNTAX_IMPORT, GateTier.T1_EXACT_UNIT, GateTier.T2_DEPENDENT),
    "before_review": (GateTier.T0_SYNTAX_IMPORT, GateTier.T1_EXACT_UNIT,
                      GateTier.T2_DEPENDENT, GateTier.T3_SMOKE),
    "before_commit": (GateTier.T0_SYNTAX_IMPORT, GateTier.T1_EXACT_UNIT,
                      GateTier.T2_DEPENDENT, GateTier.T3_SMOKE, GateTier.T4_FULL),
}


@dataclass(frozen=True)
class TestSelection:
    """A deterministic test selection for a change set."""
    selection_id: str
    changed_paths: tuple[str, ...]
    phase: str
    selected_tiers: tuple[str, ...]
    t0_commands: tuple[str, ...]      # compileall / import checks
    t1_tests: tuple[str, ...]         # exact unit tests
    t2_tests: tuple[str, ...]         # dependent-module tests
    t3_tests: tuple[str, ...]         # smoke tests
    t4_full: bool                     # run full suite?
    force_full: bool                  # sensitive/unknown -> full suite
    selection_hash: str               # binds to (changed paths + test hashes)
    reasons: tuple[str, ...]
    schema_version: int = SCHEMA_VERSION
    integrity_sha256: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "selection_id": self.selection_id,
            "changed_paths": list(self.changed_paths),
            "phase": self.phase,
            "selected_tiers": list(self.selected_tiers),
            "t0_commands": list(self.t0_commands),
            "t1_tests": list(self.t1_tests),
            "t2_tests": list(self.t2_tests),
            "t3_tests": list(self.t3_tests),
            "t4_full": self.t4_full,
            "force_full": self.force_full,
            "selection_hash": self.selection_hash,
            "reasons": list(self.reasons),
        }

    def with_integrity(self) -> "TestSelection":
        return replace(self, integrity_sha256=sha256_json(self.unsigned_dict()))

    def verify_integrity(self) -> bool:
        return bool(self.integrity_sha256) and self.integrity_sha256 == sha256_json(
            self.unsigned_dict()
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["integrity_sha256"] = self.integrity_sha256
        return payload

    @property
    def total_test_count(self) -> int:
        return len(self.t1_tests) + len(self.t2_tests) + len(self.t3_tests)

    @property
    def is_full_suite(self) -> bool:
        return self.t4_full or self.force_full


def _env_fingerprint() -> str:
    """Fingerprint the Python environment (so green results don't carry across
    interpreter changes). Uses sys.executable + version only (no env vars)."""
    import sys
    return sha256_json({"executable": sys.executable, "version": sys.version})


def compute_selection_hash(
    changed_paths: Iterable[str],
    test_files: Iterable[str],
    source_hashes: dict[str, str],
    test_hashes: dict[str, str],
) -> str:
    """Bind a selection to exact source+test content hashes.

    A green result is reusable ONLY if this hash is unchanged.
    """
    return sha256_json({
        "changed": sorted(changed_paths),
        "tests": sorted(test_files),
        "source_hashes": {k: source_hashes.get(k, "") for k in sorted(source_hashes)},
        "test_hashes": {k: test_hashes.get(k, "") for k in sorted(test_hashes)},
        "env": _env_fingerprint(),
    })


def _is_unit_test_for(test_path: str, changed_path: str) -> bool:
    """Heuristic: test_path is an exact unit test for changed_path.

    e.g. tests/test_app.py <-> src/app.py, tests/test_core.py <-> src/core.py
    """
    import os.path
    tname = os.path.splitext(os.path.basename(test_path))[0]
    cname = os.path.splitext(os.path.basename(changed_path))[0]
    if tname.startswith("test_"):
        tname = tname[5:]
    return tname == cname


def select_tests(
    graph: ImpactGraph,
    index: RepoIndex,
    changed_paths: Iterable[str],
    phase: str = "editing",
    force_full: bool = False,
) -> TestSelection:
    """Select the minimal test set for a change set at a given phase.

    Args:
      graph: impact graph from T4.
      index: repo index from T4 (for content hashes).
      changed_paths: files touched by the task.
      phase: "editing" | "before_review" | "before_commit".
      force_full: if True (sensitive/unknown), force full suite.
    """
    changed = tuple(sorted(set(changed_paths)))
    tiers = PHASE_TIERS.get(phase, PHASE_TIERS["editing"])
    reasons: list[str] = []

    # Sensitive/core/unknown impact -> full suite (spec rule).
    fmap = index.file_map()
    sensitive_touched = any(fmap.get(p) and fmap[p].is_sensitive for p in changed)
    unknown_impact = any(p not in fmap for p in changed)
    if sensitive_touched or unknown_impact or force_full:
        force_full = True
        reasons.append("sensitive/unknown impact -> full suite")

    # Gather candidate tests.
    impact_tests = set(relevant_tests(graph, changed))
    # T1: exact unit tests (test file name matches changed source name).
    t1 = sorted(t for t in impact_tests if any(_is_unit_test_for(t, c) for c in changed))
    # T2: dependent-module tests (remaining impact tests).
    t2 = sorted(t for t in impact_tests if t not in t1)
    # T3: smoke tests (CLI entry tests) — include tests of CLI entry files.
    t3: list[str] = []
    for c in changed:
        if c in fmap and fmap[c].is_cli_entry:
            t3.extend(relevant_tests(graph, [c]))
    t3 = sorted(set(t3))

    # T0: always compileall the changed paths' packages.
    t0: list[str] = []
    for c in changed:
        if c.endswith(".py"):
            # compileall the directory containing the changed file.
            import os.path
            d = os.path.dirname(c) or "."
            t0.append(f"compileall:{d}")
    t0 = sorted(set(t0))

    # Determine which tiers are active at this phase.
    active_tiers = tuple(t.value for t in tiers)
    t4_full = GateTier.T4_FULL in tiers and (force_full or phase == "before_commit")

    if force_full:
        t4_full = True

    # Build content hash bindings.
    source_hashes = {p: fmap[p].content_sha256 for p in changed if p in fmap}
    all_tests = t1 + t2 + t3
    test_hashes = {t: fmap[t].content_sha256 for t in all_tests if t in fmap}
    sel_hash = compute_selection_hash(changed, all_tests, source_hashes, test_hashes)

    sel = TestSelection(
        selection_id=f"sel-{sel_hash[:16]}",
        changed_paths=changed,
        phase=phase,
        selected_tiers=active_tiers,
        t0_commands=tuple(t0),
        t1_tests=tuple(t1),
        t2_tests=tuple(t2),
        t3_tests=tuple(t3),
        t4_full=t4_full,
        force_full=force_full,
        selection_hash=sel_hash,
        reasons=tuple(reasons),
    )
    return sel.with_integrity()


@dataclass(frozen=True)
class TestResult:
    """Result of running a test selection."""
    selection_id: str
    selection_hash: str
    passed: bool
    tests_run: int
    tests_failed: int
    duration_ms: int
    tier_reached: str
    failures: tuple[str, ...]
    schema_version: int = SCHEMA_VERSION
    integrity_sha256: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "selection_id": self.selection_id,
            "selection_hash": self.selection_hash,
            "passed": self.passed,
            "tests_run": self.tests_run,
            "tests_failed": self.tests_failed,
            "duration_ms": self.duration_ms,
            "tier_reached": self.tier_reached,
            "failures": list(self.failures),
        }

    def with_integrity(self) -> "TestResult":
        return replace(self, integrity_sha256=sha256_json(self.unsigned_dict()))

    def verify_integrity(self) -> bool:
        return bool(self.integrity_sha256) and self.integrity_sha256 == sha256_json(
            self.unsigned_dict()
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["integrity_sha256"] = self.integrity_sha256
        return payload


def can_reuse_green_result(
    previous: TestResult,
    current_selection_hash: str,
) -> bool:
    """A green result is reusable ONLY if the selection hash is unchanged."""
    if not previous.passed:
        return False
    return previous.selection_hash == current_selection_hash


def persist_selection(state_root: Path, selection: TestSelection) -> Path:
    """Persist a test selection audit under <state_root>/test_selection/."""
    root = Path(state_root).resolve() / "test_selection"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{selection.selection_id}.json"
    atomic_write_json(path, selection.to_dict())
    return path
