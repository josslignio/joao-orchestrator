"""Deterministic per-task telemetry for the factory (T1).

Records a consumption proxy and wall-clock for each task, using transparent
proxies (serialized byte counts, call counts, durations). Never invents token
counts: if exact tokens are unavailable, byte proxies are recorded with their
nature explicit in the record.

Consumption proxy definition (published transparently, see benchmark.py):

    consumption_proxy = w_model * model_calls
                      + w_codex * codex_calls
                      + w_provider * provider_calls
                      + w_tool * tool_calls
                      + w_prompt * prompt_bytes
                      + w_context * context_packet_bytes
                      + w_review * review_packet_bytes
                      + w_diff * diff_bytes

Default weights are 1.0 so the proxy is a transparent weighted sum of published
components. Weights are part of the manifest so comparisons are reproducible.

Persistence follows storage/atomic.py: atomic_write_json for snapshots,
append_line for JSONL audit. Integrity hashing follows evaluation/models.py.

Deterministic: same inputs -> same proxy. No network. Stdlib only.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Optional

from ..evaluation.models import canonical_json_bytes, sha256_json
from ..storage.atomic import append_line, atomic_write_json


SCHEMA_VERSION = 1

# Default consumption-proxy weights. Every component is published alongside the
# proxy so the number is fully reproducible and never opaque.
DEFAULT_WEIGHTS: dict[str, float] = {
    "model_calls": 1.0,
    "codex_calls": 1.0,
    "provider_calls": 1.0,
    "tool_calls": 1.0,
    "prompt_bytes": 0.001,
    "context_packet_bytes": 0.001,
    "review_packet_bytes": 0.001,
    "diff_bytes": 0.001,
}


def _now_iso() -> str:
    # Deterministic UTC stamp with seconds resolution (no tz-naive pitfalls).
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _finite_or_zero(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return f


def _int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class TaskTelemetry:
    """One task's measured telemetry record.

    All byte fields are transparent proxies for prompt/context/review/diff
    size. They are NOT token counts. The record publishes every component so
    the consumption proxy is reproducible.
    """

    task_id: str
    project_id: str
    category: str = "implementation"
    complexity: str = "SMALL"
    wall_clock_ms: int = 0
    model_calls: int = 0
    codex_calls: int = 0
    provider_calls: int = 0
    tool_calls: int = 0
    files_listed: int = 0
    files_opened: int = 0
    files_reopened: int = 0
    prompt_bytes: int = 0
    context_packet_bytes: int = 0
    review_packet_bytes: int = 0
    diff_bytes: int = 0
    targeted_test_runs: int = 0
    full_test_runs: int = 0
    test_runtime_ms: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    questions: int = 0
    corrections: int = 0
    failed_attempts: int = 0
    worktree_count: int = 0
    final_state: str = "UNKNOWN"
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION
    integrity_sha256: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "category": self.category,
            "complexity": self.complexity,
            "wall_clock_ms": self.wall_clock_ms,
            "model_calls": self.model_calls,
            "codex_calls": self.codex_calls,
            "provider_calls": self.provider_calls,
            "tool_calls": self.tool_calls,
            "files_listed": self.files_listed,
            "files_opened": self.files_opened,
            "files_reopened": self.files_reopened,
            "prompt_bytes": self.prompt_bytes,
            "context_packet_bytes": self.context_packet_bytes,
            "review_packet_bytes": self.review_packet_bytes,
            "diff_bytes": self.diff_bytes,
            "targeted_test_runs": self.targeted_test_runs,
            "full_test_runs": self.full_test_runs,
            "test_runtime_ms": self.test_runtime_ms,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "questions": self.questions,
            "corrections": self.corrections,
            "failed_attempts": self.failed_attempts,
            "worktree_count": self.worktree_count,
            "final_state": self.final_state,
            "created_at": self.created_at,
        }

    def with_integrity(self) -> "TaskTelemetry":
        return replace(self, integrity_sha256=sha256_json(self.unsigned_dict()))

    def verify_integrity(self) -> bool:
        return bool(self.integrity_sha256) and self.integrity_sha256 == sha256_json(
            self.unsigned_dict()
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["integrity_sha256"] = self.integrity_sha256
        return payload

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TaskTelemetry":
        return cls(
            task_id=str(d["task_id"]),
            project_id=str(d["project_id"]),
            category=str(d.get("category", "implementation")),
            complexity=str(d.get("complexity", "SMALL")),
            wall_clock_ms=_int_or_zero(d.get("wall_clock_ms")),
            model_calls=_int_or_zero(d.get("model_calls")),
            codex_calls=_int_or_zero(d.get("codex_calls")),
            provider_calls=_int_or_zero(d.get("provider_calls")),
            tool_calls=_int_or_zero(d.get("tool_calls")),
            files_listed=_int_or_zero(d.get("files_listed")),
            files_opened=_int_or_zero(d.get("files_opened")),
            files_reopened=_int_or_zero(d.get("files_reopened")),
            prompt_bytes=_int_or_zero(d.get("prompt_bytes")),
            context_packet_bytes=_int_or_zero(d.get("context_packet_bytes")),
            review_packet_bytes=_int_or_zero(d.get("review_packet_bytes")),
            diff_bytes=_int_or_zero(d.get("diff_bytes")),
            targeted_test_runs=_int_or_zero(d.get("targeted_test_runs")),
            full_test_runs=_int_or_zero(d.get("full_test_runs")),
            test_runtime_ms=_int_or_zero(d.get("test_runtime_ms")),
            cache_hits=_int_or_zero(d.get("cache_hits")),
            cache_misses=_int_or_zero(d.get("cache_misses")),
            questions=_int_or_zero(d.get("questions")),
            corrections=_int_or_zero(d.get("corrections")),
            failed_attempts=_int_or_zero(d.get("failed_attempts")),
            worktree_count=_int_or_zero(d.get("worktree_count")),
            final_state=str(d.get("final_state", "UNKNOWN")),
            created_at=str(d.get("created_at", "")),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            integrity_sha256=str(d.get("integrity_sha256", "")),
        )

    def consumption_proxy(self, weights: Optional[Mapping[str, float]] = None) -> float:
        """Compute the transparent consumption proxy.

        Every component is published in the record; weights default to
        DEFAULT_WEIGHTS and are recorded in the benchmark manifest.
        """
        w = dict(DEFAULT_WEIGHTS)
        if weights:
            w.update({k: _finite_or_zero(v) for k, v in weights.items()})
        return (
            w["model_calls"] * self.model_calls
            + w["codex_calls"] * self.codex_calls
            + w["provider_calls"] * self.provider_calls
            + w["tool_calls"] * self.tool_calls
            + w["prompt_bytes"] * self.prompt_bytes
            + w["context_packet_bytes"] * self.context_packet_bytes
            + w["review_packet_bytes"] * self.review_packet_bytes
            + w["diff_bytes"] * self.diff_bytes
        )

    def proxy_components(self, weights: Optional[Mapping[str, float]] = None) -> dict[str, float]:
        """Publish every weighted component of the consumption proxy."""
        w = dict(DEFAULT_WEIGHTS)
        if weights:
            w.update({k: _finite_or_zero(v) for k, v in weights.items()})
        return {
            "model_calls": w["model_calls"] * self.model_calls,
            "codex_calls": w["codex_calls"] * self.codex_calls,
            "provider_calls": w["provider_calls"] * self.provider_calls,
            "tool_calls": w["tool_calls"] * self.tool_calls,
            "prompt_bytes": w["prompt_bytes"] * self.prompt_bytes,
            "context_packet_bytes": w["context_packet_bytes"] * self.context_packet_bytes,
            "review_packet_bytes": w["review_packet_bytes"] * self.review_packet_bytes,
            "diff_bytes": w["diff_bytes"] * self.diff_bytes,
        }


class TelemetryRecorder:
    """Accumulates telemetry counters for one task, then freezes to a record.

    Usage:
        rec = TelemetryRecorder("task-1", "proj", category="implementation",
                                complexity="SMALL", now_fn=fake_now)
        rec.start()
        ... do work, call rec.inc_model_call(), rec.add_prompt_bytes(n) ...
        rec.finish(final_state="COMPLETED")
        record = rec.to_telemetry()
    """

    def __init__(
        self,
        task_id: str,
        project_id: str,
        category: str = "implementation",
        complexity: str = "SMALL",
        now_fn=None,
        monotonic_fn=None,
    ):
        self.task_id = task_id
        self.project_id = project_id
        self.category = category
        self.complexity = complexity
        self._now_fn = now_fn or _now_iso
        self._mono = monotonic_fn or time.monotonic
        self._start_mono: Optional[float] = None
        self._test_start_mono: Optional[float] = None
        self.model_calls = 0
        self.codex_calls = 0
        self.provider_calls = 0
        self.tool_calls = 0
        self.files_listed = 0
        self.files_opened = 0
        self.files_reopened = 0
        self.prompt_bytes = 0
        self.context_packet_bytes = 0
        self.review_packet_bytes = 0
        self.diff_bytes = 0
        self.targeted_test_runs = 0
        self.full_test_runs = 0
        self.test_runtime_ms = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.questions = 0
        self.corrections = 0
        self.failed_attempts = 0
        self.worktree_count = 0
        self.final_state = "UNKNOWN"
        self._created_at = self._now_fn()

    def start(self) -> None:
        self._start_mono = self._mono()

    def _elapsed_ms(self) -> int:
        if self._start_mono is None:
            return 0
        return max(0, int((self._mono() - self._start_mono) * 1000.0))

    def inc_model_call(self, n: int = 1) -> None:
        self.model_calls += n

    def inc_codex_call(self, n: int = 1) -> None:
        self.codex_calls += n

    def inc_provider_call(self, n: int = 1) -> None:
        self.provider_calls += n

    def inc_tool_call(self, n: int = 1) -> None:
        self.tool_calls += n

    def inc_files_listed(self, n: int = 1) -> None:
        self.files_listed += n

    def inc_files_opened(self, n: int = 1) -> None:
        self.files_opened += n

    def inc_files_reopened(self, n: int = 1) -> None:
        self.files_reopened += n

    def add_prompt_bytes(self, n: int) -> None:
        if n > 0:
            self.prompt_bytes += n

    def add_context_bytes(self, n: int) -> None:
        if n > 0:
            self.context_packet_bytes += n

    def add_review_bytes(self, n: int) -> None:
        if n > 0:
            self.review_packet_bytes += n

    def add_diff_bytes(self, n: int) -> None:
        if n > 0:
            self.diff_bytes += n

    def add_prompt(self, text: str) -> None:
        self.add_prompt_bytes(len(text.encode("utf-8")))

    def add_context(self, text: str) -> None:
        self.add_context_bytes(len(text.encode("utf-8")))

    def add_review(self, text: str) -> None:
        self.add_review_bytes(len(text.encode("utf-8")))

    def add_diff(self, text: str) -> None:
        self.add_diff_bytes(len(text.encode("utf-8")))

    def inc_targeted_test_run(self, n: int = 1) -> None:
        self.targeted_test_runs += n

    def inc_full_test_run(self, n: int = 1) -> None:
        self.full_test_runs += n

    def start_test_timer(self) -> None:
        self._test_start_mono = self._mono()

    def stop_test_timer(self) -> None:
        if self._test_start_mono is not None:
            self.test_runtime_ms += max(
                0, int((self._mono() - self._test_start_mono) * 1000.0)
            )
            self._test_start_mono = None

    def inc_cache_hit(self, n: int = 1) -> None:
        self.cache_hits += n

    def inc_cache_miss(self, n: int = 1) -> None:
        self.cache_misses += n

    def inc_question(self, n: int = 1) -> None:
        self.questions += n

    def inc_correction(self, n: int = 1) -> None:
        self.corrections += n

    def inc_failed_attempt(self, n: int = 1) -> None:
        self.failed_attempts += n

    def set_worktree_count(self, n: int) -> None:
        self.worktree_count = max(0, int(n))

    def finish(self, final_state: str = "COMPLETED") -> None:
        self.final_state = final_state

    def to_telemetry(self) -> TaskTelemetry:
        return TaskTelemetry(
            task_id=self.task_id,
            project_id=self.project_id,
            category=self.category,
            complexity=self.complexity,
            wall_clock_ms=self._elapsed_ms(),
            model_calls=self.model_calls,
            codex_calls=self.codex_calls,
            provider_calls=self.provider_calls,
            tool_calls=self.tool_calls,
            files_listed=self.files_listed,
            files_opened=self.files_opened,
            files_reopened=self.files_reopened,
            prompt_bytes=self.prompt_bytes,
            context_packet_bytes=self.context_packet_bytes,
            review_packet_bytes=self.review_packet_bytes,
            diff_bytes=self.diff_bytes,
            targeted_test_runs=self.targeted_test_runs,
            full_test_runs=self.full_test_runs,
            test_runtime_ms=self.test_runtime_ms,
            cache_hits=self.cache_hits,
            cache_misses=self.cache_misses,
            questions=self.questions,
            corrections=self.corrections,
            failed_attempts=self.failed_attempts,
            worktree_count=self.worktree_count,
            final_state=self.final_state,
            created_at=self._created_at,
        ).with_integrity()


class TelemetryStore:
    """Persists telemetry records outside managed repositories.

    Layout (under a state_root outside repos):
        <state_root>/telemetry/
          records.jsonl          # append-only audit
          snapshot.json          # atomic snapshot of last N records
    """

    def __init__(self, state_root: Path):
        self.state_root = Path(state_root).resolve()
        self.dir = self.state_root / "telemetry"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records_path = self.dir / "records.jsonl"
        self.snapshot_path = self.dir / "snapshot.json"

    def record(self, telemetry: TaskTelemetry) -> TaskTelemetry:
        if not telemetry.verify_integrity():
            raise ValueError("telemetry record failed integrity check")
        append_line(self.records_path, json.dumps(telemetry.to_dict(), ensure_ascii=False))
        return telemetry

    def load_all(self) -> list[TaskTelemetry]:
        if not self.records_path.is_file():
            return []
        out: list[TaskTelemetry] = []
        for line in self.records_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(TaskTelemetry.from_dict(json.loads(line)))
        return out

    def snapshot(self, label: str = "") -> dict[str, Any]:
        records = self.load_all()
        payload = {
            "label": label,
            "record_count": len(records),
            "records": [r.to_dict() for r in records],
            "created_at": _now_iso(),
        }
        atomic_write_json(self.snapshot_path, payload)
        return payload


def median(values: list[float]) -> float:
    """Deterministic median. Empty list -> 0.0. Non-finite values treated as 0."""
    if not values:
        return 0.0
    cleaned = sorted(_finite_or_zero(v) for v in values)
    n = len(cleaned)
    mid = n // 2
    if n % 2 == 1:
        return cleaned[mid]
    return (cleaned[mid - 1] + cleaned[mid]) / 2.0


def summarize(records: list[TaskTelemetry], weights: Optional[Mapping[str, float]] = None) -> dict[str, Any]:
    """Produce a transparent summary over a set of telemetry records.

    Publishes every component so the proxy is reproducible and never opaque.
    """
    if not records:
        return {
            "record_count": 0,
            "consumption_proxy_total": 0.0,
            "consumption_proxy_median": 0.0,
            "wall_clock_ms_median": 0.0,
            "components": {},
        }
    proxies = [r.consumption_proxy(weights) for r in records]
    walls = [float(r.wall_clock_ms) for r in records]
    # Effective wall-clock includes test runtime (tests are part of the task
    # wall-clock). For synthetic benchmarks where code runs in microseconds,
    # test runtime is the dominant deterministic cost.
    effective_walls = [float(r.wall_clock_ms + r.test_runtime_ms) for r in records]
    comp_total: dict[str, float] = {}
    for r in records:
        for k, v in r.proxy_components(weights).items():
            comp_total[k] = comp_total.get(k, 0.0) + v
    return {
        "record_count": len(records),
        "weights": dict(weights) if weights else dict(DEFAULT_WEIGHTS),
        "consumption_proxy_total": sum(proxies),
        "consumption_proxy_median": median(proxies),
        "wall_clock_ms_median": median(effective_walls),
        "wall_clock_ms_total": sum(effective_walls),
        "prompt_bytes_total": sum(r.prompt_bytes for r in records),
        "context_packet_bytes_total": sum(r.context_packet_bytes for r in records),
        "review_packet_bytes_total": sum(r.review_packet_bytes for r in records),
        "diff_bytes_total": sum(r.diff_bytes for r in records),
        "model_calls_total": sum(r.model_calls for r in records),
        "codex_calls_total": sum(r.codex_calls for r in records),
        "provider_calls_total": sum(r.provider_calls for r in records),
        "tool_calls_total": sum(r.tool_calls for r in records),
        "targeted_test_runs_total": sum(r.targeted_test_runs for r in records),
        "full_test_runs_total": sum(r.full_test_runs for r in records),
        "test_runtime_ms_total": sum(r.test_runtime_ms for r in records),
        "cache_hits_total": sum(r.cache_hits for r in records),
        "cache_misses_total": sum(r.cache_misses for r in records),
        "components_total": comp_total,
    }
