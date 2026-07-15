"""Turbo v1.1 high-ROI refinements (P0.1–P0.4, P0.6).

Measured Turbo v1 evidence drove these refinements. They EXTEND the existing
T1–T11 primitives — they do not rebuild or replace them.

P0.1 Real token/call telemetry
    Extend ``TaskTelemetry``/``TelemetryRecorder`` to carry real token fields
    when providers expose them, falling back transparently to byte proxies.
    Never invent token values: fields are optional and default to 0.

P0.3 Duplicate-call suppression
    A bounded dedup layer that skips identical provider requests, identical
    context packets, identical test commands (with identical hashes), identical
    Codex reviews, and identical repository index rebuilds. Every skip records
    its reason and the bound hashes.

P0.4 Active-context limiter
    Caps the active-context window to at most the current checkpoint + the
    previous checkpoint's interface summary. Completed checkpoints are
    represented only by their compact digest (commit, artifact hashes,
    interface summary, test count, C1 verdict).

P0.6 Real two-task nightly smoke
    ``BatchExecutor``-aligned helper that guarantees ``BatchSpec.task_ids`` and
    the submitted ``TaskPlan`` objects match exactly, so two tasks are both
    executed and reported with no overlap, loss or replay.

Deterministic. No network. Stdlib only. Persistence outside repos.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..evaluation.models import sha256_json
from ..storage.atomic import append_line, atomic_write_json
from .telemetry import TaskTelemetry, TelemetryRecorder


SCHEMA_VERSION = 1


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# P0.1 — Real token / call telemetry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenUsage:
    """Real token usage reported by a provider when available.

    Every field is OPTIONAL: providers that do not expose token counts leave
    them at 0 and the telemetry records transparent byte proxies instead.
    Nothing here is ever invented.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    provider_request_id: str = ""
    provider_model: str = ""

    def has_real_tokens(self) -> bool:
        return self.input_tokens > 0 or self.output_tokens > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "provider_request_id": self.provider_request_id,
            "provider_model": self.provider_model,
        }


def record_token_usage(recorder: TelemetryRecorder, usage: TokenUsage) -> None:
    """Attach real token usage to a telemetry recorder.

    Stored on the recorder as extended attributes that the recorder surfaces
    via ``token_usage`` on the final record's extended payload. The recorder's
    core ``TaskTelemetry`` fields remain byte proxies (the published contract);
    token values are an *additional* transparency layer when available.
    """
    if not isinstance(usage, TokenUsage):
        raise TypeError("usage must be a TokenUsage")
    setattr(recorder, "_token_usage", usage)


def telemetry_token_usage(telemetry: TaskTelemetry) -> Optional[TokenUsage]:
    """Return token usage attached to a telemetry object, if any.

    ``TaskTelemetry`` is frozen and does not carry token fields directly; the
    caller may attach a ``token_usage`` payload via this helper for downstream
    reporting. Returns None when no real tokens were recorded.
    """
    raw = getattr(telemetry, "_token_usage", None)
    if raw is None:
        return None
    return raw if isinstance(raw, TokenUsage) else None


def summarize_with_tokens(
    records: list[TaskTelemetry],
    weights: Optional[Mapping[str, float]] = None,
) -> dict[str, Any]:
    """Extend ``telemetry.summarize`` with real-token aggregates when present.

    Real-token aggregates are reported only when at least one record carries
    real tokens; otherwise the summary notes that tokens were unavailable and
    the byte proxy remains authoritative.
    """
    from .telemetry import summarize
    base = summarize(records, weights)
    token_records = [telemetry_token_usage(r) for r in records]
    token_records = [t for t in token_records if t is not None and t.has_real_tokens()]
    base["token_coverage"] = len(token_records)
    base["token_coverage_ratio"] = (
        len(token_records) / len(records) if records else 0.0
    )
    if token_records:
        base["real_tokens"] = {
            "input_tokens_total": sum(t.input_tokens for t in token_records),
            "output_tokens_total": sum(t.output_tokens for t in token_records),
            "cached_input_tokens_total": sum(t.cached_input_tokens for t in token_records),
        }
    else:
        base["real_tokens"] = None
        base["real_tokens_note"] = (
            "no real token counts available; byte proxy remains authoritative"
        )
    return base


# ---------------------------------------------------------------------------
# P0.3 — Duplicate-call suppression
# ---------------------------------------------------------------------------


@dataclass
class SuppressionRecord:
    """One duplicate-call suppression decision."""

    kind: str           # provider / context_packet / test_command / review / repo_index
    key_digest: str     # content hash of the request that was skipped
    reason: str
    timestamp: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "key_digest": self.key_digest,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


class DuplicateSuppressor:
    """Bounded dedup layer across the factory call surface.

    Tracks the hashes of recent requests by kind. A repeat of the same hash is
    suppressed (the caller records the skip reason + the bound hashes). The
    store is bounded: it keeps at most ``max_per_kind`` hashes per kind to
    avoid unbounded growth.

    Security decisions are NEVER cached/suppressed: a caller marks a request
    as ``security=True`` and the suppressor always executes it.
    """

    def __init__(self, max_per_kind: int = 256, now_fn: Optional[Callable[[], str]] = None):
        if max_per_kind <= 0:
            raise ValueError("max_per_kind must be positive")
        self.max_per_kind = max_per_kind
        self._now_fn = now_fn or _now_iso
        self._seen: dict[str, dict[str, None]] = {
            "provider": {},
            "context_packet": {},
            "test_command": {},
            "review": {},
            "repo_index": {},
        }
        self.suppressions: list[SuppressionRecord] = []

    def _key_digest(self, kind: str, payload: Any) -> str:
        return sha256_json({"kind": kind, "payload": payload})

    def should_skip(
        self,
        kind: str,
        payload: Any,
        *,
        security: bool = False,
    ) -> tuple[bool, str, str]:
        """Decide whether a call should be skipped as a duplicate.

        Returns ``(skip, key_digest, reason)``. Security-sensitive calls are
        never skipped. On a skip, the suppression is recorded.
        """
        if kind not in self._seen:
            raise ValueError(f"unknown suppression kind: {kind}")
        if security:
            digest = self._key_digest(kind, payload)
            # Security decisions are always executed; the digest is returned so
            # the caller can bind it without any caching effect.
            return False, digest, "security decision: never cached"
        digest = self._key_digest(kind, payload)
        if digest in self._seen[kind]:
            rec = SuppressionRecord(
                kind=kind, key_digest=digest,
                reason=f"duplicate {kind} request suppressed",
                timestamp=self._now_fn(),
            )
            self.suppressions.append(rec)
            return True, digest, rec.reason
        # New: record and bound.
        self._seen[kind][digest] = None
        if len(self._seen[kind]) > self.max_per_kind:
            # Drop the oldest inserted (dict preserves insertion order).
            oldest = next(iter(self._seen[kind]))
            del self._seen[kind][oldest]
        return False, digest, "new request"

    def reset(self, kind: Optional[str] = None) -> None:
        if kind is None:
            for k in self._seen:
                self._seen[k] = {}
        elif kind in self._seen:
            self._seen[kind] = {}

    def stats(self) -> dict[str, Any]:
        return {
            kind: {
                "tracked": len(hashes),
                "suppressed": sum(1 for s in self.suppressions if s.kind == kind),
            }
            for kind, hashes in self._seen.items()
        }

    def persist_audit(self, state_root) -> Path:
        d = Path(state_root).resolve() / "turbo_v11"
        d.mkdir(parents=True, exist_ok=True)
        path = d / "suppressions.jsonl"
        for rec in self.suppressions:
            append_line(path, json.dumps(rec.to_dict(), ensure_ascii=False))
        return path


# ---------------------------------------------------------------------------
# P0.4 — Active-context limiter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointDigest:
    """Compact digest of a completed checkpoint.

    Completed checkpoints are represented ONLY by this digest in the active
    context window — never by their raw content.
    """

    task_id: str
    commit: str
    artifact_hashes: tuple[str, ...]
    interface_summary: str
    test_count: int
    c1_verdict: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "commit": self.commit,
            "artifact_hashes": list(self.artifact_hashes),
            "interface_summary": self.interface_summary,
            "test_count": self.test_count,
            "c1_verdict": self.c1_verdict,
        }

    def serialized_bytes(self) -> int:
        return len(json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"))


@dataclass(frozen=True)
class ActiveContextWindow:
    """The bounded active-context window.

    Contains at most:
      - the current checkpoint (full content)
      - the previous checkpoint's interface summary (a CheckpointDigest)

    All earlier checkpoints are represented only as digests in ``history``,
    which is NOT sent to the model — it is an audit trail only.
    """

    current_checkpoint_id: str
    current_content: str
    previous: Optional[CheckpointDigest]
    history: tuple[CheckpointDigest, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "current_checkpoint_id": self.current_checkpoint_id,
            "current_content_bytes": len(self.current_content.encode("utf-8")),
            "previous": self.previous.to_dict() if self.previous else None,
            "history_count": len(self.history),
        }

    def active_bytes(self) -> int:
        """Bytes transmitted to the model: current content + previous digest."""
        total = len(self.current_content.encode("utf-8"))
        if self.previous is not None:
            total += self.previous.serialized_bytes()
        return total


class ActiveContextLimiter:
    """Enforces the active-context bound across a run.

    Records each completed checkpoint as a digest. When asked for the active
    window for checkpoint N, returns:
      current = checkpoint N (full content provided by caller)
      previous = digest of checkpoint N-1 (interface summary only)
      history = digests of N-2, N-3, ... (audit only, NOT sent to model)
    """

    def __init__(self, max_history: int = 64):
        if max_history <= 0:
            raise ValueError("max_history must be positive")
        self.max_history = max_history
        self._completed: list[CheckpointDigest] = []

    def record_completed(self, digest: CheckpointDigest) -> None:
        self._completed.append(digest)
        if len(self._completed) > self.max_history:
            self._completed = self._completed[-self.max_history:]

    def window_for(self, checkpoint_id: str, current_content: str) -> ActiveContextWindow:
        previous = self._completed[-1] if self._completed else None
        history = tuple(self._completed[:-1]) if len(self._completed) > 1 else ()
        return ActiveContextWindow(
            current_checkpoint_id=checkpoint_id,
            current_content=current_content,
            previous=previous,
            history=history,
        )

    def completed_count(self) -> int:
        return len(self._completed)


# ---------------------------------------------------------------------------
# P0.6 — Real two-task nightly smoke (BatchSpec/TaskPlan consistency)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchConsistencyReport:
    """Result of verifying BatchSpec ↔ submitted TaskPlan consistency."""

    consistent: bool
    spec_task_ids: tuple[str, ...]
    plan_task_ids: tuple[str, ...]
    missing_from_plans: tuple[str, ...]
    extra_in_plans: tuple[str, ...]
    duplicate_task_ids: tuple[str, ...]
    cross_project_overlap: tuple[str, ...]
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "consistent": self.consistent,
            "spec_task_ids": list(self.spec_task_ids),
            "plan_task_ids": list(self.plan_task_ids),
            "missing_from_plans": list(self.missing_from_plans),
            "extra_in_plans": list(self.extra_in_plans),
            "duplicate_task_ids": list(self.duplicate_task_ids),
            "cross_project_overlap": list(self.cross_project_overlap),
        }


def verify_batch_task_consistency(
    spec_task_ids: tuple[str, ...],
    plans: tuple[Any, ...],
) -> BatchConsistencyReport:
    """Guarantee BatchSpec.task_ids and submitted TaskPlan objects match exactly.

    This fixes the Turbo v1 smoke limitation where the batch executor's
    ``task_plans`` could silently diverge from the spec's ``task_ids``,
    causing one task to be silently dropped or replayed.

    Checks:
      - every spec task_id has exactly one plan with the same task_id
      - no plan task_id is absent from the spec
      - no duplicate task_ids in plans
      - no path/artifact overlap between independent tasks in the same project
    """
    plan_ids = tuple(getattr(p, "task_id", "") for p in plans)
    spec_set = set(spec_task_ids)
    plan_set = set(plan_ids)

    missing = tuple(sorted(spec_set - plan_set))
    extra = tuple(sorted(plan_set - spec_set))

    seen: set[str] = set()
    dupes: list[str] = []
    for pid in plan_ids:
        if pid in seen:
            dupes.append(pid)
        seen.add(pid)

    # Cross-project overlap: tasks in the same project must not share paths.
    overlap: list[str] = []
    by_project: dict[str, list[Any]] = {}
    for p in plans:
        proj = getattr(p, "project_id", "")
        by_project.setdefault(proj, []).append(p)
    for proj, group in by_project.items():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                a_paths = set(getattr(a, "allowed_paths", ()) or ())
                b_paths = set(getattr(b, "allowed_paths", ()) or ())
                if a_paths & b_paths:
                    names = tuple(sorted((getattr(a, "task_id", ""), getattr(b, "task_id", ""))))
                    overlap.append(f"{names[0]}+{names[1]}:shared-allowed-paths")

    consistent = (
        not missing and not extra and not dupes and not overlap
        and len(plan_ids) == len(spec_task_ids)
    )
    return BatchConsistencyReport(
        consistent=consistent,
        spec_task_ids=spec_task_ids,
        plan_task_ids=plan_ids,
        missing_from_plans=missing,
        extra_in_plans=extra,
        duplicate_task_ids=tuple(sorted(set(dupes))),
        cross_project_overlap=tuple(sorted(set(overlap))),
    )
